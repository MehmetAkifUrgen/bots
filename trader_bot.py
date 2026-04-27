"""
trader_bot.py — Liquidity Trap & Exhaustion Index (LTEI) Botu
"Sistemin Bug'ı": Balina tuzağı, stop patlatma ve likidasyon iğnelerini yakalar.
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
MAX_HOLD_MIN     = 120        # 2 saat timeout (bu strateji hızlı sonuç verir)
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "60")) # Sık tarama önemli
MIN_VOL_USD      = float(os.getenv("MIN_QUOTE_VOLUME_USD", "30000000"))   # $30M+ 
MAX_VOL_USD      = float(os.getenv("MAX_QUOTE_VOLUME_USD", "10000000000"))

# LTEI STRATEJİ EŞİKLERİ
WICK_MULTIPLIER = 2.5    # İğne, mum gövdesinden en az 2.5 kat büyük olmalı
MIN_VOL_SPIKE   = 1.8    # Hacim, ortalamanın en az 1.8 katı olmalı
MIN_OI_DROP     = -0.8   # OI'de en az %0.8'lik ani düşüş olmalı (Likidasyon kanıtı)
MAX_SL_PCT      = 0.03   # İğne çok uzunsa en fazla %3 risk al

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
        print(f"[Universe]{e}"); return []

# ── LTEI SİNYAL ANALİZİ (SİSTEMİN BUG'I) ─────────────────────────────────────

def analyze_trap(sym):
    try:
        # 5 dakikalık mumları al (iğneleri en net 5m'de yakalarız)
        df = klines(sym, "5m", 30)
        if len(df) < 25: return None
        
        # Son kapanmış mumu kontrol et
        idx = -2
        row = df.iloc[idx]
        o, h, l, c, v = row['o'], row['h'], row['l'], row['c'], row['v']
        
        body = abs(o - c)
        body = body if body > 0 else (o * 0.0001) # Sıfıra bölünme hatasını engelle
        
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        
        # Hacim sıçraması kontrolü
        vol_ma = df['v'].rolling(20).mean().iloc[idx]
        vol_spike = v / vol_ma if vol_ma > 0 else 0
        
        # 1. Aşama: İğne Tespiti
        side = None
        wick_ratio = 0
        
        if lower_wick > (body * WICK_MULTIPLIER) and lower_wick > (upper_wick * 2):
            # Ayı Tuzağı (Bear Trap) -> Fiyat aşağı çakıldı, stopları patlattı, yukarı çekti
            side = "LONG"
            wick_ratio = lower_wick / body
            extreme_price = l # İğnenin ucu (Stop seviyemiz olacak)
            
        elif upper_wick > (body * WICK_MULTIPLIER) and upper_wick > (lower_wick * 2):
            # Boğa Tuzağı (Bull Trap) -> Fiyat yukarı fırladı, FOMO yarattı, geri çakıldı
            side = "SHORT"
            wick_ratio = upper_wick / body
            extreme_price = h # İğnenin ucu (Stop seviyemiz)
            
        if not side: return None
        
        # 2. Aşama: Hacim Teyidi (İğne sırasında devasa hacim olmalı = Balina yutması)
        if vol_spike < MIN_VOL_SPIKE: return None
        
        # 3. Aşama: OI (Açık Pozisyon) Çöküş Teyidi (Likidasyon Kanıtı)
        # Sadece fitil atması yetmez, o fitilde insanların patlamış olması (OI düşüşü) şart.
        try:
            oi_data = get_json(f"{BASE}/futures/data/openInterestHist", {"symbol": sym, "period": "5m", "limit": 3})
            oi_drop = 0
            if oi_data and len(oi_data) >= 2:
                # Kapanmış mumun (sondan bir önceki) OI verisini al
                oi_now = float(oi_data[-2]["sumOpenInterest"]) 
                oi_prev = float(oi_data[-3]["sumOpenInterest"])
                if oi_prev > 0:
                    oi_drop = (oi_now - oi_prev) / oi_prev * 100
        except:
            return None
            
        if oi_drop > MIN_OI_DROP: 
            return None # OI düşmemişse, sadece normal bir mumdur, likidasyon avı değildir.
            
        # Eğer buraya kadar geldiysek, MÜKEMMEL BİR TUZAK YAKALADIK!
        score = wick_ratio * abs(oi_drop) * vol_spike
        
        # ── RİSK VE HEDEF YÖNETİMİ ──
        entry = last_price(sym)
        
        # Stop'u iğnenin ucuna koyuyoruz (Muazzam bir Risk/Reward oranı)
        if side == "LONG":
            sl = extreme_price * 0.999 # İğnenin milimetrik altı
        else:
            sl = extreme_price * 1.001 # İğnenin milimetrik üstü
            
        # Stop çok uzaksa (dev bir iğne ise) max riski sınırla
        sl_pct = abs(entry - sl) / entry
        if sl_pct > MAX_SL_PCT:
            if side == "LONG": sl = entry * (1 - MAX_SL_PCT)
            else:              sl = entry * (1 + MAX_SL_PCT)
            sl_pct = MAX_SL_PCT
            
        # Hedefleri belirle (Risk'in 2 katı ve 4 katı = 2R ve 4R)
        if side == "LONG":
            tp1 = entry * (1 + (sl_pct * 2))
            tp2 = entry * (1 + (sl_pct * 4))
        else:
            tp1 = entry * (1 - (sl_pct * 2))
            tp2 = entry * (1 - (sl_pct * 4))

        trap_name = "🐻 Ayı Tuzağı (Aşağı İğne)" if side == "LONG" else "🐂 Boğa Tuzağı (Yukarı İğne)"
        reasons = [
            f"🎣 *Tuzak Tipi:* {trap_name}",
            f"🪡 *İğne / Gövde:* `{wick_ratio:.1f}x` (Dev fitil)",
            f"🩸 *Likidasyon (OI Düşüşü):* `{oi_drop:.2f}%` (Yakıt bitti)",
            f"⚡ *Hacim Yutulması:* `{vol_spike:.1f}x` (Balina malı aldı)",
            f"🎯 *Tuzak Skoru:* `{score:.1f}`"
        ]
        
        return {
            "sym": sym, "side": side, "entry": entry,
            "sl": round(sl, 5), "tp1": round(tp1, 5), "tp2": round(tp2, 5),
            "score": round(score, 1), "reasons": reasons,
            "wick_ratio": round(wick_ratio, 2), "oi_drop": round(oi_drop, 2),
            "vol_spike": round(vol_spike, 2), "sl_pct": sl_pct
        }
    except Exception as e: 
        return None

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(pos, tid):
    icon    = "🟢" if pos["side"] == "LONG" else "🔴"
    side_tr = "LONG (Balina ile birlikte)" if pos["side"] == "LONG" else "SHORT (Balina ile birlikte)"
    lines   = "\n".join(f"  • {r}" for r in pos.get("reasons", []))
    
    sl_pct  = pos.get("sl_pct", 0.01) * 100
    tp1_pct = sl_pct * 2
    tp2_pct = sl_pct * 4
    
    return (
        f"{icon} *LTEI: LİKİDASYON AVI YAKALANDI!* | `{pos['sym']}`\n\n"
        f"Yön    : *{side_tr}*\n"
        f"Giriş  : `{fp(pos['entry'])}`\n\n"
        f"Stop   : `{fp(pos['sl'])}` (-%{sl_pct:.2f})\n"
        f"Hedef 1: `{fp(pos['tp1'])}` (+%{tp1_pct:.2f} / 2R)\n"
        f"Hedef 2: `{fp(pos['tp2'])}` (+%{tp2_pct:.2f} / 4R)\n\n"
        f"*Göstergeler:*\n{lines}\n\n"
        f"Pozisyon: `${POSITION_USD:.0f}`\n"
        f"Zaman: `{ts()}` | ID: `{tid}`"
    )

def msg_close(pos, price, reason, dur_sec, tid):
    side = pos["side"]
    pct  = (price - pos["entry"]) / pos["entry"] * (1 if side == "LONG" else -1)
    pnl  = POSITION_USD * pct
    icon = "🟢" if pnl >= 0 else "🔴"
    labels = {
        "TP1": "✅ HEDEF 1 (2R)", "TP2": "✅✅ HEDEF 2 (4R)",
        "SL": "❌ STOP", "TIMEOUT": "⏱️ TIMEOUT", "BE": "🔰 BREAKEVEN"
    }
    return (
        f"{icon} *{labels.get(reason, reason)}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(price)}`\n"
        f"Sonuç : `{pct*100:+.2f}%` | P&L: `${pnl:+.2f}`\n"
        f"Süre  : `{dur_sec//60} dakika`\n"
        f"ID: `{tid}`"
    )

def msg_stats(stats):
    if stats["total"] == 0: return "📊 Henüz trade yok."
    wr = stats["wins"] / stats["total"] * 100
    return (
        f"📊 *LTEI BOT İSTATİSTİKLERİ*\n\n"
        f"Toplam  : `{stats['total']}`\n"
        f"Kazanan : `{stats['wins']}` (%{wr:.1f})\n"
        f"P&L     : `${stats['total_pnl']:+.2f}`\n"
        f"Beklenti: `${stats['expectancy']:+.4f}` / trade\n\n"
        f"En İyi Çift : `{stats.get('best_pair','—')}`\n"
        f"LONG WR     : `%{stats.get('long_wr',0):.1f}`\n"
        f"SHORT WR    : `%{stats.get('short_wr',0):.1f}`"
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
    pct = (price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "LONG" else -1)
    pnl = POSITION_USD * pct
    trades.append({
        "id": pos.get("trade_id",""), "pair": pos["sym"], "side": pos["side"],
        "entry": pos["entry"], "exit": price, "result": reason,
        "pnl": round(pnl, 4), "score": pos.get("score", 0),
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

        # Breakeven: TP1'e ulaştığında veya yarısına geldiğinde
        if not pos.get("be_hit") and pos.get("tp1"):
            be = entry + (pos["tp1"] - entry) * 0.6 if side == "LONG" \
                 else entry - (entry - pos["tp1"]) * 0.6
            if (side == "LONG" and price >= be) or (side == "SHORT" and price <= be):
                pos["sl"] = entry; pos["be_hit"] = True
                tg(f"🔰 *{pos['sym']}* BE'ye taşındı (Risksiz İşlem) | `{fp(price)}`")
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
            if len(trades) % 5 == 0:
                tg(msg_stats(calc_stats(trades)))
        else:
            pct = (price - entry) / entry * (1 if side == "LONG" else -1)
            print(f"  [AÇIK] {pos['sym']} {side} | {fp(price)} ({pct*100:+.2f}%) "
                  f"SL:{fp(pos['sl'])} TP1:{fp(pos['tp1'])} {dur//60}dk")
            still.append(pos)
        time.sleep(0.1)

    state["positions"] = still
    return state

# ── TARAMA ───────────────────────────────────────────────────────────────────

def scan(state, universe):
    open_syms = {p["sym"] for p in state.get("positions", [])}
    print(f"\n{'='*65}")
    print(f"🎯 LİTEI (LİKİDASYON AVI) SİSTEMİ — {utc().strftime('%H:%M:%S UTC')}")
    print(f"   Açık pozisyon: {len(open_syms)} | Taranan: {len(universe)} coin")
    print(f"{'='*65}")

    candidates = []
    for i, (sym, _) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r")
        if sym in open_syms: continue
        try:
            sig = analyze_trap(sym)
            if sig:
                candidates.append(sig)
                print(f"\n  ✅ {sym} {sig['side']} TUZAĞI BULUNDU! | Skor:{sig['score']}")
        except: pass
        time.sleep(0.06)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    if candidates:
        print(f"\n  🔥 {len(candidates)} LİKİDASYON TUZAĞI TESPİT EDİLDİ!\n")
    else:
        print(f"\n  🔍 Şu an piyasada balina tuzağı yok, bekliyoruz.")

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
        print(f"  🚀 İŞLEME GİRİLDİ: {sig['sym']} {sig['side']} @ {fp(sig['entry'])} | ID:{tid}")
        time.sleep(0.3)

    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("="*65)
    print("🎯 LİKİDASYON TUZAĞI VE TÜKENMİŞLİK (LTEI) BOTU BAŞLADI")
    print("="*65)
    print("🛠️ SİSTEMİN BUG'I DEVREDE:")
    print(f" - İğne Boyu  : Mumun en az {WICK_MULTIPLIER} katı")
    print(f" - Hacim Şartı: Ortalamanın en az {MIN_VOL_SPIKE} katı")
    print(f" - OI Düşüşü  : Anlık %{abs(MIN_OI_DROP)} stop patlaması")
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
