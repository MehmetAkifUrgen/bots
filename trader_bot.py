"""
trader_bot.py — Uzman Trader Botu
4h trend + 1h setup + 15m giris | Coklu teyit sistemi
"""
import json,math,os,time
from dataclasses import dataclass,asdict,field
from datetime import datetime,timezone
from typing import Optional
import numpy as np,pandas as pd,requests
from dotenv import load_dotenv
load_dotenv()

BASE  = os.getenv("BINANCE_API_FUTURES_BASE","https://fapi.binance.com")
TK    = os.getenv("TELEGRAM_BOT_TOKEN","")
TC    = os.getenv("TELEGRAM_CHAT_ID","")
COOL  = int(os.getenv("SCAN_COOLDOWN_MIN","30"))
MIN_V = float(os.getenv("MIN_QUOTE_VOLUME_USD","500000"))
MAX_V = float(os.getenv("MAX_QUOTE_VOLUME_USD","200000000"))
SF    = os.getenv("STATE_FILE","trader_state.json")
MAX_P = int(os.getenv("MAX_OPEN_POSITIONS","3"))
MIN_G = int(os.getenv("MIN_GRADE_SCORE","65"))
STABLE= {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","UST","EURC"}

def _s(v):
    try:
        r=float(v)
        return 0. if math.isnan(r) or math.isinf(r) else r
    except: return 0.
def utc(): return datetime.now(timezone.utc)
def fp(v):
    if v>=1000: return f"{v:.2f}"
    if v>=1:    return f"{v:.4f}"
    return f"{v:.6f}"
def tg(txt):
    if not TK or not TC: print(txt); return
    try:
        requests.post(f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id":TC,"text":txt,"parse_mode":"Markdown",
                  "disable_web_page_preview":True},timeout=20).raise_for_status()
    except Exception as e: print(f"[TG]{e}")
def get(url,p=None):
    r=requests.get(url,params=p,timeout=25); r.raise_for_status(); return r.json()
def kl(sym,tf,n=200):
    raw=get(f"{BASE}/fapi/v1/klines",{"symbol":sym,"interval":tf,"limit":n})
    df=pd.DataFrame(raw,columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for c in["o","h","l","c","v"]: df[c]=pd.to_numeric(df[c],errors="coerce")
    return df
def lpx(sym): return float(get(f"{BASE}/fapi/v1/ticker/price",{"symbol":sym}).get("price",0))
def funding(sym):
    try: return float(get(f"{BASE}/fapi/v1/premiumIndex",{"symbol":sym}).get("lastFundingRate",0))*100
    except: return 0.

def ind(df):
    c=df["c"]
    df["e9"]  = c.ewm(span=9,  adjust=False).mean()
    df["e20"] = c.ewm(span=20, adjust=False).mean()
    df["e50"] = c.ewm(span=50, adjust=False).mean()
    df["e200"]= c.ewm(span=200,adjust=False).mean()
    d=c.diff()
    g=d.clip(lower=0).ewm(alpha=1/14,min_periods=14,adjust=False).mean()
    l=(-d.clip(upper=0)).ewm(alpha=1/14,min_periods=14,adjust=False).mean()
    df["rsi"] =(100-100/(1+g/l.replace(0,np.nan))).fillna(50)
    df["vm"]  = df["v"].rolling(20).mean()
    df["atr"] = pd.concat([df["h"]-df["l"],(df["h"]-c.shift()).abs(),(df["l"]-c.shift()).abs()],axis=1).max(axis=1).ewm(alpha=1/14,adjust=False).mean()
    fast=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    sig=fast.ewm(span=9,adjust=False).mean()
    df["macd_h"]=fast-sig
    return df

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except: pass
    return {"positions":[],"closed":[],"last_scan":None}
def save_st(s):
    with open(SF,"w") as f: json.dump(s,f,indent=2,ensure_ascii=False)

@dataclass
class Signal:
    sym: str
    side: str          # LONG / SHORT
    grade: str         # A+ / A / B
    score: int
    reasons: list
    price: float
    entry: float
    sl: float
    tp1: float
    tp2: float
    rr: float
    tf_trend: str      # 4h trend
    rsi_1h: float
    funding_pct: float
    atr: float

@dataclass
class Position:
    sym: str; side: str; grade: str; score: int
    entry: float; sl: float; tp1: float; tp2: float
    opened: str; highest: float; lowest: float
    rr: float; status: str="OPEN"

def grade(score):
    if score>=85: return "A+"
    if score>=75: return "A"
    if score>=65: return "B"
    return "C"

def analyze(sym, pct24):
    try:
        df4 = ind(kl(sym,"4h",100))
        df1 = ind(kl(sym,"1h",100))
        df15= ind(kl(sym,"15m",150))
    except: return None

    if len(df4)<50 or len(df1)<50 or len(df15)<50: return None

    r4  = df4.iloc[-2]
    r1  = df1.iloc[-2]
    r15 = df15.iloc[-2]

    c4  = _s(r4["c"]);   e20_4=_s(r4["e20"]); e50_4=_s(r4["e50"]); e200_4=_s(r4["e200"])
    c1  = _s(r1["c"]);   e9_1=_s(r1["e9"]);   e20_1=_s(r1["e20"]); e50_1=_s(r1["e50"])
    c15 = _s(r15["c"]);  e20_15=_s(r15["e20"]);e50_15=_s(r15["e50"])
    rsi4=_s(r4["rsi"]); rsi1=_s(r1["rsi"]); rsi15=_s(r15["rsi"])
    atr15=_s(r15["atr"]); atr1=_s(r1["atr"])
    vol15=_s(r15["v"]); vm15=_s(r15["vm"])
    macd1=_s(r1["macd_h"]); macd4=_s(r4["macd_h"])
    fund = funding(sym)
    if c15<=0 or atr15<=0: return None

    score=0; reasons=[]; side=None

    # ── 4H TREND (max 30 puan) ──────────────────────────────────────────
    bull4 = c4>e20_4>e50_4 and e20_4>e50_4
    bear4 = c4<e20_4<e50_4 and e20_4<e50_4
    if bull4:
        score+=30; reasons.append("📈 4h trend YUKARI (EMA20>EMA50)")
        side="LONG"
    elif bear4:
        score+=30; reasons.append("📉 4h trend ASAGI (EMA20<EMA50)")
        side="SHORT"
    else:
        return None  # Trend net değil, geç

    # ── 1H SETUP (max 25 puan) ──────────────────────────────────────────
    if side=="LONG":
        # 1h'da fiyat EMA20'ye yakın (pullback bölgesi)
        dist1 = (c1-e20_1)/atr1 if atr1>0 else 99
        if 0<=dist1<=1.0:
            score+=20; reasons.append(f"🔄 1h EMA20 pullback bölgesi")
        elif 0<=dist1<=2.0:
            score+=10; reasons.append(f"🔄 1h EMA20 yakını")
        # 1h MACD pozitif dönüyor
        prev_macd1=_s(df1.iloc[-3]["macd_h"])
        if macd1>prev_macd1 and macd1>-abs(c1)*0.001:
            score+=5; reasons.append("📊 1h MACD pozitife dönüyor")
        # 1h RSI
        if 40<=rsi1<=60:
            score+=5; reasons.append(f"📊 1h RSI nötr ({rsi1:.0f})")
        elif 30<=rsi1<40:
            score+=8; reasons.append(f"📊 1h RSI oversold ({rsi1:.0f}) — güçlü alım fırsatı")
    else:  # SHORT
        dist1 = (e20_1-c1)/atr1 if atr1>0 else 99
        if 0<=dist1<=1.0:
            score+=20; reasons.append(f"🔄 1h EMA20 pullback (short)")
        elif 0<=dist1<=2.0:
            score+=10; reasons.append(f"🔄 1h EMA20 yakını (short)")
        prev_macd1=_s(df1.iloc[-3]["macd_h"])
        if macd1<prev_macd1 and macd1<abs(c1)*0.001:
            score+=5; reasons.append("📊 1h MACD negatife dönüyor")
        if 40<=rsi1<=60:
            score+=5; reasons.append(f"📊 1h RSI nötr ({rsi1:.0f})")
        elif 60<rsi1<=70:
            score+=8; reasons.append(f"📊 1h RSI overbought ({rsi1:.0f}) — short fırsatı")

    # ── 15M GİRİŞ KALİTESİ (max 25 puan) ────────────────────────────────
    if side=="LONG":
        if c15>e20_15 and e20_15>e50_15:
            score+=15; reasons.append("✅ 15m EMA hizalandı (e20>e50)")
        elif c15>e20_15:
            score+=8; reasons.append("↗️ 15m EMA20 üstünde")
        if rsi15>50 and rsi15<70:
            score+=5; reasons.append(f"📊 15m RSI momentum ({rsi15:.0f})")
        if vol15>vm15*1.3:
            score+=5; reasons.append(f"⚡ 15m hacim artışı ({vol15/vm15:.1f}x)")
    else:
        if c15<e20_15 and e20_15<e50_15:
            score+=15; reasons.append("✅ 15m EMA hizalandı (bear)")
        elif c15<e20_15:
            score+=8; reasons.append("↘️ 15m EMA20 altında")
        if rsi15<50 and rsi15>30:
            score+=5; reasons.append(f"📊 15m RSI zayıf ({rsi15:.0f})")
        if vol15>vm15*1.3:
            score+=5; reasons.append(f"⚡ 15m hacim artışı ({vol15/vm15:.1f}x)")

    # ── FUNDING RATE (max 10 puan) ────────────────────────────────────────
    if side=="LONG" and fund<-0.01:
        score+=10; reasons.append(f"💸 Funding negatif ({fund:.3f}%) — short squeeze riski")
    elif side=="LONG" and fund<0.02:
        score+=5; reasons.append(f"💚 Funding nötr ({fund:.3f}%)")
    elif side=="SHORT" and fund>0.05:
        score+=10; reasons.append(f"💸 Funding yüksek ({fund:.3f}%) — long sıkışması")

    # ── SKOR KONTROLÜ ─────────────────────────────────────────────────────
    if score < MIN_G: return None

    # ── SEVİYELER ─────────────────────────────────────────────────────────
    entry = c15
    if side=="LONG":
        sl   = entry - atr15*2.0
        tp1  = entry + atr15*3.0
        tp2  = entry + atr15*5.0
        # Swing high hedef
        recent_high = float(df15.iloc[-20:-2]["h"].max())
        if recent_high>tp1: tp2=recent_high
    else:
        sl   = entry + atr15*2.0
        tp1  = entry - atr15*3.0
        tp2  = entry - atr15*5.0
        recent_low = float(df15.iloc[-20:-2]["l"].min())
        if recent_low<tp1: tp2=recent_low

    risk = abs(entry-sl)
    rr   = abs(tp1-entry)/risk if risk>0 else 0

    return Signal(sym=sym,side=side,grade=grade(score),score=score,
                  reasons=reasons,price=c15,entry=entry,sl=sl,tp1=tp1,tp2=tp2,
                  rr=rr,tf_trend="BULL"if side=="LONG"else"BEAR",
                  rsi_1h=rsi1,funding_pct=fund,atr=atr15)

def msg_signal(s):
    bar="█"*(s.score//10)+"░"*(10-s.score//10)
    icon="🟢"if s.side=="LONG"else"🔴"
    rs="\n".join(f"  • {r}" for r in s.reasons)
    side_tr="LONG (AL)"if s.side=="LONG"else"SHORT (SAT)"
    return (f"{icon} *{side_tr}* | *{s.sym}* | Not: `{s.grade}` `{s.score}/100`\n"
            f"`{bar}`\n\n"
            f"*📋 Analiz:*\n{rs}\n\n"
            f"*📌 İşlem Planı:*\n"
            f"Giriş  : `{fp(s.entry)}`\n"
            f"Stop   : `{fp(s.sl)}`\n"
            f"Hedef 1: `{fp(s.tp1)}`\n"
            f"Hedef 2: `{fp(s.tp2)}`\n"
            f"R/Ödül : `{s.rr:.1f}R`\n\n"
            f"RSI 1h: `{s.rsi_1h:.0f}` | Funding: `{s.funding_pct:.3f}%`")

def msg_exit(pos,price):
    pnl=(price-pos.entry)/pos.entry*100*(1 if pos.side=="LONG"else-1)
    L={"TP1":"✅ HEDEF 1","TP2":"✅✅ HEDEF 2","SL":"❌ STOP","TIMEOUT":"⏱️ SÜRE"}
    icon="🟢"if pos.side=="LONG"else"🔴"
    return (f"{icon} {L.get(pos.status,pos.status)} | *{pos.sym}*\n"
            f"Giriş:`{fp(pos.entry)}` → Çıkış:`{fp(price)}`\n"
            f"Sonuç: `{pnl:+.2f}%` | Not: `{pos.grade}` `{pos.score}`\n"
            f"SL:`{fp(pos.sl)}` TP1:`{fp(pos.tp1)}` TP2:`{fp(pos.tp2)}`")

def msg_summary(sigs):
    if not sigs: return "🔍 Uygun setup bulunamadı."
    lines=[f"📊 *UZMAN TRADER SİNYALLERİ* | {utc().strftime('%H:%M UTC')}\n"]
    for s in sigs:
        icon="🟢"if s.side=="LONG"else"🔴"
        lines.append(f"{icon} *{s.sym}* `{s.grade}` `{s.score}/100`\n"
                     f"  G:`{fp(s.entry)}` SL:`{fp(s.sl)}` TP:`{fp(s.tp1)}` R:{s.rr:.1f}R")
    return "\n".join(lines)

def monitor(state):
    ops=[Position(**p) for p in state.get("positions",[]) if p.get("status")=="OPEN"]
    cl =[p for p in state.get("positions",[]) if p.get("status")!="OPEN"]
    still=[]
    for pos in ops:
        try:
            price=lpx(pos.sym)
            pos.highest=max(pos.highest,price)
            pos.lowest =min(pos.lowest, price)
            age=(utc()-datetime.fromisoformat(pos.opened)).total_seconds()/3600
            if age>48: pos.status="TIMEOUT"
            elif pos.side=="LONG":
                if price>=pos.tp2: pos.status="TP2"
                elif price>=pos.tp1: pos.status="TP1"
                elif price<=pos.sl: pos.status="SL"
            else:
                if price<=pos.tp2: pos.status="TP2"
                elif price<=pos.tp1: pos.status="TP1"
                elif price>=pos.sl: pos.status="SL"
            if pos.status!="OPEN":
                tg(msg_exit(pos,price)); cl.append(asdict(pos))
                print(f"  [{pos.status}] {pos.sym} {pos.side} {fp(price)}")
            else:
                still.append(asdict(pos))
                print(f"  [OPEN] {pos.sym} {pos.side} f={fp(price)} SL={fp(pos.sl)} TP1={fp(pos.tp1)}")
        except Exception as e:
            print(f"  [Err]{pos.sym}:{e}"); still.append(asdict(pos))
        time.sleep(0.1)
    state["positions"]=still
    state["closed"]=(cl+state.get("closed",[]))[:500]
    return state

def get_universe():
    try:
        info=get(f"{BASE}/fapi/v1/exchangeInfo")
        active={r["symbol"] for r in info.get("symbols",[])
                if r.get("status")=="TRADING" and r.get("contractType")=="PERPETUAL"
                and r.get("quoteAsset")=="USDT" and r.get("symbol","")[:-4].isalnum()
                and r.get("symbol","")[:-4] not in STABLE}
        tickers=get(f"{BASE}/fapi/v1/ticker/24hr")
        out=[]
        for t in tickers:
            sym=t.get("symbol","")
            if sym not in active: continue
            try: qv=float(t.get("quoteVolume",0)); pct=float(t.get("priceChangePercent",0))
            except: continue
            if MIN_V<=qv<=MAX_V: out.append((sym,pct,qv))
        out.sort(key=lambda x:x[2],reverse=True)
        return out
    except Exception as e:
        print(f"[Universe]{e}"); return []

def scan(state):
    print(f"\n{'='*55}")
    print(f"🎯 UZMAN TRADER — {utc().strftime('%H:%M:%S UTC')}")
    print(f"{'='*55}")
    universe=get_universe()
    print(f"  {len(universe)} coin analiz ediliyor...\n")
    open_syms={p["sym"] for p in state.get("positions",[]) if p.get("status")=="OPEN"}
    signals=[]
    for i,(sym,pct24,_) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym}",end="\r")
        if sym in open_syms: continue
        try:
            sig=analyze(sym,pct24)
            if sig: signals.append(sig)
        except: pass
        time.sleep(0.08)
    signals.sort(key=lambda s:s.score,reverse=True)
    print(f"\n\n  ✅ {len(signals)} sinyal üretildi.\n")

    # Özet
    if signals:
        tg(msg_summary(signals[:10])); time.sleep(0.5)
    else:
        tg(f"🔍 *{utc().strftime('%H:%M UTC')}* — Tarama tamamlandı. Uygun setup yok.")

    # Pozisyon aç (A+ ve A sinyaller önce)
    oc=len(open_syms)
    for sig in signals:
        if oc>=MAX_P: break
        if sig.grade not in ("A+","A"): continue  # Sadece A+ ve A açıyoruz
        if sig.rr<1.5: continue
        pos=Position(sym=sig.sym,side=sig.side,grade=sig.grade,score=sig.score,
                     entry=sig.entry,sl=sig.sl,tp1=sig.tp1,tp2=sig.tp2,
                     opened=utc().isoformat(),highest=sig.entry,lowest=sig.entry,
                     rr=sig.rr,status="OPEN")
        state.setdefault("positions",[]).append(asdict(pos))
        open_syms.add(sig.sym); oc+=1
        tg(msg_signal(sig))
        print(f"  [AÇILDI] {sig.sym} {sig.side} {sig.grade} G={fp(sig.entry)} SL={fp(sig.sl)}")
        time.sleep(0.5)

    state["last_scan"]=utc().isoformat()
    return state

def main():
    print("🎯 UZMAN TRADER BOTU")
    print(f"   Analiz: 4h trend → 1h setup → 15m giriş")
    print(f"   Min Not: {MIN_G} | MaxPos: {MAX_P} | Cooldown: {COOL}dk")
    print(f"   Sadece A+ ve A sinyallerde pozisyon açılır")
    print(f"   TG: {'✅' if TK else '❌'}\n")
    while True:
        state=load_st()
        try:
            if state.get("positions"):
                state=monitor(state); save_st(state)
            last=state.get("last_scan")
            do=(not last or (utc()-datetime.fromisoformat(last)).total_seconds()>=COOL*60)
            if do:
                state=scan(state); save_st(state)
            else:
                w=int(COOL*60-(utc()-datetime.fromisoformat(last)).total_seconds())
                print(f"[Cooldown] {w//60}dk {w%60}sn kaldı.")
        except Exception as e: print(f"[Hata]{e}")
        time.sleep(60)

if __name__=="__main__":
    main()
