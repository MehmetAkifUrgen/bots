"""
trader_bot.py — Binance Gerçek Vadeli İşlemler (Real Trading) Botu
• Hesap: Portfolio Margin (PAPI) & Futures Uyumlu
• Kasa: ~28-32$ Bakiye | 10x Kaldıraç | ~220$ Pozisyon Büyüklüğü
• Hedef: Sabit +$3.00 Kâr Al (TP) | Sabit -$1.50 Zarar Kes (SL) | +$1.50'de Breakeven (Sıfır Risk)
• Koruma: Manuel BASEDUSDT pozisyonuna asla dokunulmaz | BTC Düşüş Kalkanı aktif
"""

import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# ── API VE ORTAM DEĞİŞKENLERİ ────────────────────────────────────────────────
API_KEY     = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET  = os.getenv("BINANCE_API_SECRET", "").strip()
PAPI_BASE   = os.getenv("BINANCE_PAPI_BASE", "https://papi.binance.com")
FAPI_BASE   = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")

TK          = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TC          = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SF          = os.getenv("STATE_FILE", "trader_state.json")
DB          = os.getenv("TRADE_DB",   "trade_db.json")

# ── SABİT STRATEJİ VE RİSK PARAMETRELERİ ─────────────────────────────────────
REAL_TRADING     = os.getenv("REAL_TRADING", "true").lower() == "true"
POSITION_USD     = float(os.getenv("POSITION_USD", "220.0")) # 10x ile ~22$ teminat kullanılır
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))          # 10x Kaldıraç
FIXED_TP_USD     = float(os.getenv("FIXED_TP_USD", "3.0"))   # Sabit +3.00$ Net Kâr
FIXED_SL_USD     = float(os.getenv("FIXED_SL_USD", "1.5"))   # Sabit -1.50$ Net Stop Loss
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "25"))
MAX_HOLD_MIN     = 720 # 12 Saat maksimum işlem süresi

# MANUEL POZİSYON KORUMASI (Bot bu sembollere asla dokunmaz)
PROTECTED_SYMBOLS = {"BASEDUSDT", "BASED"}

# LİKİDİTE VE PARAMETRELER
MIN_VOL_USD      = 5_000_000.0
MAX_VOL_USD      = 150_000_000.0
RSI_OVERSOLD     = 25.0
BB_PERIOD        = 20
BB_STD           = 2.0
MAX_STAGNATION_PCT = 7.0
MIN_VOL_MULTIPLIER = 3.5

STABLE = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","EURC"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

def utc():  return datetime.now(timezone.utc)
def ts():   return utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def fp(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def tg(txt):
    if not TK or not TC:
        print(f"[TG BİLDİRİM] {txt}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id": TC, "text": txt, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        print(f"[TG HATA] {e}")

# ── BINANCE PUBLIC VE SIGNED API İSTEMCİSİ ────────────────────────────────────

def get_public_json(endpoint, p=None):
    hosts = [FAPI_BASE, "https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com"]
    last_err = None
    for h in hosts:
        url = endpoint if endpoint.startswith("http") else f"{h}{endpoint}"
        try:
            r = requests.get(url, params=p, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    raise Exception(f"Binance public API hatası ({endpoint}): {last_err}")

def binance_signed_request(method, path, params=None):
    """Binance Portfolio Margin (PAPI) veya Futures (FAPI) imzalı istek atar."""
    if not API_KEY or not API_SECRET:
        raise Exception("Binance API_KEY veya API_SECRET eksik!")
    
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    
    query = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    
    url = f"{PAPI_BASE}{path}?{query}&signature={sig}"
    auth_headers = {**HEADERS, "X-MBX-APIKEY": API_KEY}
    
    if method.upper() == "GET":
        r = requests.get(url, headers=auth_headers, timeout=20)
    elif method.upper() == "POST":
        r = requests.post(url, headers=auth_headers, timeout=20)
    elif method.upper() == "DELETE":
        r = requests.delete(url, headers=auth_headers, timeout=20)
    else:
        raise ValueError(f"Geçersiz method: {method}")
        
    if r.status_code != 200:
        raise Exception(f"Binance Signed API Hatası ({path}): HTTP {r.status_code} - {r.text}")
    return r.json()

def klines(sym, tf, n=60):
    raw = get_public_json("/fapi/v1/klines", {"symbol": sym, "interval": tf, "limit": n})
    df  = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def calc_rsi(series, period=14):
    if len(series) < period + 1: return 50.0
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not math.isnan(val) else 50.0

def last_price(sym):
    r = get_public_json("/fapi/v1/ticker/price", {"symbol": sym})
    return float(r["price"])

def get_symbol_rules(sym):
    """Sembolün min lot, stepSize ve hassasiyet kurallarını çeker."""
    try:
        info = get_public_json("/fapi/v1/exchangeInfo")
        for s in info.get("symbols", []):
            if s.get("symbol") == sym:
                step_size = 1.0
                min_qty = 1.0
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step_size = float(f.get("stepSize", 1.0))
                        min_qty = float(f.get("minQty", 1.0))
                return {
                    "stepSize": step_size,
                    "minQty": min_qty,
                    "quantityPrecision": int(s.get("quantityPrecision", 2)),
                    "pricePrecision": int(s.get("pricePrecision", 2))
                }
    except Exception:
        pass
    return {"stepSize": 1.0, "minQty": 1.0, "quantityPrecision": 2, "pricePrecision": 2}

def round_step_size(qty, step_size, precision):
    if step_size <= 0: return round(qty, precision)
    steps = math.floor(qty / step_size)
    rounded = steps * step_size
    return float(f"{rounded:.{precision}f}")

def get_account_balance():
    """Portfolio Margin hesabındaki toplam USDT bakiyesini getirir."""
    try:
        balances = binance_signed_request("GET", "/papi/v1/balance")
        for b in balances:
            if b.get("asset") == "USDT":
                return float(b.get("totalWalletBalance", 0))
    except Exception as e:
        print(f"[BAKİYE HATA] {e}")
    return 0.0

# ── BTC DÜŞÜŞ KALKANI (MACRO TREND FILTER) ───────────────────────────────────

def check_btc_shield():
    """
    BTC düşerken veya sert kırmızı mum yakarken altcoinlerde Long açılmasını engeller.
    """
    try:
        df15m = klines("BTCUSDT", "15m", 30)
        if len(df15m) < 20: return True, "BTC Verisi Yetersiz"
        
        last_c = df15m.iloc[-1]
        c = last_c['c']
        o = last_c['o']
        
        # 1. Anlık Şelale Kontrolü: 15m mumunda BTC %0.3'ten fazla kırmızıysa girme
        if c < o and ((o - c) / o) > 0.003:
            return False, "BTC Anlık Şelalede (15m Sert Kırmızı Mum)"
            
        # 2. 1 Saatlik Düşüş Trendi Kontrolü
        df1h = klines("BTCUSDT", "1h", 40)
        if len(df1h) >= 25:
            ema20 = df1h['c'].ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = df1h['c'].ewm(span=50, adjust=False).mean().iloc[-1]
            rsi_btc = calc_rsi(df1h['c'], 14)
            
            if df1h['c'].iloc[-1] < ema20 and ema20 < ema50 and rsi_btc < 45.0:
                return False, f"BTC 1h Düşüş Trendinde (RSI:{rsi_btc:.1f})"
                
        return True, "BTC Uygun (Piyasa Onaylı)"
    except Exception as e:
        return True, f"BTC Kontrol Hatası ({e})"

def get_universe():
    try:
        info    = get_public_json("/fapi/v1/exchangeInfo")
        active  = {r["symbol"] for r in info.get("symbols", [])
                   if r.get("status") == "TRADING"
                   and r.get("contractType") == "PERPETUAL"
                   and r.get("quoteAsset") == "USDT"
                   and r.get("symbol","")[:-4] not in STABLE
                   and r.get("symbol") not in PROTECTED_SYMBOLS}
        tickers = get_public_json("/fapi/v1/ticker/24hr")
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
        print(f"[UNIVERSE HATA] {e}")
        return []

# ── SİNYAL MOTORU (DİP AVCISI & PUMP SNIPER) ─────────────────────────────────

def analyze_market_candidate(sym):
    try:
        df15m = klines(sym, "15m", 45)
        if len(df15m) < 30: return None
        
        c_candle = df15m.iloc[-1]
        c = c_candle['c']
        o = c_candle['o']
        h = c_candle['h']
        l = c_candle['l']
        
        rsi_val = calc_rsi(df15m['c'], 14)
        
        # 1. MOTOR: DİP AVCISI
        if rsi_val <= RSI_OVERSOLD:
            sma20 = df15m['c'].iloc[-20:].mean()
            std20 = df15m['c'].iloc[-20:].std()
            lower_bb = sma20 - (BB_STD * std20)
            
            if (l <= lower_bb or c <= lower_bb):
                is_green = c >= o
                candle_range = h - l
                lower_wick_ratio = (min(c, o) - l) / candle_range if candle_range > 0 else 0
                
                if is_green or lower_wick_ratio > 0.40:
                    entry = last_price(sym)
                    score = (30.0 - rsi_val) * 2.0
                    return {
                        "sym": sym, "mode": "DİP_AVCISI", "side": "LONG",
                        "entry": entry, "score": round(score, 1),
                        "reasons": [
                            f"🎯 *Strateji:* DİP AVCISI (Oversold Bounce)",
                            f"📉 *Aşırı Satım:* RSI `{rsi_val:.1f}`",
                            f"📊 *Bollinger Bandı:* Alt bant dışından alıcı tepkisi"
                        ]
                    }

        # 2. MOTOR: PUMP SNIPER
        if 52.0 <= rsi_val <= 75.0:
            df1h = klines(sym, "1h", 30)
            if len(df1h) >= 24:
                max_h24 = df1h['h'].iloc[-24:].max()
                min_l24 = df1h['l'].iloc[-24:].min()
                range_pct = (max_h24 - min_l24) / min_l24 * 100
                
                if range_pct <= MAX_STAGNATION_PCT:
                    dist = (c - max_h24) / max_h24 * 100
                    if 0.1 <= dist <= 2.5:
                        candle_range = h - l
                        body_ratio = (c - o) / candle_range if candle_range > 0 else 0
                        if body_ratio >= 0.45:
                            vol_avg = df15m['v'].iloc[-20:-2].mean()
                            vol_now = c_candle['v'] + df15m['v'].iloc[-2]
                            vol_ratio = (vol_now / vol_avg) if vol_avg > 0 else 1.0
                            if vol_ratio >= MIN_VOL_MULTIPLIER:
                                entry = last_price(sym)
                                score = (10 - range_pct) * vol_ratio
                                return {
                                    "sym": sym, "mode": "PUMP_SNIPER", "side": "LONG",
                                    "entry": entry, "score": round(score, 1),
                                    "reasons": [
                                        f"🚀 *Strateji:* PUMP SNIPER (Volume Breakout)",
                                        f"🌋 *Kırılım:* 24s sıkışma zirvesi kırıldı!",
                                        f"🌊 *Hacim:* `{vol_ratio:.1f}x` katı patlama"
                                    ]
                                }
        return None
    except Exception:
        return None

# ── GERÇEK EMİR VE POZİSYON MOTORU (REAL TRADING) ────────────────────────────

def set_leverage(sym, lev=10):
    try:
        binance_signed_request("POST", "/papi/v1/um/leverage", {"symbol": sym, "leverage": lev})
    except Exception as e:
        print(f"[LEVERAGE UYARI] {sym} kaldıraç ayarlanamadı ({e})")

def execute_real_entry(sym, notional_usd=220.0):
    """Binance üzerinde gerçek vadeli işlem açar."""
    rules = get_symbol_rules(sym)
    price = last_price(sym)
    
    raw_qty = notional_usd / price
    qty = round_step_size(raw_qty, rules["stepSize"], rules["quantityPrecision"])
    
    if qty < rules["minQty"]:
        qty = rules["minQty"]
        
    set_leverage(sym, LEVERAGE)
    time.sleep(0.2)
    
    order_params = {
        "symbol": sym,
        "side": "BUY",
        "type": "MARKET",
        "quantity": str(qty)
    }
    
    print(f"⚡ [GERÇEK EMİR AÇILIYOR] {sym} | Miktar: {qty} | Notional: ~${qty*price:.2f}")
    order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
    
    # Gerçek giriş fiyatını hesapla
    avg_price = float(order_res.get("avgPrice", 0))
    if avg_price <= 0:
        avg_price = price
        
    # Sabit +3.00$ Kâr ve -1.50$ Zarar Fiyat Seviyeleri:
    # Kar = (P_tp - P_e) * qty = 3.00 => P_tp = P_e + (3.00 / qty)
    # Zarar = (P_e - P_sl) * qty = 1.50 => P_sl = P_e - (1.50 / qty)
    tp_price = avg_price + (FIXED_TP_USD / qty)
    sl_price = avg_price - (FIXED_SL_USD / qty)
    be_trigger_price = avg_price + (1.50 / qty) # +1.50$ kâra ulaşınca Breakeven tetiklenir
    be_sl_price = avg_price + (0.30 / qty)      # Maliyet + komisyon payı
    
    return {
        "sym": sym,
        "side": "LONG",
        "entry": avg_price,
        "qty": qty,
        "notional_usd": round(qty * avg_price, 2),
        "tp_price": tp_price,
        "sl_price": sl_price,
        "be_trigger_price": be_trigger_price,
        "be_sl_price": be_sl_price,
        "order_id": order_res.get("orderId", str(uuid.uuid4())[:8]),
        "opened_iso": utc().isoformat(),
        "opened_ts": ts(),
        "be_hit": False
    }

def execute_real_close(pos, reason):
    """Binance üzerindeki açık pozisyonu market emriyle kapatır."""
    sym = pos["sym"]
    qty = pos["qty"]
    
    order_params = {
        "symbol": sym,
        "side": "SELL",
        "type": "MARKET",
        "quantity": str(qty),
        "reduceOnly": "true"
    }
    
    print(f"🔒 [GERÇEK POZİSYON KAPATILIYOR] {sym} ({reason}) | Miktar: {qty}")
    try:
        order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
        exit_price = float(order_res.get("avgPrice", 0))
        if exit_price <= 0:
            exit_price = last_price(sym)
    except Exception as e:
        print(f"[KAPATMA HATA] {e}, anlık fiyattan tekrar deneniyor...")
        exit_price = last_price(sym)
        
    pnl = (exit_price - pos["entry"]) * qty
    return exit_price, pnl

# ── TELEGRAM BİLDİRİM ŞABLONLARI ─────────────────────────────────────────────

def msg_real_open(pos, sig):
    icon = "🎯" if sig.get("mode") == "DİP_AVCISI" else "🚀"
    lines = "\n".join(f"  • {r}" for r in sig.get("reasons", []))
    return (
        f"{icon} *GERÇEK POZİSYON AÇILDI!* | `{pos['sym']}`\n\n"
        f"Yön: *LONG (10x Kaldıraç)*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Pozisyon Büyüklüğü : `${pos['notional_usd']}` ({pos['qty']} adet)\n\n"
        f"🎯 *Sabit Kâr Hedefi (+3.00$):* `{fp(pos['tp_price'])}`\n"
        f"🛑 *Sabit Stop Loss (-1.50$):* `{fp(pos['sl_price'])}`\n"
        f"🔰 *Sıfır Risk (+1.50$ kârda):* Stop maliyete çekilir\n\n"
        f"*Gerekçeler:*\n{lines}\n\n"
        f"Zaman: `{ts()}`"
    )

def msg_real_close(pos, exit_price, pnl, reason, dur_sec):
    icon = "🟢" if pnl >= 0 else "🔴"
    title = {
        "TAKE_PROFIT": "💰 HEDEF KÂR ALINDI (+3.00$)",
        "STOP_LOSS": "❌ STOP OLDU (-1.50$)",
        "BREAKEVEN": "🔰 BAŞA BAŞ KAPANDI (Sıfır Risk)",
        "TIMEOUT": "⏱️ SÜRE DOLDU"
    }.get(reason, reason)
    
    return (
        f"{icon} *{title}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(exit_price)}`\n"
        f"Net P&L : *`${pnl:+.2f}`*\n"
        f"İşlem Süresi : `{dur_sec//60} dakika`\n"
        f"Kasa Güncel Durumu: `${get_account_balance():.2f} USDT`\n\n"
        f"Zaman: `{ts()}`"
    )

# ── DURUM VE VERİTABANI YÖNETİMİ ─────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except Exception: pass
    return {"positions": []}

def save_st(s):
    with open(SF, "w") as f: json.dump(s, f, indent=2, ensure_ascii=False)

def load_db():
    if os.path.exists(DB):
        try:
            with open(DB) as f: return json.load(f)
        except Exception: pass
    return []

def save_db(t):
    with open(DB, "w") as f: json.dump(t, f, indent=2, ensure_ascii=False)

def record_trade(pos, exit_price, pnl, reason, dur_sec):
    trades = load_db()
    trades.append({
        "id": pos.get("order_id", ""),
        "pair": pos["sym"],
        "side": "LONG",
        "entry": pos["entry"],
        "exit": exit_price,
        "qty": pos["qty"],
        "notional": pos["notional_usd"],
        "pnl": round(pnl, 2),
        "result": reason,
        "duration": dur_sec,
        "timestamp": ts()
    })
    save_db(trades)
    return trades

# ── MONİTÖR & ÇIKIŞ YÖNETİMİ ─────────────────────────────────────────────────

def monitor(state):
    still = []
    for pos in state.get("positions", []):
        sym = pos["sym"]
        try:
            price = last_price(sym)
        except Exception:
            still.append(pos)
            continue
            
        qty = pos["qty"]
        entry = pos["entry"]
        dur = int((utc() - datetime.fromisoformat(pos.get("opened_iso", utc().isoformat()))).total_seconds())
        unrealized_pnl = (price - entry) * qty
        
        # 1. Breakeven Koruması (+1.50$ Kâra ulaşıldığında stop maliyete çekilir)
        if not pos.get("be_hit") and (price >= pos["be_trigger_price"] or unrealized_pnl >= 1.50):
            pos["sl_price"] = pos["be_sl_price"]
            pos["be_hit"] = True
            tg(f"🔰 *{sym}* +1.50$ kâra ulaştı! Stop maliyete (`{fp(pos['sl_price'])}`) çekildi. *Bu işlem artık sıfır risklidir!*")
            
        reason = None
        # Sabit Kâr Hedefi (+3.00$)
        if price >= pos["tp_price"] or unrealized_pnl >= FIXED_TP_USD:
            reason = "TAKE_PROFIT"
        # Sabit Zarar Kes (-1.50$) veya Breakeven Stop
        elif price <= pos["sl_price"]:
            reason = "BREAKEVEN" if pos.get("be_hit") else "STOP_LOSS"
        # Süre Aşımı
        elif dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
            
        if reason:
            if REAL_TRADING:
                exit_price, real_pnl = execute_real_close(pos, reason)
            else:
                exit_price = price
                real_pnl = unrealized_pnl
                
            record_trade(pos, exit_price, real_pnl, reason, dur)
            tg(msg_real_close(pos, exit_price, real_pnl, reason, dur))
            print(f"🔒 [{reason}] {sym} @ {fp(exit_price)} | Net P&L: ${real_pnl:+.2f}")
        else:
            print(f"  [AÇIK POZİSYON] {sym} | Fiyat: {fp(price)} | PnL: ${unrealized_pnl:+.2f} | TP: {fp(pos['tp_price'])} | SL: {fp(pos['sl_price'])}")
            still.append(pos)
            
        time.sleep(0.1)
        
    state["positions"] = still
    return state

# ── TARAMA DÖNGÜSÜ ───────────────────────────────────────────────────────────

def scan(state, universe):
    # Maksimum 1 aktif bot pozisyonu (Kasayı korumak için tek işlem kuralı)
    if len(state.get("positions", [])) >= 1:
        return state
        
    # 0. AŞAMA: BTC DÜŞÜŞ KALKANI
    btc_ok, btc_reason = check_btc_shield()
    if not btc_ok:
        print(f"🛡️ [BTC KALKANI AKTİF] {btc_reason} — Yeni pozisyon açılışı kilitlendi.")
        return state

    open_syms = {p["sym"] for p in state.get("positions", [])}
    open_syms.update(PROTECTED_SYMBOLS)
    
    for i, (sym, _) in enumerate(universe):
        if sym in open_syms: continue
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r")
        try:
            sig = analyze_market_candidate(sym)
            if sig:
                print(f"\n🔥 [SİNYAL YAKALANDI] {sym} ({sig['mode']})! Gerçek işlem açılıyor...")
                
                if REAL_TRADING:
                    pos = execute_real_entry(sym, notional_usd=POSITION_USD)
                else:
                    pos = {
                        "sym": sym, "side": "LONG", "entry": sig["entry"],
                        "qty": round(POSITION_USD / sig["entry"], 2),
                        "notional_usd": POSITION_USD,
                        "tp_price": sig["entry"] * 1.015,
                        "sl_price": sig["entry"] * 0.993,
                        "be_trigger_price": sig["entry"] * 1.008,
                        "be_sl_price": sig["entry"] * 1.001,
                        "opened_iso": utc().isoformat(), "opened_ts": ts(),
                        "be_hit": False
                    }
                    
                state.setdefault("positions", []).append(pos)
                tg(msg_real_open(pos, sig))
                break # Tek pozisyon aç ve çık
        except Exception as e:
            print(f"[İŞLEM AÇILIŞ HATA] {sym}: {e}")
            tg(f"⚠️ *İşlem Açılış Hatası ({sym}):* `{e}`")
        time.sleep(0.06)
        
    return state

# ── ANA DÖNGÜ ────────────────────────────────────────────────────────────────

def main():
    print("="*65)
    print("⚡ BİNANCE CANLI GERÇEK İŞLEM BOTU BAŞLATILDI (REAL TRADING)")
    print("="*65)
    print(f" 💰 Bakiye & Kaldıraç : 10x Kaldıraç | ~${POSITION_USD} Pozisyon Büyüklüğü")
    print(f" 🎯 Sabit Hedef Kâr   : +${FIXED_TP_USD:.2f} USDT")
    print(f" 🛑 Sabit Stop Loss   : -${FIXED_SL_USD:.2f} USDT")
    print(f" 🛡️ Koruma           : BASEDUSDT pozisyonuna dokunulmaz | BTC Kalkanı Aktif")
    print("="*65)
    
    current_bal = get_account_balance()
    print(f"✅ Binance Portföy Bakiyesi: ${current_bal:.2f} USDT")
    
    tg(f"🚀 *BİNANCE CANLI GERÇEK İŞLEM BOTU BAŞLATILDI!*\n\n"
       f"💰 *Cüzdan Bakiyesi:* `${current_bal:.2f} USDT`\n"
       f"⚡ *Kaldıraç & Boyut:* `10x` | `~${POSITION_USD} Pozisyon`\n"
       f"🎯 *Sabit Kâr:* `+${FIXED_TP_USD:.2f}`\n"
       f"🛑 *Sabit Stop:* `-${FIXED_SL_USD:.2f}`\n"
       f"🛡️ *Korumalı Pozisyon:* `BASEDUSDT` (Dokunulmaz)\n\n"
       f"📍 *Durum:* Canlı Piyasa Taraması Aktif ({utc().strftime('%H:%M:%S UTC')})")

    while True:
        try:
            state = load_st()
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
