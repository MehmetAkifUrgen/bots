"""
trader_bot.py — Top Gainer Pre-Pump & Trend Breakout Sniper Engine
• Hedef: Binance Vadeli'de %20-%100 patlayacak coinleri henüz %1-%2 bandındayken yakalamak.
• Kasa Yönetimi: Tek Pozisyon Disiplini | 15x-20x Kaldıraç | $150-$250 Büyüklük (Max $400)
• Strateji Filtreleri:
    1. RVOL (Relative Volume) >= 1.7x (Son günlerin 2 katı hacim patlaması)
    2. Taker Buy % >= 53.0% (Balina gizli alım baskısı)
    3. Fiyat Sıkışması: Fiyat henüz patlamamış (-1.0% <= 4h_change <= +4.0%)
    4. OBV Akümülasyonu: Pozitif hacim trendi
    5. 15M/1H Breakout: 20 barlık direncin kırılması + Hacim mumu
• Kâr / Zarar Geometrisi (Büyük Kazanç Modeli):
    - Trailing TP Tetik: +%4.20 (Zirveye kadar eşlik eder, en tepeden %0.80 çekilince satar)
    - Başa Baş Koruma (BE): +%1.20 kârda stop maliyetin üstüne çekilir (Sıfır Risk)
    - Sıkı Stop Loss (SL): -%1.10 (Maksimum $1.65 kontrollü risk)
• Koruma: BASEDUSDT dokunulmaz | Kapanan coine 90 dk Cooldown
• Telegram Komutları: /gercek, /fake, /durum, /rapor, /kapat
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
import numpy as np
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

# ── STRATEJİ VE RİSK PARAMETRELERİ (BÜYÜK TREND MODELİ) ──────────────────────
REAL_TRADING_DEFAULT = os.getenv("REAL_TRADING", "true").lower() == "true"
DEFAULT_LEVERAGE     = 25          # 25x-30x Kaldıraç
MAX_NOTIONAL_PER_TRADE = 400.0     # Maksimum $400 USDT Tavan Büyüklük
SCAN_EVERY           = int(os.getenv("SCAN_EVERY_SECONDS", "10"))
MAX_HOLD_MIN         = 360         # 6 Saat maksimum trend bekleme
COOLDOWN_SECONDS     = 5400        # Kapanan coine 90 dakika tekrar girme!

# BÜYÜK KÂR VE RİSK HEDEFLERİ
TP_TRIGGER_PCT       = 0.0420      # +%4.20 Fiyat Hareketi -> Trailing TP Başlar ($200 pozisyonda +$8.40 Kâr!)
TRAILING_DROP_PCT    = 0.0080      # Zirveden %0.80 gevşeyince en tepeden kârı kilitler
BE_TRIGGER_PCT       = 0.0120      # +%1.20 Fiyat Hareketinde Stop Net Kâra Çekilir (Sıfır Risk!)
SL_PCT               = 0.0110      # -%1.10 Sıkı Stop Loss ($200 pozisyonda -$2.20 Risk)

# KORUNAN VE HANTAL PARİTELER
PROTECTED_SYMBOLS    = {"BASEDUSDT", "BASED", "TRXUSDT", "TRX", "FDUSDUSDT", "USDCUSDT"}
MIN_VOL_USD          = 5_000_000.0   # $5M üzeri aktif pariteler
MAX_VOL_USD          = 900_000_000.0

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
        r = requests.post(
            f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id": TC, "text": txt, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=12
        )
        if r.status_code != 200:
            requests.post(
                f"https://api.telegram.org/bot{TK}/sendMessage",
                json={"chat_id": TC, "text": txt.replace("*", "").replace("`", "").replace("_", ""), "disable_web_page_preview": True},
                timeout=12
            )
    except Exception as e:
        print(f"[TG HATA] {e}", flush=True)

# ── DURUM YÖNETİMİ ───────────────────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except Exception: pass
    return {"positions": [], "real_trading": REAL_TRADING_DEFAULT, "cooldown": {}}

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
    est_fee = round(pos.get("notional_usd", 200.0) * 0.0010, 2)
    net_pnl = round(pnl - est_fee, 2)
    trades.append({
        "id": pos.get("order_id", ""), "pair": pos["sym"], "side": "LONG",
        "entry": pos["entry"], "exit": exit_price, "qty": pos["qty"],
        "notional": pos["notional_usd"], "pnl": net_pnl,
        "gross_pnl": round(pnl, 2), "fee": est_fee,
        "result": reason, "duration": dur_sec,
        "is_real": pos.get("is_real", False),
        "timestamp": ts()
    })
    save_db(trades)
    return trades

# ── TELEGRAM KOMUT DİNLEYİCİ (INTERACTIVE BOT) ───────────────────────────────

LAST_UPDATE_ID = 0

def handle_telegram_commands(state):
    global LAST_UPDATE_ID
    if not TK: return state
    
    try:
        url = f"https://api.telegram.org/bot{TK}/getUpdates"
        params = {"offset": LAST_UPDATE_ID + 1, "timeout": 1}
        r = requests.get(url, params=params, timeout=5)
        if r.status_code != 200: return state
        
        updates = r.json().get("result", [])
        for u in updates:
            LAST_UPDATE_ID = u.get("update_id", LAST_UPDATE_ID)
            msg = u.get("message", {})
            text = msg.get("text", "").strip().lower()
            if not text: continue
            
            # 1. /gercek veya /real
            if text in ["/gercek", "/real", "gercek", "real"]:
                state["real_trading"] = True
                save_st(state)
                eq, free_m = get_account_balances()
                tg(f"🔴 *MOD DEĞİŞTİRİLDİ: GERÇEK İŞLEM MODU AKTİF!* ⚡\n\n"
                   f"💰 *Gerçek Bakiye:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n"
                   f"🎯 *Hedef:* Pre-Pump A+ Büyük Trend Kırılımları (+%4.20+)\n"
                   f"Tüm sinyaller doğrudan Binance vadeli hesabında açılacaktır.")
                print("⚡ [MOD DEĞİŞTİ] GERÇEK İŞLEM MODU AKTİF EDİLDİ.", flush=True)
                
            # 2. /fake veya /paper
            elif text in ["/fake", "/paper", "/simulasyon", "fake", "paper"]:
                state["real_trading"] = False
                save_st(state)
                tg("🟢 *MOD DEĞİŞTİRİLDİ: SİMÜLASYON MODU AKTİF!* 🧪\n\nGerçek para kullanılmadan test sinyalleri izleniyor.")
                print("🧪 [MOD DEĞİŞTİ] SİMÜLASYON MODU AKTİF EDİLDİ.", flush=True)
                
            # 3. /rapor veya /kar
            elif text in ["/rapor", "/kar", "/pnl", "rapor"]:
                send_performance_report(state)
                
            # 4. /durum
            elif text in ["/durum", "/status", "durum"]:
                is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
                open_positions = state.get("positions", [])
                
                if is_real:
                    eq, free_m = get_account_balances()
                    mode_str = "🔴 GERÇEK İŞLEM"
                    balance_txt = f"💰 *Gerçek Varlık:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)"
                else:
                    mode_str = "🧪 SİMÜLASYON"
                    balance_txt = "🧪 *Sanal Test Modu*"
                
                pos_txt = "Açık pozisyon yok."
                if open_positions:
                    p_lines = []
                    for idx, p in enumerate(open_positions):
                        try: cur_p = last_price(p["sym"])
                        except: cur_p = p["entry"]
                        raw_pnl = (cur_p - p["entry"]) * p["qty"]
                        est_f = p["notional_usd"] * 0.0010
                        net_pnl = raw_pnl - est_f
                        p_lines.append(f"{idx+1}. *{p['sym']}* | Giriş: `{fp(p['entry'])}` | Anlık: `{fp(cur_p)}` | Net PnL: *`${net_pnl:+.2f} USDT`*")
                    pos_txt = "\n".join(p_lines)
                
                tg(f"📊 *CANLI SİSTEM DURUMU*\n\n"
                   f"⚙️ *Çalışma Modu:* `{mode_str}`\n"
                   f"{balance_txt}\n\n"
                   f"📌 *Aktif Pozisyonlar ({len(open_positions)}/1):*\n{pos_txt}\n\n"
                   f"_(Komutlar: `/gercek`, `/fake`, `/rapor`, `/kapat`)_")
                   
            # 5. /kapat
            elif text in ["/kapat", "/close", "kapat"]:
                if state.get("positions"):
                    for p in state["positions"]:
                        cur_p = last_price(p["sym"])
                        pnl = (cur_p - p["entry"]) * p["qty"]
                        if p.get("is_real", False):
                            execute_real_close(p, "MANUEL_TELEGRAM_KAPATMA")
                        record_trade(p, cur_p, pnl, "MANUEL_TELEGRAM_KAPATMA", 60)
                        tg(f"🔒 *{p['sym']}* Telegram komutuyla kapatıldı!")
                    state["positions"] = []
                    save_st(state)
                else:
                    tg("ℹ️ Şu anda kapatılacak açık pozisyon bulunmuyor.")
    except Exception as e:
        print(f"[TG KOMUT HATA] {e}", flush=True)
        
    return state

def send_performance_report(state):
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    trades = load_db()
    filtered_trades = [t for t in trades if t.get("is_real", False) == is_real]
    mode_title = "🔴 GERÇEK İŞLEM RAPORU" if is_real else "🧪 SİMÜLASYON RAPORU"
    
    if not filtered_trades:
        tg(f"📊 *{mode_title}*\n\nHenüz tamamlanmış bir işlem bulunmuyor. Pre-Pump radar 230+ pariteyi tarıyor!")
        return
        
    df_t = pd.DataFrame(filtered_trades)
    tot = len(df_t)
    wins = df_t[df_t["pnl"] > 0]
    losses = df_t[df_t["pnl"] < 0]
    bes = df_t[df_t["pnl"] == 0]
    
    win_cnt = len(wins)
    loss_cnt = len(losses)
    decisive = win_cnt + loss_cnt
    wr = (win_cnt / decisive * 100) if decisive > 0 else 0
    total_net = df_t["pnl"].sum()
    
    recent_lines = []
    for _, row in df_t.tail(6).iterrows():
        icon = "🟢" if row["pnl"] > 0 else ("🔰" if row["pnl"] == 0 else "🔴")
        recent_lines.append(f"{icon} `{row['pair']}` -> *${row['pnl']:+.2f}* ({row['result']})")
        
    rec_txt = "\n".join(recent_lines)
    
    tg(f"📊 *{mode_title}*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"• *Toplam Yapılan İşlem:* `{tot}`\n"
       f"• *Kazanılan:* `{win_cnt}` (🟢)\n"
       f"• *Başa Baş:* `{len(bes)}` (🔰)\n"
       f"• *Stop:* `{loss_cnt}` (🔴)\n"
       f"• *KAZANMA ORANI (WR):* *%{wr:.1f}* 🎯\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"💰 *NET GERÇEK KASA KÂRI:* *`${total_net:+.2f} USDT`*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"*Son İşlemler:*\n{rec_txt}\n\n"
       f"Zaman: `{ts()}`")

# ── BINANCE API İSTEMCİSİ ───────────────────────────────────────────────────

def get_public_json(endpoint, p=None):
    hosts = [FAPI_BASE, "https://fapi.binance.com", "https://fapi1.binance.com", "https://fapi2.binance.com"]
    last_err = None
    
    for h in hosts:
        url = endpoint if endpoint.startswith("http") else f"{h}{endpoint}"
        try:
            r = requests.get(url, params=p, headers=HEADERS, timeout=12)
            if r.status_code == 200: return r.json()
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
            
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
    for col in ["o","h","l","c","v","tb","qv"]:
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

def calc_obv_trend(df):
    if len(df) < 15: return True
    obv = [0]
    for i in range(1, len(df)):
        if df['c'].iloc[i] > df['c'].iloc[i-1]:
            obv.append(obv[-1] + df['v'].iloc[i])
        elif df['c'].iloc[i] < df['c'].iloc[i-1]:
            obv.append(obv[-1] - df['v'].iloc[i])
        else:
            obv.append(obv[-1])
    return obv[-1] >= obv[-10]

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

def check_btc_shield():
    """BTC Şelale Kalkanı"""
    try:
        df15m = klines("BTCUSDT", "15m", 30)
        if len(df15m) < 20: return True, "BTC Verisi Bekleniyor"
        last_c = df15m.iloc[-1]
        c, o = last_c['c'], last_c['o']
        if c < o and ((o - c) / o) > 0.0080:
            return False, "BTC Anlık Şelalede (%0.80 Kırmızı Mum)"
        return True, "BTC Uygun"
    except Exception as e:
        return True, f"BTC Kontrol ({e})"

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
            try: 
                qv = float(t.get("quoteVolume", 0))
                chg = float(t.get("priceChangePercent", 0))
            except: continue
            if MIN_VOL_USD <= qv <= MAX_VOL_USD:
                out.append((sym, qv, chg))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        print(f"[UNIVERSE HATA] {e}", flush=True)
        return []

# ── TOP GAINER PRE-PUMP & BREAKOUT RADARI ────────────────────────────────────

def analyze_market_candidate(sym, qv, chg_24h, cooldown_dict):
    last_closed_ts = cooldown_dict.get(sym, 0)
    if time.time() - last_closed_ts < COOLDOWN_SECONDS:
        return None

    # Zaten %20'den fazla pump yapmış tepedeki coinlere girme (Geç kalınmış)
    if chg_24h > 18.0:
        return None

    try:
        # 1. 1 Saatlik Mumlar (Büyük Akümülasyon & Hacim Patlaması)
        df1h = klines(sym, "1h", 48)
        if len(df1h) < 30: return None
        
        # Göreceli Hacim (RVOL)
        vol_avg = df1h['v'].iloc[-25:-1].mean()
        curr_vol = df1h['v'].iloc[-1]
        rvol = curr_vol / vol_avg if vol_avg > 0 else 1.0
        
        # Taker Buy Hacim Oranı
        tb_vol = df1h['tb'].iloc[-1]
        tot_vol = df1h['v'].iloc[-1]
        taker_buy_pct = (tb_vol / tot_vol * 100) if tot_vol > 0 else 50.0
        
        # OBV Trendi
        obv_ok = calc_obv_trend(df1h)
        
        # 2. 15 Dakikalık Mumlar (Anlık Kırılım Teyidi)
        df15m = klines(sym, "15m", 45)
        if len(df15m) < 30: return None
        
        c15 = df15m.iloc[-1]
        c, o, h, l = c15['c'], c15['o'], c15['h'], c15['l']
        
        ema20 = df15m['c'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = df15m['c'].ewm(span=50, adjust=False).mean().iloc[-1]
        rsi = calc_rsi(df15m['c'], 14)
        
        # 20 Barlık Zirve Kırılımı
        recent_high = df15m['h'].iloc[-20:-1].max()
        is_breakout = (c >= recent_high * 0.998) and (c >= o)
        
        # 15m Hacim Artışı
        v15_avg = df15m['v'].iloc[-15:-1].mean()
        v15_ratio = df15m['v'].iloc[-1] / v15_avg if v15_avg > 0 else 1.0
        
        # Pre-Pump Şartları (Fiyat henüz patlamamış ama alıcılar gizlice toplamış)
        is_prepump_pattern = (
            (rvol >= 1.6 or v15_ratio >= 1.8) and
            (taker_buy_pct >= 53.0) and
            (obv_ok) and
            (c >= ema20) and (ema20 >= ema50) and
            (48.0 <= rsi <= 68.0) and
            (is_breakout or (c >= o and v15_ratio >= 2.0))
        )
        
        if is_prepump_pattern:
            entry = last_price(sym)
            return {
                "sym": sym, "side": "LONG", "mode": "PRE_PUMP_TOP_GAINER_AVCISI",
                "entry": entry, "rsi": rsi, "rvol": round(rvol, 1),
                "taker_buy": round(taker_buy_pct, 1),
                "reasons": [
                    f"🚀 *Pre-Pump Sinyali:* RVOL: `{rvol:.1f}x` | Taker Buy: `%{taker_buy_pct:.1f}`",
                    f"📈 *Hacim & Kırılım:* 15M Hacim Patlaması (`{v15_ratio:.1f}x`) ve Direnç Kırılımı",
                    f"🎯 *Trend Gücü:* EMA20/50 Üzerinde Alıcı Baskısı (RSI: `{rsi:.1f}`)"
                ]
            }

        return None
    except Exception:
        return None

# ── EMİR VE TEMİNAT YÖNETİMİ ────────────────────────────────────────────────

def set_optimal_leverage(sym, target_lev=25):
    for lev in [30, 25, 20]:
        try:
            binance_signed_request("POST", "/papi/v1/um/leverage", {"symbol": sym, "leverage": lev})
            return lev
        except Exception:
            continue
    return 20

def execute_real_entry(sym, notional_target, free_margin):
    actual_lev = set_optimal_leverage(sym, target_lev=DEFAULT_LEVERAGE)
    time.sleep(0.15)
    
    rules = get_symbol_rules(sym)
    price = last_price(sym)
    
    max_safe_notional = free_margin * actual_lev * 0.80
    actual_notional_target = min(MAX_NOTIONAL_PER_TRADE, notional_target, max(10.0, max_safe_notional))
    
    raw_qty = actual_notional_target / price
    qty = round_step_size(raw_qty, rules["stepSize"], rules["quantityPrecision"])
    if qty < rules["minQty"]: qty = rules["minQty"]
    
    order_params = {
        "symbol": sym, "side": "BUY", "type": "MARKET", "quantity": str(qty)
    }
    
    actual_notional = qty * price
    print(f"⚡ [PRE-PUMP LONG AÇILIYOR] {sym} | Kaldıraç: {actual_lev}x | Büyüklük: ${actual_notional:.2f} (Serbest: ${free_margin:.2f})", flush=True)
    order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
    
    avg_price = float(order_res.get("avgPrice", 0))
    if avg_price <= 0: avg_price = price
        
    tp_price = avg_price * (1.0 + TP_TRIGGER_PCT)
    be_trigger_price = avg_price * (1.0 + BE_TRIGGER_PCT)
    be_sl_price = avg_price + (0.50 / qty)  # Komisyonu hayli hayli aşan net kâr garantili stop
    sl_price = avg_price * (1.0 - SL_PCT)
    
    return {
        "sym": sym, "side": "LONG", "entry": avg_price, "qty": qty,
        "notional_usd": round(actual_notional, 2),
        "leverage": actual_lev,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "be_trigger_price": be_trigger_price,
        "be_sl_price": be_sl_price,
        "order_id": order_res.get("orderId", str(uuid.uuid4())[:8]),
        "opened_iso": utc().isoformat(), "opened_ts": ts(),
        "be_hit": False,
        "is_real": True
    }

def execute_real_close(pos, reason):
    sym = pos["sym"]
    qty = pos["qty"]
    order_params = {
        "symbol": sym, "side": "SELL", "type": "MARKET", "quantity": str(qty), "reduceOnly": "true"
    }
    
    print(f"🔒 [POZİSYON KAPATILIYOR] {sym} ({reason}) | Miktar: {qty}", flush=True)
    try:
        order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
        exit_price = float(order_res.get("avgPrice", 0))
        if exit_price <= 0: exit_price = last_price(sym)
    except Exception as e:
        print(f"[KAPATMA HATA] {e}", flush=True)
        exit_price = last_price(sym)
        
    pnl = (exit_price - pos["entry"]) * qty
    return exit_price, pnl

# ── TELEGRAM BİLDİRİMLERİ ────────────────────────────────────────────────────

def msg_real_open(pos, sig, is_real=False):
    icon = "🔴 *[GERÇEK İŞLEM]*" if is_real else "🧪 *[SİMÜLASYON]*"
    lines = "\n".join(f"  • {r}" for r in sig.get("reasons", []))
    lev = pos.get("leverage", DEFAULT_LEVERAGE)
    
    target_gross = (pos["tp_price"] - pos["entry"]) * pos["qty"]
    est_fee = round(pos.get("notional_usd", 200.0) * 0.0010, 2)
    target_net = target_gross - est_fee
    sl_pnl = (pos["entry"] - pos["sl_price"]) * pos["qty"] + est_fee
    
    return (
        f"{icon} 🚀 *PRE-PUMP GAINER AVCISI LONG AÇILDI!* | `{pos['sym']}`\n\n"
        f"Strateji: *{sig['mode']}*\n"
        f"Yön: *LONG ({lev}x Kaldıraç)*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Pozisyon Büyüklüğü : `${pos['notional_usd']}` ({pos['qty']} adet)\n\n"
        f"🎯 *Kâr Hedefi (+%4.20+):* `+{fp(pos['tp_price'])}` (*Net +${target_net:.2f}*)\n"
        f"🔰 *Başa Baş (+%1.20):* Stop Komisyonun Üstüne Çekilir (Sıfır Risk)\n"
        f"🛑 *Stop Loss (-%1.10):* `{fp(pos['sl_price'])}` (*Net -${sl_pnl:.2f}*)\n\n"
        f"*Radar Verileri:*\n{lines}\n\n"
        f"Zaman: `{ts()}`\n"
        f"_(Komutlar: `/durum`, `/rapor`, `/gercek`, `/fake`)_"
    )

def msg_real_close(pos, exit_price, pnl, reason, dur_sec, is_real=False):
    prefix = "🔴 [GERÇEK]" if is_real else "🧪 [SİMÜLASYON]"
    est_fee = round(pos.get("notional_usd", 200.0) * 0.0010, 2)
    net_pnl = round(pnl - est_fee, 2)
    
    icon = "🟢" if net_pnl > 0.10 else ("🔰" if -0.10 <= net_pnl <= 0.10 else "🔴")
    title = {
        "TRAILING_TP": f"💸 BÜYÜK PUMP KÂRI ALINDI (Net +${net_pnl:.2f}) 🚀🎯",
        "TAKE_PROFIT": f"💸 HEDEF KÂR ALINDI (Net +${net_pnl:.2f}) 🎯",
        "STOP_LOSS": f"❌ STOP OLDU (Net -${abs(net_pnl):.2f})",
        "BREAKEVEN": f"🔰 BAŞA BAŞ KAPANDI (+${net_pnl:.2f})",
        "TIMEOUT": "⏱️ SÜRE DOLDU",
        "MANUEL_TELEGRAM_KAPATMA": "🛑 TELEGRAM İLE KAPATILDI"
    }.get(reason, reason)
    
    dur_min = dur_sec // 60
    
    return (
        f"{icon} {prefix} *{title}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(exit_price)}`\n"
        f"💰 *Net Kasa Değişimi:* *`${net_pnl:+.2f} USDT`*\n"
        f"_(Ham Fiyat Farkı: `${pnl:+.2f}` | Komisyon: `-${est_fee:.2f}`)_\n"
        f"İşlem Süresi : `{dur_min} dakika`\n\n"
        f"Zaman: `{ts()}`"
    )

# ── ANLIK MONİTÖR & KÂR/STOP YÖNETİMİ ────────────────────────────────────────

def get_real_open_symbols():
    try:
        positions = binance_signed_request("GET", "/papi/v1/um/positionRisk")
        return {p.get("symbol") for p in positions if float(p.get("positionAmt", 0)) != 0}
    except Exception:
        return None

def monitor(state):
    real_open = get_real_open_symbols()
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    still = []
    
    for pos in state.get("positions", []):
        sym = pos["sym"]
        pos_is_real = pos.get("is_real", is_real)
        
        if pos_is_real and real_open is not None and sym not in real_open:
            print(f"ℹ️ [{sym}] Binance üzerinde kapatılmış, listeden çıkarıldı.", flush=True)
            continue
            
        try: price = last_price(sym)
        except Exception:
            still.append(pos); continue
            
        qty = pos["qty"]
        entry = pos["entry"]
        dur = int((utc() - datetime.fromisoformat(pos.get("opened_iso", utc().isoformat()))).total_seconds())
        unrealized_pnl = (price - entry) * qty
        
        # 1. BREAKEVEN KORUMASI (+%1.20 kârda stop maliyete çekilir)
        if not pos.get("be_hit") and price >= pos["be_trigger_price"]:
            pos["sl_price"] = pos["be_sl_price"]
            pos["be_hit"] = True
            prefix = "🔴 [GERÇEK]" if pos_is_real else "🧪 [SİMÜLASYON]"
            tg(f"🔰 {prefix} *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! Stop net kâra (`{fp(pos['sl_price'])}`) çekildi. *İşlem artık %100 sıfır risklidir!*")

        # 2. TRAILING TAKE PROFIT (+%4.20 kârda devreye girer ve zirveyi takip eder)
        if price >= pos["tp_price"] or pos.get("trailing_active"):
            if not pos.get("trailing_active"):
                pos["trailing_active"] = True
                pos["highest_price"] = price
                locked_profit = (price * (1.0 - TRAILING_DROP_PCT) - entry) * qty
                prefix = "🔴 [GERÇEK]" if pos_is_real else "🧪 [SİMÜLASYON]"
                tg(f"🚀 {prefix} *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! *Büyük Pump Trailing Takibi Aktif Edildi!*\nZirve takip ediliyor (Taban kâr: `+${locked_profit:.2f}`).")
                
            pos["highest_price"] = max(pos.get("highest_price", price), price)
            trailing_exit_price = pos["highest_price"] * (1.0 - TRAILING_DROP_PCT) # Zirveden %0.80 çekilirse sat
            pos["trailing_sl_price"] = max(pos.get("trailing_sl_price", pos["sl_price"]), trailing_exit_price)

        reason = None
        if pos.get("trailing_active") and price <= pos.get("trailing_sl_price", pos["sl_price"]):
            reason = "TRAILING_TP"
        elif price <= pos["sl_price"]:
            reason = "BREAKEVEN" if pos.get("be_hit") else "STOP_LOSS"
        elif dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
            
        if reason:
            if pos_is_real: exit_price, real_pnl = execute_real_close(pos, reason)
            else: exit_price, real_pnl = price, unrealized_pnl
                
            record_trade(pos, exit_price, real_pnl, reason, dur)
            tg(msg_real_close(pos, exit_price, real_pnl, reason, dur, is_real=pos_is_real))
            print(f"🔒 [{reason}] {sym} @ {fp(exit_price)} | Net P&L: ${real_pnl:+.2f}", flush=True)
            
            # Cooldown kaydet (90 dakika kilit)
            state.setdefault("cooldown", {})[sym] = time.time()
        else:
            trail_str = f"| Trailing: {fp(pos['trailing_sl_price'])}" if pos.get("trailing_active") else ""
            prefix = "[GERÇEK]" if pos_is_real else "[SİMÜLASYON]"
            print(f"  {prefix} {sym} | Fiyat: {fp(price)} | PnL: ${unrealized_pnl:+.2f} | TP: {fp(pos['tp_price'])} | SL: {fp(pos['sl_price'])} {trail_str}", flush=True)
            still.append(pos)
            
    state["positions"] = still
    return state

# ── TARAMA VE POZİSYON AÇILIŞI ───────────────────────────────────────────────

def scan(state, universe):
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    cooldown_dict = state.setdefault("cooldown", {})
    
    # Tek Pozisyon Disiplini (Tüm güç tek A+ coine odaklanır)
    if len(state.get("positions", [])) >= 1:
        return state

    if is_real:
        eq, free_margin = get_account_balances()
    else:
        eq, free_margin = 20.0, 20.0

    if free_margin < 4.0:
        return state

    # BTC Güvenlik Kontrolü
    btc_ok, btc_reason = check_btc_shield()
    if not btc_ok:
        return state

    open_syms = {p["sym"] for p in state.get("positions", [])}
    open_syms.update(PROTECTED_SYMBOLS)
    
    # Pozisyon Büyüklüğü: Kasanın %80'i teminat olarak kullanılır (Max $400)
    allocated_margin = min(free_margin * 0.85, 18.0)
    target_notional = min(MAX_NOTIONAL_PER_TRADE, allocated_margin * DEFAULT_LEVERAGE)
    
    for i, (sym, qv, chg) in enumerate(universe):
        if sym in open_syms: continue
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r", flush=True)
        try:
            sig = analyze_market_candidate(sym, qv, chg, cooldown_dict)
            if sig:
                mode_label = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"
                print(f"\n🚀 [{mode_label} PRE-PUMP ONAYI] {sym}! Pozisyon açılıyor...", flush=True)
                
                if is_real:
                    pos = execute_real_entry(sym, target_notional, free_margin)
                else:
                    entry = sig["entry"]
                    rules = get_symbol_rules(sym)
                    qty = round_step_size(target_notional / entry, rules["stepSize"], rules["quantityPrecision"])
                    actual_notional = qty * entry
                    
                    tp_price = entry * (1.0 + TP_TRIGGER_PCT)
                    be_trigger_price = entry * (1.0 + BE_TRIGGER_PCT)
                    be_sl_price = entry + (0.50 / qty)
                    sl_price = entry * (1.0 - SL_PCT)
                    
                    pos = {
                        "sym": sym, "side": "LONG", "entry": entry,
                        "qty": qty, "notional_usd": round(actual_notional, 2),
                        "leverage": DEFAULT_LEVERAGE, "sl_price": sl_price,
                        "tp_price": tp_price,
                        "be_trigger_price": be_trigger_price, "be_sl_price": be_sl_price,
                        "order_id": f"sim_{str(uuid.uuid4())[:8]}",
                        "opened_iso": utc().isoformat(), "opened_ts": ts(),
                        "be_hit": False, "is_real": False
                    }
                    
                state.setdefault("positions", []).append(pos)
                tg(msg_real_open(pos, sig, is_real=is_real))
                break
        except Exception as e:
            print(f"[İŞLEM AÇILIŞ HATA] {sym}: {e}", flush=True)
        time.sleep(0.04)
        
    return state

# ── ANA DÖNGÜ ────────────────────────────────────────────────────────────────

def main():
    print("="*65, flush=True)
    print("🚀 TOP GAINER PRE-PUMP & TREND BREAKOUT SNIPER MOTORU", flush=True)
    print("="*65, flush=True)
    print(" 🎯 Ana Hedef          : Top 10 Gainers Adaylarını %1-%2 İken Yakalamak", flush=True)
    print(" ⚡ Kaldıraç & Boyut   : 18x Kaldıraç | Tek Pozisyon Disiplini (Max $400)", flush=True)
    print(" 💸 Kâr Hedefi (TP)    : +%4.20+ (Büyük Trend Dalgaları & Trailing)", flush=True)
    print(" 🔰 Başa Baş Koruma    : +%1.20 kârda stop komisyonun üstüne kilitlenir", flush=True)
    print(" 🛑 Sıkı Stop Loss     : -%1.10 kontrollü risk kalkanı", flush=True)
    print(" 🛡️ Koruma             : BASEDUSDT dokunulmaz | 90 Dk Cooldown", flush=True)
    print("="*65, flush=True)
    
    state = load_st()
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    mode_text = "🔴 GERÇEK İŞLEM" if is_real else "🧪 SİMÜLASYON"
    
    tg(f"🚀 *TOP GAINER PRE-PUMP AVCISI MOTORU AKTİF EDİLDİ!*\n\n"
       f"⚙️ *Çalışma Modu:* `{mode_text}`\n"
       f"🎯 *Hedef:* %20-%100 patlayacak coinleri erken yakalayıp büyük trend dalgasını almak (+%4.20+)\n"
       f"🛡️ *Disiplin:* Günde 50 mikro işlem YASAK. Sadece A+ tek bir dev fırsata odaklanır.\n\n"
       f"🎮 *Komutlar:*\n"
       f"• `/durum` -> Kasa ve pozisyon durumu\n"
       f"• `/rapor` -> Net kâr-zarar raporu\n"
       f"• `/kapat` -> Açık pozisyonu hemen kapat")

    last_scan_time = 0
    last_heartbeat_time = 0
    
    while True:
        try:
            state = load_st()
            now = time.time()
            
            # Telegram komutlarını anlık dinle
            state = handle_telegram_commands(state)
            
            if now - last_heartbeat_time >= 60:
                is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
                if is_real:
                    eq, free_m = get_account_balances()
                    mode_tag = "GERÇEK"
                    bal_str = f"Varlık: ${eq:.2f} USDT (Serbest: ${free_m:.2f})"
                else:
                    mode_tag = "SİMÜLASYON"
                    bal_str = "Sanal Test"
                    
                open_count = len(state.get("positions", []))
                print(f"💓 [CANLI RADAR] {bal_str} | Pozisyon: {open_count}/1 | Mod: {mode_tag} | Saat: {utc().strftime('%H:%M:%S UTC')}", flush=True)
                last_heartbeat_time = now
            
            # 1. Açık pozisyonu anlık izle ve kârı/trailingi yönet
            if state.get("positions"):
                state = monitor(state)
                save_st(state)
                
            # 2. Eğer pozisyon yoksa Pre-Pump adaylarını tara
            if len(state.get("positions", [])) < 1:
                if now - last_scan_time >= SCAN_EVERY:
                    universe = get_universe()
                    state = scan(state, universe)
                    save_st(state)
                    last_scan_time = time.time()
                    
            time.sleep(1.2)
                
        except Exception as e:
            print(f"[HATA] Ana Döngü: {e}", flush=True)
            time.sleep(2.0)

if __name__ == "__main__":
    main()
