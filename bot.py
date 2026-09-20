import os
import time
import math
import logging
import requests
import ccxt

logging.basicConfig(level=logging.INFO, format="%(asctime)s | V6 | %(levelname)s | %(message)s")

SYMBOLS = os.getenv("SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT").split(",")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
RR = float(os.getenv("RR", "2.0"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

exchange = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})

def closes(r): return [float(x[4]) for x in r]
def highs(r): return [float(x[2]) for x in r]
def lows(r): return [float(x[3]) for x in r]
def opens(r): return [float(x[1]) for x in r]

def ema(values, period):
    if not values: return 0.0
    k = 2 / (period + 1); value = values[0]
    for x in values[1:]: value = x * k + value * (1 - k)
    return value

def atr(candles, period=14):
    if len(candles) < period + 2: return 0.0
    tr = []
    for i in range(1, len(candles)):
        h, l, pc = float(candles[i][2]), float(candles[i][3]), float(candles[i-1][4])
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(tr[-period:]) / period

def fetch(symbol, timeframe, limit=80):
    return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

def build_10m_from_5m(symbol, limit=80):
    # MEXC does not reliably provide native 10m candles, so build them
    # from 5m candles using real UTC 10-minute buckets: 00, 10, 20, 30, 40, 50.
    raw = fetch(symbol, "5m", limit * 2 + 12)
    buckets = {}
    for row in raw:
        ts = int(row[0])
        bucket_ts = (ts // 600000) * 600000
        buckets.setdefault(bucket_ts, []).append(row)

    result = []
    for bucket_ts in sorted(buckets):
        rows = sorted(buckets[bucket_ts], key=lambda x: int(x[0]))
        # Only use complete 10m candles: exactly two consecutive 5m candles.
        if len(rows) < 2:
            continue
        a, b = rows[0], rows[1]
        if int(b[0]) - int(a[0]) != 300000:
            continue
        result.append([
            bucket_ts,
            float(a[1]),
            max(float(a[2]), float(b[2])),
            min(float(a[3]), float(b[3])),
            float(b[4]),
            float(a[5]) + float(b[5]),
        ])
    return result[-limit:]

def bias(candles):
    v = closes(candles)
    if len(v) < 55: return "NEUTRAL"
    e20, e50, last = ema(v[:-1],20), ema(v[:-1],50), v[-2]
    if last > e20 > e50: return "LONG"
    if last < e20 < e50: return "SHORT"
    return "NEUTRAL"

def structure(candles):
    if len(candles) < 12: return "RANGE"
    c,h,l = closes(candles),highs(candles),lows(candles); last=c[-2]
    hi,lo=max(h[-8:-2]),min(l[-8:-2])
    if last > hi: return "BULL_BOS"
    if last < lo: return "BEAR_BOS"
    hi3,lo3=max(h[-5:-2]),min(l[-5:-2])
    if last > hi3: return "BULL_CHOCH"
    if last < lo3: return "BEAR_CHOCH"
    return "RANGE"

def liquidity_sweep(candles, side, lookback=10, swing=5):
    """Find a recent SSL/BSL sweep on completed candles.
    LONG: candle takes a prior swing low and closes back above it (SSL).
    SHORT: candle takes a prior swing high and closes back below it (BSL).
    We inspect several recent completed candles instead of only [-2], so a
    sweep that happened 1-5 candles ago is still available for the next stages.
    """
    if len(candles) < swing + lookback + 3:
        return False, None
    h,l,c=highs(candles),lows(candles),closes(candles)
    # Ignore the currently forming candle; inspect the last `lookback` completed candles.
    end = len(candles) - 1
    start = max(swing + 1, end - lookback)
    for i in range(end - 1, start - 1, -1):
        left=max(0, i-swing)
        right=min(i, i+swing)
        if side=="LONG":
            prior_low=min(l[left:i])
            # SSL: wick below prior lows, then close back above that liquidity.
            if l[i] < prior_low and c[i] > prior_low:
                return True, {"type":"SSL", "index":i, "level":prior_low, "low":l[i], "close":c[i]}
        else:
            prior_high=max(h[left:i])
            # BSL: wick above prior highs, then close back below that liquidity.
            if h[i] > prior_high and c[i] < prior_high:
                return True, {"type":"BSL", "index":i, "level":prior_high, "high":h[i], "close":c[i]}
    return False, None

def fvg(candles, side):
    if len(candles) < 5: return False
    h,l=highs(candles),lows(candles)
    return l[-2] > h[-4] if side=="LONG" else h[-2] < l[-4]

def retest(candles, side):
    if len(candles) < 7: return False
    o,c,h,l=opens(candles),closes(candles),highs(candles),lows(candles)
    return (l[-3] <= h[-5] and c[-3] > o[-3]) if side=="LONG" else (h[-3] >= l[-5] and c[-3] < o[-3])

def trigger(candles, side):
    if len(candles) < 5: return False
    o,c,h,l=opens(candles),closes(candles),highs(candles),lows(candles)
    return (c[-2] > o[-2] and c[-2] > h[-3] and l[-2] <= l[-3]) if side=="LONG" else (c[-2] < o[-2] and c[-2] < l[-3] and h[-2] >= h[-3])

def signal(symbol):
    try:
        logging.info("%s | STEP 1 | fetching 1H + 15m + 5m", symbol)
        d1,d15,d5=fetch(symbol,"1h"),fetch(symbol,"15m"),fetch(symbol,"5m")
        logging.info("%s | STEP 2 | building 10m from 5m", symbol)
        d10=build_10m_from_5m(symbol)
        lens=(len(d1),len(d15),len(d10),len(d5))
        if min(lens)<20:
            logging.info("%s | WAIT | candles: 1H=%d 15m=%d 10m=%d 5m=%d",symbol,*lens); return None
        b,st=bias(d1),structure(d15)
        logging.info("%s | STEP 3 | 1H=%s | 15m=%s",symbol,b,st)
        if b not in ("LONG","SHORT"):
            logging.info("%s | WAIT | 1H has no clear direction",symbol); return None

        # 1H is the primary directional filter. Do NOT reject RANGE on 15m:
        # a liquidity sweep can happen while 15m is still ranging, which is
        # exactly the pattern V6 is designed to catch.
        side=b
        score=0
        score += 2
        logging.info("%s | STEP 4 | candidate=%s | 1H direction PASS (+2)",symbol,side)

        if st in (("BULL_BOS","BULL_CHOCH") if side=="LONG" else ("BEAR_BOS","BEAR_CHOCH")):
            score += 2
            logging.info("%s | STEP 4 | 15m structure aligned PASS (+2)",symbol)
        elif st == "RANGE":
            logging.info("%s | STEP 4 | 15m RANGE | continue searching liquidity",symbol)
        else:
            logging.info("%s | STEP 4 | 15m structure opposite (%s) | continue for sweep/reversal evidence",symbol,st)

        sweep, sweep_info=liquidity_sweep(d15,side)
        logging.info("%s | STEP 5 | 15m liquidity sweep=%s",symbol,sweep)
        if sweep:
            score += 2
            logging.info("%s | STEP 5 | %s sweep | level=%.8g | extreme=%.8g | close=%.8g", symbol, sweep_info["type"], sweep_info["level"], sweep_info.get("low", sweep_info.get("high")), sweep_info["close"])
        else:
            logging.info("%s | STEP 5 | no recent %s sweep in last 10 completed 15m candles",symbol,"SSL" if side=="LONG" else "BSL")
            logging.info("%s | WAIT %s | no 15m liquidity sweep | score=%d/10",symbol,side,score); return None

        has_fvg=fvg(d10,side)
        logging.info("%s | STEP 6 | 10m FVG/IMBALANCE=%s",symbol,has_fvg)
        if has_fvg:
            score += 2
        else:
            logging.info("%s | WAIT %s | no 10m FVG | score=%d/10",symbol,side,score); return None

        has_retest=retest(d10,side)
        logging.info("%s | STEP 7 | 10m RETEST=%s",symbol,has_retest)
        if has_retest:
            score += 1
        else:
            logging.info("%s | WAIT %s | no 10m retest | score=%d/10",symbol,side,score); return None

        has_trigger=trigger(d5,side)
        logging.info("%s | STEP 8 | 5m CONFIRMATION=%s",symbol,has_trigger)
        if has_trigger:
            score += 1
        else:
            logging.info("%s | WAIT %s | no 5m confirmation | score=%d/10",symbol,side,score); return None

        logging.info("%s | STEP 9 | FINAL SCORE=%d/10 | minimum=%d",symbol,score,MIN_SCORE)
        if score < MIN_SCORE:
            logging.info("%s | WAIT | score below minimum",symbol); return None
        price,a=closes(d5)[-2],atr(d5)
        if not math.isfinite(a) or a<=0: logging.info("%s | WAIT | invalid ATR",symbol); return None
        h,l=highs(d5),lows(d5)
        if side=="LONG":
            sl=min(l[-12:])-.25*a; risk=price-sl
            if risk<=0:return None
            el,eh=price-.20*a,price+.05*a; tp1,tp2=price+risk,price+risk*RR
        else:
            sl=max(h[-12:])+.25*a; risk=sl-price
            if risk<=0:return None
            el,eh=price-.05*a,price+.20*a; tp1,tp2=price-risk,price-risk*RR
        logging.info("%s | SIGNAL READY | %s | score=%d",symbol,side,score)
        return side,symbol,score,min(el,eh),max(el,eh),sl,tp1,tp2,f"1H={b} | 15m={st} | liquidity sweep | 10m FVG/imbalance | 10m retest | 5m confirmation"
    except Exception as e:
        logging.exception("%s | ERROR during scan: %s",symbol,e); return None

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram variables are NOT configured"); return
    try:
        r=requests.post("https://api.telegram.org/bot"+TELEGRAM_TOKEN+"/sendMessage",json={"chat_id":TELEGRAM_CHAT_ID,"text":text},timeout=8)
        if r.status_code!=200: logging.warning("Telegram HTTP %s: %s",r.status_code,r.text[:300])
    except Exception as e: logging.warning("Telegram error: %s",e)

def main():
    logging.info("=== V6 DIAGNOSTIC MODE ===")
    logging.info("SCALP V6 LIGHT started | symbols=%s",",".join(SYMBOLS))
    logging.info("Settings | poll=%ss | leverage=%sx | RR=%.2f | min_score=%d",POLL_SECONDS,LEVERAGE,RR,MIN_SCORE)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: logging.warning("Telegram variables are NOT configured")
    last={}
    while True:
        logging.info("========== NEW SCAN CYCLE ==========")
        for symbol in SYMBOLS:
            symbol=symbol.strip()
            if not symbol: continue
            logging.info(">>> SCANNING %s",symbol)
            result=signal(symbol)
            if not result: continue
            if time.time()-last.get(symbol,0)<1800:
                logging.info("%s | signal cooldown active",symbol); continue
            side,symbol,score,el,eh,sl,tp1,tp2,why=result
            icon="🟢 LONG" if side=="LONG" else "🔴 SHORT"
            msg=(f"{icon}\nV6 SCALP — {symbol}\n\nScore: {score}/10\nEntry: {el:.8g} – {eh:.8g}\nSL: {sl:.8g}\nTP1: {tp1:.8g}\nTP2: {tp2:.8g}\nLeverage: {LEVERAGE}x\n\nCONFIRMATION:\n{why}\n\n⚠️ Це сигнал алгоритму, не гарантія результату.")
            send_telegram(msg); logging.info("TELEGRAM SIGNAL SENT | %s",symbol); last[symbol]=time.time()
        logging.info("========== SCAN COMPLETE | sleeping %ss ==========",POLL_SECONDS)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__": main()
