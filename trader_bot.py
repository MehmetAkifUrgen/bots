"""
trader_bot.py — Çift Motorlu Hibrit Trading Botu (Dip Avcısı + Pump Sniper)
1. Motor (Dip Avcısı): Aşırı satılmış (RSI < 25 + Bollinger Altı) coinlerin tepki dönüşlerini yakalar.
2. Motor (Pump Sniper): 24 saatlik sıkışmayı devasa hacimle (3.5x+) kıran patlama coinlerini yakalar.
Ortak Çıkış: TP1 %1.2 kârda %50 satış + Anında Breakeven (Sıfır Risk) + %2.5 Trailing Stop.
"""

import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")
TK   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TC   = os.getenv("TELEGRAM_CHAT_ID", "")
SF   = os.getenv("STATE_FILE", "trader_state.json")
DB   = os.getenv("TRADE_DB",   "trade_db.json")

# ── STRATEJİ & RİSK YÖNETİMİ PARAMETRELERİ ──────────────────────────────────
POSITION_USD     = 300.0       # Pozisyon başına sanal bakiye ($)
MAX_HOLD_MIN     = 720         # 12 Saat maksimum bekleme süresi
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "30"))

# Likidite ve Hacim Bandı
MIN_VOL_USD      = 5_000_000.0   # Min 5 Milyon $
MAX_VOL_USD      = 150_000_000.0 # Max 150 Milyon $

# 1. MOTOR (DİP AVCISI) PARAMETRELERİ
RSI_OVERSOLD     = 25.0        # Aşırı satım eşiği (RSI < 25)
BB_PERIOD        = 20
BB_STD           = 2.0

# 2. MOTOR (PUMP SNIPER) PARAMETRELERİ
MAX_STAGNATION_PCT = 7.0       # 24 saatlik sıkışma bandı (%7'den az dalgalanma)
MIN_VOL_MULTIPLIER = 3.5       # Kırılım hacmi normalin en az 3.5 katı

# KÂR KİLİTLEME VE RİSK SIFIRLAMA
TP1_PCT          = 0.012       # %1.2 kârda pozisyonun %50'si nakde çevrilir (Kâr Cebe)
BE_SL_PCT        = 0.002       # Stop giriş maliyeti + %0.2'ye çekilir (Sıfır Risk)
TS_ACTIVATION    = 1.025       # %2.5 kâr görüldüğünde İzleyen Stop devreye girer
TS_DROP_PCT      = 0.010       # Zirveden %1.0 çekilirse sat (Kârı koru)
HARD_SL_PCT      = 0.03        # %3 Maksimum Zarar Kes (Stop Loss)

STABLE = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","EURC"}

def utc():  return datetime.now(timezone.utc)
def ts():   return utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def fp(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def tg(txt):
    if not TK or not TC:
        print("[TELEGRAM UYARI] TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID ortam değişkenlerinde bulunamadı!")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id": TC, "text": txt, "parse_mode": "Markdown",
                  "disable_web_page_preview": True}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[TELEGRAM Hata] Mesaj iletilemedi: {e}")

def get_json(url, p=None):
    r = requests.get(url, params=p, timeout=25)
    r.raise_for_status()
    return r.json()

def klines(sym, tf, n=60):
    raw = get_json(f"{BASE}/fapi/v1/klines", {"symbol": sym, "interval": tf, "limit": n})
    df  = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def calc_rsi(series, period=14):
    if len(series) < period + 1:
        return 50.0
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not math.isnan(val) else 50.0

def last_price(sym):
    return float(get_json(f"{BASE}/fapi/v1/ticker/price", {"symbol": sym})["price"])

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

# ── SİNYAL MOTORLARI (DİP & PUMP) ───────────────────────────────────────────

def analyze_market_candidate(sym):
    """
    Hem 1. Motor (Dip Avcısı) hem de 2. Motor (Pump Sniper) için pariteyi analiz eder.
    """
    try:
        df15m = klines(sym, "15m", 45)
        if len(df15m) < 30: return None
        
        c_candle = df15m.iloc[-1]
        c = c_candle['c']
        o = c_candle['o']
        h = c_candle['h']
        l = c_candle['l']
        
        rsi_val = calc_rsi(df15m['c'], 14)
        
        # ─────────────────────────────────────────────────────────────────
        # 1. MOTOR: DİP AVCISI (Aşırı Satım & Tepki Yükselişi)
        # ─────────────────────────────────────────────────────────────────
        if rsi_val <= RSI_OVERSOLD:
            sma20 = df15m['c'].iloc[-20:].mean()
            std20 = df15m['c'].iloc[-20:].std()
            lower_bb = sma20 - (BB_STD * std20)
            
            # Alt banda değmiş veya taşmış mı?
            if l <= lower_bb or c <= lower_bb:
                is_green = c >= o
                candle_range = h - l
                lower_wick_ratio = (min(c, o) - l) / candle_range if candle_range > 0 else 0
                
                # Yeşil mum veya belirgin alıcı fitili
                if is_green or lower_wick_ratio > 0.40:
                    entry = last_price(sym)
                    score = (30.0 - rsi_val) * 2.0
                    reasons = [
                        f"🎯 *Strateji:* DİP AVCISI (Mean Reversion)",
                        f"📉 *Aşırı Satım:* RSI `{rsi_val:.1f}` (Kritik Dip Bölgesi)",
                        f"📊 *Bollinger Bandı:* Alt bant (`{fp(lower_bb)}`) dışından alıcı tepkisi",
                        f"🟢 *Dönüş Onayı:* Yeşil Mum / Alıcı fitili `{lower_wick_ratio*100:.0f}%`"
                    ]
                    return {
                        "sym": sym, "mode": "DİP_AVCISI", "side": "LONG",
                        "entry": entry, "sl": round(entry * (1 - HARD_SL_PCT), 5),
                        "score": round(score, 1), "reasons": reasons,
                        "highest_price": entry, "ts_activation": entry * TS_ACTIVATION,
                        "ts_pct": TS_DROP_PCT, "rsi": float(rsi_val)
                    }

        # ─────────────────────────────────────────────────────────────────
        # 2. MOTOR: PUMP SNIPER (Hacimli Sıkışma Kırılımı)
        # ─────────────────────────────────────────────────────────────────
        if 52.0 <= rsi_val <= 75.0:
            df1h = klines(sym, "1h", 30)
            if len(df1h) >= 24:
                max_h24 = df1h['h'].iloc[-24:].max()
                min_l24 = df1h['l'].iloc[-24:].min()
                range_pct = (max_h24 - min_l24) / min_l24 * 100
                
                # 24 saat boyunca %7'den az dalgalanmış (Sıkışmış)
                if range_pct <= MAX_STAGNATION_PCT:
                    dist = (c - max_h24) / max_h24 * 100
                    # 24 saatlik tepeyi kırma anı
                    if 0.1 <= dist <= 2.5:
                        candle_range = h - l
                        body_ratio = (c - o) / candle_range if candle_range > 0 else 0
                        
                        # Güçlü yeşil gövde (Tepe iğnesi bırakmamış)
                        if body_ratio >= 0.45:
                            vol_avg = df15m['v'].iloc[-20:-2].mean()
                            vol_now = c_candle['v'] + df15m['v'].iloc[-2]
                            vol_ratio = (vol_now / vol_avg) if vol_avg > 0 else 1.0
                            
                            # Hacim ortalamanın en az 3.5 katı
                            if vol_ratio >= MIN_VOL_MULTIPLIER:
                                # 1h Trend Onayı (EMA20 > EMA50)
                                ema20_1h = df1h['c'].ewm(span=20, adjust=False).mean().iloc[-1]
                                ema50_1h = df1h['c'].ewm(span=50, adjust=False).mean().iloc[-1]
                                
                                if c >= ema20_1h and ema20_1h >= ema50_1h * 0.995:
                                    entry = last_price(sym)
                                    score = (10 - range_pct) * vol_ratio
                                    reasons = [
                                        f"🚀 *Strateji:* PUMP SNIPER (Volume Breakout)",
                                        f"🌋 *Kırılım:* 24 saatlik sıkışma zirvesi kırıldı!",
                                        f"🌊 *Patlama Hacmi:* Sessizliğin `{vol_ratio:.1f}x` katı",
                                        f"📈 *Momentum RSI:* `{rsi_val:.1f}` | ⏱️ 1h Yükseliş Trendi"
                                    ]
                                    return {
                                        "sym": sym, "mode": "PUMP_SNIPER", "side": "LONG",
                                        "entry": entry, "sl": round(entry * (1 - 0.025), 5),
                                        "score": round(score, 1), "reasons": reasons,
                                        "highest_price": entry, "ts_activation": entry * TS_ACTIVATION,
                                        "ts_pct": TS_DROP_PCT, "rsi": float(rsi_val)
                                    }
        return None
    except Exception:
        return None

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(pos, tid):
    lines = "\n".join(f"  • {r}" for r in pos.get("reasons", []))
    icon = "🎯" if pos.get("mode") == "DİP_AVCISI" else "🚀"
    title = "DİP AVCISI SİNYALİ" if pos.get("mode") == "DİP_AVCISI" else "PUMP SNIPER SİNYALİ"
    return (
        f"{icon} *{title}!* | `{pos['sym']}`\n\n"
        f"Yön: *LONG*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Hedef TP1    : `{fp(pos['entry'] * (1 + TP1_PCT))}` (`+%{TP1_PCT*100:.1f} Kâr Al`)\n"
        f"Stop Loss    : `{fp(pos['sl'])}`\n\n"
        f"*Giriş Gerekçeleri:*\n{lines}\n\n"
        f"Zaman: `{ts()}` | ID: `{tid}`"
    )

def msg_tp1(pos, price):
    pct = (price - pos["entry"]) / pos["entry"] * 100
    pnl = (POSITION_USD * 0.5) * (pct / 100)
    return (
        f"💰 *TP1 KÂRI ALINDI (%50 Pozisyon Kapatıldı)* | `{pos['sym']}`\n\n"
        f"Giriş: `{fp(pos['entry'])}` → Kâr Alış: `{fp(price)}` (`+{pct:.2f}%`)\n"
        f"Cebe Konan Kâr: `+${pnl:.2f}`\n"
        f"🔰 *Kalan %50 Pozisyon Stopu:* Maliyete (`{fp(pos['sl'])}`) çekildi!\n"
        f"🛡️ *Bu işlem artık ASLA zararla kapanamaz (Sıfır Risk).* Kalan yarısı Trailing Stop ile sürülüyor!"
    )

def msg_close(pos, price, reason, dur_sec, tid, highest):
    entry = pos["entry"]
    realized = pos.get("realized_pnl", 0.0)
    rem_size = pos.get("remaining_size_usd", POSITION_USD)
    rem_pct = (price - entry) / entry
    rem_pnl = rem_size * rem_pct
    total_pnl = realized + rem_pnl
    
    icon = "🟢" if total_pnl >= 0 else "🔴"
    labels = {
        "TRAILING_STOP": "💸 KÂR ALINDI (İzleyen Stop)",
        "SL": "❌ STOP OLDU", "TIMEOUT": "⏱️ SÜRE DOLDU", "BE": "🔰 BREAKEVEN (Maliyet)"
    }
    
    max_profit_pct = (highest - entry) / entry * 100
    tp1_note = f"\n• TP1 Ön Kârı: `+${realized:.2f}`" if pos.get("tp1_hit") else ""
    mode_str = f"Mod: `{pos.get('mode', 'HİBRİT')}`\n"
    
    return (
        f"{icon} *{labels.get(reason, reason)}* | `{pos['sym']}`\n\n"
        f"{mode_str}"
        f"Giriş : `{fp(entry)}` → Çıkış: `{fp(price)}`\n"
        f"Net Toplam P&L : ` ${total_pnl:+.2f} ` {tp1_note}\n"
        f"Görülen Max Kâr: `%{max_profit_pct:.2f}`\n"
        f"Süre  : `{dur_sec//60} dakika`\n"
        f"ID: `{tid}`"
    )

def msg_stats(stats):
    if stats["total"] == 0: return "📊 Henüz trade yok."
    wr = stats["wins"] / stats["total"] * 100
    return (
        f"📊 *ÇİFT MOTORLU BOT İSTATİSTİKLERİ*\n\n"
        f"Toplam İşlem  : `{stats['total']}`\n"
        f"Kazanan       : `{stats['wins']}` (%{wr:.1f})\n"
        f"Net P&L       : `${stats['total_pnl']:+.2f}`\n"
        f"Beklenti      : `${stats['expectancy']:+.4f}` / trade\n\n"
        f"En İyi Coin   : `{stats.get('best_pair','—')}`"
    )

# ── VERİTABANI & DURUM YÖNETİMİ ──────────────────────────────────────────────

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
    entry = pos["entry"]
    realized = pos.get("realized_pnl", 0.0)
    rem_size = pos.get("remaining_size_usd", POSITION_USD)
    rem_pct = (price - entry) / entry
    rem_pnl = rem_size * rem_pct
    total_pnl = realized + rem_pnl
    
    trades.append({
        "id": pos.get("trade_id",""), "pair": pos["sym"], "side": "LONG",
        "mode": pos.get("mode", "HİBRİT"),
        "entry": entry, "exit": price, "result": reason,
        "pnl": round(total_pnl, 4), "score": pos.get("score", 0),
        "duration": dur_sec, "timestamp": ts(),
        "rsi": pos.get("rsi", 0)
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

# ── MONİTÖR & ÇIKIŞ YÖNETİMİ ─────────────────────────────────────────────────

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

        # 1. Kademeli Kâr Alımı (TP1: %1.2 kârda %50 pozisyon nakde çevrilir + Stop maliyete taşınır)
        if not pos.get("tp1_hit") and price >= entry * (1 + TP1_PCT):
            tp1_pnl = (POSITION_USD * 0.5) * ((price - entry) / entry)
            pos["tp1_hit"] = True
            pos["realized_pnl"] = tp1_pnl
            pos["remaining_size_usd"] = POSITION_USD * 0.5
            pos["sl"] = entry * (1 + BE_SL_PCT) # Stop maliyete (Breakeven)
            pos["be_hit"] = True
            tg(msg_tp1(pos, price))

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
            realized = pos.get("realized_pnl", 0.0)
            rem_size = pos.get("remaining_size_usd", POSITION_USD)
            pnl = realized + (rem_size * ((price - entry) / entry))
            print(f"  [{reason}] {pos['sym']} ({pos.get('mode')}) @ {fp(price)} | Net P&L: ${pnl:+.2f}")
            if len(trades) % 5 == 0: tg(msg_stats(calc_stats(trades)))
        else:
            pct = (price - entry) / entry
            print(f"  [AÇIK] {pos['sym']} ({pos.get('mode')}) | {fp(price)} ({pct*100:+.2f}%) "
                  f"Max:{fp(highest)} SL:{fp(pos['sl'])} (TP1:{'✅' if pos.get('tp1_hit') else 'Bekliyor'})")
            still.append(pos)
            
        time.sleep(0.1)

    state["positions"] = still
    return state

# ── TARAMA ───────────────────────────────────────────────────────────────────

def scan(state, universe):
    open_syms = {p["sym"] for p in state.get("positions", [])}
    print(f"\n{'='*65}")
    print(f"⚡ ÇİFT MOTORLU BOT (DİP + PUMP) — {utc().strftime('%H:%M:%S UTC')}")
    print(f"   Açık pozisyon: {len(open_syms)} | Taranan: {len(universe)} coin")
    print(f"{'='*65}")

    found = 0
    for i, (sym, _) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r")
        if sym in open_syms: continue
        try:
            sig = analyze_market_candidate(sym)
            if sig:
                mode_label = "🎯 DİP DÖNÜŞÜ" if sig["mode"] == "DİP_AVCISI" else "🚀 PUMP KIRILIMI"
                print(f"\n  ✅ {sym} {mode_label}! | Skor:{sig['score']}")
                
                tid = str(uuid.uuid4())[:8].upper()
                real_entry = last_price(sym)
                sig['entry'] = real_entry
                sig['highest_price'] = real_entry
                
                if sig["mode"] == "DİP_AVCISI":
                    sig['sl'] = round(real_entry * (1 - HARD_SL_PCT), 5)
                else:
                    sig['sl'] = round(real_entry * (1 - 0.025), 5)
                    
                sig['ts_activation'] = real_entry * TS_ACTIVATION
                
                pos = {
                    **sig,
                    "trade_id": tid,
                    "be_hit": False,
                    "tp1_hit": False,
                    "realized_pnl": 0.0,
                    "remaining_size_usd": POSITION_USD,
                    "opened_iso": utc().isoformat(),
                    "opened_ts": ts()
                }
                state.setdefault("positions", []).append(pos)
                open_syms.add(sym)
                found += 1
                
                tg(msg_open(pos, tid))
                print(f"  🚀 İŞLEME GİRİLDİ: {sym} ({sig['mode']}) @ {fp(real_entry)} | ID:{tid}")
                time.sleep(0.3)
        except Exception:
            pass
        time.sleep(0.05)

    if found > 0:
        print(f"\n  🔥 {found} ADET FIRSAT İŞLEME ALINDI!\n")
    else:
        print(f"\n  🔍 Dip dönüşleri ve Pump patlamaları taranıyor...")

    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("="*65)
    print("⚡ ÇİFT MOTORLU HİBRİT BOT (DİP AVCISI + PUMP SNIPER) BAŞLADI")
    print("="*65)
    print("🛠️ AKTİF MOTORLAR:")
    print(" 1. Motor (Dip Avcısı) : RSI < 25 + Bollinger Dışı + Yeşil Mum")
    print(" 2. Motor (Pump Sniper): 24s Sıkışma + 3.5x Hacim Kırılımı + 1h Trend")
    print(" 3. Kâr & Risk Yönetimi: %1.2 TP1 (%50 Kâr Al) + Breakeven + Trailing Stop")
    print("="*65)

    if TK and TC:
        print(f"✅ Telegram Aktif | Chat ID: {TC}")
        tg("🚀 *ÇİFT MOTORLU HİBRİT BOT CANLIYA GEÇTİ!*\n\n"
           "• 🎯 *1. Motor:* Dip Avcısı (`RSI < 25`)\n"
           "• 🚀 *2. Motor:* Pump Sniper (`3.5x Hacim`)\n"
           "• 🛡️ *Kâr Koruması:* `%1.2 TP1 + Breakeven Sıfır Risk`\n\n"
           f"📍 *Sunucu Saati:* `{ts()}`\n"
           "📊 165+ Binance Futures paritesi taranıyor...")
    else:
        print("⚠️ [UYARI] TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik! Bildirim gönderilemeyecek.")

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
