"""pennant_scanner.py v2 - Yükselen Flama Botu (düzeltilmiş)"""
import json, math, os, time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional
import numpy as np, pandas as pd, requests
from dotenv import load_dotenv
load_dotenv()

BASE_URL      = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")
TG_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE     = int(os.getenv("PENNANT_MIN_SCORE", "60"))
COOLDOWN_MIN  = int(os.getenv("PENNANT_COOLDOWN_MINUTES", "15"))
MIN_VOL       = float(os.getenv("MIN_QUOTE_VOLUME_USD", "5000000"))
MAX_VOL       = float(os.getenv("MAX_QUOTE_VOLUME_USD", "1000000000"))
STATE_FILE    = os.getenv("PENNANT_STATE_FILE", "pennant_state.json")
MAX_POS       = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
TFS           = ("5m", "15m")
STABLE        = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","UST","EURC"}

def _s(v):
    try:
        r = float(v)
        return 0.0 if (math.isnan(r) or math.isinf(r)) else r
    except: return 0.0

def utcnow(): return datetime.now(timezone.utc)
def fmt(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1: return f"{v:.4f}"
    return f"{v:.6f}"

def tg(text):
    if not TG_TOKEN or not TG_CHAT: print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":text,"parse_mode":"Markdown",
                  "disable_web_page_preview":True}, timeout=20).raise_for_status()
    except Exception as e: print(f"[TG] {e}")

def fetch(url, params=None):
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status(); return r.json()

def klines(sym, tf, limit=250):
    raw = fetch(f"{BASE_URL}/fapi/v1/klines", {"symbol":sym,"interval":tf,"limit":limit})
    cols = ["ot","open","high","low","close","volume","ct","qav","trades","tbbav","tbqav","ign"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open","high","low","close","volume"]: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def last_price(sym):
    return float(fetch(f"{BASE_URL}/fapi/v1/ticker/price", {"symbol":sym}).get("price",0))

def indicators(df):
    c = df["close"]
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    df["rsi"] = (100 - 100/(1 + g/l.replace(0, np.nan))).fillna(50)
    df["vol_ma"] = df["volume"].rolling(20).mean()
    return df

def slope(arr):
    y = np.array(arr, dtype=float)
    if len(y) < 3: return 0.0
    s = np.polyfit(np.arange(len(y)), y, 1)[0]
    return s / (abs(y.mean()) or 1.0) * 100

def load_st():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {"positions":[], "last_scan_at":None, "closed_trades":[]}

def save_st(s):
    with open(STATE_FILE,"w") as f: json.dump(s, f, indent=2, ensure_ascii=False)

@dataclass
class Sig:
    symbol:str; tf:str; score:int; why:list; price:float
    fp_gain:float; fp_h:float; cb:int; vdec:float
    upper:float; low:float; dist:float; rsi:float
    vspike:bool; dual:bool=False; pct24:float=0.0

@dataclass
class Pos:
    symbol:str; tf:str; entry:float; sl:float; tp1:float; tp2:float
    fp_gain:float; score:int; opened_at:str; highest:float; status:str="OPEN"

def detect(df, sym, tf, pct24=0.0):
    if len(df) < 60: return None
    df = indicators(df.copy())
    H=df["high"].values.astype(float); L=df["low"].values.astype(float)
    C=df["close"].values.astype(float); V=df["volume"].values.astype(float)
    E20=df["ema20"].values.astype(float); E50=df["ema50"].values.astype(float)
    RSI=df["rsi"].values.astype(float); VM=df["vol_ma"].values.astype(float)
    N = len(df) - 2
    cur = _s(C[N])
    if cur <= 0: return None

    # EMA trend filtresi
    if not (cur > _s(E20[N]) > _s(E50[N]) * 0.995): return None

    # Flagpole bul
    best_g, fp_s, fp_e = 0.0, -1, -1
    for fe in range(N-4, max(N-35,5), -1):
        for fl in range(3, 11):
            fs = fe - fl
            if fs < 2: break
            if _s(C[fe]) <= _s(C[fs]): continue
            lo = float(np.min(L[fs:fe+1])); hi = float(np.max(H[fs:fe+1]))
            if lo <= 0: continue
            g = (hi - lo) / lo * 100
            move = hi - lo
            ok = all(_s(C[i]) >= lo + move*0.1 for i in range(fs, fe+1))
            if g >= 3.0 and g > best_g and ok:
                best_g, fp_s, fp_e = g, fs, fe

    if fp_s < 0 or best_g < 3.0: return None
    fp_height = _s(H[fp_e]) - _s(L[fp_s])

    # Konsolidasyon
    cs, ce = fp_e, N
    cb = ce - cs
    if not (4 <= cb <= 18): return None
    cH=H[cs:ce+1]; cL=L[cs:ce+1]; cV=V[cs:ce+1]; cR=RSI[cs:ce+1]
    if len(cH) < 4: return None

    ls=slope(cL); hs=slope(cH)
    # Strict kama: higher lows + lower highs
    if not (ls > 0.05 and hs < -0.05): return None

    rsi_c = float(np.mean(cR))
    if not (35 <= rsi_c <= 72): return None

    fp_vol = float(np.mean(V[fp_s:fp_e+1])) if fp_e > fp_s else 1.0
    cv_avg = float(np.mean(cV)) if len(cV) > 0 else 1.0
    vdec = (fp_vol - cv_avg) / fp_vol * 100 if fp_vol > 0 else 0.0
    if vdec < 10: return None

    xs = np.arange(len(cH))
    coef = np.polyfit(xs, cH, 1)
    upper = float(np.polyval(coef, len(cH)))
    cons_low = float(np.min(cL))
    dist = (upper - cur) / cur * 100 if cur > 0 else 99.0
    if dist > 2.0: return None

    vspike = _s(V[N]) / _s(VM[N]) > 1.5 if _s(VM[N]) > 0 else False
    
    score=0; why=[]
    if best_g>=10: score+=30; why.append(f"🚀 Güçlü direk +%{best_g:.1f}")
    elif best_g>=6: score+=22; why.append(f"📈 Direk +%{best_g:.1f}")
    else: score+=15; why.append(f"📈 Direk +%{best_g:.1f}")
    score+=25; why.append(f"📐 Kama (↑dip:{ls:+.2f}% ↓tepe:{hs:+.2f}%)")
    if vdec>=50: score+=20; why.append(f"📉 Hacim -%{vdec:.0f}")
    elif vdec>=30: score+=13; why.append(f"📉 Hacim -%{vdec:.0f}")
    else: score+=7; why.append(f"↘️ Hacim -%{vdec:.0f}")
    if 5<=cb<=10: score+=12; why.append(f"⏱️ İdeal kons: {cb} bar")
    elif cb<=15: score+=6; why.append(f"⏱️ Kons: {cb} bar")
    if 45<=rsi_c<=60: score+=8; why.append(f"📊 RSI nötr: {rsi_c:.0f}")
    elif rsi_c<45: score+=5; why.append(f"📊 RSI: {rsi_c:.0f}")
    if vspike and dist<=0: score+=10; why.append(f"⚡ Kırılım hacmi")
    elif 0<=dist<=1.0: score+=5; why.append(f"🎯 Kırılım %{dist:.2f} uzakta")
    elif dist<0: score+=8; why.append(f"✅ Kırıldı +%{abs(dist):.2f}")

    return Sig(symbol=sym,tf=tf,score=min(score,100),why=why,price=cur,
               fp_gain=best_g,fp_h=fp_height,cb=cb,vdec=vdec,
               upper=upper,low=cons_low,dist=dist,rsi=rsi_c,
               vspike=vspike,pct24=pct24)

def lvl(sig):
    e = max(sig.upper * 1.001, sig.price)
    sl = sig.low * 0.997
    risk = e - sl
    if risk <= 0: risk = e * 0.02
    tp1 = e + risk * 1.5
    tp2 = e + sig.fp_h
    rr = (tp1 - e) / risk
    return e, sl, tp1, tp2, rr

def msg_sig(sig):
    e,sl,tp1,tp2,rr = lvl(sig)
    dual = " 🔥*ÇİFT TF*" if sig.dual else ""
    bar = "█"*(sig.score//10)+"░"*(10-sig.score//10)
    w = "\n".join(f"  • {x}" for x in sig.why)
    return (f"🏳️ *FLAMA* | *{sig.symbol}* [{sig.tf}]{dual}\n"
            f"Skor: `{sig.score}/100` `{bar}`\n\n"
            f"*Sinyaller:*\n{w}\n\n"
            f"*📌 Seviyeleri:*\n"
            f"Giriş  : `{fmt(e)}`\n"
            f"Stop   : `{fmt(sl)}`\n"
            f"Hedef 1: `{fmt(tp1)}` _(1.5R)_\n"
            f"Hedef 2: `{fmt(tp2)}` _(Direk boyu)_\n"
            f"R/Ödül : `{rr:.1f}R` | RSI: `{sig.rsi:.0f}`\n"
            f"Direk +%{sig.fp_gain:.1f} | Kons {sig.cb}bar | Hacim↓%{sig.vdec:.0f}")

def msg_sum(sigs):
    if not sigs: return "🔍 Eşik üstü flama yok."
    lines = [f"🏳️ *FLAMA RADAR* | {utcnow().strftime('%H:%M UTC')}\n"]
    for s in sigs[:12]:
        e,sl,tp1,_,rr = lvl(s)
        d = "🔥" if s.dual else "▫️"
        bo = "✅KİRILDI" if s.dist<0 else f"%{s.dist:.1f}🎯"
        lines.append(f"{d}*{s.symbol}*[{s.tf}] `{s.score}/100`\n"
                     f"  G:`{fmt(e)}` SL:`{fmt(sl)}` TP:`{fmt(tp1)}` R:{rr:.1f} {bo}")
    return "\n".join(lines)

def msg_exit(pos, price):
    pnl = (price - pos.entry) / pos.entry * 100
    L = {"TP1":"✅ HEDEF 1 VURDU","TP2":"✅✅ HEDEF 2 VURDU","SL":"❌ STOP OLDU","TIMEOUT":"⏱️ SÜRE DOLDU"}
    return (f"{L.get(pos.status,pos.status)} | *{pos.symbol}* [{pos.tf}]\n"
            f"Giriş:`{fmt(pos.entry)}` → Çıkış:`{fmt(price)}`\n"
            f"Sonuç: `{pnl:+.2f}%`\n"
            f"SL:`{fmt(pos.sl)}` TP1:`{fmt(pos.tp1)}` TP2:`{fmt(pos.tp2)}`")

def monitor(state):
    open_p = [Pos(**p) for p in state.get("positions",[]) if p.get("status")=="OPEN"]
    closed = [p for p in state.get("positions",[]) if p.get("status")!="OPEN"]
    still = []
    for pos in open_p:
        try:
            price = last_price(pos.symbol)
            pos.highest = max(pos.highest, price)
            age = (utcnow()-datetime.fromisoformat(pos.opened_at)).total_seconds()/3600
            if age > 24: pos.status="TIMEOUT"
            elif price >= pos.tp2: pos.status="TP2"
            elif price >= pos.tp1: pos.status="TP1"
            elif price <= pos.sl: pos.status="SL"
            if pos.status != "OPEN":
                tg(msg_exit(pos, price)); closed.append(asdict(pos))
                print(f"  [{pos.status}] {pos.symbol} fiyat={fmt(price)}")
            else:
                still.append(asdict(pos))
                print(f"  [OPEN] {pos.symbol}[{pos.tf}] fiyat={fmt(price)} SL={fmt(pos.sl)} TP1={fmt(pos.tp1)}")
        except Exception as e:
            print(f"  [Err] {pos.symbol}: {e}"); still.append(asdict(pos))
        time.sleep(0.1)
    state["positions"] = still
    state["closed_trades"] = (closed + state.get("closed_trades",[]))[:300]
    return state

def candidates():
    try:
        info = fetch(f"{BASE_URL}/fapi/v1/exchangeInfo")
        active = {r["symbol"] for r in info.get("symbols",[])
                  if r.get("status")=="TRADING" and r.get("contractType")=="PERPETUAL"
                  and r.get("quoteAsset")=="USDT"
                  and r.get("symbol","")[:-4].isalnum()
                  and r.get("symbol","")[:-4] not in STABLE}
        tickers = fetch(f"{BASE_URL}/fapi/v1/ticker/24hr")
        out = []
        for t in tickers:
            sym = t.get("symbol","")
            if sym not in active: continue
            try: qv=float(t.get("quoteVolume",0)); pct=float(t.get("priceChangePercent",0))
            except: continue
            if MIN_VOL <= qv <= MAX_VOL: out.append((sym, pct, qv))
        out.sort(key=lambda x: x[2], reverse=True)
        return out
    except Exception as e:
        print(f"[Candidates] {e}"); return []

def scan(state):
    print(f"\n{'='*50}\n🏳️  TARAMA — {utcnow().strftime('%H:%M:%S UTC')}\n{'='*50}")
    cands = candidates()
    print(f"  {len(cands)} coin taranıyor...")
    open_syms = {p["symbol"] for p in state.get("positions",[]) if p.get("status")=="OPEN"}
    all_sigs = []
    for i,(sym,pct24,_) in enumerate(cands):
        print(f"  [{i+1}/{len(cands)}] {sym}...", end="\r")
        rs = []
        for tf in TFS:
            try:
                sig = detect(klines(sym,tf,250), sym, tf, pct24)
                if sig and sig.score >= MIN_SCORE: rs.append(sig)
            except: pass
            time.sleep(0.05)
        if rs:
            tfs = {s.tf for s in rs}
            if "5m" in tfs and "15m" in tfs:
                for s in rs: s.dual = True
            all_sigs.extend(rs)
    all_sigs.sort(key=lambda s:(s.dual,s.score), reverse=True)
    print(f"\n  ✅ {len(all_sigs)} flama bulundu.\n")
    if all_sigs: tg(msg_sum(all_sigs)); time.sleep(0.5)
    open_cnt = len(open_syms)
    for sig in all_sigs:
        if open_cnt >= MAX_POS: break
        if sig.symbol in open_syms: continue
        if not sig.dual and sig.score < MIN_SCORE+10: continue
        if sig.dist > 1.5: continue
        e,sl,tp1,tp2,rr = lvl(sig)
        if e<=0 or sl>=e or rr<1.2: continue
        pos = Pos(symbol=sig.symbol,tf=sig.tf,entry=e,sl=sl,tp1=tp1,tp2=tp2,
                  fp_gain=sig.fp_gain,score=sig.score,
                  opened_at=utcnow().isoformat(),highest=e,status="OPEN")
        state.setdefault("positions",[]).append(asdict(pos))
        open_syms.add(sig.symbol); open_cnt+=1
        tg(msg_sig(sig))
        print(f"  [AÇILDI] {sig.symbol}[{sig.tf}] G={fmt(e)} SL={fmt(sl)} TP1={fmt(tp1)}")
        time.sleep(0.3)
    state["last_scan_at"] = utcnow().isoformat()
    return state

def main():
    print(f"🏳️ Flama Botu v2 | Skor≥{MIN_SCORE} | Cooldown:{COOLDOWN_MIN}dk | MaxPos:{MAX_POS}")
    print(f"   Kriter: Strict kama + EMA trend + RSI filtresi + hacim azalması")
    print(f"   TG: {'✅' if TG_TOKEN else '❌ (konsola yazdırılır)'}\n")
    while True:
        state = load_st()
        try:
            if state.get("positions"):
                state = monitor(state); save_st(state)
            last = state.get("last_scan_at")
            do_scan = (not last or
                       (utcnow()-datetime.fromisoformat(last)).total_seconds() >= COOLDOWN_MIN*60)
            if do_scan:
                state = scan(state); save_st(state)
            else:
                elapsed = (utcnow()-datetime.fromisoformat(last)).total_seconds()
                wait = int(COOLDOWN_MIN*60 - elapsed)
                print(f"[Cooldown] {wait//60}dk {wait%60}sn kaldı.")
        except Exception as e:
            print(f"[Hata] {e}")
        time.sleep(60)

if __name__ == "__main__":
    main()
