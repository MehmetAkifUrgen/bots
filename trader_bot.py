"""
trader_bot.py — Dormant Whale (Uyuyan Dev) Breakout Botu
Hacmi çok düşük (5M-20M), uzun süredir dümdüz (sabit) giden coinlerin uyanış (patlama) anını yakalar.
"""
import json, math, os, time, uuid
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

# ── PARAMETRELER ──────────────────────────────────────────────────────────────
POSITION_USD     = 300.0
MAX_HOLD_MIN     = 180        # Patlamanın yürümesi için zaman tanıyoruz (3 saat)
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "30")) 

# KULLANICI TALEBİ: MAX 20M - MIN 5M HACİM
MIN_VOL_USD      = 5000000.0    # 5 Milyon $
MAX_VOL_USD      = 20000000.0   # 20 Milyon $

# UYUYAN DEV EŞİKLERİ
MAX_STAGNATION_PCT = 7.0   # Coin son 24 saatte en fazla %7 dalgalanmış olmalı (Ölü gibi düz çizgi)
MIN_VOL_MULTIPLIER = 3.0   # Sessizliği bozan hacim mumunun ortalamanın 3 katı olması
HARD_SL_PCT        = 0.03  # %3 Stop (Bant altı)
TS_ACTIVATION      = 1.03  # %3 kârı geçince İzleyen Stop devreye girer
TS_DROP_PCT        = 0.015 # Zirveden %1.5 düşerse sat

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

def klines(sym, tf, n=60):
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
            # Tam olarak kullanıcının istediği 5M - 20M Hacim Bandı (Sessiz, sığ coinler)
            if MIN_VOL_USD <= qv <= MAX_VOL_USD:
                out.append((sym, qv))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        return []

# ── UYUYAN DEV / SABİTLİK ANALİZİ ─────────────────────────────────────────────

def analyze_pump(sym):
    try:
        # 1. Aşama: Sabitlik (Akümülasyon) Kontrolü
        # Son 24 saatin (1 saatlik mumlar) grafiğini alıp ne kadar "ölü" olduğuna bakıyoruz.
        df1h = klines(sym, "1h", 24)
        if len(df1h) < 24: return None
        
        max_h24 = df1h['h'].max()
        min_l24 = df1h['l'].min()
        
        # Son 24 saatteki toplam dalgalanma yüzdesi
        range_pct = (max_h24 - min_l24) / min_l24 * 100
        
        # Eğer %7'den fazla hareket etmişse bu coin "sabit kalmış" DEĞİLDİR, hareketlidir. Geçiyoruz.
        if range_pct > MAX_STAGNATION_PCT: return None 
        
        # 2. Aşama: Uyanış (Breakout) Kontrolü
        # Coin ölü gibi düz gidiyordu, peki ŞU AN uyanıyor mu? 15 Dakikalık mumlara bakıyoruz.
        df15m = klines(sym, "15m", 20)
        if len(df15m) < 15: return None
        
        row_current = df15m.iloc[-1]
        c_current   = row_current['c']
        
        # Fiyat, o sessiz 24 saatin TEPE NOKTASINI kırmaya çalışıyor mu?
        # Tepe noktasına uzaklığı (kırılım bölgesi)
        dist_to_breakout = (c_current - max_h24) / max_h24 * 100
        
        # Fiyat, 24 saatlik tepenin %0.5 altı ile %2 üstü aralığındaysa tam kırılım (patlama) anıdır!
        if not (-0.5 <= dist_to_breakout <= 2.5): return None
        
        # Hacim Teyidi: O sessizliği bozan güçlü bir hacim var mı?
        vol_avg = df15m['v'].iloc[-20:-2].mean() # Geçmişin sessiz hacmi
        vol_now = row_current['v'] + df15m.iloc[-2]['v'] # Son yarım saatin uyanış hacmi
        
        if vol_avg <= 0: return None
        vol_ratio = vol_now / vol_avg
        
        if vol_ratio < MIN_VOL_MULTIPLIER: return None # Hacimsiz kırılımlar sahtedir
        
        # Eğer buraya geldiyse: Coin sığ, hacmi 5-20M arası, 24 saattir DÜMDÜZ çizgiydi ve ŞU AN hacimle patlıyor!
        
        score = (10 - range_pct) * vol_ratio # Ne kadar sabitse ve hacim ne kadar çoksa skor o kadar yüksek
        entry = last_price(sym)
        sl    = min_l24 * 0.99 # Stop Loss, 24 saatlik o sıkışma bandının altıdır. Güvenlidir.
        
        # Risk kontrolü, stop çok uzaksa %3 ile sınırla
        if (entry - sl) / entry > HARD_SL_PCT:
            sl = entry * (1 - HARD_SL_PCT)
            
        reasons = [
            f"💤 *Sabitlik (24s):* Sadece `% {range_pct:.1f}` dalgalanmış (Dümdüz Kuluçka)",
            f"🌋 *Kırılım:* 24 saatlik zirveyi patlattı!",
            f"🌊 *Uyanış Hacmi:* Sessizliğin `{vol_ratio:.1f}x` katı",
            f"📊 *Günlük Hacim:* `${get_universe_volume(sym):,.0f}` (Tam Sığ Tahta)"
        ]
        
        return {
            "sym": sym, "side": "LONG", "entry": entry,
            "sl": round(sl, 5), "score": round(score, 1), 
            "reasons": reasons,
            "highest_price": entry, 
            "ts_activation": entry * TS_ACTIVATION,
            "ts_pct": TS_DROP_PCT,
            "vol_mult": float(vol_ratio),
            "stagnation": float(range_pct)
        }
    except Exception as e:
        return None

def get_universe_volume(sym):
    try:
        t = get_json(f"{BASE}/fapi/v1/ticker/24hr", {"symbol": sym})
        return float(t.get("quoteVolume", 0))
    except: return 0

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(pos, tid):
    lines = "\n".join(f"  • {r}" for r in pos.get("reasons", []))
    return (
        f"🚨 *UYUYAN DEV UYANDI! (Sığ Tahta Patlaması)* | `{pos['sym']}`\n\n"
        f"Yön: *LONG*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n\n"
        f"Stop Loss    : `{fp(pos['sl'])}` (Bant Altı)\n"
        f"İzleyen Stop : `%{(TS_ACTIVATION-1)*100:.1f} kârı geçince başlar`\n\n"
        f"*Neden Girdik?*\n{lines}\n\n"
        f"Zaman: `{ts()}` | ID: `{tid}`"
    )

def msg_close(pos, price, reason, dur_sec, tid, highest):
    pct  = (price - pos["entry"]) / pos["entry"]
    pnl  = POSITION_USD * pct
    icon = "🟢" if pnl >= 0 else "🔴"
    
    labels = {
        "TRAILING_STOP": "💸 KÂR ALINDI (İzleyen Stop)",
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
        f"📊 *UYUYAN DEV BOT İSTATİSTİKLERİ*\n\n"
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
        "duration": dur_sec, "timestamp": ts(),
        "vol_mult": pos.get("vol_mult", 0),
        "stagnation": pos.get("stagnation", 0)
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
        if not pos.get("be_hit") and price >= entry * 1.015:
            pos["sl"] = entry * 1.002
            pos["be_hit"] = True
            tg(f"🔰 *{pos['sym']}* %1.5 kârı geçti, Stop maliyete çekildi!")

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
    print(f"🚀 UYUYAN DEV (SABİT COİN) AVCISI — {utc().strftime('%H:%M:%S UTC')}")
    print(f"   Açık pozisyon: {len(open_syms)} | Taranan: {len(universe)} sığ coin")
    print(f"{'='*65}")

    found = 0
    for i, (sym, _) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym} kuluçka kontrolü...", end="\r")
        if sym in open_syms: continue
        try:
            sig = analyze_pump(sym)
            if sig:
                print(f"\n  ✅ {sym} UYANIŞ YAKALANDI! | Skor:{sig['score']}")
                
                # ANINDA İŞLEME GİR VE BİLDİRİM AT (Döngü sonunu bekleme, fiyat kaçmasın!)
                tid = str(uuid.uuid4())[:8].upper()
                
                # İşleme girmeden hemen önce fiyatı milisaniyelik güncelleyelim (Kusursuzluk için)
                real_entry = last_price(sym)
                sig['entry'] = real_entry
                sig['highest_price'] = real_entry
                
                # Stop loss'u güncel fiyata göre tekrar sınırla
                if (real_entry - sig['sl']) / real_entry > HARD_SL_PCT:
                    sig['sl'] = round(real_entry * (1 - HARD_SL_PCT), 5)
                sig['ts_activation'] = real_entry * TS_ACTIVATION
                
                pos = {
                    **sig,
                    "trade_id": tid, "be_hit": False,
                    "opened_iso": utc().isoformat(), "opened_ts": ts()
                }
                state.setdefault("positions", []).append(pos)
                open_syms.add(sym)
                found += 1
                
                tg(msg_open(pos, tid))
                print(f"  🚀 İŞLEME GİRİLDİ: {sym} @ {fp(real_entry)} | ID:{tid}")
                time.sleep(0.3)
        except: pass
        time.sleep(0.06)

    if found > 0:
        print(f"\n  🔥 {found} ADET KULUÇKADAN ÇIKAN COİN İŞLEME ALINDI!\n")
    else:
        print(f"\n  🔍 Şu an sığ tahtalarda yaprak kıpırdamıyor, pusudayız...")

    return state

# ── YAPAY ZEKA LİTE (OTOMATİK ÖĞRENME & OPTİMİZASYON) ─────────────────────────

def optimize_parameters():
    global MAX_STAGNATION_PCT, MIN_VOL_MULTIPLIER
    trades = load_db()
    wins = [t for t in trades if t.get("pnl", 0) > 0 and "vol_mult" in t and "stagnation" in t]
    
    if len(wins) >= 5: # Sadece yeterli kazanan veri varsa öğren
        avg_vol = sum(t["vol_mult"] for t in wins) / len(wins)
        avg_stag = sum(t["stagnation"] for t in wins) / len(wins)
        
        # Sınırları kazananların karakterine göre dinamik ayarla:
        # Kazananlar ortalama ne kadar hacimle patlamışsa, şartı ona yaklaştır (%80'i)
        new_vol_mult = max(3.0, avg_vol * 0.8) 
        # Kazananlar ortalama ne kadar sabitmişse, şartı ona göre daralt
        new_stag = min(7.0, avg_stag * 1.2)    
        
        # Anlamlı bir değişim varsa globale kaydet ve kullanıcıya bildir
        if abs(new_vol_mult - MIN_VOL_MULTIPLIER) > 0.3 or abs(new_stag - MAX_STAGNATION_PCT) > 0.5:
            msg = (
                f"🧠 *BOT ÖĞRENDİ (Makine Öğrenimi Aktif)*\n\n"
                f"Geçmişteki kârlı işlemleri analiz edip hataları ayıkladım. "
                f"Yeni sinyalleri kazananların profiline göre filtreleyeceğim:\n\n"
                f"📈 *Hacim Şartı:* `{MIN_VOL_MULTIPLIER:.1f}x` ➡️ `{new_vol_mult:.1f}x`\n"
                f"📏 *Sabitlik (Düz Çizgi) Şartı:* `% {MAX_STAGNATION_PCT:.1f}` ➡️ `% {new_stag:.1f}`\n\n"
                f"_(Artık sahte kırılımlara değil, sadece bu oranları sağlayan kusursuz işlemlere gireceğim)_"
            )
            tg(msg)
            MIN_VOL_MULTIPLIER = round(new_vol_mult, 1)
            MAX_STAGNATION_PCT = round(new_stag, 1)

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("="*65)
    print("🚀 UYUYAN DEV (DORMANT BREAKOUT) BOTU BAŞLADI")
    print("="*65)
    print("🛠️ AKTİF KURALLAR (Tamamen Senin Stratejin):")
    print(f" 1. Hacim Şartı : Sadece {MIN_VOL_USD/1000000}M - {MAX_VOL_USD/1000000}M $ arası sığ coinler")
    print(f" 2. Sabitlik    : Son 24 saatte en fazla %{MAX_STAGNATION_PCT} oynamış olacak (Ölü çizgi)")
    print(f" 3. Uyanış/Pump : 24 saatlik sessiz zirveyi yüksek hacimle kırdığı an girer")
    print("="*65+"\n")

    trades = load_db()
    if trades:
        s = calc_stats(trades)
        print(f"  DB: {s['total']} trade | WR:{s['wins']}/{s['total']} | P&L:${s['total_pnl']:+.2f}\n")

    optimize_parameters() # Başlangıçta geçmiş veriden öğren

    last_learn_time = time.time()

    while True:
        try:
            if time.time() - last_learn_time > 3600: # Her saat başı tekrar analiz et ve öğren
                optimize_parameters()
                last_learn_time = time.time()
                
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
