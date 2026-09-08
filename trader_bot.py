"""
trader_bot.py — 5M Momentum Scalp Sniper (Simülasyon Modu)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PRENSİPLER:
  1. Varsayılan mod SİMÜLASYON. Gerçek para ancak Telegram'dan /gercek komutuyla açılır.
  2. Sadece A+ Kalite LONG fırsatları değerlendirilir.
  3. Komisyon (%0.05 taker × 2 = %0.10) HER ZAMAN kuruşu kuruşuna net hesaplanır.
  4. Sanal kasa $20 ile başlar, kâr/zarar net izlenir.
  5. Tek pozisyon disiplini — aynı anda yalnızca 1 pozisyon.

STRATEJİ — 15M Üst Trend + 5M Hacimli Momentum Scalp (LONG):
  Giriş Şartları (TÜMÜ gerekli):
    - 15M Trend: EMA20 >= EMA50 ve Fiyat > EMA50 (sağlam yükseliş zemini)
    - 15M RSI: 42 - 72 arası (aşırı alım değil, sağlıklı trend)
    - 5M Trend Uyumu: EMA9 > EMA21 ve Fiyat >= EMA9
    - 5M Mum: Pozitif yeşil mum (Close > Open)
    - 5M RSI: 48 - 68 arası (ivmeli, güçlü momentum)
    - 5M Hacim Patlaması (RVOL): Son bar hacmi >= 1.6x (son 15 bar ortalamasının 1.6 katı)
    - 5M Micro-Breakout: Son 10 barın zirvesini kırma/test etme (Close >= High10 * 0.9985)
    - BTC Kalkanı: BTC son 5 dakikada %0.60, 15 dakikada %1.00 üzerinde düşmüyorsa giriş serbest

  Kâr / Zarar & Çıkış Kuralları:
    - TP Trailing Tetik: +%1.00 (Fiyat %1 yükselince Trailing kâr takibi devreye girer)
    - Trailing Drop: %0.25 (Zirveden %0.25 gevşeyince kâr kilitlenir)
    - BE (Başa Baş) Tetik: +%0.45 (Fiyat %0.45 yükselince stop maliyet + %0.15 seviyesine çekilir,
      komisyon garanti çıkarılır ve net kâr kilitlenir)
    - SL (Stop Loss): -%0.55 (Sıkı sabit stop loss)
    - Timeout: 30 dakika (Scalp pozisyonu uzamaz, 30 dakikada ivme bitmişse kapatılır)

Telegram Komutları: /durum, /rapor, /gercek, /fake, /kapat, /reset
"""

import hashlib, hmac, json, math, os, time, urllib.parse, uuid
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── ORTAM DEĞİŞKENLERİ ───────────────────────────────────────────────────────
API_KEY    = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
PAPI_BASE  = os.getenv("BINANCE_PAPI_BASE", "https://papi.binance.com")
FAPI_BASE  = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")
TK         = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TC         = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SF         = os.getenv("STATE_FILE", os.path.join(BASE_DIR, "trader_state.json"))
DB         = os.getenv("TRADE_DB", os.path.join(BASE_DIR, "trade_db.json"))

PROXY_URL  = os.getenv("FIXIE_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""

def get_proxies():
    if PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None

# ── SABİT STRATEJİ VE SCALP PARAMETRELERİ ───────────────────────────────────
REAL_TRADING_DEFAULT = False          # ← VARSAYILAN SİMÜLASYON (Fake Para)
DEFAULT_LEVERAGE     = 20
MAX_NOTIONAL         = 400.0          # Maks pozisyon büyüklüğü ($)
COMMISSION_RATE      = 0.0010         # %0.10 (giriş + çıkış taker fee)
MAX_TRADES_PER_DAY   = 50             # Güvenlik tavanı
MIN_TRADE_INTERVAL   = 120            # İşlemler arası minimum 2 dakika (saniye)
SCAN_INTERVAL        = 8              # 8 saniyede bir hızlı scalp taraması
MAX_HOLD_SECONDS     = 1800           # 30 dakika maksimum tutma süresi
COOLDOWN_SECONDS     = 600            # Kapanan coine 10 dakika tekrar girme

# Scalp Kâr / Zarar Parametreleri
TP_TRIGGER_PCT     = 0.0100           # +%1.00 → trailing başlar
TRAILING_DROP_PCT  = 0.0025           # Zirveden %0.25 geri → kâr al
BE_TRIGGER_PCT     = 0.0045           # +%0.45 → stop komisyon üstüne (maliyet + %0.15)
SL_PCT             = 0.0055           # -%0.55 → sıkı stop loss

# Sanal Kasa
SIM_STARTING_BALANCE = 20.0

# Filtreler
PROTECTED  = {"BASEDUSDT","TRXUSDT","FDUSDUSDT","USDCUSDT"}
STABLE     = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","EURC"}
MIN_VOL    = 8_000_000.0
MAX_VOL    = 800_000_000.0

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ── YARDIMCI FONKSİYONLAR ────────────────────────────────────────────────────

def utc():  return datetime.now(timezone.utc)
def ts():   return utc().strftime("%Y-%m-%d %H:%M:%S UTC")
def today_str(): return utc().strftime("%Y-%m-%d")

def fp(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def est_fee(notional):
    """Tek taraflı komisyon. Giriş+çıkış için 2x çağır veya COMMISSION_RATE kullan."""
    return round(notional * COMMISSION_RATE, 4)

def tg(txt):
    if not TK or not TC:
        print(f"[TG] {txt}", flush=True)
        return
    proxies = get_proxies()
    for use_proxy in ([None, proxies] if proxies else [None]):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TK}/sendMessage",
                json={"chat_id": TC, "text": txt, "parse_mode": "Markdown",
                      "disable_web_page_preview": True},
                proxies=use_proxy,
                timeout=12)
            if r.status_code == 200: return
            requests.post(
                f"https://api.telegram.org/bot{TK}/sendMessage",
                json={"chat_id": TC,
                      "text": txt.replace("*","").replace("`","").replace("_",""),
                      "disable_web_page_preview": True},
                proxies=use_proxy,
                timeout=12)
            return
        except Exception:
            if use_proxy is None and proxies: continue
            print(f"[TG HATA] Telegram bildirimi iletilemedi", flush=True)

# ── DURUM YÖNETİMİ ───────────────────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except Exception: pass
    return {
        "positions": [],
        "real_trading": REAL_TRADING_DEFAULT,
        "cooldown": {},
        "sim_balance": SIM_STARTING_BALANCE,
        "trades_today": [],        # [{date, count}]
        "last_trade_ts": 0,
    }

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

def get_sim_balance(state):
    return state.get("sim_balance", SIM_STARTING_BALANCE)

def trades_today_count(state):
    """Bugün kaç işlem kapandı?"""
    today = today_str()
    return sum(1 for t in state.get("trades_today", []) if t.get("date") == today)

def can_open_trade(state):
    """Yeni işlem açılabilir mi?"""
    # Güvenlik limiti
    if trades_today_count(state) >= MAX_TRADES_PER_DAY:
        return False, f"Günlük güvenlik limiti ({MAX_TRADES_PER_DAY}) doldu"
    # Zaman aralığı
    elapsed = time.time() - state.get("last_trade_ts", 0)
    if elapsed < MIN_TRADE_INTERVAL:
        remaining = int(MIN_TRADE_INTERVAL - elapsed)
        return False, f"İşlemler arası bekleme: {remaining} sn kaldı"
    return True, "OK"

def record_trade(state, pos, exit_price, gross_pnl, reason, dur_sec):
    fee = est_fee(pos["notional_usd"])
    net_pnl = round(gross_pnl - fee, 4)

    # Sanal kasa güncelle
    if not pos.get("is_real", False):
        state["sim_balance"] = round(get_sim_balance(state) + net_pnl, 4)

    # Günlük sayaç
    today = today_str()
    state.setdefault("trades_today", []).append({"date": today})
    state["trades_today"] = [t for t in state["trades_today"] if t.get("date") == today]

    state["last_trade_ts"] = time.time()

    # DB kaydet
    trades = load_db()
    trades.append({
        "pair": pos["sym"], "side": "LONG",
        "entry": pos["entry"], "exit": exit_price,
        "qty": pos["qty"], "notional": pos["notional_usd"],
        "gross_pnl": round(gross_pnl, 4), "fee": fee,
        "pnl": net_pnl,
        "result": reason, "duration_sec": dur_sec,
        "is_real": pos.get("is_real", False),
        "timestamp": ts()
    })
    save_db(trades)
    return net_pnl

# ── TELEGRAM KOMUT DİNLEYİCİ ─────────────────────────────────────────────────

LAST_UPDATE_ID = 0

def handle_telegram(state):
    global LAST_UPDATE_ID
    if not TK: return state
    try:
        proxies = get_proxies()
        r = requests.get(
            f"https://api.telegram.org/bot{TK}/getUpdates",
            params={"offset": LAST_UPDATE_ID + 1, "timeout": 1},
            proxies=proxies,
            timeout=5)
        if r.status_code != 200: return state
        for u in r.json().get("result", []):
            LAST_UPDATE_ID = u.get("update_id", LAST_UPDATE_ID)
            text = u.get("message", {}).get("text", "").strip().lower()
            if not text: continue

            if text in ["/gercek", "/real"]:
                state["real_trading"] = True
                save_st(state)
                eq, fm = get_account_balances()
                tg(f"🔴 *GERÇEK İŞLEM MODU AKTİF!*\n"
                   f"💰 Bakiye: `${eq:.2f}` (Serbest: `${fm:.2f}`)\n"
                   f"⚡ Scalp pozisyonları gerçek parayla açılacaktır!")

            elif text in ["/fake", "/sim", "/simulasyon"]:
                state["real_trading"] = False
                save_st(state)
                bal = get_sim_balance(state)
                tg(f"🧪 *SİMÜLASYON MODU AKTİF!*\n"
                   f"💰 Sanal Kasa: `${bal:.2f} USDT`\n"
                   f"Gerçek paraya dokunulmaz.")

            elif text in ["/durum", "/status"]:
                send_status(state)

            elif text in ["/rapor", "/pnl"]:
                send_report(state)

            elif text in ["/kapat", "/close"]:
                state = close_all(state)

            elif text in ["/reset"]:
                state["sim_balance"] = SIM_STARTING_BALANCE
                state["trades_today"] = []
                state["last_trade_ts"] = 0
                state["positions"] = []
                state["cooldown"] = {}
                state["real_trading"] = False
                save_st(state)
                tg(f"🔄 *Simülasyon sıfırlandı!* Kasa: `${SIM_STARTING_BALANCE:.2f} USDT` (Sanal Mod)")

    except Exception as e:
        print(f"[TG CMD HATA] {e}", flush=True)
    return state

def send_status(state):
    is_real = state.get("real_trading", False)
    mode = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"

    if is_real:
        eq, fm = get_account_balances()
        bal_txt = f"💰 Bakiye: `${eq:.2f}` (Serbest: `${fm:.2f}`)"
    else:
        bal = get_sim_balance(state)
        pnl = bal - SIM_STARTING_BALANCE
        bal_txt = f"💰 Sanal Kasa: `${bal:.2f}` (PnL: `${pnl:+.2f}`)"

    today_cnt = trades_today_count(state)
    pos_list = state.get("positions", [])

    pos_txt = "_Açık pozisyon yok. 5M Scalp radarı tarıyor..._"
    if pos_list:
        lines = []
        for p in pos_list:
            try: cur = last_price(p["sym"])
            except: cur = p["entry"]
            gpnl = (cur - p["entry"]) * p["qty"]
            fee = est_fee(p["notional_usd"])
            npnl = gpnl - fee
            pct = ((cur / p["entry"]) - 1) * 100
            lines.append(
                f"• `{p['sym']}` (LONG) | Giriş: `{fp(p['entry'])}` | "
                f"Şimdi: `{fp(cur)}` (%{pct:+.2f}) | Net: *`${npnl:+.2f}`*")
        pos_txt = "\n".join(lines)

    tg(f"📊 *5M SCALP DURUM*\n\n"
       f"⚙️ Mod: `{mode}`\n{bal_txt}\n"
       f"📌 Pozisyon: {len(pos_list)}/1\n"
       f"📅 Bugün İşlem: {today_cnt}\n\n"
       f"{pos_txt}\n\n"
       f"_Komutlar: /durum /rapor /gercek /fake /kapat /reset_")

def send_report(state):
    is_real = state.get("real_trading", False)
    trades = load_db()
    filtered = [t for t in trades if t.get("is_real", False) == is_real]
    mode_t = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"

    if not filtered:
        tg(f"📊 *{mode_t} RAPORU*\n\nHenüz tamamlanmış scalp işlemi yok.")
        return

    tot = len(filtered)
    wins = [t for t in filtered if t["pnl"] > 0.01]
    losses = [t for t in filtered if t["pnl"] < -0.01]
    bes = [t for t in filtered if -0.01 <= t["pnl"] <= 0.01]
    wr = len(wins) / (len(wins) + len(losses)) * 100 if (len(wins) + len(losses)) > 0 else 0

    total_pnl = sum(t["pnl"] for t in filtered)
    total_fee = sum(t.get("fee", 0) for t in filtered)
    total_gross = sum(t.get("gross_pnl", 0) for t in filtered)

    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    last_5 = []
    for t in filtered[-5:]:
        icon = "🟢" if t["pnl"] > 0.01 else ("🔰" if abs(t["pnl"]) <= 0.01 else "🔴")
        last_5.append(f"{icon} `{t['pair']}` → *${t['pnl']:+.2f}* ({t['result']})")

    tg(f"📊 *{mode_t} SCALP RAPORU*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"İşlem: `{tot}` | 🟢 `{len(wins)}` | 🔴 `{len(losses)}` | 🔰 `{len(bes)}`\n"
       f"*Win Rate: %{wr:.0f}*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Gross PnL: `${total_gross:+.2f}`\n"
       f"Komisyon: `-${total_fee:.2f}`\n"
       f"*Net PnL: `${total_pnl:+.2f} USDT`*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Ort. Kazanç: `${avg_win:+.2f}` | Ort. Kayıp: `${avg_loss:+.2f}`\n\n"
       f"*Son İşlemler:*\n" + "\n".join(last_5))

def close_all(state):
    is_real = state.get("real_trading", False)
    for p in state.get("positions", []):
        try: cur = last_price(p["sym"])
        except: cur = p["entry"]
        gpnl = (cur - p["entry"]) * p["qty"]
        if p.get("is_real") and is_real:
            cur, gpnl = execute_real_close(p, "TELEGRAM_KAPAT")
        net = record_trade(state, p, cur, gpnl, "TELEGRAM_KAPAT", 60)
        tg(f"🔒 *{p['sym']}* kapatıldı → Net: *${net:+.2f}*")
    state["positions"] = []
    save_st(state)
    return state

# ── BINANCE API ───────────────────────────────────────────────────────────────

def get_public(endpoint, p=None):
    hosts = [FAPI_BASE, "https://fapi.binance.com"]
    proxies = get_proxies()
    # 1. Önce doğrudan dene
    for host in hosts:
        url = f"{host}{endpoint}"
        try:
            r = requests.get(url, params=p, headers=HEADERS, timeout=12)
            if r.status_code == 200: return r.json()
        except: pass
    # 2. Doğrudan başarısız olursa ve proxy tanımlıysa proxy ile dene
    if proxies:
        for host in hosts:
            url = f"{host}{endpoint}"
            try:
                r = requests.get(url, params=p, headers=HEADERS, proxies=proxies, timeout=12)
                if r.status_code == 200: return r.json()
            except: pass
    raise Exception(f"Public API fail: {endpoint}")

def binance_signed(method, path, params=None):
    if not API_KEY or not API_SECRET:
        raise Exception("API key/secret eksik")
    if params is None: params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    q = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{PAPI_BASE}{path}?{q}&signature={sig}"
    hdrs = {**HEADERS, "X-MBX-APIKEY": API_KEY}
    proxies = get_proxies()
    for use_proxy in ([None, proxies] if proxies else [None]):
        try:
            if method == "GET":      r = requests.get(url, headers=hdrs, proxies=use_proxy, timeout=18)
            elif method == "POST":   r = requests.post(url, headers=hdrs, proxies=use_proxy, timeout=18)
            elif method == "DELETE": r = requests.delete(url, headers=hdrs, proxies=use_proxy, timeout=18)
            else: raise ValueError(method)
            if r.status_code == 200: return r.json()
        except Exception:
            if use_proxy is None and proxies: continue
    raise Exception(f"Signed API fail: {path}")

def klines(sym, tf, n=60):
    raw = get_public("/fapi/v1/klines", {"symbol": sym, "interval": tf, "limit": n})
    df = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
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
    r = get_public("/fapi/v1/ticker/price", {"symbol": sym})
    return float(r["price"])

def get_symbol_rules(sym):
    try:
        info = get_public("/fapi/v1/exchangeInfo")
        for s in info.get("symbols", []):
            if s.get("symbol") == sym:
                step = 1.0; minq = 1.0
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step = float(f.get("stepSize", 1.0))
                        minq = float(f.get("minQty", 1.0))
                return {"stepSize": step, "minQty": minq,
                        "qtyPrec": int(s.get("quantityPrecision", 2)),
                        "pricePrec": int(s.get("pricePrecision", 2))}
    except: pass
    return {"stepSize": 1.0, "minQty": 1.0, "qtyPrec": 2, "pricePrec": 2}

def round_step(qty, step, prec):
    if step <= 0: return round(qty, prec)
    return float(f"{math.floor(qty / step) * step:.{prec}f}")

def get_account_balances():
    try:
        acc = binance_signed("GET", "/papi/v1/account")
        eq = float(acc.get("accountEquity", 0))
        fm = float(acc.get("totalAvailableBalance", 0))
        if eq > 0: return round(eq, 2), round(fm, 2)
    except: pass
    try:
        for b in binance_signed("GET", "/papi/v1/balance"):
            if b.get("asset") == "USDT":
                eq = float(b.get("totalWalletBalance", 0))
                fm = float(b.get("crossMarginFree", eq * 0.5))
                return round(eq, 2), round(fm, 2)
    except Exception as e:
        print(f"[BAKİYE HATA] {e}", flush=True)
    return 0.0, 0.0

# ── BTC GÜVENLİK KALKANI ─────────────────────────────────────────────────────

def btc_safe():
    try:
        # 5 dakikalık ani şok düşüş kontrolü
        df5 = klines("BTCUSDT", "5m", 6)
        if len(df5) >= 3:
            c5 = df5.iloc[-1]
            if c5['c'] < c5['o'] and (c5['o'] - c5['c']) / c5['o'] > 0.0060:
                return False  # BTC 5m'de %0.60'tan fazla ani düşüşte

        # 15 dakikalık trend çöküşü kontrolü
        df15 = klines("BTCUSDT", "15m", 6)
        if len(df15) >= 3:
            c15 = df15.iloc[-1]
            if c15['c'] < c15['o'] and (c15['o'] - c15['c']) / c15['o'] > 0.0100:
                return False  # BTC 15m'de %1.00'den fazla düşüşte
    except: pass
    return True

# ── PARİTE EVRENİ ─────────────────────────────────────────────────────────────

def get_universe():
    try:
        info = get_public("/fapi/v1/exchangeInfo")
        active = {s["symbol"] for s in info.get("symbols", [])
                  if s.get("status") == "TRADING"
                  and s.get("contractType") == "PERPETUAL"
                  and s.get("quoteAsset") == "USDT"
                  and s["symbol"][:-4] not in STABLE
                  and s["symbol"] not in PROTECTED}
        tickers = get_public("/fapi/v1/ticker/24hr")
        out = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in active: continue
            try:
                qv = float(t.get("quoteVolume", 0))
                chg = float(t.get("priceChangePercent", 0))
            except: continue
            if MIN_VOL <= qv <= MAX_VOL:
                out.append((sym, qv, chg))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        print(f"[UNIVERSE HATA] {e}", flush=True)
        return []

# ── SİNYAL ANALİZİ — 15M TREND + 5M MOMENTUM SCALP (LONG) ───────────────────

def analyze(sym, cooldown):
    if time.time() - cooldown.get(sym, 0) < COOLDOWN_SECONDS:
        return None

    try:
        # ── 15M Üst Trend Filtresi ──
        df15 = klines(sym, "15m", 45)
        if len(df15) < 30: return None

        ema20_15 = df15['c'].ewm(span=20, adjust=False).mean()
        ema50_15 = df15['c'].ewm(span=50, adjust=False).mean()
        rsi_15 = calc_rsi(df15['c'], 14)
        c15 = df15['c'].iloc[-1]

        # 15M Trend: EMA20 >= EMA50 ve Fiyat EMA50 üzerinde
        if ema20_15.iloc[-1] < ema50_15.iloc[-1]: return None
        if c15 < ema50_15.iloc[-1]: return None
        if not (42.0 <= rsi_15 <= 72.0): return None

        # ── 5M Scalp Giriş ve Momentum Tetikleyicisi ──
        df5 = klines(sym, "5m", 35)
        if len(df5) < 25: return None

        ema9_5 = df5['c'].ewm(span=9, adjust=False).mean()
        ema21_5 = df5['c'].ewm(span=21, adjust=False).mean()
        rsi_5 = calc_rsi(df5['c'], 14)

        last5 = df5.iloc[-1]
        c5, o5, h5, l5 = last5['c'], last5['o'], last5['h'], last5['l']

        # 1. 5M Trend: EMA9 > EMA21 ve Fiyat >= EMA9
        if ema9_5.iloc[-1] <= ema21_5.iloc[-1]: return None
        if c5 < ema9_5.iloc[-1]: return None

        # 2. Mum Rengi: Pozitif Yeşil Mum
        if c5 <= o5: return None

        # 3. 5M Momentum RSI: 48 - 68 arası (ivmeli, şişmemiş)
        if not (48.0 <= rsi_5 <= 68.0): return None

        # 4. Hacim Patlaması (RVOL >= 1.6x)
        vol_avg5 = df5['v'].iloc[-15:-1].mean()
        rvol5 = df5['v'].iloc[-1] / vol_avg5 if vol_avg5 > 0 else 1.0
        if rvol5 < 1.6: return None

        # 5. Micro-Breakout: Son 10 barın zirvesini test etme/kırma
        high10 = df5['h'].iloc[-11:-1].max()
        if c5 < high10 * 0.9985: return None

        entry = last_price(sym)
        return {
            "sym": sym, "entry": entry,
            "rvol5": round(rvol5, 2),
            "rsi_15m": round(rsi_15, 1),
            "rsi_5m": round(rsi_5, 1),
            "reasons": [
                f"📈 15M Trend: EMA20 > EMA50 | RSI: `{rsi_15:.0f}`",
                f"⚡ 5M Scalp: EMA9 > EMA21 | RSI: `{rsi_5:.0f}` | RVOL: `{rvol5:.1f}x`",
                f"🎯 Micro-Breakout: 10-bar zirve testi onaylandı",
            ]
        }
    except: return None

# ── POZİSYON AÇMA ────────────────────────────────────────────────────────────

def set_leverage(sym):
    for lev in [20, 25, 15]:
        try:
            binance_signed("POST", "/papi/v1/um/leverage",
                          {"symbol": sym, "leverage": lev})
            return lev
        except: continue
    return 20

def execute_real_entry(sym, notional, free_margin):
    lev = set_leverage(sym)
    time.sleep(0.15)
    rules = get_symbol_rules(sym)
    price = last_price(sym)
    safe = free_margin * lev * 0.75
    actual_not = min(MAX_NOTIONAL, notional, max(10.0, safe))
    qty = round_step(actual_not / price, rules["stepSize"], rules["qtyPrec"])
    if qty < rules["minQty"]: qty = rules["minQty"]
    actual_not = qty * price

    res = binance_signed("POST", "/papi/v1/um/order",
                         {"symbol": sym, "side": "BUY", "type": "MARKET",
                          "quantity": str(qty)})
    avg = float(res.get("avgPrice", 0)) or price
    return build_pos(sym, avg, qty, actual_not, lev, is_real=True,
                     order_id=res.get("orderId", ""))

def execute_real_close(pos, reason):
    try:
        res = binance_signed("POST", "/papi/v1/um/order",
                             {"symbol": pos["sym"], "side": "SELL",
                              "type": "MARKET", "quantity": str(pos["qty"]),
                              "reduceOnly": "true"})
        ep = float(res.get("avgPrice", 0)) or last_price(pos["sym"])
    except:
        ep = last_price(pos["sym"])
    pnl = (ep - pos["entry"]) * pos["qty"]
    return ep, pnl

def build_pos(sym, entry, qty, notional, lev, is_real=False, order_id=""):
    # Komisyonun 1.5 katı üzerinde (+%0.15 net kârda) başa baş koruması
    be_sl_price = entry + (est_fee(notional) * 1.5 / qty)
    return {
        "sym": sym, "entry": entry, "qty": qty,
        "notional_usd": round(notional, 2), "leverage": lev,
        "tp_price": entry * (1 + TP_TRIGGER_PCT),
        "be_trigger": entry * (1 + BE_TRIGGER_PCT),
        "be_sl": be_sl_price,
        "sl_price": entry * (1 - SL_PCT),
        "order_id": order_id or f"sim_{uuid.uuid4().hex[:8]}",
        "opened_iso": utc().isoformat(), "opened_ts": ts(),
        "be_hit": False, "trailing_active": False,
        "highest_price": entry, "is_real": is_real,
    }

# ── MONİTÖR ──────────────────────────────────────────────────────────────────

def monitor(state):
    still = []
    for pos in state.get("positions", []):
        sym = pos["sym"]
        try: price = last_price(sym)
        except: still.append(pos); continue

        dur = int((utc() - datetime.fromisoformat(pos["opened_iso"])).total_seconds())
        gpnl = (price - pos["entry"]) * pos["qty"]

        # BE tetik (+%0.45 kârda stop maliyet + %0.15'e çekilir)
        if not pos.get("be_hit") and price >= pos["be_trigger"]:
            pos["sl_price"] = pos["be_sl"]
            pos["be_hit"] = True
            fee = est_fee(pos["notional_usd"])
            locked = (pos["sl_price"] - pos["entry"]) * pos["qty"] - fee
            mode = "🔴" if pos.get("is_real") else "🧪"
            tg(f"🔰 {mode} *{sym}* BE Tetiklendi! Stop → `{fp(pos['sl_price'])}` "
               f"(Komisyon korundu, net kilitli kâr: `${locked:+.2f}`)")

        # Trailing tetik (+%1.00 kârda trailing başlar)
        if price >= pos["tp_price"] or pos.get("trailing_active"):
            if not pos.get("trailing_active"):
                pos["trailing_active"] = True
                pos["highest_price"] = price
                mode = "🔴" if pos.get("is_real") else "🧪"
                tg(f"🚀 {mode} *{sym}* +%{((price/pos['entry'])-1)*100:.2f} kâr! "
                   f"Scalp Trailing aktif, zirve takip ediliyor.")

            pos["highest_price"] = max(pos.get("highest_price", price), price)
            trail_sl = pos["highest_price"] * (1 - TRAILING_DROP_PCT)
            pos["sl_price"] = max(pos["sl_price"], trail_sl)

        # Çıkış kararı
        reason = None
        if pos.get("trailing_active") and price <= pos["sl_price"]:
            reason = "TRAILING_TP"
        elif price <= pos["sl_price"]:
            reason = "BREAKEVEN" if pos.get("be_hit") else "STOP_LOSS"
        elif dur >= MAX_HOLD_SECONDS:
            reason = "TIMEOUT"

        if reason:
            if pos.get("is_real"):
                exit_p, gpnl = execute_real_close(pos, reason)
            else:
                exit_p = price

            net = record_trade(state, pos, exit_p, gpnl, reason, dur)
            fee = est_fee(pos["notional_usd"])
            mode = "🔴" if pos.get("is_real") else "🧪"

            icon = "🟢" if net > 0.01 else ("🔰" if abs(net) <= 0.01 else "🔴")
            result_text = {
                "TRAILING_TP": f"🎯 Trailing Kâr Alındı!",
                "BREAKEVEN": f"🔰 Başa Baş (Kâr Koruması)",
                "STOP_LOSS": f"🛑 Stop Loss",
                "TIMEOUT": f"⏱️ Zaman Aşımı (30 Dk)",
            }.get(reason, reason)

            tg(f"{icon} {mode} *{result_text}* | `{sym}`\n\n"
               f"Giriş: `{fp(pos['entry'])}` → Çıkış: `{fp(exit_p)}`\n"
               f"Gross: `${gpnl:+.2f}` | Fee: `-${fee:.2f}`\n"
               f"💰 *Net: `${net:+.2f} USDT`*\n"
               f"Süre: `{dur // 60} dk {dur % 60} sn`\n"
               f"Zaman: `{ts()}`")

            state.setdefault("cooldown", {})[sym] = time.time()
            print(f"🔒 [{reason}] {sym} Net: ${net:+.2f}", flush=True)
        else:
            mode = "[G]" if pos.get("is_real") else "[S]"
            trail = f" T:{fp(pos['sl_price'])}" if pos.get("trailing_active") else ""
            print(f"  {mode} {sym} P:{fp(price)} PnL:${gpnl:+.2f} "
                  f"TP:{fp(pos['tp_price'])} SL:{fp(pos['sl_price'])}{trail}",
                  flush=True)
            still.append(pos)

    state["positions"] = still
    return state

# ── TARAMA ────────────────────────────────────────────────────────────────────

def scan(state, universe):
    is_real = state.get("real_trading", False)

    if len(state.get("positions", [])) >= 1:
        return state

    ok, reason = can_open_trade(state)
    if not ok:
        return state

    if not btc_safe():
        return state

    # Bakiye ve pozisyon büyüklüğü
    if is_real:
        eq, fm = get_account_balances()
        if fm < 5.0: return state
        margin = min(fm * 0.65, 20.0)
    else:
        bal = get_sim_balance(state)
        if bal < 5.0: return state
        margin = min(bal * 0.65, 20.0)
        fm = margin

    target_not = min(MAX_NOTIONAL, margin * DEFAULT_LEVERAGE)

    open_syms = {p["sym"] for p in state.get("positions", [])} | PROTECTED
    cooldown = state.setdefault("cooldown", {})

    for i, (sym, qv, chg) in enumerate(universe):
        if sym in open_syms: continue
        if i % 20 == 0:
            print(f"  Scalp Taranıyor... [{i+1}/{len(universe)}]", end="\r", flush=True)

        sig = analyze(sym, cooldown)
        if sig:
            mode = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"
            print(f"\n✅ [{mode}] {sym} 5M Scalp sinyali bulundu!", flush=True)

            if is_real:
                pos = execute_real_entry(sym, target_not, fm)
            else:
                entry = sig["entry"]
                rules = get_symbol_rules(sym)
                qty = round_step(target_not / entry, rules["stepSize"], rules["qtyPrec"])
                if qty < rules["minQty"]: qty = rules["minQty"]
                actual_not = qty * entry
                pos = build_pos(sym, entry, qty, actual_not, DEFAULT_LEVERAGE)

            state.setdefault("positions", []).append(pos)

            fee = est_fee(pos["notional_usd"])
            today_cnt = trades_today_count(state) + 1
            reasons_txt = "\n".join(f"  {r}" for r in sig["reasons"])

            tg(f"{'🔴' if is_real else '🧪'} *YENİ SCALP POZİSYONU* | `{sym}`\n\n"
               f"Mod: *{mode}*\n"
               f"Yön: *LONG 🟢*\n"
               f"Giriş: `{fp(pos['entry'])}` | Büyüklük: `${pos['notional_usd']:.0f}` ({pos['leverage']}x)\n"
               f"🎯 TP Trailing: `{fp(pos['tp_price'])}` (+%{TP_TRIGGER_PCT*100:.1f})\n"
               f"🔰 BE: `{fp(pos['be_trigger'])}` (+%{BE_TRIGGER_PCT*100:.2f})\n"
               f"🛑 SL: `{fp(pos['sl_price'])}` (-%{SL_PCT*100:.2f})\n"
               f"💸 Tahmini Fee: `${fee:.2f}`\n"
               f"📅 Bugün İşlem: {today_cnt}\n\n"
               f"*Setup:*\n{reasons_txt}\n\n"
               f"Zaman: `{ts()}`")
            break

        time.sleep(0.02)

    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 60, flush=True)
    print("⚡ 5M MOMENTUM SCALP SNIPER v2.0 (LONG ONLY)", flush=True)
    print("=" * 60, flush=True)
    print(f" Mod           : {'SİMÜLASYON (gerçek paraya dokunulmaz)' if not REAL_TRADING_DEFAULT else 'GERÇEK'}", flush=True)
    print(f" Strateji      : 15M Trend + 5M Momentum Scalp (LONG)", flush=True)
    print(f" Kaldıraç      : {DEFAULT_LEVERAGE}x | Maks Pozisyon: ${MAX_NOTIONAL:.0f}", flush=True)
    print(f" TP Trailing   : +%{TP_TRIGGER_PCT*100:.1f} tetik, -%{TRAILING_DROP_PCT*100:.2f} geri çekilme", flush=True)
    print(f" BE Koruma     : +%{BE_TRIGGER_PCT*100:.2f} kârda stop komisyon üstüne", flush=True)
    print(f" SL            : -%{SL_PCT*100:.2f}", flush=True)
    print(f" Zaman Aşımı   : {MAX_HOLD_SECONDS//60} dakika", flush=True)
    print(f" Komisyon      : %{COMMISSION_RATE*100:.2f} (her zaman net hesaplanır)", flush=True)
    print("=" * 60, flush=True)

    state = load_st()

    # Varsayılan simülasyona başla
    if "sim_balance" not in state or "real_trading" not in state:
        state["sim_balance"] = SIM_STARTING_BALANCE
        state["real_trading"] = False
        save_st(state)

    # 24 saatten eski bayat pozisyonları temizle
    cleaned_positions = []
    for p in state.get("positions", []):
        try:
            opened = datetime.fromisoformat(p["opened_iso"])
            if (utc() - opened).total_seconds() < 86400:
                cleaned_positions.append(p)
        except: pass
    state["positions"] = cleaned_positions
    save_st(state)

    is_real = state.get("real_trading", False)
    mode = "🔴 GERÇEK İŞLEM" if is_real else "🧪 SİMÜLASYON ($20 sanal kasa)"
    bal = get_sim_balance(state)

    tg(f"⚡ *5M Momentum Scalp Sniper v2.0 Başlatıldı*\n\n"
       f"⚙️ Mod: `{mode}`\n"
       f"🎯 Yön: `SADECE LONG (A+ Kalite)`\n"
       f"💰 Sanal Kasa: `${bal:.2f} USDT`\n"
       f"📋 Scalp Kuralları:\n"
       f"  • 15M Trend Filtresi + 5M Micro Breakout\n"
       f"  • Hacim Patlaması (RVOL >= 1.6x) Şartı\n"
       f"  • TP: +%{TP_TRIGGER_PCT*100:.1f} Trailing | BE: +%{BE_TRIGGER_PCT*100:.2f} | SL: -%{SL_PCT*100:.2f}\n"
       f"  • Max Süre: {MAX_HOLD_SECONDS//60} dk | Komisyon garantili koruma\n\n"
       f"🎮 Komutlar: /durum /rapor /gercek /fake /kapat /reset")

    last_scan = 0
    last_hb = 0

    while True:
        try:
            state = load_st()
            now = time.time()

            state = handle_telegram(state)

            # Heartbeat her 2 dakika
            if now - last_hb >= 120:
                is_real = state.get("real_trading", False)
                bal = get_sim_balance(state)
                pnl = bal - SIM_STARTING_BALANCE
                today_cnt = trades_today_count(state)
                n_pos = len(state.get("positions", []))
                mode_tag = "G" if is_real else "S"
                print(f"💓 [{mode_tag}] Kasa:${bal:.2f} (PnL:${pnl:+.2f}) "
                      f"Pos:{n_pos}/1 Bugün:{today_cnt} "
                      f"{utc().strftime('%H:%M UTC')}", flush=True)
                last_hb = now

            if state.get("positions"):
                state = monitor(state)
                save_st(state)

            if not state.get("positions") and now - last_scan >= SCAN_INTERVAL:
                universe = get_universe()
                state = scan(state, universe)
                save_st(state)
                last_scan = time.time()

            time.sleep(1.5)

        except Exception as e:
            print(f"[HATA] {e}", flush=True)
            time.sleep(3.0)

if __name__ == "__main__":
    main()
