"""
trader_bot.py — Yapay Zeka Beyinli (Gemini 2.5 Flash) Bileşik Scalp Robotu
• Mod Yönetimi: Varsayılan SİMÜLASYON (Fake Para). Telegram komutu ile GERÇEK moda geçebilir!
• Telegram Komutları:
    /gercek   -> Gerçek Binance vadeli işlem modunu açar
    /fake     -> Simülasyon (Fake Para) moduna geçer
    /rapor    -> Günlük kâr/zarar ve işlem karnesini Telegram'a döker
    /durum    -> Anlık kasa ve açık pozisyon durumunu gösterir
    /kapat    -> Açık olan işlemi piyasa fiyatından hemen kapatır
• Karar Verici: %100 Google Gemini 2.5 Flash Master AI
• Pozisyon: 20x Kaldıraç | $250 Notional Scalp
• Trailing Kâr: +$1.80 kârda başlar, zirveden $0.80 çekilince satar (+1.00$ kâr garanti)
• Başa Baş: +$1.00 kârda stop maliyete (+0.10$) çekilir (Sıfır Risk)
• Stop Loss: -$1.00 seviyesinde anlık koruma
• Koruma: BASEDUSDT dokunulmaz | BTC Düşüş Kalkanı Aktif
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
# Varsayılan olarak FAKE PARA (Simülasyon). Telegramdan /gercek yazarak değiştirilebilir.
REAL_TRADING_DEFAULT = os.getenv("REAL_TRADING", "false").lower() == "true"
TARGET_NOTIONAL      = 250.0       # Hedef Pozisyon: Tam $250.00 USDT
DEFAULT_LEVERAGE     = 20          # 20x Kaldıraç ($250 için sadece $12.50 teminat)
SCAN_EVERY           = int(os.getenv("SCAN_EVERY_SECONDS", "15"))
MAX_HOLD_MIN         = 360         # 6 Saat maksimum bekleme

# BİLEŞİK BÜYÜME VE HIZLI SCALP KÂR/ZARAR PARAMETRELERİ
DEFAULT_TP_TRIGGER_USD  = 1.80  # +$1.80 kârda Trailing Stop başlar
DEFAULT_TRAILING_DROP   = 0.80  # Zirveden $0.80 çekilirse kârı alıp çıkar (+1.00$ taban garanti)
DEFAULT_BE_TRIGGER_USD  = 1.00  # +$1.00 kârda stop maliyete çekilir (Sıfır Risk)
DEFAULT_SL_USD          = 1.00  # -$1.00 Stop Loss (Hızlı çıkış, minimum kayıp)

# MANUEL POZİSYON KORUMASI VE HANTAL COİN KARA LİSTESİ
PROTECTED_SYMBOLS = {"BASEDUSDT", "BASED", "TRXUSDT", "TRX", "FDUSDUSDT", "USDCUSDT"}

# LİKİDİTE VE TEKNİK FİLTRELER (Tüm Binance Vadeli Piyasasını Kapsar)
MIN_VOL_USD      = 3_000_000.0   # $3M üzeri tüm hareketli pariteler
MAX_VOL_USD      = 800_000_000.0 # $800M hacme kadar tüm coinler
MIN_VOL_MULTIPLIER = 2.2

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
            # Markdown hatası olursa düz metin olarak tekrar dene
            requests.post(
                f"https://api.telegram.org/bot{TK}/sendMessage",
                json={"chat_id": TC, "text": txt.replace("*", "").replace("`", "").replace("_", ""), "disable_web_page_preview": True},
                timeout=12
            )
    except Exception as e:
        print(f"[TG HATA] {e}", flush=True)

# ── SANAL KASA (VIRTUAL WALLET) MOTORU ───────────────────────────────────────
SIM_STARTING_BALANCE = 100.0   # Sanal Test Başlangıç Kasası: Tam $100.00 USDT

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
            used_margin += p.get("notional_usd", TARGET_NOTIONAL) / p.get("leverage", DEFAULT_LEVERAGE)
            
    virtual_equity = round(SIM_STARTING_BALANCE + sim_realized_pnl + unrealized_pnl, 2)
    virtual_free_margin = round(max(0.0, virtual_equity - used_margin), 2)
    return virtual_equity, virtual_free_margin, sim_realized_pnl

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
            chat_id = str(msg.get("chat", {}).get("id", ""))
            
            if not text: continue
            
            # 1. /gercek veya /real
            if text in ["/gercek", "/real", "gercek", "real"]:
                state["real_trading"] = True
                save_st(state)
                tg("🔴 *MOD DEĞİŞTİRİLDİ: GERÇEK İŞLEM MODU AKTİF!* ⚡\n\nBundan sonraki tüm sinyaller Binance hesabında gerçek parayla açılacaktır.")
                print("⚡ [MOD DEĞİŞTİ] GERÇEK İŞLEM MODU AKTİF EDİLDİ.", flush=True)
                
            # 2. /fake veya /paper veya /simulasyon
            elif text in ["/fake", "/paper", "/simulasyon", "fake", "paper"]:
                state["real_trading"] = False
                save_st(state)
                virt_eq, virt_free, _ = get_virtual_balance(state)
                tg(f"🟢 *MOD DEĞİŞTİRİLDİ: SİMÜLASYON (FAKE PARA) MODU AKTİF!* 🧪\n\n"
                   f"💰 *Sanal Kasa:* `${virt_eq:.2f} USDT` (Başlangıç: `$100.00 USDT`)\n"
                   f"İşlemler $100 sanal bakiye ile canlı piyasa fiyatlarında test edilecektir. Hesabındaki gerçek paraya dokunulmaz.")
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
                    mode_str = "🔴 GERÇEK İŞLEM"
                    balance_txt = f"💰 *Gerçek Hesap Varlığı:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)"
                else:
                    virt_eq, virt_free, sim_pnl = get_virtual_balance(state)
                    mode_str = "🧪 SİMÜLASYON ($100 FAKE PARA)"
                    pnl_sign = f"+${sim_pnl:.2f}" if sim_pnl >= 0 else f"-${abs(sim_pnl):.2f}"
                    balance_txt = (
                        f"💰 *Sanal Kasa:* `${virt_eq:.2f} USDT` (Serbest: `${virt_free:.2f}`)\n"
                        f"🏁 *Başlangıç:* `$100.00 USDT` | *Net PnL:* `{pnl_sign}`"
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
                   f"📌 *Aktif Pozisyonlar ({len(open_positions)}/4):*\n{pos_txt}\n\n"
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
                   "• `/fake` -> Simülasyon ($100 Fake Para) moduna geç\n"
                   "• `/gercek` -> Gerçek Binance vadeli işlem moduna geç\n"
                   "• `/rapor` -> Günün/Toplamın kâr-zarar raporunu al\n"
                   "• `/durum` -> Anlık kasa ve pozisyon durumunu gör\n"
                   "• `/kapat` -> Açık olan pozisyonları hemen kapat")
    except Exception as e:
        print(f"[TG KOMUT HATA] {e}", flush=True)
        
    return state

def send_performance_report(state):
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    trades = load_db()
    
    # Aktif moda göre trade listesini filtrele
    filtered_trades = [t for t in trades if t.get("is_real", False) == is_real]
    mode_title = "🔴 GERÇEK İŞLEM RAPORU" if is_real else "🧪 SİMÜLASYON ($100 SANAL KASA) RAPORU"
    
    if not filtered_trades:
        tg(f"📊 *{mode_title}*\n\nHenüz tamamlanmış bir işlem bulunmuyor. Bot 229 pariteyi taramaya devam ediyor!")
        return
        
    df_t = pd.DataFrame(filtered_trades)
    tot = len(df_t)
    wins = df_t[df_t["pnl"] > 0]
    losses = df_t[df_t["pnl"] < 0]
    
    win_cnt = len(wins)
    loss_cnt = len(losses)
    wr = (win_cnt / tot * 100) if tot > 0 else 0
    total_net = df_t["pnl"].sum()
    
    gross_win = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    pf = (gross_win / gross_loss) if gross_loss > 0 else 99.0
    
    recent_lines = []
    for _, row in df_t.tail(6).iterrows():
        icon = "🟢" if row["pnl"] >= 0 else "🔴"
        recent_lines.append(f"{icon} `{row['pair']}` -> *${row['pnl']:+.2f}* ({row['result']})")
        
    rec_txt = "\n".join(recent_lines)
    
    extra_b = ""
    if not is_real:
        virt_eq, _, _ = get_virtual_balance(state)
        extra_b = f"• *Başlangıç Kasası:* `$100.00 USDT`\n• *Güncel Sanal Kasa:* *`${virt_eq:.2f} USDT`*\n"
    
    tg(f"📊 *{mode_title}*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"{extra_b}"
       f"• *Toplam Yapılan İşlem:* `{tot}`\n"
       f"• *Kazanılan:* `{win_cnt}` (%{wr:.1f} Win Rate)\n"
       f"• *Kaybedilen:* `{loss_cnt}`\n"
       f"• *Kâr Faktörü (PF):* `{pf:.2f}`\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"💰 *TOPLAM NET KÂR/ZARAR:* *`${total_net:+.2f} USDT`*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"*Son Kapanan İşlemler:*\n{rec_txt}\n\n"
       f"Zaman: `{ts()}`")

# ── GEMINI 2.5 FLASH ANA YAPAY ZEKA BEYNİ (MASTER SCALP AI) ──────────────────

def gemini_master_ai_decision(sym, price, rsi_15m, rsi_5m, vol_ratio, btc_status, candles_summary):
    if not GEMINI_KEY:
        return True, 80, "Gemini anahtarı girilmedi, teknik onayla devam ediliyor."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    prompt = f"""
Sen dünyanın en başarılı Kripto Vadeli Scalp Fon Yöneticisisin.
Amacımız: Kasayı 20x kaldıraç ile $250 büyüklüğünde LONG scalp pozisyonları açarak adım adım büyütmek.

GÖREV: Aşağıdaki pariteye ait anlık mumları, hacim patlamasını ve piyasa yapısını titizlikle incele.

Parite: {sym}
Anlık Fiyat: {price}
15m RSI: {rsi_15m:.1f} | 5m RSI: {rsi_5m:.1f}
Hacim Artışı: {vol_ratio:.1f}x katı alıcı hacmi
BTC Durumu: {btc_status}
Son Mumlar (OHLC): {candles_summary}

KURALLAR:
1. Eğer mum yapısında güçlü alıcı baskısı, basamak yükselişi veya direnç kırılımı varsa APPROVE ver.
2. Eğer sahte pump (fakeout), tepe iğnesi veya düşüş riski varsa kesinlikle REJECT ver.
3. Sadece kazanma ihtimali çok yüksek (%80+) net fırsatları onayla.

SADECE aşağıdaki JSON formatında yanıt ver:
{{
  "decision": "APPROVE" veya "REJECT",
  "confidence": 0 ile 100 arası sayı,
  "reason": "Türkçe net 1-2 cümlelik profesyonel açıklama"
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
            reason = parsed.get("reason", "Yapay zeka alıcı baskısını onayladı.")
            is_approved = (decision == "APPROVE" and confidence >= 80)
            return is_approved, confidence, reason
    except Exception as e:
        print(f"[GEMINI EXCEPTION] {e}", flush=True)
        
    return False, 50, "Yapay zeka yanıt veremedi, güvenlik gereği pas geçildi."

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

# ── BTC ANLIK ŞELALE KALKANI ────────────────────────────────────────────────

def check_btc_shield():
    try:
        df15m = klines("BTCUSDT", "15m", 30)
        if len(df15m) < 20: return True, "BTC Verisi Yetersiz"
        last_c = df15m.iloc[-1]
        c, o = last_c['c'], last_c['o']
        
        if c < o and ((o - c) / o) > 0.0035:
            return False, "BTC Anlık Şelalede (15m Sert Kırmızı Mum)"
            
        df1h = klines("BTCUSDT", "1h", 40)
        if len(df1h) >= 25:
            ema20 = df1h['c'].ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = df1h['c'].ewm(span=50, adjust=False).mean().iloc[-1]
            rsi_btc = calc_rsi(df1h['c'], 14)
            if df1h['c'].iloc[-1] < ema20 and ema20 < ema50 and rsi_btc < 42.0:
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
        print(f"[UNIVERSE HATA] {e}", flush=True)
        return []

# ── YAPAY ZEKA GÖZLEM VE SİNYAL ADAY MOTORU ──────────────────────────────────

def analyze_market_candidate(sym, btc_status):
    try:
        df15m = klines(sym, "15m", 50)
        if len(df15m) < 40: return None
        
        df5m = klines(sym, "5m", 30)
        if len(df5m) < 20: return None
        
        c15 = df15m.iloc[-1]
        c, o, h, l = c15['c'], c15['o'], c15['h'], c15['l']
        rsi_15m = calc_rsi(df15m['c'], 14)
        rsi_5m = calc_rsi(df5m['c'], 14)
        
        vol_avg = df15m['v'].iloc[-20:-2].mean()
        vol_now = c15['v'] + df15m['v'].iloc[-2]
        vol_ratio = (vol_now / vol_avg) if vol_avg > 0 else 1.0
        
        ema20 = df15m['c'].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = df15m['c'].ewm(span=50, adjust=False).mean().iloc[-1]
        
        last_4 = [f"M{idx+1}: O={row['o']:.4f} C={row['c']:.4f} H={row['h']:.4f} L={row['l']:.4f} V={row['v']:.0f}" for idx, row in df15m.iloc[-4:].iterrows()]
        candles_summary = " | ".join(last_4)
        
        p1 = df15m["l"].iloc[-48:-36].min() if len(df15m) >= 48 else df15m["l"].iloc[-36:-24].min()
        p2 = df15m["l"].iloc[-24:-12].min()
        p3 = df15m["l"].iloc[-12:].min()
        
        is_staircase = (p1 < p2 < p3) and (ema20 >= ema50) and (45.0 <= rsi_15m <= 68.0)
        is_breakout = (vol_ratio >= MIN_VOL_MULTIPLIER) and (52.0 <= rsi_15m <= 75.0) and (c >= o)
        
        if is_staircase or is_breakout:
            strategy_name = "BASAMAK_AKÜMÜLASYONU" if is_staircase else "HACİMLİ_BREAKOUT"
            entry = last_price(sym)
            
            ai_approved, ai_confidence, ai_reason = gemini_master_ai_decision(
                sym=sym, price=entry, rsi_15m=rsi_15m, rsi_5m=rsi_5m,
                vol_ratio=vol_ratio, btc_status=btc_status,
                candles_summary=candles_summary
            )
            
            if ai_approved:
                return {
                    "sym": sym, "side": "LONG", "mode": strategy_name,
                    "entry": entry, "rsi": rsi_15m,
                    "ai_conf": ai_confidence, "ai_reason": ai_reason,
                    "reasons": [
                        f"🧠 *AI Onayı:* %{ai_confidence} Güven — _{ai_reason}_",
                        f"📊 *Formasyon:* {strategy_name} (Hacim: `{vol_ratio:.1f}x`)",
                        f"📈 *İndikatör:* 15m RSI `{rsi_15m:.1f}` | 5m RSI `{rsi_5m:.1f}`"
                    ]
                }
            else:
                print(f"❌ [GEMINI RED] {sym} (%{ai_confidence}) — {ai_reason}", flush=True)

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

def execute_real_entry(sym, free_margin):
    actual_lev = set_optimal_leverage(sym, target_lev=DEFAULT_LEVERAGE)
    time.sleep(0.15)
    
    rules = get_symbol_rules(sym)
    price = last_price(sym)
    
    target_usd = TARGET_NOTIONAL
    max_safe_notional = free_margin * actual_lev * 0.85
    actual_notional_target = min(target_usd, max(10.0, max_safe_notional))
    
    raw_qty = actual_notional_target / price
    qty = round_step_size(raw_qty, rules["stepSize"], rules["quantityPrecision"])
    if qty < rules["minQty"]: qty = rules["minQty"]
    
    order_params = {
        "symbol": sym, "side": "BUY", "type": "MARKET", "quantity": str(qty)
    }
    
    actual_notional = qty * price
    print(f"⚡ [GERÇEK LONG AÇILIYOR] {sym} | Kaldıraç: {actual_lev}x | Miktar: {qty} | Büyüklük: ${actual_notional:.2f} (Serbest: ${free_margin:.2f})", flush=True)
    order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
    
    avg_price = float(order_res.get("avgPrice", 0))
    if avg_price <= 0: avg_price = price
        
    scale_factor = min(1.0, max(0.40, actual_notional / 250.0))
    dyn_tp_trigger_usd = round(DEFAULT_TP_TRIGGER_USD * scale_factor, 2)
    dyn_trailing_drop_usd = round(DEFAULT_TRAILING_DROP * scale_factor, 2)
    dyn_be_trigger_usd = round(DEFAULT_BE_TRIGGER_USD * scale_factor, 2)
    dyn_sl_usd = round(DEFAULT_SL_USD * scale_factor, 2)
    
    sl_price = avg_price - (dyn_sl_usd / qty)
    be_trigger_price = avg_price + (dyn_be_trigger_usd / qty)
    be_sl_price = avg_price + (0.10 / qty)
    
    return {
        "sym": sym, "side": "LONG", "entry": avg_price, "qty": qty,
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
    tp_val = float(pos.get("dyn_tp_trigger_usd", DEFAULT_TP_TRIGGER_USD))
    drop_val = float(pos.get("dyn_trailing_drop_usd", DEFAULT_TRAILING_DROP))
    lock_val = tp_val - drop_val
    sl_val = float(pos.get("dyn_sl_usd", DEFAULT_SL_USD))
    be_val = float(pos.get("dyn_be_trigger_usd", DEFAULT_BE_TRIGGER_USD))
    
    return (
        f"{icon} *YAPAY ZEKA LONG AÇTI!* | `{pos['sym']}`\n\n"
        f"Strateji: *{sig['mode']}*\n"
        f"Yön: *LONG ({lev}x Kaldıraç)*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Pozisyon Büyüklüğü : `${pos['notional_usd']}` ({pos['qty']} adet)\n\n"
        f"📈 *Trailing Kâr:* `+${tp_val:.2f}` geçilince başlar (+${lock_val:.2f} kilitlenir)\n"
        f"🛑 *Stop Loss:* `-${sl_val:.2f}` (`{fp(pos['sl_price'])}`)\n"
        f"🔰 *Sıfır Risk (+${be_val:.2f} kârda):* Stop maliyete çekilir\n\n"
        f"*Yapay Zeka Analizi:*\n{lines}\n\n"
        f"Zaman: `{ts()}`\n"
        f"_(Komutlar: `/durum`, `/rapor`, `/gercek`, `/fake`)_"
    )

def msg_real_close(pos, exit_price, pnl, reason, dur_sec, is_real=False):
    prefix = "🔴 [GERÇEK]" if is_real else "🧪 [SİMÜLASYON]"
    icon = "🟢" if pnl >= 0 else "🔴"
    abs_pnl = abs(pnl)
    title = {
        "TRAILING_TP": f"💸 TRAILING KÂR ALINDI (+${pnl:.2f})",
        "STOP_LOSS": f"❌ STOP OLDU (-${abs_pnl:.2f})",
        "BREAKEVEN": "🔰 BAŞA BAŞ KAPANDI (Sıfır Risk)",
        "TIMEOUT": "⏱️ SÜRE DOLDU",
        "MANUEL_TELEGRAM_KAPATMA": "🛑 TELEGRAM İLE KAPATILDI"
    }.get(reason, reason)
    
    eq, free_m = get_account_balances()
    highest_pnl = float(pos.get("highest_profit_usd", pnl))
    dur_min = dur_sec // 60
    
    return (
        f"{icon} {prefix} *{title}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(exit_price)}`\n"
        f"Net P&L : *`${pnl:+.2f} USDT`*\n"
        f"Görülen Zirve Kâr : `+${highest_pnl:.2f}`\n"
        f"İşlem Süresi : `{dur_min} dakika`\n"
        f"💰 *Hesap Varlığı:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n\n"
        f"Zaman: `{ts()}`"
    )

# ── DURUM YÖNETİMİ ───────────────────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except Exception: pass
    return {"positions": [], "real_trading": REAL_TRADING_DEFAULT}

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
        "result": reason, "duration": dur_sec,
        "is_real": pos.get("is_real", False),
        "timestamp": ts()
    })
    save_db(trades)
    return trades

# ── ANLIK MONİTÖR & TRAILING KÂR MOTORU ──────────────────────────────────────

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
            print(f"ℹ️ [{sym}] Binance üzerinde kapatılmış, takip listesinden çıkarıldı.", flush=True)
            continue
            
        try: price = last_price(sym)
        except Exception:
            still.append(pos); continue
            
        qty = pos["qty"]
        entry = pos["entry"]
        dur = int((utc() - datetime.fromisoformat(pos.get("opened_iso", utc().isoformat()))).total_seconds())
        unrealized_pnl = (price - entry) * qty
        
        if unrealized_pnl > pos.get("highest_profit_usd", 0.0):
            pos["highest_profit_usd"] = unrealized_pnl
            
        highest_pnl = pos["highest_profit_usd"]
        dyn_tp_trigger = pos.get("dyn_tp_trigger_usd", DEFAULT_TP_TRIGGER_USD)
        dyn_be_trigger = pos.get("dyn_be_trigger_usd", DEFAULT_BE_TRIGGER_USD)
        dyn_trailing_drop = pos.get("dyn_trailing_drop_usd", DEFAULT_TRAILING_DROP)
        
        # 1. TRAILING TAKE PROFIT (+ $1.80 kârda başlar ve stopu kilitler)
        if unrealized_pnl >= dyn_tp_trigger or pos.get("trailing_active"):
            if not pos.get("trailing_active"):
                pos["trailing_active"] = True
                locked_profit = max(1.00, unrealized_pnl - dyn_trailing_drop)
                prefix = "🔴 [GERÇEK]" if pos_is_real else "🧪 [SİMÜLASYON]"
                tg(f"🚀 {prefix} *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! *Trailing Kâr Takibi Aktif Edildi!*\nStop seviyesi `+${locked_profit:.2f}` kâra kilitlendi.")
                
            trailing_exit_pnl = highest_pnl - dyn_trailing_drop
            trailing_exit_price = entry + (trailing_exit_pnl / qty)
            pos["trailing_sl_price"] = max(pos.get("trailing_sl_price", pos["sl_price"]), trailing_exit_price)
            
        # 2. BREAKEVEN KORUMASI (+ $1.00 kârda stop maliyet seviyesine çekilir)
        if not pos.get("be_hit") and (price >= pos.get("be_trigger_price", entry * 1.004) or unrealized_pnl >= dyn_be_trigger):
            pos["sl_price"] = pos["be_sl_price"]
            pos["be_hit"] = True
            prefix = "🔴 [GERÇEK]" if pos_is_real else "🧪 [SİMÜLASYON]"
            tg(f"🔰 {prefix} *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! Stop maliyete (`{fp(pos['sl_price'])}`) çekildi. *İşlem artık sıfır risklidir!*")

        reason = None
        if pos.get("trailing_active") and price <= pos["trailing_sl_price"]:
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
        else:
            trail_str = f"| Trailing Stop: {fp(pos['trailing_sl_price'])}" if pos.get("trailing_active") else ""
            prefix = "[GERÇEK]" if pos_is_real else "[SİMÜLASYON]"
            print(f"  {prefix} {sym} | Fiyat: {fp(price)} | PnL: ${unrealized_pnl:+.2f} (Zirve: ${highest_pnl:+.2f}) | SL: {fp(pos['sl_price'])} {trail_str}", flush=True)
            still.append(pos)
            
    state["positions"] = still
    return state

# ── TARAMA VE AI DOĞRULAMA ───────────────────────────────────────────────────

def scan(state, universe):
    eq, free_margin = get_account_balances()
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    
    # Simülasyonda (Fake) 4 pozisyona kadar izin ver; Gerçekte kasa koruması
    if not is_real:
        max_allowed_positions = 4
    else:
        max_allowed_positions = 1 if eq < 70.0 else max(1, min(4, int(eq / 35.0)))
        
    if len(state.get("positions", [])) >= max_allowed_positions:
        return state
        
    if is_real and free_margin < 0.60:
        return state
        
    btc_ok, btc_reason = check_btc_shield()
    if not btc_ok:
        return state

    open_syms = {p["sym"] for p in state.get("positions", [])}
    open_syms.update(PROTECTED_SYMBOLS)
    
    for i, (sym, _) in enumerate(universe):
        if sym in open_syms: continue
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r", flush=True)
        try:
            sig = analyze_market_candidate(sym, btc_reason)
            if sig:
                mode_label = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"
                print(f"\n✅ [{mode_label} ONAY - %{sig['ai_conf']}] {sym}! Pozisyon açılıyor...", flush=True)
                
                if is_real:
                    pos = execute_real_entry(sym, free_margin=free_margin)
                else:
                    entry = sig["entry"]
                    rules = get_symbol_rules(sym)
                    qty = round_step_size(TARGET_NOTIONAL / entry, rules["stepSize"], rules["quantityPrecision"])
                    actual_notional = qty * entry
                    
                    sl_price = entry - (DEFAULT_SL_USD / qty)
                    be_trigger_price = entry + (DEFAULT_BE_TRIGGER_USD / qty)
                    be_sl_price = entry + (0.10 / qty)
                    
                    pos = {
                        "sym": sym, "side": "LONG", "entry": entry,
                        "qty": qty, "notional_usd": round(actual_notional, 2),
                        "leverage": DEFAULT_LEVERAGE, "sl_price": sl_price,
                        "dyn_sl_usd": DEFAULT_SL_USD, "dyn_tp_trigger_usd": DEFAULT_TP_TRIGGER_USD,
                        "dyn_be_trigger_usd": DEFAULT_BE_TRIGGER_USD, "dyn_trailing_drop_usd": DEFAULT_TRAILING_DROP,
                        "be_trigger_price": be_trigger_price, "be_sl_price": be_sl_price,
                        "trailing_active": False, "highest_profit_usd": 0.0,
                        "trailing_sl_price": sl_price, "order_id": f"sim_{str(uuid.uuid4())[:8]}",
                        "opened_iso": utc().isoformat(), "opened_ts": ts(),
                        "be_hit": False, "is_real": False
                    }
                    
                state.setdefault("positions", []).append(pos)
                tg(msg_real_open(pos, sig, is_real=is_real))
                break
        except Exception as e:
            print(f"[İŞLEM AÇILIŞ HATA] {sym}: {e}", flush=True)
            tg(f"⚠️ *İşlem Hatası ({sym}):* `{e}`")
        time.sleep(0.05)
        
    return state

# ── ANA DÖNGÜ ────────────────────────────────────────────────────────────────

def main():
    print("="*65, flush=True)
    print("🧠 BİNANCE MASTER AI (GEMINI 2.5 FLASH) BİLEŞİK SCALP MOTORU", flush=True)
    print("="*65, flush=True)
    print(" 🧠 Karar Verici       : Google Gemini 2.5 Flash (%80+ Güven Şartı)", flush=True)
    print(" 🎮 İnteraktif Mod     : Telegramdan /gercek veya /fake ile anında geçiş", flush=True)
    print(" ⚡ Kaldıraç & Boyut   : 20x Kaldıraç | Tam $250.00 Pozisyon", flush=True)
    print(" 💸 Hızlı Trailing Kâr : +$1.80 kârda devreye girer, en tepeden satar", flush=True)
    print(" 🔰 Başa Baş Koruma    : +$1.00 kârda stop maliyete çekilir (Sıfır Risk)", flush=True)
    print(" 🛑 Sıkı Stop Loss     : -$1.00 seviyesinde anlık koruma", flush=True)
    print(" 🛡️ Koruma             : BASEDUSDT dokunulmaz | BTC Kalkanı Aktif", flush=True)
    print("="*65, flush=True)
    
    state = load_st()
    is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
    mode_text = "🔴 GERÇEK İŞLEM" if is_real else "🧪 SİMÜLASYON (FAKE PARA)"
    
    eq, free_m = get_account_balances()
    print(f"✅ Toplam Varlık: ${eq:.2f} USDT | Serbest Teminat: ${free_m:.2f} USDT | Mod: {mode_text}", flush=True)
    
    tg(f"🚀 *MASTER AI (GEMINI 2.5 FLASH) BOTU YENİLENDİ!*\n\n"
       f"⚙️ *Aktif Çalışma Modu:* `{mode_text}`\n"
       f"💰 *Hesap Varlığı:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n"
       f"🧠 *Karar:* Google Gemini 2.5 Flash Master AI.\n\n"
       f"🎮 *Telegram Komutları:*\n"
       f"• `/gercek` -> Gerçek parayla işleme geç\n"
       f"• `/fake` -> Simülasyon (Fake Para) moduna geç\n"
       f"• `/rapor` -> Günün kâr/zarar karnesini al\n"
       f"• `/durum` -> Anlık pozisyon ve bakiye\n"
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
                    virt_eq, virt_free, _ = get_virtual_balance(state)
                    mode_tag = "SİMÜLASYON"
                    bal_str = f"Sanal Kasa: ${virt_eq:.2f} USDT (Serbest: ${virt_free:.2f})"
                    
                open_count = len(state.get("positions", []))
                print(f"💓 [CANLI DURUM] {bal_str} | Pozisyon: {open_count}/4 | Mod: {mode_tag} | Saat: {utc().strftime('%H:%M:%S UTC')}", flush=True)
                last_heartbeat_time = now
            
            # 1. Açık pozisyonları anlık izle ve kârı/stopu yönet
            if state.get("positions"):
                state = monitor(state)
                save_st(state)
                
            # 2. Eğer pozisyon limiti dolmamışsa piyasayı taramaya devam et (Simülasyonda 4'e kadar)
            is_real = state.get("real_trading", REAL_TRADING_DEFAULT)
            max_pos = 4 if not is_real else (1 if get_account_balances()[0] < 70.0 else 2)
            
            if len(state.get("positions", [])) < max_pos:
                if now - last_scan_time >= 15:
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
