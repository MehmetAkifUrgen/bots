"""Flama + VWAP Scalp Bot — Küçük Sermaye Modu"""
import json,math,os,time
from dataclasses import dataclass,asdict
from datetime import datetime,timezone
from typing import Optional
import numpy as np,pandas as pd,requests
from dotenv import load_dotenv
load_dotenv()

BASE=os.getenv("BINANCE_API_FUTURES_BASE","https://fapi.binance.com")
TK=os.getenv("TELEGRAM_BOT_TOKEN","");TC=os.getenv("TELEGRAM_CHAT_ID","")
MIN_SCORE=int(os.getenv("PENNANT_MIN_SCORE","60"))
COOL=int(os.getenv("PENNANT_COOLDOWN_MINUTES","15"))
MIN_V=float(os.getenv("MIN_QUOTE_VOLUME_USD","5000000"))
MAX_V=float(os.getenv("MAX_QUOTE_VOLUME_USD","1000000000"))
SF=os.getenv("PENNANT_STATE_FILE","pennant_state.json")
MAX_P=int(os.getenv("MAX_OPEN_POSITIONS","5"))
TFS=("5m","15m")
STABLE={"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","UST","EURC"}

def _s(v):
    try:
        r=float(v)
        return 0.0 if(math.isnan(r)or math.isinf(r))else r
    except:return 0.0
def now():return datetime.now(timezone.utc)
def fp(v):
    if v>=1000:return f"{v:.2f}"
    if v>=1:return f"{v:.4f}"
    return f"{v:.6f}"
def tg(txt):
    if not TK or not TC:print(txt);return
    try:requests.post(f"https://api.telegram.org/bot{TK}/sendMessage",
        json={"chat_id":TC,"text":txt,"parse_mode":"Markdown","disable_web_page_preview":True},timeout=20).raise_for_status()
    except Exception as e:print(f"[TG]{e}")
def jget(url,p=None):
    r=requests.get(url,params=p,timeout=25);r.raise_for_status();return r.json()
def kl(sym,tf,n=250):
    raw=jget(f"{BASE}/fapi/v1/klines",{"symbol":sym,"interval":tf,"limit":n})
    cols=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","ign"]
    df=pd.DataFrame(raw,columns=cols)
    for c in["o","h","l","c","v"]:df[c]=pd.to_numeric(df[c],errors="coerce")
    return df
def lp(sym):return float(jget(f"{BASE}/fapi/v1/ticker/price",{"symbol":sym}).get("price",0))
def ind(df):
    c=df["c"]
    df["e20"]=c.ewm(span=20,adjust=False).mean()
    df["e50"]=c.ewm(span=50,adjust=False).mean()
    df["e9"]=c.ewm(span=9,adjust=False).mean()
    d=c.diff()
    g=d.clip(lower=0).ewm(alpha=1/14,min_periods=14,adjust=False).mean()
    l=(-d.clip(upper=0)).ewm(alpha=1/14,min_periods=14,adjust=False).mean()
    df["rsi"]=(100-100/(1+g/l.replace(0,np.nan))).fillna(50)
    df["atr"]=pd.concat([df["h"]-df["l"],(df["h"]-c.shift()).abs(),(df["l"]-c.shift()).abs()],axis=1).max(axis=1).ewm(alpha=1/14,adjust=False).mean()
    df["vm"]=df["v"].rolling(20).mean()
    tp=(df["h"]+df["l"]+df["c"])/3
    df["vwap"]=(tp*df["v"]).cumsum()/df["v"].cumsum()
    return df
def slp(a):
    y=np.array(a,dtype=float)
    if len(y)<3:return 0.0
    s=np.polyfit(np.arange(len(y)),y,1)[0]
    return s/(abs(y.mean())or 1)*100

def ld():
    if os.path.exists(SF):
        try:
            with open(SF)as f:return json.load(f)
        except:pass
    return{"positions":[],"last_scan_at":None,"closed_trades":[]}
def sv(s):
    with open(SF,"w")as f:json.dump(s,f,indent=2,ensure_ascii=False)

def btc_up():
    try:
        df=ind(kl("BTCUSDT","1h",60))
        r=df.iloc[-2]
        return _s(r["e20"])>_s(r["e50"])*0.998
    except:return True

@dataclass
class Sig:
    sym:str;tf:str;score:int;why:list;price:float
    fp_g:float;fp_h:float;cb:int;vd:float
    upper:float;low:float;dist:float;rsi:float
    atr:float;dual:bool=False;pct24:float=0.0;kind:str="FLAMA"

@dataclass
class ScalpSig:
    sym:str;price:float;vwap:float;rsi:float;atr:float
    vol_ratio:float;score:int;why:list;pct24:float=0.0;kind:str="SCALP"

@dataclass
class Pos:
    sym:str;tf:str;entry:float;sl:float;tp1:float;tp2:float
    fp_g:float;score:int;opened:str;high:float;status:str="OPEN";kind:str="FLAMA"

def detect_flama(df,sym,tf,pct24=0.0):
    if len(df)<60:return None
    df=ind(df.copy())
    H=df["h"].values.astype(float);L=df["l"].values.astype(float)
    C=df["c"].values.astype(float);V=df["v"].values.astype(float)
    E20=df["e20"].values.astype(float);E50=df["e50"].values.astype(float)
    RSI=df["rsi"].values.astype(float);VM=df["vm"].values.astype(float)
    ATR=df["atr"].values.astype(float)
    N=len(df)-2;cur=_s(C[N])
    if cur<=0:return None
    if not(cur>_s(E20[N])>_s(E50[N])*0.995):return None
    bg,fps,fpe=0.0,-1,-1
    for fe in range(N-4,max(N-35,5),-1):
        for fl in range(3,11):
            fs=fe-fl
            if fs<2:break
            if _s(C[fe])<=_s(C[fs]):continue
            lo=float(np.min(L[fs:fe+1]));hi=float(np.max(H[fs:fe+1]))
            if lo<=0:continue
            g=(hi-lo)/lo*100
            mv=hi-lo
            ok=all(_s(C[i])>=lo+mv*0.1 for i in range(fs,fe+1))
            if g>=3 and g>bg and ok:bg,fps,fpe=g,fs,fe
    if fps<0 or bg<3:return None
    fph=_s(H[fpe])-_s(L[fps])
    cs,ce=fpe,N;cb=ce-cs
    if not(4<=cb<=18):return None
    cH=H[cs:ce+1];cL=L[cs:ce+1];cV=V[cs:ce+1];cR=RSI[cs:ce+1]
    if len(cH)<4:return None
    ls=slp(cL);hs=slp(cH)
    if not(ls>0.05 and hs<-0.05):return None
    rc=float(np.mean(cR))
    if not(35<=rc<=72):return None
    fv=float(np.mean(V[fps:fpe+1]))if fpe>fps else 1.0
    cv=float(np.mean(cV))if len(cV)>0 else 1.0
    vd=(fv-cv)/fv*100 if fv>0 else 0.0
    if vd<10:return None
    xs=np.arange(len(cH))
    coef=np.polyfit(xs,cH,1)
    upper=float(np.polyval(coef,len(cH)))
    clow=float(np.min(cL))
    dist=(upper-cur)/cur*100 if cur>0 else 99.0
    if dist>2.0:return None
    atr=_s(ATR[N])
    score=0;why=[]
    if bg>=10:score+=30;why.append(f"🚀 Güçlü direk +%{bg:.1f}")
    elif bg>=6:score+=22;why.append(f"📈 Direk +%{bg:.1f}")
    else:score+=15;why.append(f"📈 Direk +%{bg:.1f}")
    score+=25;why.append(f"📐 Kama (↑{ls:+.2f}% ↓{hs:+.2f}%)")
    if vd>=50:score+=20;why.append(f"📉 Hacim -%{vd:.0f}")
    elif vd>=30:score+=13;why.append(f"📉 Hacim -%{vd:.0f}")
    else:score+=7;why.append(f"↘️ Hacim -%{vd:.0f}")
    if 5<=cb<=10:score+=12;why.append(f"⏱️ İdeal kons {cb}bar")
    elif cb<=15:score+=6;why.append(f"⏱️ Kons {cb}bar")
    if 45<=rc<=60:score+=8;why.append(f"📊 RSI nötr {rc:.0f}")
    elif rc<45:score+=5;why.append(f"📊 RSI {rc:.0f}")
    if 0<=dist<=1.0:score+=5;why.append(f"🎯 Kırılım %{dist:.2f}")
    elif dist<0:score+=8;why.append(f"✅ Kırıldı +%{abs(dist):.2f}")
    return Sig(sym=sym,tf=tf,score=min(score,100),why=why,price=cur,
               fp_g=bg,fp_h=fph,cb=cb,vd=vd,upper=upper,low=clow,
               dist=dist,rsi=rc,atr=atr,pct24=pct24)

def detect_scalp(df,sym,pct24=0.0):
    if len(df)<30:return None
    df=ind(df.copy())
    N=len(df)-2
    cur=_s(df["c"].iloc[N])
    if cur<=0:return None
    vwap=_s(df["vwap"].iloc[N])
    rsi=_s(df["rsi"].iloc[N])
    atr=_s(df["atr"].iloc[N])
    vm=_s(df["vm"].iloc[N])
    v=_s(df["v"].iloc[N])
    e20=_s(df["e20"].iloc[N])
    e9=_s(df["e9"].iloc[N])
    e50=_s(df["e50"].iloc[N])
    if vwap<=0 or atr<=0:return None
    vr=v/vm if vm>0 else 1.0
    dist_vwap=(cur-vwap)/vwap*100
    score=0;why=[]
    # VWAP bounce: fiyat VWAP'ı az önce kesti, şimdi üstünde
    prev_c=_s(df["c"].iloc[N-1])
    prev_l=_s(df["l"].iloc[N-1])
    if prev_l<=vwap<=prev_c and cur>vwap:
        score+=35;why.append(f"💠 VWAP kırılımı (VWAP={fp(vwap)})")
    elif 0<=dist_vwap<=0.3:
        score+=20;why.append(f"💠 VWAP üstünde %{dist_vwap:.2f}")
    else:return None
    if e9>e20>e50*0.998:score+=20;why.append("📈 EMA9>EMA20>EMA50 hizası")
    elif e9>e20:score+=10;why.append("📈 Kısa EMA yükseliyor")
    if 40<=rsi<=65:score+=15;why.append(f"📊 RSI nötr {rsi:.0f}")
    elif rsi<40:score+=8;why.append(f"📊 RSI düşük {rsi:.0f}")
    if vr>=2.0:score+=20;why.append(f"⚡ Hacim patlaması {vr:.1f}x")
    elif vr>=1.3:score+=10;why.append(f"📊 Hacim artışı {vr:.1f}x")
    if score<45:return None
    return ScalpSig(sym=sym,price=cur,vwap=vwap,rsi=rsi,atr=atr,
                    vol_ratio=vr,score=min(score,100),why=why,pct24=pct24)

def lvl_flama(s):
    e=max(s.upper*1.001,s.price)
    sl=e-s.atr*1.5  # Kucuk sermaye: daha dar stop
    risk=e-sl
    if risk<=0:risk=e*0.02
    tp1=e+risk*1.5;tp2=e+s.fp_h
    rr=(tp1-e)/risk
    return e,sl,tp1,tp2,rr

def lvl_scalp(s):
    e=s.price
    sl=e-s.atr*1.2
    risk=e-sl
    if risk<=0:risk=e*0.01
    tp1=e+risk*1.5;tp2=e+risk*2.5
    rr=(tp1-e)/risk
    return e,sl,tp1,tp2,rr

def msg_flama(s):
    e,sl,tp1,tp2,rr=lvl_flama(s)
    dual=" 🔥*ÇİFT TF*"if s.dual else""
    bar="█"*(s.score//10)+"░"*(10-s.score//10)
    w="\n".join(f"  • {x}"for x in s.why)
    return(f"🏳️ *FLAMA* | *{s.sym}* [{s.tf}]{dual}\n"
           f"Skor: `{s.score}/100` `{bar}`\n\n"
           f"*Sinyaller:*\n{w}\n\n"
           f"*📌 Seviyeleri:*\n"
           f"Giriş  : `{fp(e)}`\n"
           f"Stop   : `{fp(sl)}` _(ATR x2)_\n"
           f"Hedef 1: `{fp(tp1)}` _(1.5R)_\n"
           f"Hedef 2: `{fp(tp2)}` _(Direk boyu)_\n"
           f"R/Ödül : `{rr:.1f}R` | RSI: `{s.rsi:.0f}`")

def msg_scalp(s):
    e,sl,tp1,tp2,rr=lvl_scalp(s)
    bar="█"*(s.score//10)+"░"*(10-s.score//10)
    w="\n".join(f"  • {x}"for x in s.why)
    return(f"⚡ *VWAP SCALP* | *{s.sym}*\n"
           f"Skor: `{s.score}/100` `{bar}`\n\n"
           f"*Sinyaller:*\n{w}\n\n"
           f"*📌 Seviyeleri:*\n"
           f"Giriş  : `{fp(e)}`\n"
           f"Stop   : `{fp(sl)}` _(ATR x1.2)_\n"
           f"Hedef 1: `{fp(tp1)}` _(1.5R)_\n"
           f"Hedef 2: `{fp(tp2)}` _(2.5R)_\n"
           f"R/Ödül : `{rr:.1f}R` | Vol: `{s.vol_ratio:.1f}x`\n"
           f"VWAP: `{fp(s.vwap)}` | RSI: `{s.rsi:.0f}`")

def msg_sum(fs,ss):
    lines=[f"📊 *TARAMA ÖZET* | {now().strftime('%H:%M UTC')} | BTC: {'✅'if btc_ok else '❌'}\n"]
    if fs:
        lines.append("*🏳️ FLAMALAR:*")
        for s in fs[:6]:
            e,sl,tp1,_,rr=lvl_flama(s)
            d="🔥"if s.dual else"▫️"
            lines.append(f"{d}*{s.sym}*[{s.tf}] `{s.score}/100` G:`{fp(e)}` SL:`{fp(sl)}` TP:`{fp(tp1)}` R:{rr:.1f}")
    if ss:
        lines.append("\n*⚡ VWAP SCALP:*")
        for s in ss[:5]:
            e,sl,tp1,_,rr=lvl_scalp(s)
            lines.append(f"*{s.sym}* `{s.score}/100` G:`{fp(e)}` SL:`{fp(sl)}` TP:`{fp(tp1)}` R:{rr:.1f}")
    return"\n".join(lines)

def msg_exit(pos,price):
    pnl=(price-pos.entry)/pos.entry*100
    L={"TP1":"✅ HEDEF 1","TP2":"✅✅ HEDEF 2","SL":"❌ STOP","TIMEOUT":"⏱️ SÜRE"}
    icon="🏳️"if pos.kind=="FLAMA"else"⚡"
    return(f"{icon} {L.get(pos.status,pos.status)} | *{pos.sym}* [{pos.tf}]\n"
           f"Giriş:`{fp(pos.entry)}` → Çıkış:`{fp(price)}`\n"
           f"Sonuç: `{pnl:+.2f}%`\n"
           f"SL:`{fp(pos.sl)}` TP1:`{fp(pos.tp1)}` TP2:`{fp(pos.tp2)}`")

def monitor(state):
    ops=[Pos(**p)for p in state.get("positions",[])if p.get("status")=="OPEN"]
    cl=[p for p in state.get("positions",[])if p.get("status")!="OPEN"]
    still=[]
    for pos in ops:
        try:
            price=lp(pos.sym)
            pos.high=max(pos.high,price)
            age=(now()-datetime.fromisoformat(pos.opened)).total_seconds()/3600
            timeout=24 if pos.kind=="FLAMA"else 1  # Scalp max 1 saat
            if age>timeout:pos.status="TIMEOUT"
            elif price>=pos.tp2:pos.status="TP2"
            elif price>=pos.tp1:pos.status="TP1"
            elif price<=pos.sl:pos.status="SL"
            if pos.status!="OPEN":
                tg(msg_exit(pos,price));cl.append(asdict(pos))
                print(f"  [{pos.status}] {pos.sym}[{pos.kind}] {fp(price)}")
            else:
                still.append(asdict(pos))
                print(f"  [OPEN][{pos.kind}] {pos.sym} f={fp(price)} SL={fp(pos.sl)} TP1={fp(pos.tp1)}")
        except Exception as e:
            print(f"  [Err]{pos.sym}:{e}");still.append(asdict(pos))
        time.sleep(0.1)
    state["positions"]=still
    state["closed_trades"]=(cl+state.get("closed_trades",[]))[:300]
    return state

def cands():
    try:
        info=jget(f"{BASE}/fapi/v1/exchangeInfo")
        active={r["symbol"]for r in info.get("symbols",[])
                if r.get("status")=="TRADING"and r.get("contractType")=="PERPETUAL"
                and r.get("quoteAsset")=="USDT"and r.get("symbol","")[:-4].isalnum()
                and r.get("symbol","")[:-4]not in STABLE}
        tickers=jget(f"{BASE}/fapi/v1/ticker/24hr")
        out=[]
        for t in tickers:
            sym=t.get("symbol","")
            if sym not in active:continue
            try:qv=float(t.get("quoteVolume",0));pct=float(t.get("priceChangePercent",0))
            except:continue
            if MIN_V<=qv<=MAX_V:out.append((sym,pct,qv))
        out.sort(key=lambda x:x[2],reverse=True)
        return out
    except Exception as e:
        print(f"[Cands]{e}");return[]

btc_ok=True

def scan(state):
    global btc_ok
    print(f"\n{'='*50}\n📡 TARAMA — {now().strftime('%H:%M:%S UTC')}")
    btc_ok=btc_up()
    print(f"   BTC Trend: {'✅ Yukari'if btc_ok else'❌ Asagi — Long acilmaz'}")
    cs=cands()
    print(f"   {len(cs)} coin taranıyor...\n")
    open_syms={p["sym"]for p in state.get("positions",[])if p.get("status")=="OPEN"}
    flamas=[];scalps=[]
    for i,(sym,pct24,_)in enumerate(cs):
        print(f"  [{i+1}/{len(cs)}] {sym}",end="\r")
        # Flama (sadece BTC yukari ise)
        if btc_ok:
            rs=[]
            for tf in TFS:
                try:
                    sig=detect_flama(kl(sym,tf,250),sym,tf,pct24)
                    if sig and sig.score>=MIN_SCORE:rs.append(sig)
                except:pass
                time.sleep(0.04)
            if rs:
                tfs={s.tf for s in rs}
                if"5m"in tfs and"15m"in tfs:
                    for s in rs:s.dual=True
                flamas.extend(rs)
        # Scalp (her zaman)
        try:
            ss=detect_scalp(kl(sym,"5m",100),sym,pct24)
            if ss and ss.score>=50:scalps.append(ss)
        except:pass
        time.sleep(0.04)
    flamas.sort(key=lambda s:(s.dual,s.score),reverse=True)
    scalps.sort(key=lambda s:s.score,reverse=True)
    print(f"\n  ✅ {len(flamas)} flama, {len(scalps)} VWAP scalp bulundu.\n")
    if flamas or scalps:
        tg(msg_sum(flamas,scalps));time.sleep(0.5)
    oc=len(open_syms)
    # Flama pozisyonları
    for sig in flamas:
        if oc>=MAX_P:break
        if sig.sym in open_syms:continue
        if not sig.dual and sig.score<MIN_SCORE+10:continue
        if sig.dist>1.5:continue
        e,sl,tp1,tp2,rr=lvl_flama(sig)
        if e<=0 or sl>=e or rr<1.2:continue
        pos=Pos(sym=sig.sym,tf=sig.tf,entry=e,sl=sl,tp1=tp1,tp2=tp2,
                fp_g=sig.fp_g,score=sig.score,opened=now().isoformat(),
                high=e,status="OPEN",kind="FLAMA")
        state.setdefault("positions",[]).append(asdict(pos))
        open_syms.add(sig.sym);oc+=1
        tg(msg_flama(sig))
        print(f"  [FLAMA AÇILDI] {sig.sym}[{sig.tf}] G={fp(e)} SL={fp(sl)} TP1={fp(tp1)}")
        time.sleep(0.3)
    # Scalp pozisyonları
    for sig in scalps[:3]:
        if oc>=MAX_P:break
        if sig.sym in open_syms:continue
        e,sl,tp1,tp2,rr=lvl_scalp(sig)
        if e<=0 or sl>=e or rr<1.2:continue
        pos=Pos(sym=sig.sym,tf="5m",entry=e,sl=sl,tp1=tp1,tp2=tp2,
                fp_g=0,score=sig.score,opened=now().isoformat(),
                high=e,status="OPEN",kind="SCALP")
        state.setdefault("positions",[]).append(asdict(pos))
        open_syms.add(sig.sym);oc+=1
        tg(msg_scalp(sig))
        print(f"  [SCALP AÇILDI] {sig.sym} G={fp(e)} SL={fp(sl)} TP1={fp(tp1)}")
        time.sleep(0.3)
    state["last_scan_at"]=now().isoformat()
    return state

def main():
    print(f"🤖 Flama+VWAP Scalp Botu — Küçük Sermaye Modu")
    print(f"   Flama  : Skor≥{MIN_SCORE} | Stop:ATR×1.5 | Timeout:24h")
    print(f"   Scalp  : Skor≥50 | Stop:ATR×1.2 | Timeout:1h")
    print(f"   Hacim  : {MIN_V/1e6:.1f}M–{MAX_V/1e6:.0f}M USD | MaxPos:{MAX_P}")
    print(f"   TG:{'✅'if TK else'❌'}\n")
    while True:
        state=ld()
        try:
            if state.get("positions"):
                state=monitor(state);sv(state)
            last=state.get("last_scan_at")
            do=(not last or(now()-datetime.fromisoformat(last)).total_seconds()>=COOL*60)
            if do:state=scan(state);sv(state)
            else:
                w=int(COOL*60-(now()-datetime.fromisoformat(last)).total_seconds())
                print(f"[Cooldown] {w//60}dk {w%60}sn kaldı.")
        except Exception as e:print(f"[Hata]{e}")
        time.sleep(60)

if __name__=="__main__":
    main()
