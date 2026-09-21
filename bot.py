import os,time,math,json,logging,requests,ccxt
logging.basicConfig(level=logging.INFO,format="%(asctime)s | V6.5 | %(levelname)s | %(message)s")

SYMBOLS=os.getenv("SYMBOLS","BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT").split(",")
POLL_SECONDS=int(os.getenv("POLL_SECONDS","60"))
LEVERAGE=int(os.getenv("LEVERAGE","30"))
MIN_MOVE_PCT=float(os.getenv("MIN_MOVE_PCT","0.010"))
TARGET_PCT=float(os.getenv("TARGET_PCT","0.0125"))
MIN_RR=float(os.getenv("MIN_RR","1.5"))
COOLDOWN_SECONDS=int(os.getenv("COOLDOWN_SECONDS","5400"))
TELEGRAM_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID","")
STATE_FILE="/tmp/v65_state.json"

exchange=ccxt.mexc({"enableRateLimit":True,"options":{"defaultType":"swap"}})

def closes(r): return [float(x[4]) for x in r]
def opens(r): return [float(x[1]) for x in r]
def highs(r): return [float(x[2]) for x in r]
def lows(r): return [float(x[3]) for x in r]

def atr(r,p=14):
    if len(r)<p+2:return 0.0
    tr=[]
    for i in range(1,len(r)):
        h,l,pc=float(r[i][2]),float(r[i][3]),float(r[i-1][4])
        tr.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(tr[-p:])/p

def ema(v,p):
    if not v:return 0.0
    k=2/(p+1); e=v[0]
    for x in v[1:]: e=x*k+e*(1-k)
    return e

def fetch(s,tf,limit=100): return exchange.fetch_ohlcv(s,timeframe=tf,limit=limit)

def build_10m(s,limit=140):
    raw=fetch(s,"5m",limit); out=[]; buckets={}
    for x in raw: buckets.setdefault(int(x[0])//600000,[]).append(x)
    for k in sorted(buckets):
        a=buckets[k]
        if len(a)<2: continue
        out.append([k*600000,float(a[0][1]),max(float(x[2]) for x in a),min(float(x[3]) for x in a),float(a[-1][4]),sum(float(x[5]) for x in a)])
    return out

def closed(d): return d[:-1] if len(d)>2 else d

def bias_1h(d):
    c=closes(closed(d))
    if len(c)<55:return "RANGE"
    e20,e50=ema(c,20),ema(c,50); last=c[-1]
    if last>e20>e50:return "LONG"
    if last<e20<e50:return "SHORT"
    return "RANGE"

def swing_high(d,i,n=2):
    h=highs(d)
    return n<=i<len(d)-n and h[i]==max(h[i-n:i+n+1])

def swing_low(d,i,n=2):
    l=lows(d)
    return n<=i<len(d)-n and l[i]==min(l[i-n:i+n+1])

def structure(d):
    d=closed(d)
    if len(d)<15:return "RANGE"
    h,l,c=highs(d),lows(d),closes(d)
    if c[-1]>max(h[-9:-1]):return "BULL_BOS"
    if c[-1]<min(l[-9:-1]):return "BEAR_BOS"
    return "RANGE"

def sweep_choch(d,side):
    d=closed(d)
    if len(d)<25:return None
    h,l,c=highs(d),lows(d),closes(d)
    start=max(3,len(d)-35)
    for i in range(len(d)-3,start-1,-1):
        if side=="LONG":
            sws=[j for j in range(max(2,i-12),i) if swing_low(d,j)]
            if not sws: continue
            sw=sws[-1]
            if l[i]>=l[sw] or c[i]<=l[sw]: continue
            for k in range(i+2,min(len(d)-1,i+9)):
                if c[k]>max(h[i+1:k]):
                    return i,k,l[i]
        else:
            sws=[j for j in range(max(2,i-12),i) if swing_high(d,j)]
            if not sws: continue
            sw=sws[-1]
            if h[i]<=h[sw] or c[i]>=h[sw]: continue
            for k in range(i+2,min(len(d)-1,i+9)):
                if c[k]<min(l[i+1:k]):
                    return i,k,h[i]
    return None

def fvg_after(d,side):
    d=closed(d)
    h,l=highs(d),lows(d)
    start=max(2,len(d)-20)
    for i in range(start,len(d)):
        if side=="LONG" and l[i]>h[i-2]: return i,h[i-2],l[i]
        if side=="SHORT" and h[i]<l[i-2]: return i,h[i],l[i-2]
    return None

def retest(d,side,fvg):
    if not fvg:return False
    idx,zl,zh=fvg; d=closed(d)
    if len(d)<=idx+1:return False
    h,l,o,c=highs(d),lows(d),opens(d),closes(d)
    for i in range(idx+1,len(d)):
        if l[i]<=zh and h[i]>=zl:
            if side=="LONG" and c[i]>o[i]: return True
            if side=="SHORT" and c[i]<o[i]: return True
    return False

def confirm5(d,side):
    d=closed(d)
    if len(d)<5:return False
    o,c,h,l=opens(d),closes(d),highs(d),lows(d); i=len(d)-1
    if side=="LONG": return c[i]>o[i] and c[i]>h[i-1] and l[i]<=l[i-1]
    return c[i]<o[i] and c[i]<l[i-1] and h[i]>=h[i-1]

def target(d,side,entry):
    d=closed(d); h,l=highs(d),lows(d)
    if side=="LONG":
        xs=[x for x in h[-40:-2] if x>entry]
        return min(xs) if xs else entry*(1+TARGET_PCT)
    xs=[x for x in l[-40:-2] if x<entry]
    return max(xs) if xs else entry*(1-TARGET_PCT)

def signal(s):
    try:
        d1,d15,d10,d5=fetch(s,"1h"),fetch(s,"15m"),build_10m(s),fetch(s,"5m")
        b,st=bias_1h(d1),structure(d15)
        if b=="LONG" and st!="BEAR_BOS": side="LONG"
        elif b=="SHORT" and st!="BULL_BOS": side="SHORT"
        else:return None
        seq=sweep_choch(d15,side)
        if not seq:return None
        sweep_i,choch_i,sweep_extreme=seq
        fv=fvg_after(d10,side)
        if not fv or not retest(d10,side,fv) or not confirm5(d5,side):return None
        a=atr(d5)
        if a<=0:return None
        _,zl,zh=fv; entry_low,entry_high=min(zl,zh),max(zl,zh)
        width=entry_high-entry_low; maxw=closes(closed(d5))[-1]*0.004
        if width>maxw:
            if side=="LONG": entry_low=entry_high-maxw
            else: entry_high=entry_low+maxw
        if side=="LONG":
            sl=min(sweep_extreme,entry_low)-0.15*a
            tp=max(target(d15,side,entry_high),entry_high*(1+MIN_MOVE_PCT))
            risk=entry_high-sl; rr=(tp-entry_high)/risk if risk>0 else 0
        else:
            sl=max(sweep_extreme,entry_high)+0.15*a
            tp=min(target(d15,side,entry_low),entry_low*(1-MIN_MOVE_PCT))
            risk=sl-entry_low; rr=(entry_low-tp)/risk if risk>0 else 0
        if risk<=0 or rr<MIN_RR:return None
        return side,s,entry_low,entry_high,sl,tp,rr,f"1H={b} | 15m={st} | liquidity sweep | CHoCH/BOS | IMB/FVG | POI retest | 5m confirmation"
    except Exception as e:
        logging.warning("%s | ERROR | %s",s,e); return None

def load_state():
    try:return json.load(open(STATE_FILE,encoding="utf-8"))
    except:return {}

def save_state(x):
    try:json.dump(x,open(STATE_FILE,"w",encoding="utf-8"))
    except:pass

def send(t):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return
    try:requests.post("https://api.telegram.org/bot"+TELEGRAM_TOKEN+"/sendMessage",json={"chat_id":TELEGRAM_CHAT_ID,"text":t},timeout=8)
    except Exception as e:logging.warning("Telegram error: %s",e)

def main():
    logging.info("SCALP V6.5 started | CHoCH + FVG + POI")
    state=load_state()
    while True:
        for s in SYMBOLS:
            s=s.strip()
            if not s:continue
            x=signal(s)
            if not x or time.time()-float(state.get(s,0))<COOLDOWN_SECONDS:continue
            side,sym,el,eh,sl,tp,rr,why=x
            icon="🟢 LONG" if side=="LONG" else "🔴 SHORT"
            msg=(f"{icon}\nV6.5 SCALP — {sym}\n\n"
                 f"Entry zone: {el:.8g} – {eh:.8g}\nSL: {sl:.8g}\nTP: {tp:.8g}\n"
                 f"RR: {rr:.2f}\nLeverage: {LEVERAGE}x\n\nCONFIRMATION:\n{why}\n\n"
                 f"⚠️ Алгоритмічний сигнал, не гарантія результату.")
            send(msg); logging.info("%s | SIGNAL | RR=%.2f",s,rr)
            state[s]=time.time(); save_state(state)
        time.sleep(POLL_SECONDS)

if __name__=="__main__":main()
