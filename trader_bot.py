"""
trader_bot.py — Ultra-Early Pump Sniper (1-Minute Engine)
Balinaların mal toplamaya başladığı İLK DAKİKAYI yakalar.
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
MAX_HOLD_MIN     = 45         # Pump biterse erken çık
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "20")) # 20 saniyede bir ultra hızlı tarama
MIN_VOL_USD      = float(os.getenv("MIN_QUOTE_VOLUME_USD", "15000000"))   # $15M+ 
MAX_VOL_USD      = float(os.getenv("MAX_QUOTE_VOLUME_USD", "10000000000"))

# ULTRA ERKEN PUMP EŞİKLERİ (1 Dakikalık Mumlar İçin)
MIN_PRICE_SURGE  = 0.8   # Sadece %0.8 artsa bile harekete geç (Çok erken)
MIN_VOL_MULTIPLIER = 5.0 # Ancak hacim normalin en az 5 katı olmalı (Kesin teyit)
MIN_OI_SURGE     = 0.5   # OI %0.5 artsa yeter (Para girmeye yeni başlıyor)
HARD_SL_PCT      = 0.02  # %2 Hard Stop
TS_ACTIVATION    = 1.015 # %1.5 kârı geçince İzleyen Stop (Trailing) devreye girer
TS_DROP_PCT      = 0.008 # Zirveden %0.8 düşerse kârı al ve çık

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

def klines(sym, tf, n=50):
    raw = get_json(f"{BASE}/fapi/v1/klines", {"symbol":sym,"interval":tf,"limit":n})
    df  = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def last_price(sym):
    return float(get_json(f"{BASE}/fapi/v1/ticker/price", {"symbol":sym})["price"])

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
        return []

# ── ERKEN PUMP ANALİZİ (1M) ──────────────────────────────────────────────────

def analyze_pump(sym):
    try:
        # Piyasayı 1 dakikalık mumlarla izliyoruz (Gecikmeyi sıfırlamak için)
        df = klines(sym, "1m", 30)
        if len(df) < 25: return None
        
        # Sadece son 2 dakikaya bakıyoruz (Şu anki mum + Bir önceki)
        c_current = df.iloc[-1]['c']
        o_prev    = df.iloc[-2]['o']
        
        # Fiyat sadece ufak bir kıpırdanma yapsa bile (%0.8) yakalayacağız
        price_change_pct = (c_current - o_prev) / o_prev * 100
        if price_change_pct < MIN_PRICE_SURGE: return None
        
        # Ancak bu ufak hareketi DOĞRULAYAN şey DEVASA hacimdir
        vol_ma = df['v'].iloc[-22:-2].mean() # Son 20 dakikanın normal hacmi
        vol_recent = df.iloc[-1]['v'] + df.iloc[-2]['v']
        
        if vol_ma <= 0: return None
        vol_ratio = vol_recent / vol_ma
        if vol_ratio < MIN_VOL_MULTIPLIER: return None # Eğer hacim normalin 5 katı değilse sahtedir
        
        # Açık Pozisyonda ufak kıpırdanma bile yeterli
        oi_data = get_json(f"{BASE}/futures/data/openInterestHist", {"symbol": sym, "period": "5m", "limit": 2})
        oi_surge = 0
        if oi_data and len(oi_data) >= 2:
            oi_now = float(oi_data[-1]["sumOpenInterest"])
            oi_old = float(oi_data[-2]["sumOpenInterest"])
            if oi_old > 0:
                oi_surge = (oi_now - oi_old) / oi_old * 100
                
        if oi_surge < MIN_OI_SURGE: return None
        
        score = price_change_pct * vol_ratio * oi_surge
        entry = last_price(sym)
        sl    = entry * (1 - HARD_SL_PCT)
        
        reasons = [
            f"⚡ *Erken Uyarı Fırlaması:* `+{price_change_pct:.2f}%` (Son 2 dk)",
            f"🌊 *Anormal Hacim:* `{vol_ratio:.1f}x` Katı (Balina alımı başladı)",
            f"🐳 *Yeni Para Girişi:* `+{oi_surge:.2f}%`"
        ]
        
        return {
            "sym": sym, "side": "LONG", "entry": entry,
            "sl": round(sl, 5), "score": round(score, 1), 
            "reasons": reasons,
            "highest_price": entry, 
            "ts_activation": entry * TS_ACTIVATION,
            "ts_pct": TS_DROP_PCT
        }
    except Exception as e:
        return None

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(pos, tid):
    lines = "\n".join(f"  • {r}" for r in pos.get("reasons", []))
    return (
        f"🚨 *ULTRA ERKEN PUMP (İLK MUM) YAKALANDI!* | `{pos['sym']}`\n\n"
        f"Yön: *LONG*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n\n"
        f"Stop Loss    : `{fp(pos['sl'])}` (-%{HARD_SL_PCT*100:.1f})\n"
        f"İzleyen Stop : `%{(TS_ACTIVATION-1)*100:.1f} kârı geçince başlar`\n\n"
        f"*Neden Girdik?*\n{lines}\n\n"
        f"Zaman: `{ts()}` | ID: `{tid}`"
    )

def msg_close(pos, price, reason, dur_sec, tid, highest):
    pct  = (price - pos["entry"]) / pos["entry"]
    pnl  = POSITION_USD * pct
    icon = "🟢" if pnl >= 0 else "🔴"
    
    labels = {
        "TRAILING_STOP": "💸 KÂR ALINDI (Trailing Stop)",
        "SL": "❌ STOP OLDU", "TIMEOUT": "⏱️ SÜRE DOLDU", "BE": "🔰 BREAKEVEN"
    }
    
    max_profit_pct = (highest - pos["entry"]) / pos["entry"] * 100
    
    return (
        f"{icon} *{labels.get(reason, reason)}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(price)}`\n"
        f"Sonuç : `{pct*100:+.2f}%` | P&L: `${pnl:+.2f}`\n"
        f"Görülen Max Kâr: `%{max_profit_pct:.2f}`\n"
        f"Süre  : `{dur_sec//60} dakika`\n"
        f"ID: `{tid}`"
    )

def msg_stats(stats):
    if stats["total"] == 0: return "📊 Henüz trade yok."
    wr = stats["wins"] / stats["total"] * 100
    return (
        f"📊 *1M PUMP SNIPER İSTATİSTİKLERİ*\n\n"
        f"Toplam  : `{stats['total']}`\n"
        f"Kazanan : `{stats['wins']}` (%{wr:.1f})\n"
        f"P&L     : `${stats['total_pnl']:+.2f}`\n"
        f"Beklenti: `${stats['expectancy']:+.4f}` / trade\n\n"
        f"En İyi Coin : `{stats.get('best_pair','—')}`"
    )

# ── DB & STATE ────────────────────────────────────────────────────────────────

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
    pct = (price - pos["entry"]) / pos["entry"]
    pnl = POSITION_USD * pct
    trades.append({
        "id": pos.get("trade_id",""), "pair": pos["sym"], "side": "LONG",
        "entry": pos["entry"], "exit": price, "result": reason,
        "pnl": round(pnl, 4), "score": pos.get("score", 0),
        "duration": dur_sec, "timestamp": ts()
    })
    save_db(trades)
    return trades

def calc_stats(trades):
    if not trades: return {"total":0,"wins":0,"losses":0,"total_pnl":0,"expectancy":0,"best_pair":"—"}
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
    return {
        "total": total, "wins": w, "losses": l, "total_pnl": round(tp, 4),
        "expectancy": round(exp, 4),
        "best_pair": max(pp, key=pp.get) if pp else "—",
    }

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
        except:
            still.append(pos); continue

        entry = pos["entry"]
        dur = int((utc() - datetime.fromisoformat(pos.get("opened_iso", utc().isoformat()))).total_seconds())

        highest = pos.get("highest_price", entry)
        if price > highest:
            pos["highest_price"] = price
            highest = price

        ts_activation = pos.get("ts_activation", entry * TS_ACTIVATION)
        ts_pct        = pos.get("ts_pct", TS_DROP_PCT)

        # Breakeven
        if not pos.get("be_hit") and price >= entry * 1.01:
            pos["sl"] = entry * 1.002
            pos["be_hit"] = True
            tg(f"🔰 *{pos['sym']}* %1 kârı geçti, Stop maliyete çekildi!")

        reason = None
        if dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
        elif price <= pos["sl"]:
            reason = "BE" if pos.get("be_hit") else "SL"
        elif highest >= ts_activation:
            trailing_stop_price = highest * (1 - ts_pct)
            if price <= trailing_stop_price:
                reason = "TRAILING_STOP"

        if reason:
            tid    = pos.get("trade_id", "—")
            trades = record_trade(pos, price, reason, dur)
            tg(msg_close(pos, price, reason, dur, tid, highest))
            pct = (price - entry) / entry
            pnl = POSITION_USD * pct
            print(f"  [{reason}] {pos['sym']} @ {fp(price)} | P&L: ${pnl:+.2f}")
            if len(trades) % 5 == 0: tg(msg_stats(calc_stats(trades)))
        else:
            pct = (price - entry) / entry
            print(f"  [AÇIK] {pos['sym']} | {fp(price)} ({pct*100:+.2f}%) "
                  f"Max:{fp(highest)} SL:{fp(pos['sl'])}")
            still.append(pos)
            
        time.sleep(0.1)

    state["positions"] = still
    return state

# ── TARAMA ───────────────────────────────────────────────────────────────────

def scan(state, universe):
    open_syms = {p["sym"] for p in state.get("positions", [])}
    print(f"\n{'='*65}")
    print(f"🚀 ULTRA ERKEN PUMP (1M) SNIPER — {utc().strftime('%H:%M:%S UTC')}")
    print(f"   Açık pozisyon: {len(open_syms)} | Taranan: {len(universe)} coin")
    print(f"{'='*65}")

    candidates = []
    for i, (sym, _) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r")
        if sym in open_syms: continue
        try:
            sig = analyze_pump(sym)
            if sig:
                candidates.append(sig)
                print(f"\n  ✅ {sym} İLK MUM YAKALANDI! | Skor:{sig['score']}")
        except: pass
        time.sleep(0.06)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    if candidates:
        print(f"\n  🔥 {len(candidates)} COIN'DE BALİNA GİRİŞİ TESPİT EDİLDİ!\n")
    else:
        print(f"\n  🔍 Şu an piyasa sakin, sinsi para girişi aranıyor...")

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
        print(f"  🚀 İLK MUMDAN GİRİLDİ: {sig['sym']} @ {fp(sig['entry'])} | ID:{tid}")
        time.sleep(0.3)

    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("="*65)
    print("🚀 ULTRA ERKEN (1M) PUMP SNIPER BOTU BAŞLADI")
    print("="*65)
    print("🛠️ İLK MUM YAKALAMA STRATEJİSİ:")
    print(f" - Tarama Aralığı : Sadece {SCAN_EVERY} Saniye!")
    print(f" - Fiyat Kıpırdaması: Son 2 dakikada en az %{MIN_PRICE_SURGE} artış (Çok Erken)")
    print(f" - Hacim Şartı      : Ortalamanın en az {MIN_VOL_MULTIPLIER} katı devasa hacim")
    print(f" - OI Artışı        : %{MIN_OI_SURGE} yeni para girişi teyidi")
    print(f" - Zarar Kes (SL)   : %{HARD_SL_PCT*100}")
    print(f" - İzleyen Stop     : %{(TS_ACTIVATION-1)*100} kârda başlar, %{TS_DROP_PCT*100} düşünce satar")
    print("="*65+"\n")

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
            print(f"[HATA] Ana Döngü: {e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    main()
