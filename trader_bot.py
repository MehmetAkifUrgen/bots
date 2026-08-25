"""
trader_bot.py — Yapay Zekalı (Gemini 2.5 Flash) & Dinamik Oransal Korumalı Binance Vadeli Botu
• Hesap: Portfolio Margin (PAPI) & Futures Uyumlu
• Kasa & Pozisyon Yönetimi: Kasa durumuna göre dinamik pozisyon büyüklüğü (~%70 serbest teminat * 10x)
• Dinamik Oransal Kâr (Trailing TP): Pozisyon büyüklüğüne göre orantılı kâr hedefi (~%1.3 kârda Trailing başlar)
• Hızlı Anlık İzleme: 1.5 saniyelik ultra hızlı döngü ile kaymasız Breakeven, SL ve Trailing TP
• Koruma: BASEDUSDT dokunulmaz | BTC Düşüş Kalkanı aktif | Gemini 2.5 Flash AI Teyitli | Proxy Desteği
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
LEVERAGE         = int(os.getenv("LEVERAGE", "10"))          # 10x Kaldıraç
SCAN_EVERY       = int(os.getenv("SCAN_EVERY_SECONDS", "25"))
MAX_HOLD_MIN     = 720 # 12 Saat maksimum bekleme

# MANUEL POZİSYON KORUMASI (Bot bu sembollere asla dokunmaz)
PROTECTED_SYMBOLS = {"BASEDUSDT", "BASED"}

# LİKİDİTE VE TEKNİK FİLTRELER
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

PROXY_URL   = os.getenv("FIXIE_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""

def get_proxies():
    if PROXY_URL:
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

# ── GEMINI 2.5 FLASH YAPAY ZEKA TEYİT MOTORU ─────────────────────────────────

def gemini_ai_validate(sym, mode, rsi, price, btc_status, last_candles_summary):
    if not GEMINI_KEY:
        return True, 80, "Gemini API anahtarı girilmedi, teknik sinyalle devam ediliyor."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    prompt = f"""
Sen dünyanın en iyi Kripto Vadeli İşlemler Analistisin.
Botumuz Binance vadeli işlemlerde 10x kaldıraç ile scalp LONG pozisyonuna girmek üzere.

Parite: {sym} | Mod: {mode} | Fiyat: {price} | 15m RSI: {rsi:.1f}
BTC Durumu: {btc_status}
Son Mumlar: {last_candles_summary}

GÖREV: Bu sinyalin sahte bir tuzak mı yoksa karlı bir dönüş/patlama mı olduğunu değerlendir.
SADECE aşağıdaki JSON formatında yanıt ver:
{{
  "decision": "APPROVE" veya "REJECT",
  "confidence": 0 ile 100 arası sayı,
  "reason": "Türkçe 1-2 cümlelik net açıklama"
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
            is_approved = (decision == "APPROVE" and confidence >= 70)
            return is_approved, confidence, reason
    except Exception as e:
        print(f"[GEMINI EXCEPTION] {e}")
        
    return True, 75, "Teknik teyitle devam ediliyor."

# ── BINANCE API İSTEMCİSİ ───────────────────────────────────────────────────

def get_public_json(endpoint, p=None):
    hosts = [FAPI_BASE, "https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com"]
    last_err = None
    proxies = get_proxies()
    for h in hosts:
        url = endpoint if endpoint.startswith("http") else f"{h}{endpoint}"
        try:
            r = requests.get(url, params=p, headers=HEADERS, proxies=proxies, timeout=15)
            if r.status_code == 200: return r.json()
            last_err = f"HTTP {r.status_code}"
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
    
    if method.upper() == "GET": r = requests.get(url, headers=auth_headers, proxies=proxies, timeout=20)
    elif method.upper() == "POST": r = requests.post(url, headers=auth_headers, proxies=proxies, timeout=20)
    elif method.upper() == "DELETE": r = requests.delete(url, headers=auth_headers, proxies=proxies, timeout=20)
    else: raise ValueError(f"Geçersiz method: {method}")
        
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

def get_account_balance():
    try:
        balances = binance_signed_request("GET", "/papi/v1/balance")
        for b in balances:
            if b.get("asset") == "USDT":
                return float(b.get("totalWalletBalance", 0))
    except Exception as e:
        print(f"[BAKİYE HATA] {e}")
    return 0.0

# ── BTC DÜŞÜŞ KALKANI ────────────────────────────────────────────────────────

def check_btc_shield():
    try:
        df15m = klines("BTCUSDT", "15m", 30)
        if len(df15m) < 20: return True, "BTC Verisi Yetersiz"
        last_c = df15m.iloc[-1]
        c, o = last_c['c'], last_c['o']
        
        # 1. Anlık Şelale: 15m mumunda %0.3'ten fazla sert kırmızıysa girme
        if c < o and ((o - c) / o) > 0.003:
            return False, "BTC Anlık Şelalede (15m Sert Kırmızı Mum)"
            
        # 2. 1 Saatlik Düşüş Trendi
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

# ── SİNYAL MOTORU ────────────────────────────────────────────────────────────

def analyze_market_candidate(sym):
    try:
        df15m = klines(sym, "15m", 45)
        if len(df15m) < 30: return None
        
        c_candle = df15m.iloc[-1]
        c, o, h, l = c_candle['c'], c_candle['o'], c_candle['h'], c_candle['l']
        rsi_val = calc_rsi(df15m['c'], 14)
        
        last_3_candles = [f"M{idx+1}: O={row['o']:.4f} C={row['c']:.4f} H={row['h']:.4f} L={row['l']:.4f}" for idx, row in df15m.iloc[-3:].iterrows()]
        candles_summary = " | ".join(last_3_candles)
        
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
                        "rsi": rsi_val, "candles_summary": candles_summary,
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
                                    "rsi": rsi_val, "candles_summary": candles_summary,
                                    "reasons": [
                                        f"🚀 *Strateji:* PUMP SNIPER (Volume Breakout)",
                                        f"🌋 *Kırılım:* 24s sıkışma zirvesi kırıldı!",
                                        f"🌊 *Hacim:* `{vol_ratio:.1f}x` katı patlama"
                                    ]
                                }
        return None
    except Exception:
        return None

# ── DİNAMİK ORANSAL KÂR & EMİR MOTORU ────────────────────────────────────────

def set_leverage(sym, lev=10):
    try: binance_signed_request("POST", "/papi/v1/um/leverage", {"symbol": sym, "leverage": lev})
    except Exception as e: print(f"[LEVERAGE UYARI] {sym} kaldıraç ayarlanamadı ({e})")

def execute_real_entry(sym, available_balance):
    """
    Kasa ve pozisyon büyüklüğüne göre dinamik oransal kâr ve stop hedefleri belirler.
    """
    usable_margin = max(10.0, available_balance * 0.70)
    notional_usd = usable_margin * LEVERAGE
    
    rules = get_symbol_rules(sym)
    price = last_price(sym)
    
    raw_qty = notional_usd / price
    qty = round_step_size(raw_qty, rules["stepSize"], rules["quantityPrecision"])
    if qty < rules["minQty"]: qty = rules["minQty"]
        
    set_leverage(sym, LEVERAGE)
    time.sleep(0.15)
    
    order_params = {
        "symbol": sym, "side": "BUY", "type": "MARKET", "quantity": str(qty)
    }
    
    actual_notional = qty * price
    print(f"⚡ [GERÇEK EMİR AÇILIYOR] {sym} | Miktar: {qty} | Notional: ~${actual_notional:.2f} (Kasa: ${available_balance:.2f})")
    order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
    
    avg_price = float(order_res.get("avgPrice", 0))
    if avg_price <= 0: avg_price = price
        
    # DİNAMİK ORANSAL HEDEFLER (Pozisyon büyüklüğüne göre ayarlanır):
    # Trailing TP Tetikleme: Pozisyonun ~%1.2 - %1.4'ü (Örn: 220$'da 2.50$ - 3.00$, 150$'da 1.80$)
    dyn_tp_trigger_usd = max(1.80, round(actual_notional * 0.013, 2))
    # Breakeven Tetikleme: Pozisyonun ~%0.7'si (Örn: 220$'da 1.50$, 150$'da 1.00$)
    dyn_be_trigger_usd = max(0.80, round(actual_notional * 0.007, 2))
    # Stop Loss: Pozisyonun ~%0.7'si (En fazla 1.50$ kayıp)
    dyn_sl_usd = min(1.50, max(0.80, round(actual_notional * 0.007, 2)))
    # Trailing Geri Çekilme Payı: Kâr hedefinin %25'i (Örn: 0.60$ - 0.75$)
    dyn_trailing_drop_usd = max(0.40, round(dyn_tp_trigger_usd * 0.25, 2))
    
    sl_price = avg_price - (dyn_sl_usd / qty)
    be_trigger_price = avg_price + (dyn_be_trigger_usd / qty)
    be_sl_price = avg_price + (0.30 / qty)
    
    return {
        "sym": sym, "side": "LONG", "entry": avg_price, "qty": qty,
        "notional_usd": round(actual_notional, 2),
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
    order_params = {
        "symbol": sym, "side": "SELL", "type": "MARKET", "quantity": str(qty), "reduceOnly": "true"
    }
    
    print(f"🔒 [GERÇEK POZİSYON KAPATILIYOR] {sym} ({reason}) | Miktar: {qty}")
    try:
        order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
        exit_price = float(order_res.get("avgPrice", 0))
        if exit_price <= 0: exit_price = last_price(sym)
    except Exception as e:
        print(f"[KAPATMA HATA] {e}")
        exit_price = last_price(sym)
        
    pnl = (exit_price - pos["entry"]) * qty
    return exit_price, pnl

# ── TELEGRAM BİLDİRİMLERİ ────────────────────────────────────────────────────

def msg_real_open(pos, sig, ai_conf, ai_reason):
    icon = "🎯" if sig.get("mode") == "DİP_AVCISI" else "🚀"
    lines = "\n".join(f"  • {r}" for r in sig.get("reasons", []))
    return (
        f"{icon} *GERÇEK POZİSYON AÇILDI!* | `{pos['sym']}`\n\n"
        f"Yön: *LONG (10x Kaldıraç)*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Pozisyon Büyüklüğü : `${pos['notional_usd']}` ({pos['qty']} adet)\n\n"
        f"📈 *Dinamik Trailing Kâr:* `+${pos['dyn_tp_trigger_usd']:.2f}` geçilince başlar (Limitsiz Kâr!)\n"
        f"🛑 *Dinamik Stop Loss:* `-${pos['dyn_sl_usd']:.2f}` (`{fp(pos['sl_price'])}`)\n"
        f"🔰 *Sıfır Risk (+${pos['dyn_be_trigger_usd']:.2f} kârda):* Stop maliyete çekilir\n\n"
        f"🧠 *Gemini 2.5 Flash AI Teyidi:* `%{ai_conf} Güven`\n"
        f"💬 *AI Gerekçesi:* _{ai_reason}_\n\n"
        f"*Teknik Gerekçeler:*\n{lines}\n\n"
        f"Zaman: `{ts()}`"
    )

def msg_real_close(pos, exit_price, pnl, reason, dur_sec):
    icon = "🟢" if pnl >= 0 else "🔴"
    title = {
        "TRAILING_TP": f"💸 TRAILING KÂR ALINDI (+${pnl:.2f})",
        "STOP_LOSS": f"❌ STOP OLDU (-${abs(pnl):.2f})",
        "BREAKEVEN": "🔰 BAŞA BAŞ KAPANDI (Sıfır Risk)",
        "TIMEOUT": "⏱️ SÜRE DOLDU"
    }.get(reason, reason)
    
    bal = get_account_balance()
    return (
        f"{icon} *{title}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(exit_price)}`\n"
        f"Net P&L : *`${pnl:+.2f} USDT`*\n"
        f"Görülen Zirve Kâr : `+${pos.get('highest_profit_usd', pnl):.2f}`\n"
        f"İşlem Süresi : `{dur_sec//60} dakika`\n"
        f"💰 *Güncel Kasa:* `${bal:.2f} USDT`\n\n"
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
        "id": pos.get("order_id", ""), "pair": pos["sym"], "side": "LONG",
        "entry": pos["entry"], "exit": exit_price, "qty": pos["qty"],
        "notional": pos["notional_usd"], "pnl": round(pnl, 2),
        "result": reason, "duration": dur_sec, "timestamp": ts()
    })
    save_db(trades)
    return trades

# ── ANLIK MONİTÖR & TRAILING KÂR MOTORU ──────────────────────────────────────

def monitor(state):
    still = []
    for pos in state.get("positions", []):
        sym = pos["sym"]
        try: price = last_price(sym)
        except Exception:
            still.append(pos); continue
            
        qty = pos["qty"]
        entry = pos["entry"]
        dur = int((utc() - datetime.fromisoformat(pos.get("opened_iso", utc().isoformat()))).total_seconds())
        unrealized_pnl = (price - entry) * qty
        
        # En yüksek görülen kârı takip et
        if unrealized_pnl > pos.get("highest_profit_usd", 0.0):
            pos["highest_profit_usd"] = unrealized_pnl
            
        highest_pnl = pos["highest_profit_usd"]
        dyn_tp_trigger = pos.get("dyn_tp_trigger_usd", 2.50)
        dyn_be_trigger = pos.get("dyn_be_trigger_usd", 1.20)
        dyn_trailing_drop = pos.get("dyn_trailing_drop_usd", 0.60)
        
        # 1. TRAILING TAKE PROFIT AKTİVASYONU (Oransal kâra ulaşıldığında devreye girer)
        if unrealized_pnl >= dyn_tp_trigger or pos.get("trailing_active"):
            if not pos.get("trailing_active"):
                pos["trailing_active"] = True
                tg(f"🚀 *{sym}* +${unrealized_pnl:.2f} kâra ulaştı! *Trailing Kâr Takibi Aktif Edildi!* Fiyat yükseldikçe kâr sürülüyor.")
                
            # Zirveden geri çekilme stopu
            trailing_exit_pnl = highest_pnl - dyn_trailing_drop
            trailing_exit_price = entry + (trailing_exit_pnl / qty)
            pos["trailing_sl_price"] = max(pos.get("trailing_sl_price", pos["sl_price"]), trailing_exit_price)
            
        # 2. BREAKEVEN KORUMASI (Oransal +%0.7 kârda stop maliyet + komisyon payına çekilir)
        if not pos.get("be_hit") and (price >= pos.get("be_trigger_price", entry * 1.008) or unrealized_pnl >= dyn_be_trigger):
            pos["sl_price"] = pos["be_sl_price"]
            pos["be_hit"] = True
            tg(f"🔰 *{sym}* +${unrealized_pnl:.2f} kâra ulaştı! Stop maliyete (`{fp(pos['sl_price'])}`) çekildi. *İşlem artık sıfır risklidir!*")

        reason = None
        # Trailing Kâr Tetiklenmesi (Zirveden çekilince sat)
        if pos.get("trailing_active") and price <= pos["trailing_sl_price"]:
            reason = "TRAILING_TP"
        # Stop Loss veya Breakeven Stop
        elif price <= pos["sl_price"]:
            reason = "BREAKEVEN" if pos.get("be_hit") else "STOP_LOSS"
        # Süre Aşımı
        elif dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
            
        if reason:
            if REAL_TRADING: exit_price, real_pnl = execute_real_close(pos, reason)
            else: exit_price, real_pnl = price, unrealized_pnl
                
            record_trade(pos, exit_price, real_pnl, reason, dur)
            tg(msg_real_close(pos, exit_price, real_pnl, reason, dur))
            print(f"🔒 [{reason}] {sym} @ {fp(exit_price)} | Net P&L: ${real_pnl:+.2f}")
        else:
            trail_str = f"| Trailing Stop: {fp(pos['trailing_sl_price'])}" if pos.get("trailing_active") else ""
            print(f"  [AÇIK POZİSYON] {sym} | Fiyat: {fp(price)} | PnL: ${unrealized_pnl:+.2f} (Zirve: ${highest_pnl:+.2f}) | SL: {fp(pos['sl_price'])} {trail_str}")
            still.append(pos)
            
    state["positions"] = still
    return state

# ── TARAMA VE AI DOĞRULAMA ───────────────────────────────────────────────────

def scan(state, universe):
    current_balance = get_account_balance()
    max_allowed_positions = max(1, min(5, int(current_balance / 25.0)))
    
    if len(state.get("positions", [])) >= max_allowed_positions:
        return state
        
    btc_ok, btc_reason = check_btc_shield()
    if not btc_ok:
        print(f"🛡️ [BTC KALKANI] {btc_reason} — Yeni işlem kilitlendi.")
        return state

    open_syms = {p["sym"] for p in state.get("positions", [])}
    open_syms.update(PROTECTED_SYMBOLS)
    
    for i, (sym, _) in enumerate(universe):
        if sym in open_syms: continue
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r")
        try:
            sig = analyze_market_candidate(sym)
            if sig:
                print(f"\n🔍 [TEKNİK SİNYAL] {sym} ({sig['mode']})! Gemini 2.5 Flash analiz ediyor...")
                
                ai_ok, ai_conf, ai_reason = gemini_ai_validate(
                    sym=sym, mode=sig["mode"], rsi=sig["rsi"],
                    price=sig["entry"], btc_status=btc_reason,
                    last_candles_summary=sig.get("candles_summary", "")
                )
                
                if not ai_ok:
                    print(f"❌ [GEMINI RED] {sym} (%{ai_conf}) — {ai_reason}")
                    continue
                    
                print(f"✅ [GEMINI ONAY] {sym} (%{ai_conf})! Gerçek işlem açılıyor...")
                
                if REAL_TRADING:
                    pos = execute_real_entry(sym, available_balance=current_balance)
                else:
                    pos = {
                        "sym": sym, "side": "LONG", "entry": sig["entry"],
                        "qty": round(200.0 / sig["entry"], 2),
                        "notional_usd": 200.0, "sl_price": sig["entry"] * 0.993,
                        "dyn_sl_usd": 1.50, "dyn_tp_trigger_usd": 2.50,
                        "dyn_be_trigger_usd": 1.20, "dyn_trailing_drop_usd": 0.60,
                        "be_trigger_price": sig["entry"] * 1.008,
                        "be_sl_price": sig["entry"] * 1.001, "trailing_active": False,
                        "highest_profit_usd": 0.0, "trailing_sl_price": sig["entry"] * 0.993,
                        "opened_iso": utc().isoformat(), "opened_ts": ts(), "be_hit": False
                    }
                    
                state.setdefault("positions", []).append(pos)
                tg(msg_real_open(pos, sig, ai_conf, ai_reason))
                break
        except Exception as e:
            print(f"[İŞLEM AÇILIŞ HATA] {sym}: {e}")
            tg(f"⚠️ *İşlem Hatası ({sym}):* `{e}`")
        time.sleep(0.06)
        
    return state

# ── ANA DÖNGÜ ────────────────────────────────────────────────────────────────

def main():
    print("="*65)
    print("⚡ BİNANCE GERÇEK İŞLEM BOTU (GEMINI 2.5 FLASH & DİNAMİK ORANSAL KÂR)")
    print("="*65)
    print(" 🧠 Yapay Zeka         : Google Gemini 2.5 Flash Onay Motoru")
    print(" 💸 Dinamik Trailing   : Pozisyona orantılı (~%1.3) kâr hedefi ve limitsiz sürüş")
    print(" 🛑 Hızlı Stop Loss    : 1.5 saniyelik anlık izleme ile sıfır kaymalı çıkış")
    print(" 📈 Dinamik Kasa       : Bakiye azaldıkça/arttıkça pozisyon ve kâr orantılanır")
    print(" 🛡️ Koruma             : BASEDUSDT dokunulmaz | BTC Kalkanı Aktif")
    print("="*65)
    
    current_bal = get_account_balance()
    max_pos = max(1, min(5, int(current_bal / 25.0)))
    print(f"✅ Portföy Bakiyesi: ${current_bal:.2f} USDT (Eşzamanlı Kapasite: {max_pos} İşlem)")
    
    tg(f"🚀 *DİNAMİK ORANSAL KÂR & GEMINI AI BOT GÜNCELLENDİ!*\n\n"
       f"💰 *Güncel Kasa:* `${current_bal:.2f} USDT`\n"
       f"📈 *Dinamik Kâr Kuralı:* Pozisyon büyüklüğüne göre orantılı (~%1.3) kârda *Trailing Kâr Takibi* başlar.\n"
       f"🛑 *Stop Loss:* 1.5 saniyelik ultra hızlı takiple sıfır gecikme.\n"
       f"🛡️ *Korumalı:* `BASEDUSDT` dokunulmaz | BTC Kalkanı devrede.\n\n"
       f"📍 *Durum:* Canlı Piyasa Taraması Aktif ({utc().strftime('%H:%M:%S UTC')})")

    last_scan_time = 0
    while True:
        try:
            state = load_st()
            
            if state.get("positions"):
                state = monitor(state)
                save_st(state)
                time.sleep(1.5)
            else:
                now = time.time()
                if now - last_scan_time >= 20:
                    universe = get_universe()
                    state = scan(state, universe)
                    save_st(state)
                    last_scan_time = time.time()
                time.sleep(1.0)
                
        except Exception as e:
            print(f"[HATA] Ana Döngü: {e}")
            time.sleep(2.0)

if __name__ == "__main__":
    main()
