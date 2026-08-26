"""
trader_bot.py — Profesyonel Çift Yönlü (LONG & SHORT) & Yapay Zekalı Binance Vadeli Botu
• İşlem Yönü: Hem LONG (Yükseliş) hem SHORT (Düşüş) trendine göre işlem açar
• Kaldıraç: 20x - 25x Dinamik Kaldıraç ($250 Pozisyon için sadece ~$12.50 teminat)
• Hedef Pozisyon: Tam $250.00 USDT
• Net Trailing Stop: +$2.00 kârda devreye girer ve stopu hemen +$1.00 kâra kilitler (Zirveden $1.00 çekilince satar)
• Başa Baş (Breakeven): +$1.00 kârda stop maliyete çekilir (Sıfır Risk)
• Stop Loss: -$1.50 net risk koruması
• Yapay Zeka: Google Gemini 2.5 Flash Sentinel ile %80+ güven teyidi
• Koruma: BASEDUSDT dokunulmaz | BTC Trend Kalkanı | Serbest Teminat Koruması
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

GEMINI_KEY  = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL= os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

TK          = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TC          = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SF          = os.getenv("STATE_FILE", "trader_state.json")
DB          = os.getenv("TRADE_DB",   "trade_db.json")

# ── STRATEJİ VE RİSK PARAMETRELERİ ───────────────────────────────────────────
REAL_TRADING     = os.getenv("REAL_TRADING", "true").lower() == "true"
TARGET_NOTIONAL  = 250.0       # Hedef Pozisyon Büyüklüğü: Tam $250.00 USDT
DEFAULT_LEVERAGE = 20          # 20x Kaldıraç ($250 için sadece $12.50 teminat)
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "20"))
MAX_HOLD_MIN     = 720 # 12 Saat maksimum bekleme

# KULLANICININ İSTEDİĞİ NET KÂR VE STOP PARAMETRELERİ
DEFAULT_TP_TRIGGER_USD  = 2.00  # +$2.00 kârda Trailing Stop başlar
DEFAULT_TRAILING_DROP   = 1.00  # +$2.00 kârdayken stop +$1.00'e çekilir (Zirveden $1.00 çekilirse satar)
DEFAULT_BE_TRIGGER_USD  = 1.00  # +$1.00 kârda başa baş koruması (stop maliyete)
DEFAULT_SL_USD          = 1.50  # -$1.50 Stop Loss

# MANUEL POZİSYON KORUMASI VE HANTAL COİN KARA LİSTESİ
PROTECTED_SYMBOLS = {"BASEDUSDT", "BASED", "TRXUSDT", "TRX", "FDUSDUSDT", "USDCUSDT"}

# LİKİDİTE VE TEKNİK FİLTRELER
MIN_VOL_USD      = 8_000_000.0   # Yüksek likidite
MAX_VOL_USD      = 250_000_000.0
MAX_STAGNATION_PCT = 6.0
MIN_VOL_MULTIPLIER = 2.5

STABLE = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","EURC"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

PROXY_URL   = os.getenv("FIXIE_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""

def get_proxies():
    if PROXY_URL and "@" in PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None

def utc():  return datetime.now(timezone.utc)
def ts():   return utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def fp(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def tg(txt):
    if not TK or not TC:
        print(f"[TG BİLDİRİM] {txt}", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id": TC, "text": txt, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        print(f"[TG HATA] {e}", flush=True)

# ── GEMINI 2.5 FLASH YAPAY ZEKA TEYİT MOTORU ─────────────────────────────────

def gemini_ai_validate(sym, side, mode, rsi, price, btc_status, last_candles_summary):
    if not GEMINI_KEY:
        return True, 80, "Gemini API anahtarı girilmedi, teknik sinyalle devam ediliyor."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    prompt = f"""
Sen dünyanın en disiplinli ve profesyonel Kripto Vadeli Scalp Uzmanısın.
Botumuz Binance vadeli işlemlerde 20x kaldıraç ile $250 büyüklüğünde {side} pozisyonuna girmek üzere.

Parite: {sym} | Yön: {side} | Strateji: {mode} | Fiyat: {price} | 15m RSI: {rsi:.1f}
BTC Durumu: {btc_status}
Son Mumlar: {last_candles_summary}

GÖREV: Bu sinyalin sahte bir tuzak mı yoksa yüksek olasılıklı bir {side} fırsatı mı olduğunu ÇOK SIKI değerlendir.
Risk yüksekse veya piyasa yönüyle çelişiyorsa REJECT ver. Sadece net fırsatları APPROVE et.

SADECE aşağıdaki JSON formatında yanıt ver:
{{
  "decision": "APPROVE" veya "REJECT",
  "confidence": 0 ile 100 arası sayı,
  "reason": "Türkçe net 1-2 cümlelik açıklama"
}}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    try:
        r = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=12)
        if r.status_code == 200:
            res_json = r.json()
            content_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(content_text)
            decision = parsed.get("decision", "APPROVE").upper()
            confidence = int(parsed.get("confidence", 80))
            reason = parsed.get("reason", "Yapay zeka sinyali onayladı.")
            # Yüksek güven eşiği (%80+)
            is_approved = (decision == "APPROVE" and confidence >= 80)
            return is_approved, confidence, reason
    except Exception as e:
        print(f"[GEMINI EXCEPTION] {e}", flush=True)
        
    return False, 50, "Yapay zeka yanıt veremedi, güvenlik gereği pas geçildi."

# ── BINANCE API İSTEMCİSİ ───────────────────────────────────────────────────

def get_public_json(endpoint, p=None):
    hosts = [FAPI_BASE, "https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com"]
    last_err = None
    
    # 1. Önce doğrudan dene
    for h in hosts:
        url = endpoint if endpoint.startswith("http") else f"{h}{endpoint}"
        try:
            r = requests.get(url, params=p, headers=HEADERS, timeout=12)
            if r.status_code == 200: return r.json()
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            
    # 2. Proxy varsa proxy ile dene
    proxies = get_proxies()
    if proxies:
        for h in hosts:
            url = endpoint if endpoint.startswith("http") else f"{h}{endpoint}"
            try:
                r = requests.get(url, params=p, headers=HEADERS, proxies=proxies, timeout=12)
                if r.status_code == 200: return r.json()
                last_err = f"Proxy HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
                
    raise Exception(f"Binance public API hatası ({endpoint}): {last_err}")

def binance_signed_request(method, path, params=None):
    if not API_KEY or not API_SECRET:
        raise Exception("Binance API_KEY veya API_SECRET eksik!")
    if params is None: params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    
    query = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{PAPI_BASE}{path}?{query}&signature={sig}"
    auth_headers = {**HEADERS, "X-MBX-APIKEY": API_KEY}
    proxies = get_proxies()
    
    for use_proxy in ([proxies, None] if proxies else [None]):
        try:
            if method.upper() == "GET": r = requests.get(url, headers=auth_headers, proxies=use_proxy, timeout=18)
            elif method.upper() == "POST": r = requests.post(url, headers=auth_headers, proxies=use_proxy, timeout=18)
            elif method.upper() == "DELETE": r = requests.delete(url, headers=auth_headers, proxies=use_proxy, timeout=18)
            else: raise ValueError(f"Geçersiz method: {method}")
            
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 407 and use_proxy:
                continue
            else:
                raise Exception(f"HTTP {r.status_code} - {r.text}")
        except Exception as e:
            if use_proxy: continue
            raise Exception(f"Binance Signed API Hatası ({path}): {e}")
            
    raise Exception(f"Binance Signed API Hatası ({path}): Bağlantı kurulamadı.")

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
    try:
        info = get_public_json("/fapi/v1/exchangeInfo")
        for s in info.get("symbols", []):
            if s.get("symbol") == sym:
                step_size, min_qty = 1.0, 1.0
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step_size = float(f.get("stepSize", 1.0))
                        min_qty = float(f.get("minQty", 1.0))
                return {
                    "stepSize": step_size, "minQty": min_qty,
                    "quantityPrecision": int(s.get("quantityPrecision", 2)),
                    "pricePrecision": int(s.get("pricePrecision", 2))
                }
    except Exception: pass
    return {"stepSize": 1.0, "minQty": 1.0, "quantityPrecision": 2, "pricePrecision": 2}

def round_step_size(qty, step_size, precision):
    if step_size <= 0: return round(qty, precision)
    steps = math.floor(qty / step_size)
    rounded = steps * step_size
    return float(f"{rounded:.{precision}f}")

def get_account_balances():
    """
    Portföyün toplam varlığını (equity) ve KULLANILABİLİR SERBEST TEMİNATINI (free_margin) döner.
    """
    equity = 0.0
    free_margin = 0.0
    try:
        acc = binance_signed_request("GET", "/papi/v1/account")
        equity = float(acc.get("accountEquity", 0))
        free_margin = float(acc.get("totalAvailableBalance", 0))
        return equity, free_margin
    except Exception:
        pass
        
    try:
        balances = binance_signed_request("GET", "/papi/v1/balance")
        for b in balances:
            if b.get("asset") == "USDT":
                eq = float(b.get("totalWalletBalance", 0))
                return eq, max(0.0, eq * 0.50)
    except Exception as e:
        print(f"[BAKİYE HATA] {e}", flush=True)
        
    return equity, free_margin

# ── BTC TREND VE DÜŞÜŞ KALKANI ──────────────────────────────────────────────

def check_btc_status():
    """
    BTC'nin anlık durumunu ve yönünü tespit eder: 'BULLISH', 'BEARISH' veya 'DUMPING'
    """
    try:
        df15m = klines("BTCUSDT", "15m", 30)
        if len(df15m) < 20: return "NEUTRAL", "BTC Verisi Yetersiz"
        last_c = df15m.iloc[-1]
        c, o = last_c['c'], last_c['o']
        
        # 1. Anlık Şelale Koruması
        if c < o and ((o - c) / o) > 0.0035:
            return "DUMPING", "BTC 15m Şelale Düşüşünde (Sert Kırmızı Mum)"
            
        df1h = klines("BTCUSDT", "1h", 40)
        if len(df1h) >= 25:
            ema20 = df1h['c'].ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = df1h['c'].ewm(span=50, adjust=False).mean().iloc[-1]
            rsi_btc = calc_rsi(df1h['c'], 14)
            
            if df1h['c'].iloc[-1] > ema20 and ema20 > ema50:
                return "BULLISH", f"BTC 1h Yükseliş Trendinde (RSI:{rsi_btc:.1f})"
            elif df1h['c'].iloc[-1] < ema20 and ema20 < ema50:
                return "BEARISH", f"BTC 1h Düşüş Trendinde (RSI:{rsi_btc:.1f})"
                
        return "NEUTRAL", "BTC Yatay / Kararsız"
    except Exception as e:
        return "NEUTRAL", f"BTC Kontrol Hatası ({e})"

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
        print(f"[UNIVERSE HATA] {e}", flush=True)
        return []

# ── ÇİFT YÖNLÜ SİNYAL MOTORU (LONG & SHORT TREND STRATEJİSİ) ─────────────────

def analyze_market_candidate(sym, btc_state):
    """
    1 Saatlik trend yönüne göre:
    - Yükseliş Trendindeyse -> Geri çekilme dönüşü (LONG) veya Direnç Kırılımı (LONG)
    - Düşüş Trendindeyse -> Tepki yükselişi reddi (SHORT) veya Destek Kırılımı (SHORT)
    """
    try:
        df1h = klines(sym, "1h", 45)
        if len(df1h) < 30: return None
        
        df15m = klines(sym, "15m", 45)
        if len(df15m) < 30: return None
        
        c1h = df1h['c'].iloc[-1]
        ema20_1h = df1h['c'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50_1h = df1h['c'].ewm(span=50, adjust=False).mean().iloc[-1]
        rsi_1h = calc_rsi(df1h['c'], 14)
        
        c15 = df15m.iloc[-1]
        c, o, h, l = c15['c'], c15['o'], c15['h'], c15['l']
        rsi_15m = calc_rsi(df15m['c'], 14)
        
        last_3_candles = [f"M{idx+1}: O={row['o']:.4f} C={row['c']:.4f} H={row['h']:.4f} L={row['l']:.4f}" for idx, row in df15m.iloc[-3:].iterrows()]
        candles_summary = " | ".join(last_3_candles)
        
        # ── 1. STRATEJİ: YÜKSELİŞ TRENDİ LONG FIRSATI ──
        # Şart: 1h EMA20 > EMA50 ve BTC çöküşte değil
        if c1h > ema50_1h and ema20_1h >= ema50_1h and btc_state != "DUMPING":
            # A) Trend İçi Dip Dönüşü (Pullback Bounce)
            if rsi_15m <= 38.0 and (c >= o or (c - l) > (h - c)):
                sma20 = df15m['c'].iloc[-20:].mean()
                std20 = df15m['c'].iloc[-20:].std()
                lower_bb = sma20 - (2.0 * std20)
                
                if l <= lower_bb or c <= sma20:
                    entry = last_price(sym)
                    return {
                        "sym": sym, "side": "LONG", "mode": "TREND_PULLBACK_LONG",
                        "entry": entry, "rsi": rsi_15m, "candles_summary": candles_summary,
                        "reasons": [
                            f"📈 *Trend:* 1h Yükseliş Trendi (EMA20 > EMA50)",
                            f"🎯 *Giriş:* 15m Desteğinden Alıcı Tepkisi (RSI: `{rsi_15m:.1f}`)",
                            f"🌊 *Yön:* Güçlü trend yönünde LONG"
                        ]
                    }
                    
            # B) Hacimli Direnç Kırılımı (Breakout LONG)
            if 52.0 <= rsi_15m <= 72.0:
                max_h24 = df1h['h'].iloc[-24:].max()
                dist = (c - max_h24) / max_h24 * 100
                if 0.1 <= dist <= 2.2:
                    vol_avg = df15m['v'].iloc[-20:-2].mean()
                    vol_now = c15['v'] + df15m['v'].iloc[-2]
                    vol_ratio = (vol_now / vol_avg) if vol_avg > 0 else 1.0
                    if vol_ratio >= MIN_VOL_MULTIPLIER:
                        entry = last_price(sym)
                        return {
                            "sym": sym, "side": "LONG", "mode": "MOMENTUM_BREAKOUT_LONG",
                            "entry": entry, "rsi": rsi_15m, "candles_summary": candles_summary,
                            "reasons": [
                                f"🚀 *Kırılım:* 24s Zirve Kırıldı (Breakout)",
                                f"🔥 *Hacim:* `{vol_ratio:.1f}x` katı alıcı hacmi",
                                f"📈 *Trend:* 1h Yükseliş Onaylı"
                            ]
                        }

        # ── 2. STRATEJİ: DÜŞÜŞ TRENDİ SHORT FIRSATI ──
        # Şart: 1h EMA20 < EMA50 (Düşüş Trendi) veya BTC Düşüşte
        if c1h < ema50_1h and ema20_1h <= ema50_1h:
            # A) Tepki Yükselişi Reddi (Bearish Rejection SHORT)
            if rsi_15m >= 62.0 and (c <= o or (h - c) > (c - l)):
                sma20 = df15m['c'].iloc[-20:].mean()
                std20 = df15m['c'].iloc[-20:].std()
                upper_bb = sma20 + (2.0 * std20)
                
                if h >= upper_bb or c >= sma20:
                    entry = last_price(sym)
                    return {
                        "sym": sym, "side": "SHORT", "mode": "BEARISH_REJECTION_SHORT",
                        "entry": entry, "rsi": rsi_15m, "candles_summary": candles_summary,
                        "reasons": [
                            f"📉 *Trend:* 1h Düşüş Trendi (EMA20 < EMA50)",
                            f"🛑 *Direnç Reddi:* 15m Aşırı Alım Tepesinden Satış Baskısı (RSI: `{rsi_15m:.1f}`)",
                            f"🔻 *Yön:* Ana düşüş trendi yönünde SHORT"
                        ]
                    }
                    
            # B) Destek Kırılımı (Breakdown SHORT)
            if 30.0 <= rsi_15m <= 48.0:
                min_l24 = df1h['l'].iloc[-24:].min()
                dist = (min_l24 - c) / min_l24 * 100
                if 0.1 <= dist <= 2.2:
                    vol_avg = df15m['v'].iloc[-20:-2].mean()
                    vol_now = c15['v'] + df15m['v'].iloc[-2]
                    vol_ratio = (vol_now / vol_avg) if vol_avg > 0 else 1.0
                    if vol_ratio >= MIN_VOL_MULTIPLIER:
                        entry = last_price(sym)
                        return {
                            "sym": sym, "side": "SHORT", "mode": "BREAKDOWN_SHORT",
                            "entry": entry, "rsi": rsi_15m, "candles_summary": candles_summary,
                            "reasons": [
                                f"💥 *Kırılım:* 24s Ana Destek Aşağı Kırıldı (Breakdown)",
                                f"🌊 *Hacim:* `{vol_ratio:.1f}x` katı satıcı hacmi",
                                f"🔻 *Yön:* Düşüş trendi hızlanıyor (SHORT)"
                            ]
                        }

        return None
    except Exception:
        return None

# ── EMİR VE TEMİNAT YÖNETİMİ ────────────────────────────────────────────────

def set_optimal_leverage(sym, target_lev=20):
    for lev in [target_lev, 20, 15, 12, 10]:
        try:
            binance_signed_request("POST", "/papi/v1/um/leverage", {"symbol": sym, "leverage": lev})
            return lev
        except Exception:
            continue
    return 10

def execute_real_entry(sym, side, free_margin):
    """
    20x-25x kaldıraç ile $250 büyüklüğünde LONG veya SHORT pozisyonu açar.
    """
    actual_lev = set_optimal_leverage(sym, target_lev=DEFAULT_LEVERAGE)
    time.sleep(0.15)
    
    rules = get_symbol_rules(sym)
    price = last_price(sym)
    
    # $250 pozisyon büyüklüğü hedefi
    target_usd = TARGET_NOTIONAL
    max_safe_notional = free_margin * actual_lev * 0.85
    actual_notional_target = min(target_usd, max_safe_notional)
    
    raw_qty = actual_notional_target / price
    qty = round_step_size(raw_qty, rules["stepSize"], rules["quantityPrecision"])
    if qty < rules["minQty"]: qty = rules["minQty"]
    
    order_side = "BUY" if side == "LONG" else "SELL"
    order_params = {
        "symbol": sym, "side": order_side, "type": "MARKET", "quantity": str(qty)
    }
    
    actual_notional = qty * price
    print(f"⚡ [GERÇEK EMİR AÇILIYOR] {sym} | Yön: {side} | Kaldıraç: {actual_lev}x | Miktar: {qty} | Büyüklük: ${actual_notional:.2f} (Serbest: ${free_margin:.2f})", flush=True)
    order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
    
    avg_price = float(order_res.get("avgPrice", 0))
    if avg_price <= 0: avg_price = price
        
    dyn_tp_trigger_usd = DEFAULT_TP_TRIGGER_USD
    dyn_trailing_drop_usd = DEFAULT_TRAILING_DROP
    dyn_be_trigger_usd = DEFAULT_BE_TRIGGER_USD
    dyn_sl_usd = DEFAULT_SL_USD
    
    if side == "LONG":
        sl_price = avg_price - (dyn_sl_usd / qty)
        be_trigger_price = avg_price + (dyn_be_trigger_usd / qty)
        be_sl_price = avg_price + (0.20 / qty)
    else: # SHORT
        sl_price = avg_price + (dyn_sl_usd / qty)
        be_trigger_price = avg_price - (dyn_be_trigger_usd / qty)
        be_sl_price = avg_price - (0.20 / qty)
    
    return {
        "sym": sym, "side": side, "entry": avg_price, "qty": qty,
        "notional_usd": round(actual_notional, 2),
        "leverage": actual_lev,
        "sl_price": sl_price,
        "dyn_sl_usd": dyn_sl_usd,
        "dyn_tp_trigger_usd": dyn_tp_trigger_usd,
        "dyn_be_trigger_usd": dyn_be_trigger_usd,
        "dyn_trailing_drop_usd": dyn_trailing_drop_usd,
        "be_trigger_price": be_trigger_price,
        "be_sl_price": be_sl_price,
        "trailing_active": False,
        "highest_profit_usd": 0.0,
        "trailing_sl_price": sl_price,
        "order_id": order_res.get("orderId", str(uuid.uuid4())[:8]),
        "opened_iso": utc().isoformat(), "opened_ts": ts(),
        "be_hit": False
    }

def execute_real_close(pos, reason):
    sym = pos["sym"]
    qty = pos["qty"]
    close_side = "SELL" if pos["side"] == "LONG" else "BUY"
    
    order_params = {
        "symbol": sym, "side": close_side, "type": "MARKET", "quantity": str(qty), "reduceOnly": "true"
    }
    
    print(f"🔒 [GERÇEK POZİSYON KAPATILIYOR] {sym} ({pos['side']} - {reason}) | Miktar: {qty}", flush=True)
    try:
        order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
        exit_price = float(order_res.get("avgPrice", 0))
        if exit_price <= 0: exit_price = last_price(sym)
    except Exception as e:
        print(f"[KAPATMA HATA] {e}", flush=True)
        exit_price = last_price(sym)
        
    if pos["side"] == "LONG":
        pnl = (exit_price - pos["entry"]) * qty
    else: # SHORT
        pnl = (pos["entry"] - exit_price) * qty
        
    return exit_price, pnl

# ── TELEGRAM BİLDİRİMLERİ ────────────────────────────────────────────────────

def msg_real_open(pos, sig, ai_conf, ai_reason):
    icon = "📈" if pos["side"] == "LONG" else "📉"
    lines = "\n".join(f"  • {r}" for r in sig.get("reasons", []))
    lev = pos.get("leverage", DEFAULT_LEVERAGE)
    tp_val = float(pos.get("dyn_tp_trigger_usd", DEFAULT_TP_TRIGGER_USD))
    drop_val = float(pos.get("dyn_trailing_drop_usd", DEFAULT_TRAILING_DROP))
    lock_val = tp_val - drop_val
    sl_val = float(pos.get("dyn_sl_usd", DEFAULT_SL_USD))
    be_val = float(pos.get("dyn_be_trigger_usd", DEFAULT_BE_TRIGGER_USD))
    
    return (
        f"{icon} *GERÇEK POZİSYON AÇILDI!* | `{pos['sym']}`\n\n"
        f"Yön: *{pos['side']} ({lev}x Kaldıraç)*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Pozisyon Büyüklüğü : `${pos['notional_usd']}` ({pos['qty']} adet)\n\n"
        f"📈 *Trailing Kâr:* `+${tp_val:.2f}` geçilince başlar (+${lock_val:.2f} kilitlenir)\n"
        f"🛑 *Stop Loss:* `-${sl_val:.2f}` (`{fp(pos['sl_price'])}`)\n"
        f"🔰 *Sıfır Risk (+${be_val:.2f} kârda):* Stop maliyete çekilir\n\n"
        f"🧠 *Gemini 2.5 Flash AI Teyidi:* `%{ai_conf} Güven`\n"
        f"💬 *AI Gerekçesi:* _{ai_reason}_\n\n"
        f"*Teknik Gerekçeler:*\n{lines}\n\n"
        f"Zaman: `{ts()}`"
    )

def msg_real_close(pos, exit_price, pnl, reason, dur_sec):
    icon = "🟢" if pnl >= 0 else "🔴"
    abs_pnl = abs(pnl)
    title = {
        "TRAILING_TP": f"💸 TRAILING KÂR ALINDI (+${pnl:.2f})",
        "STOP_LOSS": f"❌ STOP OLDU (-${abs_pnl:.2f})",
        "BREAKEVEN": "🔰 BAŞA BAŞ KAPANDI (Sıfır Risk)",
        "TIMEOUT": "⏱️ SÜRE DOLDU"
    }.get(reason, reason)
    
    eq, free_m = get_account_balances()
    highest_pnl = float(pos.get("highest_profit_usd", pnl))
    dur_min = dur_sec // 60
    
    return (
        f"{icon} *{title}* | `{pos['sym']} ({pos['side']})`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(exit_price)}`\n"
        f"Net P&L : *`${pnl:+.2f} USDT`*\n"
        f"Görülen Zirve Kâr : `+${highest_pnl:.2f}`\n"
        f"İşlem Süresi : `{dur_min} dakika`\n"
        f"💰 *Güncel Varlık:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n\n"
        f"Zaman: `{ts()}`"
    )

# ── DURUM YÖNETİMİ ───────────────────────────────────────────────────────────

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
        "id": pos.get("order_id", ""), "pair": pos["sym"], "side": pos["side"],
        "entry": pos["entry"], "exit": exit_price, "qty": pos["qty"],
        "notional": pos["notional_usd"], "pnl": round(pnl, 2),
        "result": reason, "duration": dur_sec, "timestamp": ts()
    })
    save_db(trades)
    return trades

# ── ANLIK MONİTÖR & TRAILING KÂR MOTORU (LONG & SHORT UYUMLU) ────────────────

def get_real_open_symbols():
    try:
        positions = binance_signed_request("GET", "/papi/v1/um/positionRisk")
        return {p.get("symbol") for p in positions if float(p.get("positionAmt", 0)) != 0}
    except Exception:
        return None

def monitor(state):
    real_open = get_real_open_symbols()
    still = []
    
    for pos in state.get("positions", []):
        sym = pos["sym"]
        side = pos.get("side", "LONG")
        
        # Eğer pozisyon Binance üzerinde manuel kapatılmışsa durumdan temizle
        if real_open is not None and sym not in real_open:
            print(f"ℹ️ [{sym}] Binance üzerinde kapatılmış, takip listesinden çıkarıldı.", flush=True)
            continue
            
        try: price = last_price(sym)
        except Exception:
            still.append(pos); continue
            
        qty = pos["qty"]
        entry = pos["entry"]
        dur = int((utc() - datetime.fromisoformat(pos.get("opened_iso", utc().isoformat()))).total_seconds())
        
        # PnL Hesabı: LONG ve SHORT için simetrik
        if side == "LONG":
            unrealized_pnl = (price - entry) * qty
        else: # SHORT
            unrealized_pnl = (entry - price) * qty
        
        # En yüksek görülen kârı takip et
        if unrealized_pnl > pos.get("highest_profit_usd", 0.0):
            pos["highest_profit_usd"] = unrealized_pnl
            
        highest_pnl = pos["highest_profit_usd"]
        dyn_tp_trigger = pos.get("dyn_tp_trigger_usd", DEFAULT_TP_TRIGGER_USD)
        dyn_be_trigger = pos.get("dyn_be_trigger_usd", DEFAULT_BE_TRIGGER_USD)
        dyn_trailing_drop = pos.get("dyn_trailing_drop_usd", DEFAULT_TRAILING_DROP)
        
        # 1. TRAILING TAKE PROFIT (+$2.00 kârda başlar ve stopu hemen +$1.00 kâra kilitler)
        if unrealized_pnl >= dyn_tp_trigger or pos.get("trailing_active"):
            if not pos.get("trailing_active"):
                pos["trailing_active"] = True
                locked_profit = max(1.00, unrealized_pnl - dyn_trailing_drop)
                tg(f"🚀 *{sym} ({side})* `+${unrealized_pnl:.2f}` kâra ulaştı! *Trailing Kâr Takibi Aktif Edildi!*\nStop seviyesi `+${locked_profit:.2f}` kâra kilitlendi. Fiyat ilerledikçe stop peşinden sürecek.")
                
            # Zirveden $1.00 geri çekilme stopu
            trailing_exit_pnl = highest_pnl - dyn_trailing_drop
            if side == "LONG":
                trailing_exit_price = entry + (trailing_exit_pnl / qty)
                pos["trailing_sl_price"] = max(pos.get("trailing_sl_price", pos["sl_price"]), trailing_exit_price)
            else: # SHORT
                trailing_exit_price = entry - (trailing_exit_pnl / qty)
                pos["trailing_sl_price"] = min(pos.get("trailing_sl_price", pos["sl_price"]), trailing_exit_price)
            
        # 2. BREAKEVEN KORUMASI (+ $1.00 kârda stop maliyet seviyesine çekilir)
        if not pos.get("be_hit"):
            be_condition = (price >= pos.get("be_trigger_price", entry * 1.005)) if side == "LONG" else (price <= pos.get("be_trigger_price", entry * 0.995))
            if be_condition or unrealized_pnl >= dyn_be_trigger:
                pos["sl_price"] = pos["be_sl_price"]
                pos["be_hit"] = True
                tg(f"🔰 *{sym} ({side})* `+${unrealized_pnl:.2f}` kâra ulaştı! Stop maliyete (`{fp(pos['sl_price'])}`) çekildi. *İşlem artık sıfır risklidir!*")

        reason = None
        # Trailing Kâr Tetiklenmesi
        if pos.get("trailing_active"):
            trail_hit = (price <= pos["trailing_sl_price"]) if side == "LONG" else (price >= pos["trailing_sl_price"])
            if trail_hit: reason = "TRAILING_TP"
            
        # Stop Loss veya Breakeven Stop
        if not reason:
            sl_hit = (price <= pos["sl_price"]) if side == "LONG" else (price >= pos["sl_price"])
            if sl_hit:
                reason = "BREAKEVEN" if pos.get("be_hit") else "STOP_LOSS"
                
        # Süre Aşımı
        if not reason and dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
            
        if reason:
            if REAL_TRADING: exit_price, real_pnl = execute_real_close(pos, reason)
            else: exit_price, real_pnl = price, unrealized_pnl
                
            record_trade(pos, exit_price, real_pnl, reason, dur)
            tg(msg_real_close(pos, exit_price, real_pnl, reason, dur))
            print(f"🔒 [{reason}] {sym} ({side}) @ {fp(exit_price)} | Net P&L: ${real_pnl:+.2f}", flush=True)
        else:
            trail_str = f"| Trailing Stop: {fp(pos['trailing_sl_price'])}" if pos.get("trailing_active") else ""
            print(f"  [AÇIK POZİSYON] {sym} ({side}) | Fiyat: {fp(price)} | PnL: ${unrealized_pnl:+.2f} (Zirve: ${highest_pnl:+.2f}) | SL: {fp(pos['sl_price'])} {trail_str}", flush=True)
            still.append(pos)
            
    state["positions"] = still
    return state

# ── TARAMA VE AI DOĞRULAMA ───────────────────────────────────────────────────

def scan(state, universe):
    eq, free_margin = get_account_balances()
    
    # Küçük kasalarda (< $70) sadece 1 aktif bot pozisyonuna izin ver
    max_allowed_positions = 1 if eq < 70.0 else max(1, min(4, int(eq / 35.0)))
    
    if len(state.get("positions", [])) >= max_allowed_positions:
        return state
        
    # Yetersiz serbest teminat koruması (-2019 hatasını önler)
    if free_margin < 9.0:
        return state
        
    btc_state, btc_reason = check_btc_status()

    open_syms = {p["sym"] for p in state.get("positions", [])}
    open_syms.update(PROTECTED_SYMBOLS)
    
    for i, (sym, _) in enumerate(universe):
        if sym in open_syms: continue
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r", flush=True)
        try:
            sig = analyze_market_candidate(sym, btc_state)
            if sig:
                side = sig["side"]
                print(f"\n🔍 [TEKNİK SİNYAL] {sym} ({side} - {sig['mode']})! Gemini 2.5 Flash analiz ediyor...", flush=True)
                
                ai_ok, ai_conf, ai_reason = gemini_ai_validate(
                    sym=sym, side=side, mode=sig["mode"], rsi=sig["rsi"],
                    price=sig["entry"], btc_status=btc_reason,
                    last_candles_summary=sig.get("candles_summary", "")
                )
                
                if not ai_ok:
                    print(f"❌ [GEMINI RED] {sym} (%{ai_conf}) — {ai_reason}", flush=True)
                    continue
                    
                print(f"✅ [GEMINI ONAY] {sym} ({side} %{ai_conf})! Gerçek işlem açılıyor...", flush=True)
                
                if REAL_TRADING:
                    pos = execute_real_entry(sym, side=side, free_margin=free_margin)
                else:
                    pos = {
                        "sym": sym, "side": side, "entry": sig["entry"],
                        "qty": round(250.0 / sig["entry"], 2),
                        "notional_usd": 250.0, "leverage": DEFAULT_LEVERAGE,
                        "sl_price": sig["entry"] * (0.994 if side == "LONG" else 1.006),
                        "dyn_sl_usd": DEFAULT_SL_USD, "dyn_tp_trigger_usd": DEFAULT_TP_TRIGGER_USD,
                        "dyn_be_trigger_usd": DEFAULT_BE_TRIGGER_USD, "dyn_trailing_drop_usd": DEFAULT_TRAILING_DROP,
                        "be_trigger_price": sig["entry"] * (1.004 if side == "LONG" else 0.996),
                        "be_sl_price": sig["entry"] * (1.001 if side == "LONG" else 0.999),
                        "trailing_active": False, "highest_profit_usd": 0.0,
                        "trailing_sl_price": sig["entry"] * (0.994 if side == "LONG" else 1.006),
                        "opened_iso": utc().isoformat(), "opened_ts": ts(), "be_hit": False
                    }
                    
                state.setdefault("positions", []).append(pos)
                tg(msg_real_open(pos, sig, ai_conf, ai_reason))
                break
        except Exception as e:
            print(f"[İŞLEM AÇILIŞ HATA] {sym}: {e}", flush=True)
            tg(f"⚠️ *İşlem Hatası ({sym}):* `{e}`")
        time.sleep(0.06)
        
    return state

# ── ANA DÖNGÜ ────────────────────────────────────────────────────────────────

def main():
    print("="*65, flush=True)
    print("⚡ BİNANCE ÇİFT YÖNLÜ (LONG & SHORT) YAPAY ZEKA ROBOTU", flush=True)
    print("="*65, flush=True)
    print(" 🧠 Yapay Zeka         : Google Gemini 2.5 Flash (%80+ Güven Filtresi)", flush=True)
    print(" 🔄 Çift Yönlü Motor   : Yükselen trendde LONG, Düşen trendde SHORT", flush=True)
    print(" ⚡ Kaldıraç & Boyut   : 20x - 25x Kaldıraç | Tam $250.00 Pozisyon", flush=True)
    print(" 💸 Trailing Stop      : +$2.00 kârda başlar, +$1.00 kârı kilitler ve sürer", flush=True)
    print(" 🔰 Başa Baş Koruma    : +$1.00 kârda stop maliyete çekilir (Sıfır Risk)", flush=True)
    print(" 🛑 Hızlı Stop Loss    : -$1.50 seviyesinde anlık koruma", flush=True)
    print(" 🛡️ Koruma             : BASEDUSDT dokunulmaz | BTC Trend Kalkanı Aktif", flush=True)
    print("="*65, flush=True)
    
    eq, free_m = get_account_balances()
    print(f"✅ Toplam Varlık: ${eq:.2f} USDT | Serbest Teminat: ${free_m:.2f} USDT", flush=True)
    
    tg(f"🚀 *ÇİFT YÖNLÜ (LONG & SHORT) VADELİ BOTU BAŞLATILDI!*\n\n"
       f"💰 *Toplam Varlık:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n"
       f"🔄 *İşlem Yönü:* Yükselişte *LONG*, Düşüşte *SHORT* açılır.\n"
       f"⚡ *Pozisyon Boyutu:* `20x Kaldıraç` ile tam `$250.00 USDT`.\n"
       f"📈 *Trailing Kâr:* `+$2.00` kârda başlar, `+$1.00` kilitler.\n"
       f"🔰 *Başa Baş:* `+$1.00` kârda stop maliyete çekilir.\n"
       f"🛑 *Stop Loss:* `-$1.50` anlık çıkış.\n"
       f"🛡️ *Korumalı:* `BASEDUSDT` dokunulmaz.\n\n"
       f"📍 *Durum:* Canlı Piyasa Taraması Aktif ({utc().strftime('%H:%M:%S UTC')})")

    last_scan_time = 0
    last_heartbeat_time = 0
    
    while True:
        try:
            state = load_st()
            now = time.time()
            
            # Her 60 saniyede bir loglara durum bildirimi yaz
            if now - last_heartbeat_time >= 60:
                eq, free_m = get_account_balances()
                open_count = len(state.get("positions", []))
                print(f"💓 [CANLI DURUM] Varlık: ${eq:.2f} USDT (Serbest: ${free_m:.2f}) | Açık Bot Pozisyonu: {open_count} | Saat: {utc().strftime('%H:%M:%S UTC')}", flush=True)
                last_heartbeat_time = now
            
            if state.get("positions"):
                state = monitor(state)
                save_st(state)
                time.sleep(1.5)
            else:
                if now - last_scan_time >= 20:
                    universe = get_universe()
                    state = scan(state, universe)
                    save_st(state)
                    last_scan_time = time.time()
                time.sleep(1.0)
                
        except Exception as e:
            print(f"[HATA] Ana Döngü: {e}", flush=True)
            time.sleep(2.0)

if __name__ == "__main__":
    main()
