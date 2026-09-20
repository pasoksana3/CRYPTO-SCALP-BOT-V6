import os,time,logging,ccxt,requests
logging.basicConfig(level=logging.INFO,format="%(asctime)s | V6.3 | %(levelname)s | %(message)s")
log=logging.getLogger("v6")
SYMBOLS=[x.strip() for x in os.getenv("SYMBOLS","BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT").split(",") if x.strip()]
POLL_SECONDS=int(os.getenv("POLL_SECONDS","60")); LEVERAGE=int(os.getenv("LEVERAGE","30")); RR=float(os.getenv("RR","2")); MIN_SCORE=int(os.getenv("MIN_SCORE","7"))
SWING_N=int(os.getenv("SWING_N","2")); SWEEP_LOOKBACK=int(os.getenv("SWEEP_LOOKBACK","30"))
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN",""); TG_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","")
exchange=ccxt.mexc({"apiKey":os.getenv("MEXC_API_KEY",""),"secret":os.getenv("MEXC_SECRET",""),"enableRateLimit":True,"options":{"defaultType":"swap"}})

def tg(s):
    if TG_TOKEN and TG_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",data={"chat_id":TG_CHAT_ID,"text":s},timeout=10).raise_for_status()
        except Exception as e: log.warning("Telegram error: %s",e)
def fetch(sym,tf,n=200): return exchange.fetch_ohlcv(sym,timeframe=tf,limit=n)
def build10(rows):
    b={}
    for r in rows: b.setdefault((r[0]//600000)*600000,[]).append(r)
    out=[]
    for ts,rs in sorted(b.items()):
        rs=sorted(rs)[-2:]
        if len(rs)==2: out.append([ts,rs[0][1],max(x[2] for x in rs),min(x[3] for x in rs),rs[-1][4],sum(x[5] for x in rs)])
    return out
def d1(rows):
    c=[r[4] for r in rows[-20:]]
    if len(c)<20:return"RANGE"
    f=sum(c[-5:])/5;s=sum(c)/20
    return "LONG" if f>s*1.002 else "SHORT" if f<s*.998 else "RANGE"
def s15(rows):
    if len(rows)<8:return"RANGE"
    a=rows[-6:-2];b=rows[-2:]
    ah=max(x[2] for x in a);al=min(x[3] for x in a);bh=max(x[2] for x in b);bl=min(x[3] for x in b)
    return "LONG" if bh>ah and bl>=al else "SHORT" if bl<al and bh<=ah else "RANGE"
def slocal(rows,i,n):
    return i>=n and i+n<len(rows) and all(rows[i][3]<rows[j][3] for j in range(i-n,i+n+1) if j!=i)
def hlocal(rows,i,n):
    return i>=n and i+n<len(rows) and all(rows[i][2]>rows[j][2] for j in range(i-n,i+n+1) if j!=i)
def sweep(rows,cand):
    r=rows[:-1]
    start=max(SWING_N,len(r)-SWEEP_LOOKBACK-SWING_N); end=len(r)-SWING_N-1
    if cand=="LONG":
        swings=[(i,r[i][3]) for i in range(start,end+1) if slocal(r,i,SWING_N)]
        if not swings: log.info("STEP 5 | no local swing low found"); return False,None
        for i,lev in reversed(swings):
            for j in range(i+SWING_N+1,len(r)):
                if r[j][3]<lev and r[j][4]>lev:
                    return True,{"type":"SSL","level":lev,"extreme":r[j][3],"close":r[j][4]}
        log.info("STEP 5 | swing low=%.8f | current low=%.8f | close=%.8f",swings[-1][1],min(x[3] for x in r[-5:]),r[-1][4])
        return False,None
    swings=[(i,r[i][2]) for i in range(start,end+1) if hlocal(r,i,SWING_N)]
    if not swings: log.info("STEP 5 | no local swing high found"); return False,None
    for i,lev in reversed(swings):
        for j in range(i+SWING_N+1,len(r)):
            if r[j][2]>lev and r[j][4]<lev:
                return True,{"type":"BSL","level":lev,"extreme":r[j][2],"close":r[j][4]}
    log.info("STEP 5 | swing high=%.8f | current high=%.8f | close=%.8f",swings[-1][1],max(x[2] for x in r[-5:]),r[-1][4])
    return False,None
def fvg(r,c):
    if len(r)<4:return False
    a=r[-4];x=r[-2]
    return x[3]>a[2] if c=="LONG" else x[2]<a[3]
def retest(r,c):
    if len(r)<4:return False
    a=r[-3];x=r[-2]
    return (x[4]>a[4] and x[3]<=a[4]) if c=="LONG" else (x[4]<a[4] and x[2]>=a[4])
def confirm(r,c):
    if len(r)<4:return False
    a=r[-3];x=r[-2]
    return (x[4]>x[1] and x[4]>a[4]) if c=="LONG" else (x[4]<x[1] and x[4]<a[4])
def scan(sym):
    try:
        log.info("========== SCAN %s ==========",sym)
        h=fetch(sym,"1h",100); m=fetch(sym,"15m",120); m5=fetch(sym,"5m",240); m10=build10(m5)
        a=d1(h); st=s15(m); log.info("%s | STEP 3 | 1H=%s | 15m=%s",sym,a,st)
        cand=a if a in("LONG","SHORT") else st
        score=2 if cand in("LONG","SHORT") else 0
        if not cand: return
        if st==cand: score+=2
        log.info("%s | STEP 4 | candidate=%s | score=%d/10",sym,cand,score)
        ok,info=sweep(m,cand); log.info("%s | STEP 5 | %s sweep=%s",sym,"SSL" if cand=="LONG" else "BSL",ok)
        if not ok: log.info("%s | WAIT %s | no local swing liquidity sweep | score=%d/10",sym,cand,score); return
        score+=2; fv=fvg(m10,cand); score+=2 if fv else 0; log.info("%s | STEP 6 | 10m FVG=%s | score=%d/10",sym,fv,score)
        rt=retest(m10,cand); score+=rt; cf=confirm(m5,cand); score+=cf
        log.info("%s | STEP 7 | retest=%s | STEP 8 | 5m confirmation=%s | score=%d/10",sym,rt,cf,score)
        if score>=MIN_SCORE:
            last=m10[-2]; price=last[4]; ex=info["extreme"]
            if cand=="LONG": lo=min(last[3],ex); hi=max(last[4],lo); sl=lo*.998; tp=hi+(hi-sl)*RR
            else: hi=max(last[2],ex); lo=min(last[4],hi); sl=hi*1.002; tp=lo-(sl-lo)*RR
            msg=f'{"🟢 LONG" if cand=="LONG" else "🔴 SHORT"}\n{sym} Futures\nEntry: {lo:.8g} - {hi:.8g}\nSL: {sl:.8g}\nTP: {tp:.8g}\nLeverage: {LEVERAGE}x\nRR: {RR:.1f}\nLiquidity: {info["type"]} sweep\nV6.3'
            log.info("%s | SIGNAL %s | score=%d/10",sym,cand,score); tg(msg)
        else: log.info("%s | WAIT %s | score=%d/10",sym,cand,score)
    except Exception as e: log.exception("%s | ERROR | %s",sym,e)
def main():
    log.info("=== CRYPTO SCALP BOT V6.3 ===")
    while True:
        for s in SYMBOLS: scan(s)
        log.info("========== SCAN COMPLETE | sleeping %ss ==========",POLL_SECONDS); time.sleep(POLL_SECONDS)
if __name__=="__main__": main()
