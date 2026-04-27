"""
trader_bot.py — Funding Rate + RSI Squeeze Botu
Aşırı funding → piyasa sıkışması → ters yön
Funding + RSI + Hacim teyidi
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

# ── PARAMETRELER ──────────────────────────────────────────────────────────────
POSITION_USD     = 300.0
SL_PCT           = 0.02       # %2 stop
TP1_PCT          = 0.04       # %4 hedef 1 (2R)
TP2_PCT          = 0.07       # %7 hedef 2 (3.5R)
MAX_HOLD_MIN     = 240        # 4 saat timeout
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "120"))
MIN_VOL_USD      = float(os.getenv("MIN_QUOTE_VOLUME_USD", "20000000"))   # $20M+
MAX_VOL_USD      = float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000"))

# Funding eşikleri
FUND_LONG_THRESH  = -0.05   # funding bu değerin altındaysa → LONG (short squeeze)
FUND_SHORT_THRESH =  0.10   # funding bu değerin üstündeyse → SHORT (long squeeze)

# RSI eşikleri (1h)
RSI_OVERSOLD  = 38    # LONG için
RSI_OVERBOUGHT= 62    # SHORT için

# Hacim çarpanı (15m)
VOL_MULT = 1.5
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

def klines(sym, tf, n=100):
    raw = get_json(f"{BASE}/fapi/v1/klines", {"symbol":sym,"interval":tf,"limit":n})
    df  = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def last_price(sym):
    return float(get_json(f"{BASE}/fapi/v1/ticker/price", {"symbol":sym})["price"])

# ── İNDİKATÖRLER ─────────────────────────────────────────────────────────────

def calc_rsi(series, period=14):
    d = series.diff()
    g = d.clip(lower=0).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return (100 - 100 / (1 + g / l.replace(0, np.nan))).fillna(50)

def calc_atr(df, period=14):
    tr = pd.concat([
        df["h"] - df["l"],
        (df["h"] - df["c"].shift()).abs(),
        (df["l"] - df["c"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

# ── VERİ FONKSİYONLARI ───────────────────────────────────────────────────────

def get_funding(sym):
    try:
        d = get_json(f"{BASE}/fapi/v1/premiumIndex", {"symbol": sym})
        return float(d.get("lastFundingRate", 0)) * 100
    except: return None

def get_funding_history(sym, limit=8):
    """Son 8 funding periyodunun ortalaması (son ~32 saat)"""
    try:
        data = get_json(f"{BASE}/fapi/v1/fundingRate",
                        {"symbol": sym, "limit": limit})
        rates = [float(d["fundingRate"]) * 100 for d in data]
        return sum(rates) / len(rates) if rates else 0.0
    except: return 0.0

def get_oi_change(sym):
    """OI değişimi son 1 saatte %"""
    try:
        data = get_json(f"{BASE}/futures/data/openInterestHist",
                        {"symbol": sym, "period": "5m", "limit": 12})
        if len(data) < 2: return 0.0
        new = float(data[-1]["sumOpenInterest"])
        old = float(data[0]["sumOpenInterest"])
        return (new - old) / old * 100 if old > 0 else 0.0
    except: return 0.0

def get_long_short_ratio(sym):
    """Global long/short hesap oranı"""
    try:
        data = get_json(f"{BASE}/futures/data/globalLongShortAccountRatio",
                        {"symbol": sym, "period": "5m", "limit": 1})
        if not data: return 1.0
        return float(data[-1]["longShortRatio"])
    except: return 1.0

# ── EVREN ─────────────────────────────────────────────────────────────────────

def get_universe():
    try:
        info    = get_json(f"{BASE}/fapi/v1/exchangeInfo")
        active  = {r["symbol"] for r in info.get("symbols", [])
                   if r.get("status") == "TRADING"
                   and r.get("contractType") == "PERPETUAL"
                   and r.get("quoteAsset") == "USDT"
                   and r.get("symbol","")[:-4] not in STABLE}
        tickers = get_json(f"{BASE}/fapi/v1/ticker/24hr")
        out = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in active: continue
            try: qv = float(t.get("quoteVolume", 0))
            except: continue
            if MIN_VOL_USD <= qv <= MAX_VOL_USD:
                out.append((sym, qv))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        print(f"[Universe]{e}"); return []

# ── SİNYAL ANALİZİ ───────────────────────────────────────────────────────────

def analyze(sym):
    # 1. Funding rate al
    fund = get_funding(sym)
    if fund is None: return None

    # Funding eşiği geçiyor mu?
    if fund > FUND_SHORT_THRESH:
        side = "SHORT"   # Long sıkışması → short aç
    elif fund < FUND_LONG_THRESH:
        side = "LONG"    # Short sıkışması → long aç
    else:
        return None      # Funding normal, geç

    # 2. RSI teyidi (1h)
    try:
        df1h = klines(sym, "1h", 50)
        if len(df1h) < 20: return None
        rsi1h = calc_rsi(df1h["c"]).iloc[-2]
        if side == "LONG"  and rsi1h > RSI_OVERSOLD:   return None  # RSI hâlâ yüksek
        if side == "SHORT" and rsi1h < RSI_OVERBOUGHT:  return None  # RSI hâlâ düşük
    except: return None

    # 3. Hacim teyidi (15m)
    try:
        df15 = klines(sym, "15m", 50)
        if len(df15) < 25: return None
        r15   = df15.iloc[-2]
        vol   = float(r15["v"])
        vm    = float(df15["v"].rolling(20).mean().iloc[-2])
        if math.isnan(vm) or vm <= 0: return None
        if vol < vm * VOL_MULT: return None   # hacim yeterli değil

        atr15 = calc_atr(df15).iloc[-2]
        if atr15 <= 0: return None
        entry = float(r15["c"])
    except: return None

    # 4. Funding geçmişi (daha güçlü sinyal için)
    fund_avg = get_funding_history(sym, limit=8)

    # 5. OI değişimi
    oi_chg = get_oi_change(sym)

    # 6. L/S oranı
    ls_ratio = get_long_short_ratio(sym)

    # Seviyeler
    if side == "LONG":
        sl  = round(entry * (1 - SL_PCT), 8)
        tp1 = round(entry * (1 + TP1_PCT), 8)
        tp2 = round(entry * (1 + TP2_PCT), 8)
    else:
        sl  = round(entry * (1 + SL_PCT), 8)
        tp1 = round(entry * (1 - TP1_PCT), 8)
        tp2 = round(entry * (1 - TP2_PCT), 8)

    # Skor hesapla (sinyal gücü)
    score = 0
    reasons = []

    # Funding ana sinyal
    if side == "SHORT":
        reasons.append(f"💸 Funding: +{fund:.3f}% (long sıkışması)")
        score += 4
        if fund > 0.2:
            reasons.append(f"🔥 Aşırı yüksek funding: +{fund:.3f}%")
            score += 2
    else:
        reasons.append(f"💸 Funding: {fund:.3f}% (short sıkışması)")
        score += 4
        if fund < -0.1:
            reasons.append(f"🔥 Aşırı negatif funding: {fund:.3f}%")
            score += 2

    # RSI
    if side == "SHORT" and rsi1h > 70:
        reasons.append(f"📊 RSI aşırı alım: {rsi1h:.0f}")
        score += 2
    elif side == "LONG" and rsi1h < 30:
        reasons.append(f"📊 RSI aşırı satım: {rsi1h:.0f}")
        score += 2
    else:
        reasons.append(f"📊 RSI teyit: {rsi1h:.0f}")
        score += 1

    # Hacim
    vol_ratio = vol / vm
    reasons.append(f"⚡ Hacim: {vol_ratio:.1f}x ortalama")
    score += 1

    # OI artışı (sıkışmayı destekliyor mu?)
    if oi_chg > 3 and side == "SHORT":
        reasons.append(f"📈 OI +{oi_chg:.1f}% (long pozisyon artıyor)")
        score += 1
    elif oi_chg < -3 and side == "LONG":
        reasons.append(f"📉 OI {oi_chg:.1f}% (short pozisyon azalıyor)")
        score += 1

    # L/S oranı
    if side == "SHORT" and ls_ratio > 1.5:
        reasons.append(f"🐋 L/S oran: {ls_ratio:.2f} (long kalabalık)")
        score += 1
    elif side == "LONG" and ls_ratio < 0.7:
        reasons.append(f"🐋 L/S oran: {ls_ratio:.2f} (short kalabalık)")
        score += 1

    return {
        "sym": sym, "side": side, "entry": entry,
        "sl": sl, "tp1": tp1, "tp2": tp2,
        "score": score, "reasons": reasons,
        "funding": round(fund, 4),
        "funding_avg": round(fund_avg, 4),
        "rsi1h": round(rsi1h, 1),
        "vol_ratio": round(vol_ratio, 2),
        "oi_chg": round(oi_chg, 2),
        "ls_ratio": round(ls_ratio, 2),
        "atr15": atr15
    }

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(pos, tid):
    icon    = "🟢" if pos["side"] == "LONG" else "🔴"
    side_tr = "LONG — SHORT SQUEEZE" if pos["side"] == "LONG" else "SHORT — LONG SQUEEZE"
    lines   = "\n".join(f"  • {r}" for r in pos.get("reasons", []))
    squeeze = "📉 Short Squeeze (kısa pozisyonlar kapanacak)" if pos["side"] == "LONG" \
              else "📈 Long Squeeze (uzun pozisyonlar kapanacak)"
    return (
        f"{icon} *TRADE AÇILDI* | `{pos['sym']}`\n\n"
        f"Strateji: `{squeeze}`\n\n"
        f"Yön    : *{side_tr}*\n"
        f"Giriş  : `{fp(pos['entry'])}`\n\n"
        f"Stop   : `{fp(pos['sl'])}` (-%{SL_PCT*100:.1f})\n"
        f"Hedef 1: `{fp(pos['tp1'])}` (+%{TP1_PCT*100:.1f} / 2R)\n"
        f"Hedef 2: `{fp(pos['tp2'])}` (+%{TP2_PCT*100:.1f} / 3.5R)\n\n"
        f"*Sinyal Gerekçesi (Skor: {pos['score']}):*\n{lines}\n\n"
        f"Pozisyon: `${POSITION_USD:.0f}`\n"
        f"Zaman: `{ts()}` | ID: `{tid}`"
    )

def msg_close(pos, price, reason, dur_sec, tid):
    side = pos["side"]
    pct  = (price - pos["entry"]) / pos["entry"] * (1 if side == "LONG" else -1)
    pnl  = POSITION_USD * pct
    icon = "🟢" if pnl >= 0 else "🔴"
    labels = {
        "TP1": "✅ HEDEF 1", "TP2": "✅✅ HEDEF 2",
        "SL": "❌ STOP", "TIMEOUT": "⏱️ TIMEOUT", "BE": "🔰 BREAKEVEN"
    }
    return (
        f"{icon} *{labels.get(reason, reason)}* | `{pos['sym']}`\n\n"
        f"Funding: `{pos.get('funding', 0):.4f}%` | RSI: `{pos.get('rsi1h', 0):.0f}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(price)}`\n"
        f"Sonuç : `{pct*100:+.2f}%` | P&L: `${pnl:+.2f}`\n"
        f"Süre  : `{dur_sec//60} dakika`\n"
        f"ID: `{tid}`"
    )

def msg_stats(stats):
    if stats["total"] == 0: return "📊 Henüz trade yok."
    wr = stats["wins"] / stats["total"] * 100
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
    with open(DB, "w") as f: json.dump(t, f, indent=2, ensure_ascii=False)

def record_trade(pos, price, reason, dur_sec):
    trades = load_db()
    pct = (price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "LONG" else -1)
    pnl = POSITION_USD * pct
    trades.append({
        "id": pos.get("trade_id",""), "pair": pos["sym"], "side": pos["side"],
        "entry": pos["entry"], "exit": price, "result": reason,
        "pnl": round(pnl, 4), "score": pos.get("score", 0),
        "funding": pos.get("funding", 0), "rsi1h": pos.get("rsi1h", 0),
        "vol_ratio": pos.get("vol_ratio", 0),
        "duration": dur_sec, "hour_utc": utc().hour,
        "date": utc().strftime("%Y-%m-%d"), "timestamp": ts()
    })
    save_db(trades)
    return trades

def calc_stats(trades):
    if not trades:
        return {"total":0,"wins":0,"losses":0,"total_pnl":0,
                "expectancy":0,"best_pair":"—","long_wr":0,"short_wr":0}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = len(trades)
    w, l   = len(wins), len(losses)
    tp     = sum(t["pnl"] for t in trades)
    aw     = sum(t["pnl"] for t in wins) / w   if w else 0
    al     = sum(t["pnl"] for t in losses) / l if l else 0
    exp    = (w/total * aw) + (l/total * al)    if total else 0
    from collections import defaultdict
    pp = defaultdict(float)
    for t in trades: pp[t["pair"]] += t["pnl"]
    longs  = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    return {
        "total": total, "wins": w, "losses": l, "total_pnl": round(tp, 4),
        "expectancy": round(exp, 4),
        "best_pair": max(pp, key=pp.get) if pp else "—",
        "long_wr":  round(len([t for t in longs  if t["pnl"]>0])/len(longs)*100,  1) if longs  else 0,
        "short_wr": round(len([t for t in shorts if t["pnl"]>0])/len(shorts)*100, 1) if shorts else 0,
    }

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except: pass
    return {"positions": []}

def save_st(s):
    with open(SF, "w") as f: json.dump(s, f, indent=2, ensure_ascii=False)

# ── MONİTÖR ──────────────────────────────────────────────────────────────────

def monitor(state):
    still = []
    for pos in state.get("positions", []):
        try:
            price = last_price(pos["sym"])
        except Exception as e:
            print(f"  [Mon]{pos['sym']}: {e}"); still.append(pos); continue

        side  = pos["side"]
        entry = pos["entry"]
        try:
            dur = int((utc() - datetime.fromisoformat(
                pos.get("opened_iso", utc().isoformat()))).total_seconds())
        except: dur = 0

        # Breakeven: TP1'e %50 yaklaşınca
        if not pos.get("be_hit") and pos.get("tp1"):
            be = entry + (pos["tp1"] - entry) * 0.5 if side == "LONG" \
                 else entry - (entry - pos["tp1"]) * 0.5
            if (side == "LONG" and price >= be) or (side == "SHORT" and price <= be):
                pos["sl"] = entry; pos["be_hit"] = True
                tg(f"🔰 *{pos['sym']}* BE'ye taşındı | `{fp(price)}`")
                print(f"  🔰 [{pos['sym']}] BE @ {fp(price)}")

        reason = None
        if dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
        elif side == "LONG":
            if   price <= pos["sl"]:  reason = "BE" if pos.get("be_hit") else "SL"
            elif price >= pos["tp2"]: reason = "TP2"
            elif price >= pos["tp1"]: reason = "TP1"
        else:
            if   price >= pos["sl"]:  reason = "BE" if pos.get("be_hit") else "SL"
            elif price <= pos["tp2"]: reason = "TP2"
            elif price <= pos["tp1"]: reason = "TP1"

        if reason:
            tid    = pos.get("trade_id", "—")
            trades = record_trade(pos, price, reason, dur)
            tg(msg_close(pos, price, reason, dur, tid))
            pct = (price - entry) / entry * (1 if side == "LONG" else -1)
            pnl = POSITION_USD * pct
            print(f"  [{reason}] {pos['sym']} @ {fp(price)} | P&L: ${pnl:+.2f}")
            if len(trades) % 10 == 0:
                tg(msg_stats(calc_stats(trades)))
        else:
            pct = (price - entry) / entry * (1 if side == "LONG" else -1)
            print(f"  [OPEN] {pos['sym']} {side} | {fp(price)} ({pct*100:+.2f}%) "
                  f"SL:{fp(pos['sl'])} TP1:{fp(pos['tp1'])} {dur//60}dk")
            still.append(pos)
        time.sleep(0.1)

    state["positions"] = still
    return state

# ── TARAMA ───────────────────────────────────────────────────────────────────

def scan(state, universe):
    open_syms = {p["sym"] for p in state.get("positions", [])}
    print(f"\n{'='*55}")
    print(f"💸 FUNDING SQUEEZE BOT — {utc().strftime('%H:%M:%S UTC')}")
    print(f"   Açık pozisyon: {len(open_syms)} | {len(universe)} coin")
    print(f"{'='*55}")

    candidates = []
    for i, (sym, _) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym}", end="\r")
        if sym in open_syms: continue
        try:
            sig = analyze(sym)
            if sig:
                candidates.append(sig)
                print(f"\n  ✅ {sym} {sig['side']} | Fund:{sig['funding']:.3f}% | "
                      f"RSI:{sig['rsi1h']} | Skor:{sig['score']}")
        except: pass
        time.sleep(0.06)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  {len(candidates)} sinyal bulundu.\n")

    for sig in candidates:
        if sig["sym"] in {p["sym"] for p in state.get("positions", [])}: continue
        tid = str(uuid.uuid4())[:8].upper()
        pos = {
            **sig,
            "trade_id": tid, "be_hit": False,
            "opened_iso": utc().isoformat(), "opened_ts": ts()
        }
        state.setdefault("positions", []).append(pos)
        tg(msg_open(pos, tid))
        print(f"  🚀 AÇILDI: {sig['sym']} {sig['side']} @ {fp(sig['entry'])} | ID:{tid}")
        time.sleep(0.3)

    if not candidates:
        print(f"  🔍 Aşırı funding tespit edilmedi.")

    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("="*55)
    print("💸 FUNDING RATE SQUEEZE BOTU")
    print(f"   SHORT: Funding > +{FUND_SHORT_THRESH}% + RSI > {RSI_OVERBOUGHT}")
    print(f"   LONG : Funding < {FUND_LONG_THRESH}% + RSI < {RSI_OVERSOLD}")
    print(f"   SL:%{SL_PCT*100:.0f} | TP1:%{TP1_PCT*100:.0f} | TP2:%{TP2_PCT*100:.0f}")
    print(f"   Timeout:{MAX_HOLD_MIN}dk | TG:{'✅' if TK else '❌'}")
    print("="*55+"\n")

    trades = load_db()
    if trades:
        s = calc_stats(trades)
        print(f"  DB: {s['total']} trade | WR:{s['wins']}/{s['total']} | P&L:${s['total_pnl']:+.2f}\n")

    while True:
        try:
            state    = load_st()
            universe = get_universe()

            if state.get("positions"):
                state = monitor(state)
                save_st(state)

            state = scan(state, universe)
            save_st(state)

        except Exception as e:
            print(f"[HATA]{e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    main()
