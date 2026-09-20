import os,time,math,logging,requests,ccxt
logging.basicConfig(level=logging.INFO,format='%(asctime)s | V6 | %(levelname)s | %(message)s')
SYMBOLS=os.getenv('SYMBOLS','BTC/USDT:USDT,ETH/USDT:USDT,NEAR/USDT:USDT,PYTH/USDT:USDT,ADA/USDT:USDT,ENA/USDT:USDT').split(',')
POLL_SECONDS=int(os.getenv('POLL_SECONDS','60')); LEVERAGE=int(os.getenv('LEVERAGE','30')); RR=float(os.getenv('RR','2.0')); MIN_SCORE=int(os.getenv('MIN_SCORE','7'))
TELEGRAM_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN',''); TELEGRAM_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID','')
exchange=ccxt.mexc({'enableRateLimit':True,'options':{'defaultType':'swap'}})
def closes(r): return [float(x[4]) for x in r]
def highs(r): return [float(x[2]) for x in r]
def lows(r): return [float(x[3]) for x in r]
def opens(r): return [float(x[1]) for x in r]
def ema(v,p):
    if not v:return 0.0
    k=2/(p+1); e=v[0]
    for x in v[1:]: e=x*k+e*(1-k)
    return e
def atr(r,p=14):
    if len(r)<p+2:return 0.0
    tr=[]
    for i in range(1,len(r)):
        h,l,pc=float(r[i][2]),float(r[i][3]),float(r[i-1][4]); tr.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(tr[-p:])/p
def fetch(s,tf): return exchange.fetch_ohlcv(s,timeframe=tf,limit=80)
def bias(d):
    c=closes(d)
    if len(c)<55:return 'NEUTRAL'
    e20,e50,last=ema(c[:-1],20),ema(c[:-1],50),c[-2]
    if last>e20>e50:return 'LONG'
    if last<e20<e50:return 'SHORT'
    return 'NEUTRAL'
def structure(d):
    if len(d)<12:return 'RANGE'
    c,h,l=closes(d),highs(d),lows(d); last=c[-2]; hi,lo=max(h[-8:-2]),min(l[-8:-2])
    if last>hi:return 'BULL_BOS'
    if last<lo:return 'BEAR_BOS'
    hi3,lo3=max(h[-5:-2]),min(l[-5:-2])
    if last>hi3:return 'BULL_CHOCH'
    if last<lo3:return 'BEAR_CHOCH'
    return 'RANGE'
def sweep(d,side):
    if len(d)<15:return False
    h,l,c=highs(d),lows(d),closes(d); hi,lo=max(h[-14:-2]),min(l[-14:-2])
    return l[-2]<lo and c[-2]>lo if side=='LONG' else h[-2]>hi and c[-2]<hi
def fvg(d,side):
    if len(d)<5:return False
    h,l=highs(d),lows(d); return l[-2]>h[-4] if side=='LONG' else h[-2]<l[-4]
def retest(d,side):
    if len(d)<7:return False
    o,c,h,l=opens(d),closes(d),highs(d),lows(d)
    return l[-3]<=h[-5] and c[-3]>o[-3] if side=='LONG' else h[-3]>=l[-5] and c[-3]<o[-3]
def trigger(d,side):
    if len(d)<5:return False
    o,c,h,l=opens(d),closes(d),highs(d),lows(d)
    return c[-2]>o[-2] and c[-2]>h[-3] and l[-2]<=l[-3] if side=='LONG' else c[-2]<o[-2] and c[-2]<l[-3] and h[-2]>=h[-3]
def signal(s):
    try:
        d1,d15,d10,d5=fetch(s,'1h'),fetch(s,'15m'),fetch(s,'10m'),fetch(s,'5m'); b,st=bias(d1),structure(d15)
        if b=='LONG' and st in ('BULL_BOS','BULL_CHOCH'): side='LONG'
        elif b=='SHORT' and st in ('BEAR_BOS','BEAR_CHOCH'): side='SHORT'
        else:return None
        score=4; reasons=['1H='+b,'15m='+st]
        if not sweep(d15,side):return None
        score+=2; reasons.append('15m liquidity sweep')
        if not fvg(d10,side):return None
        score+=2; reasons.append('10m imbalance/FVG')
        if not retest(d10,side):return None
        score+=1; reasons.append('10m retest')
        if not trigger(d5,side):return None
        score+=1; reasons.append('5m confirmation')
        if score<MIN_SCORE:return None
        price,a=closes(d5)[-2],atr(d5)
        if not math.isfinite(a) or a<=0:return None
        h,l=highs(d5),lows(d5)
        if side=='LONG':
            sl=min(l[-12:])-.25*a; risk=price-sl
            if risk<=0:return None
            el,eh=price-.20*a,price+.05*a; tp1,tp2=price+risk,price+risk*RR
        else:
            sl=max(h[-12:])+.25*a; risk=sl-price
            if risk<=0:return None
            el,eh=price-.05*a,price+.20*a; tp1,tp2=price-risk,price-risk*RR
        return side,s,score,min(el,eh),max(el,eh),sl,tp1,tp2,' | '.join(reasons)
    except Exception as e: logging.warning('%s: %s',s,e); return None
def send(t):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:return
    try: requests.post('https://api.telegram.org/bot'+TELEGRAM_TOKEN+'/sendMessage',json={'chat_id':TELEGRAM_CHAT_ID,'text':t},timeout=8)
    except Exception as e:logging.warning('Telegram error: %s',e)
def main():
    logging.info('SCALP V6 LIGHT started | symbols=%s',','.join(SYMBOLS)); last={}
    while True:
        for s in SYMBOLS:
            s=s.strip()
            if not s:continue
            x=signal(s)
            if not x or time.time()-last.get(s,0)<1800:continue
            side,sym,score,el,eh,sl,tp1,tp2,why=x; icon='🟢 LONG' if side=='LONG' else '🔴 SHORT'
            msg=f'{icon}\nV6 SCALP — {sym}\n\nScore: {score}/10\nEntry: {el:.8g} – {eh:.8g}\nSL: {sl:.8g}\nTP1: {tp1:.8g}\nTP2: {tp2:.8g}\nLeverage: {LEVERAGE}x\n\nCONFIRMATION:\n{why}\n\n⚠️ Це сигнал алгоритму, не гарантія результату.'
            send(msg); logging.info(msg); last[s]=time.time()
        time.sleep(POLL_SECONDS)
if __name__=='__main__':main()
