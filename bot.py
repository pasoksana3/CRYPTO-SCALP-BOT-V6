import os
import time
import math
import logging
import requests
import ccxt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | V6 | %(levelname)s | %(message)s"
)

SYMBOLS = os.getenv(
    "SYMBOLS",
    "BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT"
).split(",")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
RR = float(os.getenv("RR", "2.0"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

exchange = ccxt.mexc({
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap"
    }
})


def closes(r):
    return [float(x[4]) for x in r]


def highs(r):
    return [float(x[2]) for x in r]


def lows(r):
    return [float(x[3]) for x in r]


def opens(r):
    return [float(x[1]) for x in r]


def ema(v, p):
    if not v:
        return 0.0

    k = 2 / (p + 1)
    e = v[0]

    for x in v[1:]:
        e = x * k + e * (1 - k)

    return e


def atr(r, p=14):
    if len(r) < p + 2:
        return 0.0

    tr = []

    for i in range(1, len(r)):
        h = float(r[i][2])
        l = float(r[i][3])
        pc = float(r[i - 1][4])

        tr.append(
            max(
                h - l,
                abs(h - pc),
                abs(l - pc)
            )
        )

    return sum(tr[-p:]) / p


def fetch(s, tf, limit=80):
    return exchange.fetch_ohlcv(
        s,
        timeframe=tf,
        limit=limit
    )


# =========================================================
# BUILD 10m FROM 5m
# MEXC може не приймати прямий timeframe 10m.
# Тому V6 формує 10m із двох завершених 5m свічок.
# =========================================================

def build_10m_from_5m(s, limit=80):

    raw = fetch(
        s,
        "5m",
        limit * 2 + 4
    )

    out = []

    for i in range(0, len(raw) - 1, 2):

        a = raw[i]
        b = raw[i + 1]

        # Обидві 5m свічки повинні належати
        # одному 10-хвилинному інтервалу.
        if int(a[0]) // 600000 != int(b[0]) // 600000:
            continue

        out.append([
            a[0],
            float(a[1]),
            max(float(a[2]), float(b[2])),
            min(float(a[3]), float(b[3])),
            float(b[4]),
            float(a[5]) + float(b[5])
        ])

    return out[-limit:]


def bias(d):

    c = closes(d)

    if len(c) < 55:
        return "NEUTRAL"

    e20 = ema(c[:-1], 20)
    e50 = ema(c[:-1], 50)
    last = c[-2]

    if last > e20 > e50:
        return "LONG"

    if last < e20 < e50:
        return "SHORT"

    return "NEUTRAL"


def structure(d):

    if len(d) < 12:
        return "RANGE"

    c = closes(d)
    h = highs(d)
    l = lows(d)

    last = c[-2]

    hi = max(h[-8:-2])
    lo = min(l[-8:-2])

    if last > hi:
        return "BULL_BOS"

    if last < lo:
        return "BEAR_BOS"

    hi3 = max(h[-5:-2])
    lo3 = min(l[-5:-2])

    if last > hi3:
        return "BULL_CHOCH"

    if last < lo3:
        return "BEAR_CHOCH"

    return "RANGE"


def sweep(d, side):

    if len(d) < 15:
        return False

    h = highs(d)
    l = lows(d)
    c = closes(d)

    hi = max(h[-14:-2])
    lo = min(l[-14:-2])

    if side == "LONG":
        return l[-2] < lo and c[-2] > lo

    return h[-2] > hi and c[-2] < hi


def fvg(d, side):

    if len(d) < 5:
        return False

    h = highs(d)
    l = lows(d)

    if side == "LONG":
        return l[-2] > h[-4]

    return h[-2] < l[-4]


def retest(d, side):

    if len(d) < 7:
        return False

    o = opens(d)
    c = closes(d)
    h = highs(d)
    l = lows(d)

    if side == "LONG":
        return l[-3] <= h[-5] and c[-3] > o[-3]

    return h[-3] >= l[-5] and c[-3] < o[-3]


def trigger(d, side):

    if len(d) < 5:
        return False

    o = opens(d)
    c = closes(d)
    h = highs(d)
    l = lows(d)

    if side == "LONG":
        return (
            c[-2] > o[-2]
            and c[-2] > h[-3]
            and l[-2] <= l[-3]
        )

    return (
        c[-2] < o[-2]
        and c[-2] < l[-3]
        and h[-2] >= h[-3]
    )


def signal(s):

    try:

        # 1H
        d1 = fetch(s, "1h")

        # 15m
        d15 = fetch(s, "15m")

        # 10m будуємо з 5m
        d10 = build_10m_from_5m(s)

        # 5m
        d5 = fetch(s, "5m")

        if min(
            len(d1),
            len(d15),
            len(d10),
            len(d5)
        ) < 20:
            return None

        # -------------------------------------------------
        # 1H DIRECTION
        # -------------------------------------------------

        b = bias(d1)

        # -------------------------------------------------
        # 15m STRUCTURE
        # -------------------------------------------------

        st = structure(d15)

        if (
            b == "LONG"
            and st in ("BULL_BOS", "BULL_CHOCH")
        ):
            side = "LONG"

        elif (
            b == "SHORT"
            and st in ("BEAR_BOS", "BEAR_CHOCH")
        ):
            side = "SHORT"

        else:
            return None

        score = 4

        reasons = [
            "1H=" + b,
            "15m=" + st
        ]

        # -------------------------------------------------
        # LIQUIDITY SWEEP
        # -------------------------------------------------

        if not sweep(d15, side):
            return None

        score += 2
        reasons.append("15m liquidity sweep")

        # -------------------------------------------------
        # FVG / IMBALANCE
        # -------------------------------------------------

        if not fvg(d10, side):
            return None

        score += 2
        reasons.append("10m imbalance/FVG")

        # -------------------------------------------------
        # RETEST
        # -------------------------------------------------

        if not retest(d10, side):
            return None

        score += 1
        reasons.append("10m retest")

        # -------------------------------------------------
        # 5m CONFIRMATION
        # -------------------------------------------------

        if not trigger(d5, side):
            return None

        score += 1
        reasons.append("5m confirmation")

        if score < MIN_SCORE:
            return None

        # -------------------------------------------------
        # ENTRY / SL / TP
        # -------------------------------------------------

        price = closes(d5)[-2]
        a = atr(d5)

        if not math.isfinite(a) or a <= 0:
            return None

        h = highs(d5)
        l = lows(d5)

        if side == "LONG":

            sl = min(l[-12:]) - 0.25 * a
            risk = price - sl

            if risk <= 0:
                return None

            el = price - 0.20 * a
            eh = price + 0.05 * a

            tp1 = price + risk
            tp2 = price + risk * RR

        else:

            sl = max(h[-12:]) + 0.25 * a
            risk = sl - price

            if risk <= 0:
                return None

            el = price - 0.05 * a
            eh = price + 0.20 * a

            tp1 = price - risk
            tp2 = price - risk * RR

        return (
            side,
            s,
            score,
            min(el, eh),
            max(el, eh),
            sl,
            tp1,
            tp2,
            " | ".join(reasons)
        )

    except Exception as e:

        logging.warning(
            "%s: %s",
            s,
            e
        )

        return None


def send(t):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning(
            "Telegram variables are not configured"
        )
        return

    try:

        requests.post(
            "https://api.telegram.org/bot"
            + TELEGRAM_TOKEN
            + "/sendMessage",

            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": t
            },

            timeout=8
        )

    except Exception as e:

        logging.warning(
            "Telegram error: %s",
            e
        )


def main():

    logging.info(
        "SCALP V6 LIGHT started | symbols=%s",
        ",".join(SYMBOLS)
    )

    last = {}

    while True:

        for s in SYMBOLS:

            s = s.strip()

            if not s:
                continue

            x = signal(s)

            if not x:
                continue

            if time.time() - last.get(s, 0) < 1800:
                continue

            (
                side,
                sym,
                score,
                el,
                eh,
                sl,
                tp1,
                tp2,
                why
            ) = x

            if side == "LONG":
                icon = "🟢 LONG"
            else:
                icon = "🔴 SHORT"

            msg = (
                f"{icon}\n"
                f"V6 SCALP — {sym}\n\n"
                f"Score: {score}/10\n"
                f"Entry: {el:.8g} – {eh:.8g}\n"
                f"SL: {sl:.8g}\n"
                f"TP1: {tp1:.8g}\n"
                f"TP2: {tp2:.8g}\n"
                f"Leverage: {LEVERAGE}x\n\n"
                f"CONFIRMATION:\n"
                f"{why}\n\n"
                f"⚠️ Це сигнал алгоритму, "
                f"не гарантія результату."
            )

            send(msg)

            logging.info(
                "%s",
                msg
            )

            last[s] = time.time()

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
