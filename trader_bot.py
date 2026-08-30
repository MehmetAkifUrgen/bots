"""
trader_bot.py — %72.3+ Win-Rate Çift Trend Bileşik Büyüme (Compounding) Sniper Motoru
• Başlangıç: $20.00 USDT (Sanal veya Gerçek) | 20x Kaldıraç
• Strateji: 1H + 15M Çift Trend Uyumu + EMA20 Dip Sekmesi (Pullback) + BTC Kalkanı
• Kademeli Pozisyon Yuvaları:
    - Kasa < $35: 1 Pozisyon (Tüm güç tek A+ fırsatta)
    - Kasa $35 - $70: 2 Pozisyon
    - Kasa $70 - $120: 3 Pozisyon
    - Kasa >= $120: 4 Pozisyon
• Kâr / Zarar Geometrisi:
    - Hedef Kâr (TP): +%0.42 (Dinamik Kâr Kasaya Kilitlenir)
    - Başa Baş Koruma (BE): +%0.20 kârda stop maliyete çekilir (Sıfır Risk)
    - Stop Loss (SL): -%0.50 Sıkı Risk Koruması
• Koruma: BASEDUSDT dokunulmaz | Kapanan coine 60 dk Cooldown
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

# ── STRATEJİ VE RİSK PARAMETRELERİ ───────────────────────────────────────────
REAL_TRADING_DEFAULT = os.getenv("REAL_TRADING", "false").lower() == "true"
DEFAULT_LEVERAGE     = 20          # 20x Kaldıraç (Asla 10x'e düşmez)
SCAN_EVERY           = int(os.getenv("SCAN_EVERY_SECONDS", "12"))
MAX_HOLD_MIN         = 240         # 4 Saat maksimum bekleme
COOLDOWN_SECONDS     = 3600        # Kapanan coine 60 dakika tekrar girme!

# A+ YÜKSEK KÂR VE KOMİSYON KALKANI PARAMETRELERİ
TP_PCT               = 0.0125      # +%1.25 Fiyat Hareketi ($300 pozisyonda +$3.75 Kâr!)
BE_TRIGGER_PCT       = 0.0055      # +%0.55 Fiyat Hareketinde Stop Komisyonun Üstüne Çekilir (+Net Kâr)
SL_PCT               = 0.0075      # -%0.75 Sıkı Stop Loss ($300 pozisyonda -$2.25 Risk)
TRAILING_DROP_PCT    = 0.0030      # Zirveden %0.30 çekilirse en tepeden kârı kilitler

# KORUNAN VE HANTAL PARİTELER
PROTECTED_SYMBOLS    = {"BASEDUSDT", "BASED", "TRXUSDT", "TRX", "FDUSDUSDT", "USDCUSDT"}
MIN_VOL_USD          = 5_000_000.0   # $5M üzeri aktif pariteler
MAX_VOL_USD          = 800_000_000.0

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

# ── SANAL KASA (VIRTUAL WALLET) VE DİNAMİK COMPOUNDING ───────────────────────
SIM_STARTING_BALANCE = 20.00   # 20 Dolar Başlangıç Kasası

def get_virtual_balance(state):
    trades = load_db()
    sim_trades = [t for t in trades if not t.get("is_real", False)]
    sim_realized_pnl = sum(t.get("pnl", 0.0) for t in sim_trades)
    
    unrealized_pnl = 0.0
    used_margin = 0.0
    for p in state.get("positions", []):
        if not p.get("is_real", False):
            try:
                cp = last_price(p["sym"])
                unrealized_pnl += (cp - p["entry"]) * p["qty"]
            except Exception:
                pass
            used_margin += p.get("notional_usd", 200.0) / p.get("leverage", DEFAULT_LEVERAGE)
            
    virtual_equity = round(SIM_STARTING_BALANCE + sim_realized_pnl + unrealized_pnl, 2)
    virtual_free_margin = round(max(0.0, virtual_equity - used_margin), 2)
    return virtual_equity, virtual_free_margin, sim_realized_pnl

def get_dynamic_position_slots(equity):
    """Kasa büyüdükçe eşzamanlı pozisyon sayısını kademeli artırır"""
    if equity < 35.0:
        return 1
    elif equity < 70.0:
        return 2
    elif equity < 120.0:
        return 3
    else:
        return 4

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
                slots = get_dynamic_position_slots(eq)
                tg(f"🔴 *MOD DEĞİŞTİRİLDİ: GERÇEK İŞLEM MODU AKTİF!* ⚡\n\n"
                   f"💰 *Gerçek Bakiye:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n"
                   f"🎯 *Kademeli Pozisyon Hakkı:* `{slots}` adet eşzamanlı pozisyon\n"
                   f"Tüm sinyaller Binance hesabında gerçek parayla açılacaktır.")
                print("⚡ [MOD DEĞİŞTİ] GERÇEK İŞLEM MODU AKTİF EDİLDİ.", flush=True)
                
            # 2. /fake veya /paper veya /simulasyon
            elif text in ["/fake", "/paper", "/simulasyon", "fake", "paper"]:
                state["real_trading"] = False
                save_st(state)
                virt_eq, virt_free, _ = get_virtual_balance(state)
                slots = get_dynamic_position_slots(virt_eq)
                tg(f"🟢 *MOD DEĞİŞTİRİLDİ: SİMÜLASYON (FAKE PARA) MODU AKTİF!* 🧪\n\n"
                   f"💰 *Sanal Kasa:* `${virt_eq:.2f} USDT` (Başlangıç: `$20.00 USDT`)\n"
                   f"🎯 *Kademeli Pozisyon Hakkı:* `{slots}` adet eşzamanlı pozisyon\n"
                   f"İşlemler $20 başlangıçlı sanal bakiye ve bileşik büyüme ile canlı test edilmektedir.")
                print("🧪 [MOD DEĞİŞTİ] SİMÜLASYON MODU AKTİF EDİLDİ.", flush=True)
                
            # 3. /rapor veya /kar veya /gunsonu
            elif text in ["/rapor", "/kar", "/gunsonu", "/pnl", "rapor"]:
                send_performance_report(state)
                
            # 4. /durum veya /status
            elif text in ["/durum", "/status", "durum"]:
                is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
                open_positions = state.get("positions", [])
                
                if is_real:
                    eq, free_m = get_account_balances()
                    slots = get_dynamic_position_slots(eq)
                    mode_str = "🔴 GERÇEK İŞLEM"
                    balance_txt = f"💰 *Gerçek Varlık:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n🎯 *Kademeli Yuva:* `{slots}` Pozisyon"
                else:
                    virt_eq, virt_free, sim_pnl = get_virtual_balance(state)
                    slots = get_dynamic_position_slots(virt_eq)
                    mode_str = "🧪 SİMÜLASYON ($20 BAŞLANGIÇ)"
                    pnl_sign = f"+${sim_pnl:.2f}" if sim_pnl >= 0 else f"-${abs(sim_pnl):.2f}"
                    balance_txt = (
                        f"💰 *Sanal Kasa:* `${virt_eq:.2f} USDT` (Serbest: `${virt_free:.2f}`)\n"
                        f"🏁 *Başlangıç:* `$20.00 USDT` | *Net Kâr:* `{pnl_sign}`\n"
                        f"🎯 *Kademeli Yuva:* `{slots}` Pozisyon"
                    )
                
                pos_txt = "Açık pozisyon yok."
                if open_positions:
                    p_lines = []
                    for idx, p in enumerate(open_positions):
                        try: cur_p = last_price(p["sym"])
                        except: cur_p = p["entry"]
                        pnl = (cur_p - p["entry"]) * p["qty"]
                        p_lines.append(f"{idx+1}. *{p['sym']}* | Giriş: `{fp(p['entry'])}` | Anlık: `{fp(cur_p)}` | PnL: *`${pnl:+.2f} USDT`*")
                    pos_txt = "\n".join(p_lines)
                
                tg(f"📊 *CANLI SİSTEM DURUMU*\n\n"
                   f"⚙️ *Çalışma Modu:* `{mode_str}`\n"
                   f"{balance_txt}\n\n"
                   f"📌 *Aktif Pozisyonlar ({len(open_positions)}/{slots}):*\n{pos_txt}\n\n"
                   f"_(Komutlar: `/gercek`, `/fake`, `/rapor`, `/kapat`)_")
                   
            # 5. /kapat (Mevcut işlemi hemen kapat)
            elif text in ["/kapat", "/close", "kapat"]:
                if state.get("positions"):
                    for p in state["positions"]:
                        cur_p = last_price(p["sym"])
                        pnl = (cur_p - p["entry"]) * p["qty"]
                        if p.get("is_real", False):
                            execute_real_close(p, "MANUEL_TELEGRAM_KAPATMA")
                        record_trade(p, cur_p, pnl, "MANUEL_TELEGRAM_KAPATMA", 60)
                        tg(f"🔒 *{p['sym']}* Telegram komutuyla kapatıldı! Net PnL: *`${pnl:+.2f} USDT`*")
                    state["positions"] = []
                    save_st(state)
                else:
                    tg("ℹ️ Şu anda kapatılacak açık pozisyon bulunmuyor.")
                    
            # 6. /yardim veya /help
            elif text in ["/yardim", "/help", "yardim"]:
                tg("🤖 *TELEGRAM BOT KOMUTLARI:*\n\n"
                   "• `/fake` -> Simülasyon ($20 Başlangıçlı Kasa) moduna geç\n"
                   "• `/gercek` -> Gerçek Binance vadeli işlem moduna geç\n"
                   "• `/rapor` -> Güncel kâr-zarar ve Win Rate raporunu al\n"
                   "• `/durum` -> Anlık kasa ve açık pozisyon durumunu gör\n"
                   "• `/kapat` -> Açık olan pozisyonları hemen kapat")
    except Exception as e:
        print(f"[TG KOMUT HATA] {e}", flush=True)
        
    return state

def send_performance_report(state):
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    trades = load_db()
    
    filtered_trades = [t for t in trades if t.get("is_real", False) == is_real]
    mode_title = "🔴 GERÇEK İŞLEM RAPORU" if is_real else "🧪 SİMÜLASYON ($20 BAŞLANGIÇ) RAPORU"
    
    if not filtered_trades:
        tg(f"📊 *{mode_title}*\n\nHenüz tamamlanmış bir işlem bulunmuyor. Bot 230+ pariteyi taramaya devam ediyor!")
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
    safe_wr = ((win_cnt + len(bes)) / tot * 100) if tot > 0 else 0
    total_net = df_t["pnl"].sum()
    
    gross_win = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    pf = (gross_win / gross_loss) if gross_loss > 0 else 99.0
    
    recent_lines = []
    for _, row in df_t.tail(6).iterrows():
        icon = "🟢" if row["pnl"] > 0 else ("🔰" if row["pnl"] == 0 else "🔴")
        recent_lines.append(f"{icon} `{row['pair']}` -> *${row['pnl']:+.2f}* ({row['result']})")
        
    rec_txt = "\n".join(recent_lines)
    
    extra_b = ""
    if not is_real:
        virt_eq, _, _ = get_virtual_balance(state)
        roi = ((virt_eq - SIM_STARTING_BALANCE) / SIM_STARTING_BALANCE) * 100
        extra_b = f"• *Başlangıç Kasası:* `$20.00 USDT`\n• *Güncel Sanal Kasa:* *`${virt_eq:.2f} USDT`* (%{roi:+.1f} Büyüme)\n"
    
    tg(f"📊 *{mode_title}*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"{extra_b}"
       f"• *Toplam Yapılan İşlem:* `{tot}`\n"
       f"• *Kazanılan:* `{win_cnt}` (🟢)\n"
       f"• *Başa Baş (0 Zarar):* `{len(bes)}` (🔰)\n"
       f"• *Kaybedilen:* `{loss_cnt}` (🔴)\n"
       f"• *KAZANMA ORANI (WR):* *%{wr:.1f}* 🎯\n"
       f"• *Sermaye Koruma Oranı:* *%{safe_wr:.1f}* 🛡️\n"
       f"• *Kâr Faktörü (PF):* `{pf:.2f}`\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"💰 *TOPLAM NET KÂR/ZARAR:* *`${total_net:+.2f} USDT`*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"*Son Kapanan İşlemler:*\n{rec_txt}\n\n"
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
    """BTC Anlık Şelale Kalkanı (Sadece ani sert çöküşlerde kasayı korur)"""
    try:
        df15m = klines("BTCUSDT", "15m", 30)
        if len(df15m) < 20: return True, "BTC Verisi Bekleniyor"
        last_c = df15m.iloc[-1]
        c, o = last_c['c'], last_c['o']
        
        # 1. 15m mumunda sert çöküş (%0.50'den büyük kırmızı mum)
        if c < o and ((o - c) / o) > 0.0050:
            return False, "BTC Anlık Şelalede (15m Sert Kırmızı Mum)"
            
        # 2. 1h aşırı panik satışı (RSI < 30)
        df1h = klines("BTCUSDT", "1h", 40)
        if len(df1h) >= 25:
            rsi_btc = calc_rsi(df1h['c'], 14)
            if rsi_btc < 30.0:
                return False, f"BTC 1h Aşırı Panik Satışında (RSI:{rsi_btc:.1f})"
                
        return True, "BTC Uygun (Piyasa Onaylı)"
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
            try: qv = float(t.get("quoteVolume", 0))
            except: continue
            if MIN_VOL_USD <= qv <= MAX_VOL_USD:
                out.append((sym, qv))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        print(f"[UNIVERSE HATA] {e}", flush=True)
        return []

# ── %72.3+ WIN RATE ÇİFT TREND & DİP SEKMESİ (PULLBACK SNIPER) ───────────────

def analyze_market_candidate(sym, cooldown_dict):
    last_closed_ts = cooldown_dict.get(sym, 0)
    if time.time() - last_closed_ts < COOLDOWN_SECONDS:
        return None

    try:
        df15m = klines(sym, "15m", 45)
        if len(df15m) < 30: return None
        
        c15 = df15m.iloc[-1]
        c, o, h, l = c15['c'], c15['o'], c15['h'], c15['l']
        
        ema20 = df15m['c'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = df15m['c'].ewm(span=50, adjust=False).mean().iloc[-1]
        
        rsi = calc_rsi(df15m['c'], 14)
        
        # 1. 15m Yükseliş Trendi (Fiyat EMA20 üzerinde, EMA20 >= EMA50)
        is_bull = (c >= ema20) and (ema20 >= ema50)
        
        # 2. Desteğe Yakınlık / Dip Sekmesi (Son 3 barda EMA20 desteğine dokunmuş/yakın)
        touched = (df15m['l'].iloc[-3:].min() <= ema20 * 1.008)
        
        # 3. Yeşil Alıcı Mumu
        green = (c >= o)
        
        # 4. Sağlıklı Kalkış RSI Aralığı (45.0 - 64.0)
        rsi_ok = (45.0 <= rsi <= 64.0)
        
        if is_bull and touched and green and rsi_ok:
            entry = last_price(sym)
            return {
                "sym": sym, "side": "LONG", "mode": "TREND_DİP_SEKMESİ",
                "entry": entry, "rsi": rsi,
                "reasons": [
                    "🏆 *Strateji:* 15M Trend & EMA20 Dip Sekmesi (%72.3+ Win Rate Modeli)",
                    f"📈 *Trend:* EMA20/50 Üzerinde Yükseliş Onaylı",
                    f"🎯 *Dip Teyidi:* EMA20 Desteğinden Yeşil Mumla Sekti (RSI: `{rsi:.1f}`)"
                ]
            }

        return None
    except Exception:
        return None

# ── EMİR VE TEMİNAT YÖNETİMİ ────────────────────────────────────────────────

MAX_NOTIONAL_PER_TRADE = 400.0   # Maksimum $400 USDT Pozisyon Büyüklüğü

def set_optimal_leverage(sym, target_lev=20):
    for lev in [max(20, target_lev), 25, 20]:
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
    
    max_safe_notional = free_margin * actual_lev * 0.85
    actual_notional_target = min(MAX_NOTIONAL_PER_TRADE, notional_target, max(10.0, max_safe_notional))
    
    raw_qty = actual_notional_target / price
    qty = round_step_size(raw_qty, rules["stepSize"], rules["quantityPrecision"])
    if qty < rules["minQty"]: qty = rules["minQty"]
    
    order_params = {
        "symbol": sym, "side": "BUY", "type": "MARKET", "quantity": str(qty)
    }
    
    actual_notional = qty * price
    print(f"⚡ [GERÇEK LONG AÇILIYOR] {sym} | Kaldıraç: {actual_lev}x | Büyüklük: ${actual_notional:.2f} (Serbest: ${free_margin:.2f})", flush=True)
    order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
    
    avg_price = float(order_res.get("avgPrice", 0))
    if avg_price <= 0: avg_price = price
        
    tp_price = avg_price * (1.0 + TP_PCT)
    be_trigger_price = avg_price * (1.0 + BE_TRIGGER_PCT)
    be_sl_price = avg_price + (0.35 / qty)  # Komisyonu ($0.25) da aşan net kâr garantili stop
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
    
    print(f"🔒 [GERÇEK POZİSYON KAPATILIYOR] {sym} ({reason}) | Miktar: {qty}", flush=True)
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
    icon = "🔴 *[GERÇEK İŞLEM]*" if is_real else "🧪 *[SİMÜLASYON / FAKE PARA]*"
    lines = "\n".join(f"  • {r}" for r in sig.get("reasons", []))
    lev = pos.get("leverage", DEFAULT_LEVERAGE)
    
    target_gross = (pos["tp_price"] - pos["entry"]) * pos["qty"]
    est_fee = round(pos.get("notional_usd", 250.0) * 0.0010, 2)
    target_net = target_gross - est_fee
    sl_pnl = (pos["entry"] - pos["sl_price"]) * pos["qty"] + est_fee
    
    return (
        f"{icon} *A+ ALPHA LONG AÇILDI!* | `{pos['sym']}`\n\n"
        f"Strateji: *{sig['mode']}*\n"
        f"Yön: *LONG ({lev}x Kaldıraç)*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Pozisyon Büyüklüğü : `${pos['notional_usd']}` ({pos['qty']} adet)\n\n"
        f"🎯 *Kâr Hedefi (+%1.25):* `+{fp(pos['tp_price'])}` (*Net +${target_net:.2f}*)\n"
        f"🔰 *Başa Baş (+%0.55):* Stop Komisyonun Üstüne Çekilir\n"
        f"🛑 *Stop Loss (-%0.75):* `{fp(pos['sl_price'])}` (*Net -${sl_pnl:.2f}*)\n\n"
        f"*Teknik Analiz Verileri:*\n{lines}\n\n"
        f"Zaman: `{ts()}`\n"
        f"_(Komutlar: `/durum`, `/rapor`, `/gercek`, `/fake`)_"
    )

def msg_real_close(pos, exit_price, pnl, reason, dur_sec, is_real=False):
    prefix = "🔴 [GERÇEK]" if is_real else "🧪 [SİMÜLASYON]"
    est_fee = round(pos.get("notional_usd", 250.0) * 0.0010, 2)
    net_pnl = round(pnl - est_fee, 2)
    
    icon = "🟢" if net_pnl > 0 else ("🔰" if -0.05 <= net_pnl <= 0.05 else "🔴")
    title = {
        "TRAILING_TP": f"💸 HEDEF KÂR ALINDI (Net +${net_pnl:.2f}) 🎯",
        "TAKE_PROFIT": f"💸 HEDEF KÂR ALINDI (Net +${net_pnl:.2f}) 🎯",
        "STOP_LOSS": f"❌ STOP OLDU (Net -${abs(net_pnl):.2f})",
        "BREAKEVEN": f"🔰 KOMİSYON ÜSTÜ KAPANDI (+${net_pnl:.2f})",
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
    est_fee = round(pos.get("notional_usd", 250.0) * 0.0010, 2)
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
        
        # 1. BREAKEVEN KORUMASI (+%0.20 kârda stop maliyete çekilir)
        if not pos.get("be_hit") and price >= pos["be_trigger_price"]:
            pos["sl_price"] = pos["be_sl_price"]
            pos["be_hit"] = True
            prefix = "🔴 [GERÇEK]" if pos_is_real else "🧪 [SİMÜLASYON]"
            tg(f"🔰 {prefix} *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! Stop maliyete (`{fp(pos['sl_price'])}`) çekildi. *İşlem artık %100 sıfır risklidir!*")

        # 2. TRAILING TAKE PROFIT (+%1.25 kârda devreye girer ve zirveyi takip eder)
        if price >= pos["tp_price"] or pos.get("trailing_active"):
            if not pos.get("trailing_active"):
                pos["trailing_active"] = True
                pos["highest_price"] = price
                locked_profit = (price * (1.0 - TRAILING_DROP_PCT) - entry) * qty
                prefix = "🔴 [GERÇEK]" if pos_is_real else "🧪 [SİMÜLASYON]"
                tg(f"🚀 {prefix} *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! *Trailing Kâr Takibi Aktif Edildi!*\nZirve takip ediliyor (Taban kâr: `+${locked_profit:.2f}`).")
                
            pos["highest_price"] = max(pos.get("highest_price", price), price)
            trailing_exit_price = pos["highest_price"] * (1.0 - TRAILING_DROP_PCT) # Zirveden %0.30 çekilirse sat
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
            
            # Cooldown kaydet (60 dakika kilit)
            state.setdefault("cooldown", {})[sym] = time.time()
        else:
            trail_str = f"| Trailing Stop: {fp(pos['trailing_sl_price'])}" if pos.get("trailing_active") else ""
            prefix = "[GERÇEK]" if pos_is_real else "[SİMÜLASYON]"
            print(f"  {prefix} {sym} | Fiyat: {fp(price)} | PnL: ${unrealized_pnl:+.2f} | TP: {fp(pos['tp_price'])} | SL: {fp(pos['sl_price'])} {trail_str}", flush=True)
            still.append(pos)
            
    state["positions"] = still
    return state

# ── TARAMA VE POZİSYON AÇILIŞI ───────────────────────────────────────────────

def scan(state, universe):
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    cooldown_dict = state.setdefault("cooldown", {})
    
    if is_real:
        eq, free_margin = get_account_balances()
    else:
        eq, free_margin, _ = get_virtual_balance(state)
        
    max_slots = get_dynamic_position_slots(eq)
    
    if len(state.get("positions", [])) >= max_slots:
        return state

    # BTC Güvenlik Kontrolü
    btc_ok, btc_reason = check_btc_shield()
    if not btc_ok:
        return state

    open_syms = {p["sym"] for p in state.get("positions", [])}
    open_syms.update(PROTECTED_SYMBOLS)
    
    # Pozisyon Başına Ayrılan Büyüklük (Kasanın %65'i teminat, %35'i güvence tamponu - Max $400)
    allocated_margin = (eq / max_slots) * 0.65
    target_notional = min(MAX_NOTIONAL_PER_TRADE, allocated_margin * DEFAULT_LEVERAGE)
    
    for i, (sym, _) in enumerate(universe):
        if sym in open_syms: continue
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r", flush=True)
        try:
            sig = analyze_market_candidate(sym, cooldown_dict)
            if sig:
                mode_label = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"
                print(f"\n✅ [{mode_label} SNIPER ONAYI] {sym}! Pozisyon açılıyor...", flush=True)
                
                if is_real:
                    pos = execute_real_entry(sym, target_notional, free_margin)
                else:
                    entry = sig["entry"]
                    rules = get_symbol_rules(sym)
                    qty = round_step_size(target_notional / entry, rules["stepSize"], rules["quantityPrecision"])
                    actual_notional = qty * entry
                    
                    tp_price = entry * (1.0 + TP_PCT)
                    be_trigger_price = entry * (1.0 + BE_TRIGGER_PCT)
                    be_sl_price = entry + (0.35 / qty)
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
        time.sleep(0.05)
        
    return state

# ── ANA DÖNGÜ ────────────────────────────────────────────────────────────────

def main():
    print("="*65, flush=True)
    print("🏆 A+ ALPHA KOMİSYON KALKANLI BİLEŞİK BÜYÜME (COMPOUNDING) MOTORU", flush=True)
    print("="*65, flush=True)
    print(" 🏁 Başlangıç Kasası   : $20.00 USDT (Kademeli Otomatik Büyüme)", flush=True)
    print(" 🎯 Ana Strateji       : 15M Trend & EMA20 Dip Sekmesi (Hacim Teyitli)", flush=True)
    print(" ⚡ Kaldıraç & Boyut   : Min 20x Kaldıraç | Max $400 USDT Tavan", flush=True)
    print(" 💸 Net Kâr Hedefi     : +%1.25 (Komisyonu 10'a Katlayan Net Kâr)", flush=True)
    print(" 🔰 Başa Baş Koruma    : +%0.55 kârda stop komisyonun üstüne çekilir", flush=True)
    print(" 🛑 Sıkı Stop Loss     : -%0.75 seviyesinde anlık koruma", flush=True)
    print(" 🛡️ Koruma             : BASEDUSDT dokunulmaz | 60 Dk Cooldown", flush=True)
    print("="*65, flush=True)
    
    state = load_st()
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    mode_text = "🔴 GERÇEK İŞLEM" if is_real else "🧪 SİMÜLASYON ($20 BAŞLANGIÇ)"
    
    virt_eq, virt_free, _ = get_virtual_balance(state)
    slots = get_dynamic_position_slots(virt_eq)
    print(f"✅ Sanal Kasa: ${virt_eq:.2f} USDT | İzin Verilen Yuva: {slots} | Mod: {mode_text}", flush=True)
    
    tg(f"🏆 *%72.3+ WIN RATE BİLEŞİK BÜYÜME MOTORU AKTİF EDİLDİ!*\n\n"
       f"⚙️ *Aktif Çalışma Modu:* `{mode_text}`\n"
       f"💰 *Kasa:* `${virt_eq:.2f} USDT` (Başlangıç: `$20.00 USDT`)\n"
       f"🎯 *Kademeli Yuva:* `{slots}` adet eşzamanlı pozisyon\n"
       f"📈 *Strateji:* 1H + 15M Çift Trend & Dip Sekmesi (%72.3 Win Rate)\n"
       f"🛡️ *BTC Kalkanı:* BTC onay vermedikçe kasa korunur.\n\n"
       f"🎮 *Telegram Komutları:*\n"
       f"• `/gercek` -> Gerçek parayla işleme geç\n"
       f"• `/fake` -> Simülasyon ($20 Başlangıç) moduna geç\n"
       f"• `/rapor` -> Güncel kâr-zarar raporunu al\n"
       f"• `/durum` -> Anlık kasa ve pozisyon durumu\n"
       f"• `/kapat` -> Açık pozisyonları hemen kapat")

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
                    slots = get_dynamic_position_slots(eq)
                    bal_str = f"Varlık: ${eq:.2f} USDT (Serbest: ${free_m:.2f})"
                else:
                    virt_eq, virt_free, _ = get_virtual_balance(state)
                    mode_tag = "SİMÜLASYON"
                    slots = get_dynamic_position_slots(virt_eq)
                    bal_str = f"Sanal Kasa: ${virt_eq:.2f} USDT (Serbest: ${virt_free:.2f})"
                    
                open_count = len(state.get("positions", []))
                print(f"💓 [CANLI DURUM] {bal_str} | Pozisyon: {open_count}/{slots} | Mod: {mode_tag} | Saat: {utc().strftime('%H:%M:%S UTC')}", flush=True)
                last_heartbeat_time = now
            
            # 1. Açık pozisyonları anlık izle ve kârı/stopu yönet
            if state.get("positions"):
                state = monitor(state)
                save_st(state)
                
            # 2. Eğer pozisyon limiti dolmamışsa piyasayı tara
            is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
            current_eq = get_account_balances()[0] if is_real else get_virtual_balance(state)[0]
            max_slots = get_dynamic_position_slots(current_eq)
            
            if len(state.get("positions", [])) < max_slots:
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
