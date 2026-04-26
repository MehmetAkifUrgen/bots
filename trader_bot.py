"""
trader_bot.py — Trend Pullback Botu v2
4h trend → 1h EMA50 pullback → 15m onay
Sıkı filtreler | Min 2:1 RR | Swing SL
"""
import json, math, os, time, uuid
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")
TK   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TC   = os.getenv("TELEGRAM_CHAT_ID", "")
SF   = os.getenv("STATE_FILE", "trader_state.json")
DB   = os.getenv("TRADE_DB",   "trade_db.json")

# ── PARAMETRELER ─────────────────────────────────────────────────────────────
MAX_OPEN     = 3
MAX_CONSEC_L = 2
DAILY_LOSS_L = 6.0
MAX_HOLD_MIN = 120           # 2 saat timeout
POSITION_USD = 300.0
MIN_RR       = 2.0           # minimum risk/reward
SCAN_EVERY   = int(os.getenv("SCAN_EVERY_SECONDS", "120"))
MIN_VOL_USD  = float(os.getenv("MIN_QUOTE_VOLUME_USD", "10000000"))   # $10M+
MAX_VOL_USD  = float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000"))
# ─────────────────────────────────────────────────────────────────────────────

STABLE = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","EURC"}

def utc():  return datetime.now(timezone.utc)
def ts():   return utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def fp(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def tg(txt):
    if not TK or not TC: print(txt); return
    try:
        requests.post(f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id":TC,"text":txt,"parse_mode":"Markdown",
                  "disable_web_page_preview":True}, timeout=20).raise_for_status()
    except Exception as e: print(f"[TG]{e}")

def get_json(url, p=None):
    r = requests.get(url, params=p, timeout=25)
    r.raise_for_status()
    return r.json()

def klines(sym, tf, n=200):
    raw = get_json(f"{BASE}/fapi/v1/klines", {"symbol":sym,"interval":tf,"limit":n})
    df  = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def last_price(sym):
    return float(get_json(f"{BASE}/fapi/v1/ticker/price", {"symbol":sym})["price"])

# ── İNDİKATÖRLER ─────────────────────────────────────────────────────────────

def add_ind(df):
    c = df["c"]
    df["e20"]  = c.ewm(span=20,  adjust=False).mean()
    df["e50"]  = c.ewm(span=50,  adjust=False).mean()
    df["e200"] = c.ewm(span=200, adjust=False).mean()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    df["rsi"]  = (100 - 100/(1 + g/l.replace(0, np.nan))).fillna(50)
    tr = pd.concat([df["h"]-df["l"],
                    (df["h"]-c.shift()).abs(),
                    (df["l"]-c.shift()).abs()], axis=1).max(axis=1)
    df["atr"]  = tr.ewm(alpha=1/14, adjust=False).mean()
    df["vm"]   = df["v"].rolling(20).mean()
    return df

# ── EVREN ─────────────────────────────────────────────────────────────────────

def get_universe():
    try:
        info   = get_json(f"{BASE}/fapi/v1/exchangeInfo")
        active = {r["symbol"] for r in info.get("symbols",[])
                  if r.get("status")=="TRADING" and r.get("contractType")=="PERPETUAL"
                  and r.get("quoteAsset")=="USDT"
                  and r.get("symbol","")[:-4] not in STABLE}
        tickers = get_json(f"{BASE}/fapi/v1/ticker/24hr")
        out = []
        for t in tickers:
            sym = t.get("symbol","")
            if sym not in active: continue
            try: qv = float(t.get("quoteVolume",0))
            except: continue
            if MIN_VOL_USD <= qv <= MAX_VOL_USD:
                out.append((sym, qv))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        print(f"[Universe]{e}"); return []

# ── SİNYAL ANALİZİ ───────────────────────────────────────────────────────────

def analyze(sym):
    try:
        df4  = add_ind(klines(sym, "4h", 100))
        df1  = add_ind(klines(sym, "1h", 100))
        df15 = add_ind(klines(sym, "15m", 100))
    except: return None

    if len(df4) < 60 or len(df1) < 60 or len(df15) < 60: return None

    # ── 4H TREND ──────────────────────────────────────────────────────────────
    r4  = df4.iloc[-2]
    c4  = float(r4["c"])
    e20_4  = float(r4["e20"])
    e50_4  = float(r4["e50"])
    e200_4 = float(r4["e200"])
    rsi4   = float(r4["rsi"])

    bull4 = c4 > e20_4 > e50_4 > e200_4   # tam sıralı bull
    bear4 = c4 < e20_4 < e50_4 < e200_4   # tam sıralı bear

    if not bull4 and not bear4: return None  # trend yok, geç
    side = "LONG" if bull4 else "SHORT"

    # 4h RSI aşırı bölgede değil mi?
    if side == "LONG"  and rsi4 > 75: return None  # overbought
    if side == "SHORT" and rsi4 < 25: return None  # oversold

    # ── 1H PULLBACK ───────────────────────────────────────────────────────────
    r1   = df1.iloc[-2]
    c1   = float(r1["c"])
    e50_1 = float(r1["e50"])
    rsi1  = float(r1["rsi"])
    atr1  = float(r1["atr"])

    if atr1 <= 0: return None

    # Fiyat 1h EMA50'ye yakın mı? (±1.5 ATR içinde)
    dist1 = abs(c1 - e50_1) / atr1
    if dist1 > 1.5: return None  # çok uzak, pullback yok

    # 1h momentum yönü doğru mu?
    if side == "LONG"  and rsi1 > 60: return None  # hala çok yüksek
    if side == "SHORT" and rsi1 < 40: return None  # hala çok düşük

    # ── 15M ONAY ──────────────────────────────────────────────────────────────
    r15  = df15.iloc[-2]
    r15p = df15.iloc[-3]   # bir önceki mum
    c15  = float(r15["c"])
    o15  = float(r15["o"])
    h15  = float(r15["h"])
    l15  = float(r15["l"])
    v15  = float(r15["v"])
    vm15 = float(r15["vm"]) if not math.isnan(float(r15["vm"])) else 1
    atr15 = float(r15["atr"])

    if atr15 <= 0: return None

    # 15m onay mumu: yön doğru + hacim spike
    vol_ok   = v15 > vm15 * 1.5
    if side == "LONG":
        candle_ok = c15 > o15  # yeşil mum
        if not candle_ok or not vol_ok: return None
    else:
        candle_ok = c15 < o15  # kırmızı mum
        if not candle_ok or not vol_ok: return None

    # ── SEVİYELER (Swing SL) ──────────────────────────────────────────────────
    entry = c15

    if side == "LONG":
        # SL: son 10 mumun en düşüğünün altına (swing low)
        swing_low  = float(df15.iloc[-12:-2]["l"].min())
        sl         = swing_low - atr15 * 0.3   # biraz buffer
        if sl >= entry: return None             # geçersiz
        risk       = entry - sl
        tp1        = entry + risk * 2.0         # 2R
        tp2        = entry + risk * 3.0         # 3R
    else:
        swing_high = float(df15.iloc[-12:-2]["h"].max())
        sl         = swing_high + atr15 * 0.3
        if sl <= entry: return None
        risk       = sl - entry
        tp1        = entry - risk * 2.0
        tp2        = entry - risk * 3.0

    if risk <= 0: return None
    rr = (abs(tp1 - entry)) / risk

    if rr < MIN_RR: return None  # RR yeterli değil

    sl_pct = risk / entry * 100

    return {
        "sym": sym, "side": side, "entry": entry,
        "sl": round(sl, 8), "tp1": round(tp1, 8), "tp2": round(tp2, 8),
        "rr": round(rr, 2), "sl_pct": round(sl_pct, 2),
        "risk_usd": round(POSITION_USD * (risk/entry), 2),
        "rsi4": round(rsi4, 1), "rsi1": round(rsi1, 1),
        "dist_ema50": round(dist1, 2),
        "vol_ratio": round(v15/vm15, 2),
        "atr15": atr15
    }

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(sig, tid):
    icon = "🟢" if sig["side"] == "LONG" else "🔴"
    side_tr = "LONG (AL)" if sig["side"] == "LONG" else "SHORT (SAT)"
    return (
        f"{icon} *TRADE AÇILDI* | `{sig['sym']}`\n\n"
        f"Yön    : *{side_tr}*\n"
        f"Giriş  : `{fp(sig['entry'])}`\n\n"
        f"Stop   : `{fp(sig['sl'])}` (-%{sig['sl_pct']:.2f} / -${sig['risk_usd']:.2f})\n"
        f"Hedef 1: `{fp(sig['tp1'])}` (+{sig['rr']:.1f}R)\n"
        f"Hedef 2: `{fp(sig['tp2'])}` (+{sig['rr']*1.5:.1f}R)\n\n"
        f"*Analiz:*\n"
        f"- 4h Trend : `{'BULL' if sig['side']=='LONG' else 'BEAR'}` | RSI: `{sig['rsi4']}`\n"
        f"- 1h EMA50 : `{sig['dist_ema50']}x ATR uzaklık`\n"
        f"- 15m Hacim: `{sig['vol_ratio']}x` ortalama\n"
        f"- 1h RSI   : `{sig['rsi1']}`\n\n"
        f"Strateji: `4h Trend → 1h Pullback → 15m Onay`\n"
        f"Pozisyon: `${POSITION_USD:.0f}`\n\n"
        f"Zaman   : `{ts()}`\n"
        f"ID      : `{tid}`"
    )

def msg_close(pos, price, reason, dur_sec, tid):
    side = pos["side"]
    pct  = (price - pos["entry"]) / pos["entry"] * (1 if side == "LONG" else -1)
    pnl  = POSITION_USD * pct
    risk = abs(pos["entry"] - pos["sl"]) / pos["entry"]
    rmul = pct / risk if risk > 0 else 0
    icon = "🟢" if pnl >= 0 else "🔴"
    labels = {"TP1":"✅ HEDEF 1","TP2":"✅✅ HEDEF 2","SL":"❌ STOP","TIMEOUT":"⏱️ TIMEOUT","BE":"🔰 BREAKEVEN"}
    mins = dur_sec // 60
    return (
        f"{icon} *{labels.get(reason,reason)}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(price)}`\n"
        f"Sonuç : `{pct*100:+.2f}%` | P&L: `${pnl:+.2f}`\n"
        f"R-Çarpan: `{rmul:+.2f}R`\n"
        f"Süre  : `{mins} dakika`\n\n"
        f"ID: `{tid}`"
    )

def msg_stats(stats):
    if stats["total"] == 0: return "📊 Henüz trade yok."
    wr = stats["wins"]/stats["total"]*100
    return (
        f"📊 *BOT İSTATİSTİKLERİ*\n\n"
        f"Toplam  : `{stats['total']}`\n"
        f"Kazanan : `{stats['wins']}` (%{wr:.1f})\n"
        f"P&L     : `${stats['total_pnl']:+.2f}`\n"
        f"Beklenti: `${stats['expectancy']:+.4f}` / trade\n\n"
        f"En İyi Çift : `{stats.get('best_pair','—')}`\n"
        f"LONG WR     : `%{stats.get('long_wr',0):.1f}`\n"
        f"SHORT WR    : `%{stats.get('short_wr',0):.1f}`"
    )

# ── DB ────────────────────────────────────────────────────────────────────────

def load_db():
    if os.path.exists(DB):
        try:
            with open(DB) as f: return json.load(f)
        except: pass
    return []

def save_db(t):
    with open(DB,"w") as f: json.dump(t,f,indent=2,ensure_ascii=False)

def record_trade(pos, price, reason, dur_sec):
    trades = load_db()
    pct = (price-pos["entry"])/pos["entry"]*(1 if pos["side"]=="LONG" else -1)
    pnl = POSITION_USD * pct
    trades.append({
        "id":pos.get("trade_id",""), "pair":pos["sym"], "side":pos["side"],
        "entry":pos["entry"], "exit":price, "result":reason,
        "pnl":round(pnl,4), "rr":pos.get("rr",0),
        "rsi4":pos.get("rsi4",0), "rsi1":pos.get("rsi1",0),
        "vol_ratio":pos.get("vol_ratio",0),
        "duration":dur_sec, "hour_utc":utc().hour,
        "date":utc().strftime("%Y-%m-%d"), "timestamp":ts()
    })
    save_db(trades)
    return trades

def calc_stats(trades):
    if not trades:
        return {"total":0,"wins":0,"losses":0,"total_pnl":0,
                "expectancy":0,"best_pair":"—","long_wr":0,"short_wr":0}
    wins   = [t for t in trades if t["pnl"]>0]
    losses = [t for t in trades if t["pnl"]<=0]
    total  = len(trades)
    w,l    = len(wins),len(losses)
    tp     = sum(t["pnl"] for t in trades)
    aw     = sum(t["pnl"] for t in wins)/w   if w else 0
    al     = sum(t["pnl"] for t in losses)/l if l else 0
    exp    = (w/total*aw)+(l/total*al) if total else 0
    from collections import defaultdict
    pp = defaultdict(float)
    for t in trades: pp[t["pair"]] += t["pnl"]
    longs  = [t for t in trades if t["side"]=="LONG"]
    shorts = [t for t in trades if t["side"]=="SHORT"]
    return {
        "total":total,"wins":w,"losses":l,"total_pnl":round(tp,4),
        "expectancy":round(exp,4),
        "best_pair":max(pp,key=pp.get) if pp else "—",
        "long_wr":round(len([t for t in longs  if t["pnl"]>0])/len(longs)*100,1)  if longs  else 0,
        "short_wr":round(len([t for t in shorts if t["pnl"]>0])/len(shorts)*100,1) if shorts else 0,
    }

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except: pass
    return {"positions":[],"daily":{"date":utc().strftime("%Y-%m-%d"),
            "loss_usd":0.0,"consec_losses":0}}

def save_st(s):
    with open(SF,"w") as f: json.dump(s,f,indent=2,ensure_ascii=False)

def reset_daily(state):
    today = utc().strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date":today,"loss_usd":0.0,"consec_losses":0}
    return state

def can_open(state):
    d = state["daily"]
    oc = len(state.get("positions",[]))
    if oc >= MAX_OPEN:
        print(f"  ⏳ Max pozisyon ({oc}/{MAX_OPEN})")
        return False
    if d["consec_losses"] >= MAX_CONSEC_L:
        print(f"  ⛔ {MAX_CONSEC_L} ardışık kayıp — bugün dur")
        return False
    if d["loss_usd"] >= DAILY_LOSS_L:
        print(f"  ⛔ Günlük kayıp: ${d['loss_usd']:.2f}")
        return False
    return True

# ── MONİTÖR ──────────────────────────────────────────────────────────────────

def monitor(state):
    still = []
    for pos in state.get("positions",[]):
        try:
            price = last_price(pos["sym"])
        except Exception as e:
            print(f"  [Mon]{pos['sym']}: {e}"); still.append(pos); continue

        side  = pos["side"]
        entry = pos["entry"]
        try:
            dur = int((utc()-datetime.fromisoformat(pos.get("opened_iso",utc().isoformat()))).total_seconds())
        except: dur = 0

        # Breakeven: TP1'in yarısına gelince SL'yi entry'e çek
        if not pos.get("be_hit") and pos.get("tp1"):
            be_level = entry + (pos["tp1"]-entry)*0.5 if side=="LONG" else entry - (entry-pos["tp1"])*0.5
            if (side=="LONG" and price >= be_level) or (side=="SHORT" and price <= be_level):
                pos["sl"]     = entry
                pos["be_hit"] = True
                tg(f"🔰 *{pos['sym']}* | BE'ye taşındı | `{fp(price)}`")
                print(f"  🔰 [{pos['sym']}] BE @ {fp(price)}")

        reason = None
        if dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
        elif side == "LONG":
            if   price <= pos["sl"]:  reason = "BE" if pos.get("be_hit") else "SL"
            elif price >= pos.get("tp2", pos["tp1"]): reason = "TP2"
            elif price >= pos["tp1"]: reason = "TP1"
        else:
            if   price >= pos["sl"]:  reason = "BE" if pos.get("be_hit") else "SL"
            elif price <= pos.get("tp2", pos["tp1"]): reason = "TP2"
            elif price <= pos["tp1"]: reason = "TP1"

        if reason:
            tid    = pos.get("trade_id","—")
            trades = record_trade(pos, price, reason, dur)
            tg(msg_close(pos, price, reason, dur, tid))
            pct = (price-entry)/entry*(1 if side=="LONG" else -1)
            pnl = POSITION_USD * pct
            d   = state["daily"]
            if pnl < 0:
                d["loss_usd"]      += abs(pnl)
                d["consec_losses"] += 1
            else:
                d["consec_losses"] = 0
            print(f"  [{reason}] {pos['sym']} @ {fp(price)} | P&L: ${pnl:+.2f}")
            if len(trades) % 10 == 0:
                tg(msg_stats(calc_stats(trades)))
        else:
            pct = (price-entry)/entry*(1 if side=="LONG" else -1)
            print(f"  [OPEN] {pos['sym']} {side} | {fp(price)} ({pct*100:+.2f}%) "
                  f"SL:{fp(pos['sl'])} TP1:{fp(pos['tp1'])} {dur//60}dk")
            still.append(pos)
        time.sleep(0.1)

    state["positions"] = still
    return state

# ── TARAMA ───────────────────────────────────────────────────────────────────

def scan(state):
    slots     = MAX_OPEN - len(state.get("positions",[]))
    open_syms = {p["sym"] for p in state.get("positions",[])}

    print(f"\n{'='*55}")
    print(f"🎯 TREND PULLBACK — {utc().strftime('%H:%M:%S UTC')}")
    d = state["daily"]
    print(f"  Açık:{len(open_syms)}/{MAX_OPEN} | Kayıp:${d['loss_usd']:.2f} | Ardışık:{d['consec_losses']}")
    print(f"{'='*55}")

    if not can_open(state): return state

    universe = get_universe()
    print(f"  {len(universe)} coin taranıyor...\n")

    candidates = []
    for i, (sym, qv) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym}", end="\r")
        if sym in open_syms: continue
        try:
            sig = analyze(sym)
            if sig:
                candidates.append(sig)
                print(f"\n  ✅ {sym} {sig['side']} RR:{sig['rr']} Vol:{sig['vol_ratio']}x")
        except: pass
        time.sleep(0.08)

    candidates.sort(key=lambda x: x["rr"], reverse=True)
    print(f"\n  {len(candidates)} sinyal | En iyi RR sıralaması\n")

    opened = 0
    for sig in candidates:
        if opened >= slots or not can_open(state): break
        if sig["sym"] in {p["sym"] for p in state.get("positions",[])}: continue
        tid = str(uuid.uuid4())[:8].upper()
        state.setdefault("positions",[]).append({
            **sig, "trade_id":tid, "be_hit":False,
            "opened_iso":utc().isoformat(), "opened_ts":ts()
        })
        tg(msg_open(sig, tid))
        print(f"  🚀 [{sig['sym']}] {sig['side']} @ {fp(sig['entry'])} | RR:{sig['rr']} | ID:{tid}")
        opened += 1
        time.sleep(0.3)

    if opened == 0: print(f"  🔍 Uygun setup bulunamadı.")
    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("="*55)
    print("🎯 TREND PULLBACK BOT v2")
    print("   4h Trend → 1h EMA50 Pullback → 15m Onay")
    print(f"  Min RR:{MIN_RR} | Max {MAX_OPEN} pozisyon | Timeout:{MAX_HOLD_MIN}dk")
    print(f"  TG: {'✅' if TK else '❌'}")
    print("="*55+"\n")

    trades = load_db()
    if trades:
        s = calc_stats(trades)
        print(f"  DB: {s['total']} trade | WR:{s['wins']}/{s['total']} | P&L:${s['total_pnl']:+.2f}\n")

    while True:
        try:
            state = load_st()
            state = reset_daily(state)
            if state.get("positions"):
                state = monitor(state)
                save_st(state)
            if can_open(state):
                state = scan(state)
                save_st(state)
            else:
                print(f"  [Bekle] Açık:{len(state.get('positions',[]))}/{MAX_OPEN}")
        except Exception as e:
            print(f"[HATA]{e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    main()
