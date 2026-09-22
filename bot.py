import os,time,logging,ccxt,requests,json
logging.basicConfig(level=logging.INFO,format="%(asctime)s | V6.5-REVERSE | %(levelname)s | %(message)s")
log=logging.getLogger("v65")

SYMBOLS=[x.strip() for x in os.getenv("SYMBOLS","BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT").split(",") if x.strip()]
POLL_SECONDS=int(os.getenv("POLL_SECONDS","60"))
LEVERAGE=int(os.getenv("LEVERAGE","30"))
TP_PCT=float(os.getenv("TP_PCT","0.01"))
SL_PCT=float(os.getenv("SL_PCT","0.005"))
REVERSE_CONFIRM_5M=int(os.getenv("REVERSE_CONFIRM_5M","2"))
REVERSE_COOLDOWN_SECONDS=int(os.getenv("REVERSE_COOLDOWN_SECONDS","1800"))
COOLDOWN_SECONDS=int(os.getenv("COOLDOWN_SECONDS","5400"))
SWING_N=int(os.getenv("SWING_N","2"))
SWEEP_LOOKBACK=int(os.getenv("SWEEP_LOOKBACK","30"))
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","")

exchange=ccxt.mexc({
    "apiKey":os.getenv("MEXC_API_KEY",""),
    "secret":os.getenv("MEXC_SECRET",""),
    "enableRateLimit":True,
    "options":{"defaultType":"swap"}
})

state_file="/tmp/v64_state.json"

def load_state():
    try:
        with open(state_file) as f:return json.load(f)
    except:return {}

def save_state(s):
    try:
        with open(state_file,"w") as f:json.dump(s,f)
    except Exception as e:log.warning("state save error: %s",e)

state=load_state()

def tg(s):
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data={"chat_id":TG_CHAT_ID,"text":s},
                timeout=10
            ).raise_for_status()
        except Exception as e:
            log.warning("Telegram error: %s",e)

def fetch(sym,tf,n=200):
    return exchange.fetch_ohlcv(sym,timeframe=tf,limit=n)

def build10(rows):
    b={}
    for r in rows:
        b.setdefault((r[0]//600000)*600000,[]).append(r)
    out=[]
    for ts,rs in sorted(b.items()):
        rs=sorted(rs)[-2:]
        if len(rs)==2:
            out.append([
                ts,rs[0][1],max(x[2] for x in rs),min(x[3] for x in rs),
                rs[-1][4],sum(x[5] for x in rs)
            ])
    return out

def d1(rows):
    c=[r[4] for r in rows[-20:]]
    if len(c)<20:return"RANGE"
    f=sum(c[-5:])/5
    s=sum(c)/20
    return"LONG" if f>s*1.002 else"SHORT" if f<s*.998 else"RANGE"

def s15(rows):
    if len(rows)<8:return"RANGE"
    a=rows[-6:-2]
    b=rows[-2:]
    ah=max(x[2] for x in a)
    al=min(x[3] for x in a)
    bh=max(x[2] for x in b)
    bl=min(x[3] for x in b)
    return"LONG" if bh>ah and bl>=al else"SHORT" if bl<al and bh<=ah else"RANGE"

def slocal(r,i,n):
    return i>=n and i+n<len(r) and all(
        r[i][3]<r[j][3] for j in range(i-n,i+n+1) if j!=i
    )

def hlocal(r,i,n):
    return i>=n and i+n<len(r) and all(
        r[i][2]>r[j][2] for j in range(i-n,i+n+1) if j!=i
    )

def sweep(rows,c):
    r=rows[:-1]
    start=max(SWING_N,len(r)-SWEEP_LOOKBACK-SWING_N)
    end=len(r)-SWING_N-1

    if c=="LONG":
        swings=[(i,r[i][3]) for i in range(start,end+1) if slocal(r,i,SWING_N)]
        for i,lev in reversed(swings):
            for j in range(i+SWING_N+1,len(r)):
                if r[j][3]<lev and r[j][4]>lev:
                    return True,{"type":"SSL","level":lev,"extreme":r[j][3],"index":j}
        return False,None

    swings=[(i,r[i][2]) for i in range(start,end+1) if hlocal(r,i,SWING_N)]
    for i,lev in reversed(swings):
        for j in range(i+SWING_N+1,len(r)):
            if r[j][2]>lev and r[j][4]<lev:
                return True,{"type":"BSL","level":lev,"extreme":r[j][2],"index":j}
    return False,None

def fvg(r,c):
    if len(r)<5:return False
    a=r[-4]
    x=r[-2]
    return x[3]>a[2] if c=="LONG" else x[2]<a[3]

def retest(r,c):
    if len(r)<5:return False
    a=r[-3]
    x=r[-2]
    return (x[4]>a[4] and x[3]<=a[4]) if c=="LONG" else (x[4]<a[4] and x[2]>=a[4])

def confirm(r,c):
    if len(r)<4:return False
    a=r[-3]
    x=r[-2]
    return (x[4]>x[1] and x[4]>a[4]) if c=="LONG" else (x[4]<x[1] and x[4]<a[4])

def can_signal(sym):
    now=time.time()
    old=state.get(sym,0)
    last=float(old.get("signal_time",0)) if isinstance(old,dict) else float(old)
    if now-last<COOLDOWN_SECONDS:
        return False,int(COOLDOWN_SECONDS-(now-last))
    return True,0

def reverse_confirm(rows,original_side):
    if len(rows)<8:return False

    # Ignore currently open candle.
    r=rows[:-1]
    need="SHORT" if original_side=="LONG" else "LONG"

    # Required consecutive closed candles in reverse direction.
    for n in range(1,REVERSE_CONFIRM_5M+1):
        x=r[-n]
        if need=="SHORT" and not x[4]<x[1]:
            return False
        if need=="LONG" and not x[4]>x[1]:
            return False

    last=r[-1]
    prev=r[-2]

    # Additional local break/momentum confirmation.
    if need=="SHORT":
        return last[4]<prev[3] or (last[4]<last[1] and last[3]<prev[3])

    return last[4]>prev[2] or (last[4]>last[1] and last[2]>prev[2])

def monitor_reverse(sym,m5):
    st=state.get(sym)

    if not isinstance(st,dict):
        return
    if not st.get("side"):
        return
    if st.get("reverse_sent"):
        return

    if time.time()-float(st.get("reverse_time",0))<REVERSE_COOLDOWN_SECONDS:
        return

    original_side=st["side"]

    if not reverse_confirm(m5,original_side):
        return

    price=m5[-2][4]
    reverse_side="SHORT" if original_side=="LONG" else "LONG"

    if reverse_side=="LONG":
        tp=price*(1+TP_PCT)
        sl=price*(1-SL_PCT)
        icon="ð¢ LONG"
    else:
        tp=price*(1-TP_PCT)
        sl=price*(1+SL_PCT)
        icon="ð´ SHORT"

    msg=(
        f"{icon}\n"
        f"{sym} Futures\n"
        f"Entry: {price:.8g}\n"
        f"TP: {tp:.8g} ({TP_PCT*100:.2f}%)\n"
        f"SL: {sl:.8g} ({SL_PCT*100:.2f}%)\n"
        f"Leverage: {LEVERAGE}x"
    )

    tg(msg)

    st["reverse_sent"]=True
    st["reverse_time"]=time.time()
    state[sym]=st
    save_state(state)

    log.info("%s | REVERSE CONFIRMED | %s",sym,reverse_side)

def scan(sym):
    try:
        log.info("========== SCAN %s ==========",sym)

        h=fetch(sym,"1h",100)
        m=fetch(sym,"15m",120)
        m5=fetch(sym,"5m",240)
        m10=build10(m5)

        # REVERSE monitor is independent from original V6.4 detection.
        monitor_reverse(sym,m5)

        a=d1(h)
        st=s15(m)

        log.info("%s | STEP 3 | 1H=%s | 15m=%s",sym,a,st)

        if a not in("LONG","SHORT"):
            log.info("%s | WAIT | 1H RANGE",sym)
            return

        cand=a

        if st in("LONG","SHORT") and st!=cand:
            log.info("%s | WAIT %s | 15m conflicts with 1H",sym,cand)
            return

        log.info("%s | STEP 4 | candidate=%s | direction aligned",sym,cand)

        ok,info=sweep(m,cand)

        log.info(
            "%s | STEP 5 | %s sweep=%s",
            sym,
            "SSL" if cand=="LONG" else"BSL",
            ok
        )

        if not ok:
            return

        fv=fvg(m10,cand)
        rt=retest(m10,cand)
        cf=confirm(m5,cand)

        log.info("%s | STEP 6 | FVG=%s",sym,fv)
        log.info("%s | STEP 7 | retest=%s",sym,rt)
        log.info("%s | STEP 8 | 5m confirmation=%s",sym,cf)

        valid=cf and (fv or rt)

        log.info(
            "%s | SETUP | sweep=%s | FVG=%s | retest=%s | 5m=%s | valid=%s",
            sym,ok,fv,rt,cf,valid
        )

        if not valid:
            log.info("%s | WAIT %s | incomplete confirmation",sym,cand)
            return

        allowed,left=can_signal(sym)

        if not allowed:
            log.info("%s | COOLDOWN | %ss remaining",sym,left)
            return

        price=m10[-2][4]

        if cand=="LONG":
            entry=price
            tp=entry*(1+TP_PCT)
            sl=entry*(1-SL_PCT)
            icon="ð¢ LONG"
        else:
            entry=price
            tp=entry*(1-TP_PCT)
            sl=entry*(1+SL_PCT)
            icon="ð´ SHORT"

        msg=(
            f"{icon}\n"
            f"{sym} Futures\n"
            f"Entry: {entry:.8g}\n"
            f"TP: {tp:.8g} ({TP_PCT*100:.2f}%)\n"
            f"SL: {sl:.8g} ({SL_PCT*100:.2f}%)\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Liquidity: {info['type']} sweep\n"
            f"Confirm: {'FVG' if fv else 'Retest'} + 5m\n"
            f"V6.5-REVERSE"
        )

        tg(msg)

        state[sym]={
            "signal_time":time.time(),
            "side":cand,
            "entry":entry,
            "tp":tp,
            "sl":sl,
            "reverse_sent":False,
            "reverse_time":0
        }

        save_state(state)

        log.info(
            "%s | SIGNAL %s | TP=%.2f%% | SL=%.2f%% | cooldown=%ss",
            sym,cand,TP_PCT*100,SL_PCT*100,COOLDOWN_SECONDS
        )

    except Exception as e:
        log.exception("%s | ERROR | %s",sym,e)

def main():
    log.info("=== CRYPTO SCALP BOT V6.5-REVERSE ===")
    log.info("=== ORIGINAL V6.4 FILTERS UNCHANGED ===")
    log.info("=== REVERSE MONITOR ENABLED ===")
    log.info(
        "TP=%.2f%% | SL=%.2f%% | cooldown=%ss | leverage=%sx | reverse_confirm=%s candles",
        TP_PCT*100,SL_PCT*100,COOLDOWN_SECONDS,LEVERAGE,REVERSE_CONFIRM_5M
    )

    while True:
        for s in SYMBOLS:
            scan(s)

        log.info(
            "========== SCAN COMPLETE | sleeping %ss ==========",
            POLL_SECONDS
        )
        time.sleep(POLL_SECONDS)

if __name__=="__main__":
    main()
