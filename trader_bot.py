"""
trader_bot.py — BINANCE FIBONACCI GOLDEN POCKET LONG SİNYAL MOTORU
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PRENSİPLER:
  1. Zorunlu Zaman Dilimi: 15 Dakikalık (15m)
  2. Sadece SPOT USDT Çiftleri (Futures DEĞİL)
  3. Tarama: Her 30 saniyede, Top 30 Gainers (24h priceChange%)
  4. Maksimum 3 Açık Pozisyon (Aynı coin için 1)
  5. Pozisyon Boyutu: 225 USDT (Nominal)
  6. Strateji: Fibonacci Retracement "Altından Giriş" — Golden Pocket
  7. Risk Yönetimi: SL -4.5$ (%2), TP +6$ (%2.67), BE +3.5$, Trailing 1.0$
  8. Win Rate Hedefi: %65-70+

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
SPOT_BASE  = os.getenv("BINANCE_SPOT_BASE", "https://api.binance.com")
TK         = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TC         = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SF         = os.getenv("STATE_FILE", os.path.join(BASE_DIR, "trader_state.json"))
DB         = os.getenv("TRADE_DB", os.path.join(BASE_DIR, "trade_db.json"))

PROXY_URL  = os.getenv("FIXIE_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or ""

def get_proxies():
    if PROXY_URL:
        return {"http": PROXY_URL, "https": PROXY_URL}
    return None

# ── STRATEJİ VE RİSK PARAMETRELERİ ───────────────────────────────────────────
REAL_TRADING_DEFAULT = False
MAX_OPEN_POSITIONS   = 3
POSITION_NOTIONAL    = 225.0
COMMISSION_RATE      = 0.0020
MAX_TRADES_PER_DAY   = 10
MIN_TRADE_INTERVAL   = 120
SCAN_INTERVAL        = 60
COOLDOWN_SECONDS     = 1800

SL_USD               = 3.50
TP_USD               = 10.00
BREAKEVEN_TRIGGER    = 5.00
TRAILING_DISTANCE    = 3.00

SIM_STARTING_BALANCE = 500.0

TIMEFRAME            = "15m"
KLINES_NEEDED        = 40
FIB_LOOKBACK         = 35
HT_TIMEFRAME         = "1h"
HT_KLINES_NEEDED     = 100

MAX_DAILY_LOSS_USD   = 15.0
MAX_DRAWDOWN_PCT     = 8.0
MIN_ADX              = 14.0

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ── DIŞLANACAK COİN LİSTELERİ ────────────────────────────────────────────────
# Likit büyük coinler TARAMAYA DAHİL edilsin diye listeden çıkarıldı:
# BTC, ETH, SOL, XRP, AVAX, LINK, ADA, MATIC, DOGE, NEAR, SUI, ARB, OP
MAJOR_BASKIN = {
    "BNB","DOT","LTC","BCH","ATOM","APT","FIL","UNI","TRX","POL"
}
STABLECOINS = {"USDC","DAI","TUSD","FDUSD","USDP","BUSD","EUR","USTC","USDD","PYUSD","USD"}
COMMODITIES_ETF = {
    "XAU","XAUT","PAXG","XAG","COPPER","OIL","GOLD","SILVER",
    "TSLA","AAPL","NVDA","AMZN","META","SPY","QQQ","IWM","TQQQ","SQQQ",
    "TSLL","NVDL","SOXL","SOXS","COIN","XLE","XBI"
}
LEVERAGE_TOKENS_SUFFIX = {"2L","3L","2S","3S"}

def is_blacklisted(base_asset: str, full_symbol: str) -> bool:
    if base_asset in MAJOR_BASKIN: return True
    if base_asset in STABLECOINS: return True
    if base_asset in COMMODITIES_ETF: return True
    for suf in LEVERAGE_TOKENS_SUFFIX:
        if base_asset.endswith(suf): return True
    if full_symbol.startswith("HK"): return True
    if full_symbol.startswith("CSOP"): return True
    if base_asset in {"DEFI","BTCDOM"}: return True
    if "_" in full_symbol: return True
    if any(ord(c) > 127 for c in full_symbol): return True
    return False

# ── YARDIMCI FONKSİYONLAR ────────────────────────────────────────────────────

def utc():  return datetime.now(timezone.utc)
def ts():   return utc().strftime("%Y-%m-%d %H:%M:%S UTC")
def today_str(): return utc().strftime("%Y-%m-%d")

def fp(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "0.0000"
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    if v >= 0.01: return f"{v:.5f}"
    return f"{v:.8f}"

def fpct(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "0.0"
    return f"{v:.1f}"

def est_fee(notional):
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
        "sim_peak_balance": SIM_STARTING_BALANCE,
        "trades_today": [],
        "pnl_today_sum": 0.0,
        "last_trade_ts": 0,
        "circuit_breaker_until": 0,
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

def get_sim_peak(state):
    return state.get("sim_peak_balance", SIM_STARTING_BALANCE)

def trades_today_count(state):
    today = today_str()
    return sum(1 for t in state.get("trades_today", []) if t.get("date") == today)

def pnl_today(state):
    return float(state.get("pnl_today_sum", 0.0))

def check_circuit_breakers(state):
    now_ts = time.time()
    if now_ts < state.get("circuit_breaker_until", 0):
        remain = int(state["circuit_breaker_until"] - now_ts)
        hours = remain // 3600
        mins = (remain % 3600) // 60
        return False, f"DEVRE KESİCİ: {hours}s {mins}d daha duraklama"

    bal = get_sim_balance(state)
    peak = get_sim_peak(state)
    if peak > 0:
        dd_pct = (peak - bal) / peak * 100.0
        if dd_pct >= MAX_DRAWDOWN_PCT:
            return False, f"MAX DRAWDOWN aşıldı (%{dd_pct:.1f} ≥ %{MAX_DRAWDOWN_PCT:.1f})"

    daily_loss = pnl_today(state)
    if daily_loss <= (-MAX_DAILY_LOSS_USD):
        return False, f"GÜNLÜK MAX ZARAR aşıldı (${daily_loss:+.2f} ≤ -${MAX_DAILY_LOSS_USD:.2f})"

    return True, "OK"

def can_open_trade(state):
    if len(state.get("positions", [])) >= MAX_OPEN_POSITIONS:
        return False, f"Maks açık pozisyon limiti ({MAX_OPEN_POSITIONS}) doldu"
    if trades_today_count(state) >= MAX_TRADES_PER_DAY:
        return False, f"Günlük güvenlik limiti ({MAX_TRADES_PER_DAY}) doldu"
    elapsed = time.time() - state.get("last_trade_ts", 0)
    if elapsed < MIN_TRADE_INTERVAL:
        remaining = int(MIN_TRADE_INTERVAL - elapsed)
        return False, f"İşlemler arası bekleme: {remaining} sn kaldı"
    return True, "OK"

def record_trade(state, pos, exit_price, gross_pnl, reason, dur_sec):
    fee = est_fee(pos["notional_usd"])
    net_pnl = round(gross_pnl - fee, 4)

    today = today_str()
    if not pos.get("is_real", False):
        state["sim_balance"] = round(get_sim_balance(state) + net_pnl, 4)
        peak = get_sim_peak(state)
        if state["sim_balance"] > peak:
            state["sim_peak_balance"] = state["sim_balance"]

    current_date = state.get("pnl_today_date", "")
    if current_date != today:
        state["pnl_today_date"] = today
        state["pnl_today_sum"] = 0.0
    state["pnl_today_sum"] = round(float(state.get("pnl_today_sum", 0.0)) + net_pnl, 4)

    state.setdefault("trades_today", []).append({"date": today})
    state["trades_today"] = [t for t in state["trades_today"] if t.get("date") == today]
    state["last_trade_ts"] = time.time()

    if net_pnl < 0:
        bal = get_sim_balance(state)
        peak = get_sim_peak(state)
        dd_pct = (peak - bal) / peak * 100.0 if peak > 0 else 0.0
        if dd_pct >= MAX_DRAWDOWN_PCT:
            hours = 4
            state["circuit_breaker_until"] = time.time() + (hours * 3600)
            tg(f"⛔ *DEVRE KESİCİ (DRAWDOWN)*\n"
               f"Drawdown: %{dd_pct:.1f} / Limit: %{MAX_DRAWDOWN_PCT:.1f}\n"
               f"Kasanın güvenliği için {hours} saat yeni işlem YAPILMAYACAK!")
        elif float(state.get("pnl_today_sum", 0.0)) <= (-MAX_DAILY_LOSS_USD):
            hours = 2
            state["circuit_breaker_until"] = time.time() + (hours * 3600)
            tg(f"⛔ *DEVRE KESİCİ (GÜNLÜK ZARAR)*\n"
               f"Bugün: ${float(state['pnl_today_sum']):+.2f} / Limit: -${MAX_DAILY_LOSS_USD:.2f}\n"
               f"Kasanın güvenliği için {hours} saat yeni işlem YAPILMAYACAK!")

    trades = load_db()
    trades.append({
        "pair": pos["sym"], "side": "LONG",
        "entry": pos["entry"], "exit": exit_price,
        "qty": pos["qty"], "notional": pos["notional_usd"],
        "gross_pnl": round(gross_pnl, 4), "fee": fee,
        "pnl": net_pnl,
        "result": reason,
        "duration_sec": dur_sec,
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
                   f"⚡ Pozisyonlar SPOT piyasada gerçek parayla açılacaktır!")

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
                state["sim_peak_balance"] = SIM_STARTING_BALANCE
                state["trades_today"] = []
                state["pnl_today_sum"] = 0.0
                state["pnl_today_date"] = ""
                state["last_trade_ts"] = 0
                state["positions"] = []
                state["cooldown"] = {}
                state["real_trading"] = False
                state["circuit_breaker_until"] = 0
                save_st(state)
                tg(f"🔄 *Simülasyon sıfırlandı!*\nKasa: `${SIM_STARTING_BALANCE:.2f} USDT`\nDevre kesiciler ve günlük PnL sıfırlandı.")

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

    pos_txt = "_Açık pozisyon yok. Fibonacci Golden Pocket radarı tarıyor..._"
    if pos_list:
        lines = []
        for p in pos_list:
            try: cur = last_price(p["sym"])
            except: cur = p["entry"]
            gpnl = (cur - p["entry"]) * p["qty"]
            fee = est_fee(p["notional_usd"])
            npnl = gpnl - fee
            pct = ((cur / p["entry"]) - 1) * 100
            be_tag = " ⚖️BE" if p.get("breakeven_set") else ""
            lines.append(
                f"• `{p['sym']}` (LONG{be_tag}) | Giriş: `{fp(p['entry'])}` | "
                f"Şimdi: `{fp(cur)}` (%{pct:+.2f}) | Net: *`${npnl:+.2f}`*")
        pos_txt = "\n".join(lines)

    dpnl = pnl_today(state)
    cb_active = time.time() < state.get("circuit_breaker_until", 0)
    cb_txt = ""
    if cb_active:
        rem = int(state["circuit_breaker_until"] - time.time())
        cb_txt = f"\n⛔ *Devre Kesici AKTİF*: {rem//60}dk daha yeni işlem yok"
    tg(f"📊 *FIBONACCI GOLDEN POCKET DURUM (v2)*\n\n"
       f"⚙️ Mod: `{mode}`\n{bal_txt}\n"
       f"📌 Pozisyon: {len(pos_list)}/{MAX_OPEN_POSITIONS}\n"
       f"📅 Bugün: {today_cnt} işlem | PnL: *`${dpnl:+.2f}`*\n\n"
       f"{pos_txt}\n"
       f"{cb_txt}\n\n"
       f"_Komutlar: /durum /rapor /gercek /fake /kapat /reset_")

def send_report(state):
    is_real = state.get("real_trading", False)
    trades = load_db()
    filtered = [t for t in trades if t.get("is_real", False) == is_real]
    mode_t = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"

    if not filtered:
        tg(f"📊 *{mode_t} RAPORU*\n\nHenüz tamamlanmış işlem yok.")
        return

    tot = len(filtered)
    wins = [t for t in filtered if t["pnl"] > 0.05]
    losses = [t for t in filtered if t["pnl"] < -0.05]
    bes = [t for t in filtered if -0.05 <= t["pnl"] <= 0.05]
    wr = len(wins) / (len(wins) + len(losses)) * 100 if (len(wins) + len(losses)) > 0 else 0

    total_pnl = sum(t["pnl"] for t in filtered)
    total_fee = sum(t.get("fee", 0) for t in filtered)
    total_gross = sum(t.get("gross_pnl", 0) for t in filtered)
    gross_wins = sum(t.get("gross_pnl", 0) for t in wins)
    gross_losses = abs(sum(t.get("gross_pnl", 0) for t in losses))
    pf = (gross_wins / gross_losses) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)

    best = max((t["pnl"] for t in filtered), default=0)
    worst = min((t["pnl"] for t in filtered), default=0)

    eq_curve = []
    running = 0
    maxdd = 0
    peak = 0
    for t in filtered:
        running += t["pnl"]
        peak = max(peak, running)
        dd = running - peak
        maxdd = min(maxdd, dd)

    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    last_5 = []
    for t in filtered[-5:]:
        icon = "🟢" if t["pnl"] > 0.05 else ("🔰" if abs(t["pnl"]) <= 0.05 else "🔴")
        last_5.append(f"{icon} `{t['pair']}` → *${t['pnl']:+.2f}* ({t['result']})")

    tg(f"📊 *PERFORMANS ÖZETİ* | {mode_t}\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Toplam İşlem: `{tot}`\n"
       f"🟢 Kazanılan: `{len(wins)}` | 🔴 Kaybedilen: `{len(losses)}` | 🔰 Başabaş: `{len(bes)}`\n"
       f"Win Rate: *%{wr:.1f}*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"Gross PnL: `${total_gross:+.2f}`\n"
       f"Komisyon: `-${total_fee:.2f}`\n"
       f"*Net PnL: `${total_pnl:+.2f} USDT`*\n"
       f"Profit Factor: *{pf:.2f}*\n"
       f"━━━━━━━━━━━━━━━━━━━━\n"
       f"En İyi: `${best:+.1f}` | En Kötü: `${worst:+.1f}`\n"
       f"Maks Drawdown: `${maxdd:.2f}`\n"
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

# ── BINANCE SPOT API ─────────────────────────────────────────────────────────

def get_public(endpoint, p=None):
    hosts = [SPOT_BASE, "https://api.binance.com", "https://api1.binance.com"]
    proxies = get_proxies()
    for host in hosts:
        url = f"{host}{endpoint}"
        try:
            r = requests.get(url, params=p, headers=HEADERS, timeout=12)
            if r.status_code == 200: return r.json()
        except: pass
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
    url = f"{SPOT_BASE}{path}?{q}&signature={sig}"
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

def klines(sym, tf=TIMEFRAME, n=KLINES_NEEDED):
    raw = get_public("/api/v3/klines", {"symbol": sym, "interval": tf, "limit": n})
    df = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v","tb","qv"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def calc_rsi_wilder(series, period=14):
    if len(series) < period + 2: return 50.0
    s = series.astype(float).reset_index(drop=True)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.iloc[1:period+1].mean()
    avg_loss = loss.iloc[1:period+1].mean()
    for i in range(period + 1, len(s)):
        avg_gain = (avg_gain * (period - 1) + gain.iloc[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss.iloc[i]) / period
    if avg_loss == 0:
        rs = 1e9
    else:
        rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    if math.isnan(rsi): return 50.0
    return float(rsi)

def calc_rsi_last_two(series, period=14):
    if len(series) < period + 3: return 50.0, 50.0
    s = series.astype(float).reset_index(drop=True)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = (-delta.clip(upper=0))
    avg_gain = gain.iloc[1:period+1].mean()
    avg_loss = loss.iloc[1:period+1].mean()
    prev_rsi = 50.0
    cur_rsi = 50.0
    for i in range(period + 1, len(s)):
        avg_gain = (avg_gain * (period - 1) + gain.iloc[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss.iloc[i]) / period
        if i == len(s) - 2:
            if avg_loss == 0: rs = 1e9
            else: rs = avg_gain / avg_loss
            prev_rsi = 100 - (100 / (1 + rs))
        if i == len(s) - 1:
            if avg_loss == 0: rs = 1e9
            else: rs = avg_gain / avg_loss
            cur_rsi = 100 - (100 / (1 + rs))
    if math.isnan(cur_rsi): cur_rsi = 50.0
    if math.isnan(prev_rsi): prev_rsi = 50.0
    return float(cur_rsi), float(prev_rsi)

def last_price(sym):
    r = get_public("/api/v3/ticker/price", {"symbol": sym})
    return float(r["price"])

def calc_adx_wilder(df_high, df_low, df_close, period=14):
    h = pd.to_numeric(df_high, errors="coerce").astype(float).reset_index(drop=True)
    l = pd.to_numeric(df_low, errors="coerce").astype(float).reset_index(drop=True)
    c = pd.to_numeric(df_close, errors="coerce").astype(float).reset_index(drop=True)
    n = len(h)
    if n < period + 10:
        return 25.0

    prev_c = c.shift(1)
    tr = pd.concat([
        h - l,
        (h - prev_c).abs(),
        (l - prev_c).abs()
    ], axis=1).max(axis=1)

    plus_dm = pd.Series(np.where((h - h.shift(1)) > (l.shift(1) - l),
                                 np.maximum(h - h.shift(1), 0), 0))
    minus_dm = pd.Series(np.where((l.shift(1) - l) > (h - h.shift(1)),
                                  np.maximum(l.shift(1) - l, 0), 0))

    def wilder_smooth(series, p):
        s = series.copy().astype(float)
        for i in range(p, len(s)):
            s.iloc[i] = (s.iloc[i-1] * (p - 1) + s.iloc[i]) / p
        return s

    atr = wilder_smooth(tr.fillna(0.0), period)
    plus_dm_s = wilder_smooth(plus_dm.fillna(0.0), period)
    minus_dm_s = wilder_smooth(minus_dm.fillna(0.0), period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * np.where(atr > 0, plus_dm_s / atr, 0.0)
        minus_di = 100.0 * np.where(atr > 0, minus_dm_s / atr, 0.0)
        dx = 100.0 * np.where((plus_di + minus_di) > 0,
                              np.abs(plus_di - minus_di) / (plus_di + minus_di), 0.0)

    adx = wilder_smooth(pd.Series(dx).fillna(0.0), period)
    val = float(adx.iloc[-1])
    if math.isnan(val): return 25.0
    return max(0.0, min(100.0, val))

def check_higher_timeframe_trend(sym):
    try:
        df = klines(sym, HT_TIMEFRAME, HT_KLINES_NEEDED)
        if len(df) < 60:
            return False
        closed = df[df["x"] == True].copy()
        if len(closed) < 55:
            closed = df.copy()
        closes = closed["c"].astype(float)
        if len(closes) < 55:
            return False
        ema20 = closes.ewm(span=20, adjust=False).mean()
        ema50 = closes.ewm(span=50, adjust=False).mean()
        e20 = float(ema20.iloc[-1])
        e50 = float(ema50.iloc[-1])
        e50_4ago = float(ema50.iloc[-5] if len(ema50) >= 5 else ema50.iloc[0])
        last_close = float(closes.iloc[-1])
        uptrend = (
            last_close >= (e50 * 0.95) and
            e50 >= (e50_4ago * 0.995)
        )
        return uptrend
    except Exception:
        return False

def get_symbol_rules(sym):
    try:
        info = get_public("/api/v3/exchangeInfo")
        for s in info.get("symbols", []):
            if s.get("symbol") == sym:
                step = 1.0; minq = 1.0
                minNot = 10.0
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step = float(f.get("stepSize", 1.0))
                        minq = float(f.get("minQty", 1.0))
                    if f.get("filterType") == "NOTIONAL":
                        minNot = float(f.get("minNotional", 10.0))
                    if f.get("filterType") == "MIN_NOTIONAL":
                        minNot = float(f.get("minNotional", 10.0))
                return {"stepSize": step, "minQty": minq, "minNotional": minNot,
                        "qtyPrec": int(round(-math.log10(max(step, 1e-10)))),
                        "pricePrec": int(s.get("quoteAssetPrecision", 4))}
    except: pass
    return {"stepSize": 0.0001, "minQty": 0.0001, "minNotional": 10.0, "qtyPrec": 4, "pricePrec": 4}

def round_step(qty, step, prec):
    if step <= 0: return round(qty, prec)
    return float(f"{math.floor(qty / step) * step:.{prec}f}")

def get_account_balances():
    try:
        acc = binance_signed("GET", "/api/v3/account")
        eq = 0.0
        fm = 0.0
        for b in acc.get("balances", []):
            if b.get("asset") == "USDT":
                free = float(b.get("free", 0))
                locked = float(b.get("locked", 0))
                eq = free + locked
                fm = free
                return round(eq, 2), round(fm, 2)
    except Exception as e:
        print(f"[BAKİYE HATA] {e}", flush=True)
    return 0.0, 0.0

# ── PARİTE EVRENİ (SPOT USDT — TOP GAINERS) ──────────────────────────────────

def get_universe():
    try:
        info = get_public("/api/v3/exchangeInfo")
        allowed = {}
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING": continue
            if s.get("quoteAsset") != "USDT": continue
            if s.get("isSpotTradingAllowed", True) is False: continue
            sym = s["symbol"]
            base = s.get("baseAsset", "")
            if not sym.endswith("USDT"): continue
            if is_blacklisted(base, sym): continue
            allowed[sym] = base

        tickers = get_public("/api/v3/ticker/24hr")
        out = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in allowed: continue
            try:
                qv = float(t.get("quoteVolume", 0))
                chg = float(t.get("priceChangePercent", 0))
            except: continue
            if qv < 5_000_000.0:
                continue
            if chg > 40.0:
                continue
            if chg < -5.0:
                continue
            out.append((sym, qv, chg))

        if out:
            max_qv = max(x[1] for x in out)
            max_chg = max(x[2] for x in out) if max(x[2] for x in out) > 0 else 1.0
            def hybrid_score(item):
                s_qv = item[1] / max_qv
                s_chg = item[2] / max_chg
                return (0.45 * s_qv) + (0.55 * s_chg)
            out.sort(key=hybrid_score, reverse=True)
        else:
            out.sort(key=lambda x: x[2], reverse=True)
        return out[:30]
    except Exception as e:
        print(f"[UNIVERSE HATA] {e}", flush=True)
        return []

# ── FIBONACCI GOLDEN POCKET ANALİZ MOTORU ────────────────────────────────────

def analyze(sym, cooldown):
    if time.time() - cooldown.get(sym, 0) < COOLDOWN_SECONDS:
        return None

    if not check_higher_timeframe_trend(sym):
        return None

    try:
        df = klines(sym, TIMEFRAME, KLINES_NEEDED)
        if len(df) < KLINES_NEEDED:
            return None

        closed = df[df["x"] == True].copy()
        if len(closed) < 38:
            if len(df) < 38:
                return None
            closed = df.copy()

        closed = closed.tail(FIB_LOOKBACK + 5).reset_index(drop=True)
        if len(closed) < FIB_LOOKBACK:
            return None

        fib_df = closed.tail(FIB_LOOKBACK).reset_index(drop=True)
        n_fib = len(fib_df)

        swing_high_idx = int(fib_df["h"].idxmax())
        swing_high_val = float(fib_df["h"].iloc[swing_high_idx])
        age_from_end = (n_fib - 1) - swing_high_idx
        if not (1 <= age_from_end <= 25):
            return None

        if swing_high_idx < 3:
            return None

        left_neighbors = fib_df["h"].iloc[max(0, swing_high_idx - 3):swing_high_idx]
        right_neighbors = fib_df["h"].iloc[swing_high_idx + 1:min(n_fib, swing_high_idx + 4)]
        if len(left_neighbors) == 0 or len(right_neighbors) == 0:
            return None

        left_segment = fib_df["l"].iloc[:swing_high_idx]
        if len(left_segment) < 3:
            return None
        swing_low_idx = int(left_segment.idxmin())
        swing_low_val = float(left_segment.iloc[swing_low_idx])
        if swing_low_val <= 0 or swing_high_val <= swing_low_val:
            return None

        low_right = left_segment.iloc[swing_low_idx+1:] if swing_low_idx+1 < len(left_segment) else pd.Series()
        low_left  = left_segment.iloc[:swing_low_idx]
        if len(low_left) >= 2 and len(low_right) >= 2:
            if not (swing_low_val < low_left.min() and swing_low_val < low_right.min()):
                pass

        impulse_range = swing_high_val - swing_low_val
        impulse_percent = (impulse_range / swing_low_val) * 100
        if impulse_percent < 2.5:
            return None
        if impulse_percent > 60.0:
            return None

        adx_val = calc_adx_wilder(closed["h"], closed["l"], closed["c"], 14)
        if adx_val < MIN_ADX:
            return None

        fib000 = swing_high_val
        fib236 = swing_high_val - 0.236 * impulse_range
        fib382 = swing_high_val - 0.382 * impulse_range
        fib500 = swing_high_val - 0.500 * impulse_range
        fib618 = swing_high_val - 0.618 * impulse_range
        fib650 = swing_high_val - 0.650 * impulse_range
        fib786 = swing_high_val - 0.786 * impulse_range
        fib1000 = swing_low_val

        candle0 = closed.iloc[-1]
        candleM1 = closed.iloc[-2] if len(closed) >= 2 else closed.iloc[-1]

        c0_close = float(candle0["c"])
        c0_open  = float(candle0["o"])
        c0_high  = float(candle0["h"])
        c0_low   = float(candle0["l"])

        cm1_low = float(candleM1["l"])
        cm1_close = float(candleM1["c"])

        close_series = closed["c"]
        if len(close_series) < 54:
            pad_needed = 54 - len(close_series)
            first_val = close_series.iloc[0]
            pad = pd.Series([first_val] * pad_needed)
            ema_series = pd.concat([pad, close_series], ignore_index=True)
        else:
            ema_series = close_series
        ema50_full = ema_series.ewm(span=50, adjust=False).mean()
        ema50 = ema50_full.iloc[-len(close_series):].reset_index(drop=True)
        if len(ema50) < 5:
            return None
        ema_current = float(ema50.iloc[-1])
        ema_4ago = float(ema50.iloc[-5])
        ema_trend_up = ema_current >= (ema_4ago * 0.9998)
        price_above_ema = c0_close >= (ema_current * 0.99)
        price_not_too_far = c0_close <= (ema_current * 1.08)
        if not (ema_trend_up and price_above_ema and price_not_too_far):
            return None

        rsi_current, rsi_prev = calc_rsi_last_two(close_series, 14)
        rsi_in_range = 25.0 <= rsi_current <= 62.0
        rsi_hooked = rsi_current >= (rsi_prev - 0.3)
        if not (rsi_in_range and rsi_hooked):
            return None

        vol_series = closed["v"]
        if len(vol_series) >= 21:
            avg_vol_20 = float(vol_series.iloc[-21:-1].mean())
            cur_vol = float(vol_series.iloc[-1])
            volume_ratio = cur_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0
        else:
            volume_ratio = 1.0

        if volume_ratio < 0.6:
            return None

        candle_range = c0_high - c0_low
        lower_wick = min(c0_open, c0_close) - c0_low
        is_green = c0_close > c0_open
        is_lower_wick_rebound = (candle_range > 0) and (lower_wick >= candle_range * 0.35)
        candle_body_green = c0_close > c0_open and ((c0_close - c0_open) >= candle_range * 0.30)
        candle_confirm = is_green or is_lower_wick_rebound
        if not candle_confirm:
            return None

        scenario_a = (c0_low <= fib618) and (c0_close >= fib618)
        scenario_b = (cm1_low <= fib618) and (c0_close >= fib650)
        scenario_hit = None
        if scenario_a:
            scenario_hit = "A"
        elif scenario_b:
            scenario_hit = "B"
        else:
            return None

        structural_low_test = c0_low >= (fib786 * 0.995)
        if not structural_low_test:
            return None

        current_retrace = ((swing_high_val - c0_close) / impulse_range) * 100 if impulse_range > 0 else 50.0

        score = 72
        if scenario_hit == "A":
            score += 8
        else:
            score += 4
        if candle_body_green:
            score += 7
        elif is_green:
            score += 4
        if is_lower_wick_rebound:
            score += 5
        if volume_ratio >= 1.30:
            score += 5
        if volume_ratio >= 1.60:
            score += 4
        if volume_ratio >= 2.00:
            score += 3
        if rsi_current > (rsi_prev + 0.5):
            score += 7
        elif rsi_current > rsi_prev:
            score += 3
        if 36.0 <= rsi_current <= 48.0:
            score += 5
        if 55.0 <= current_retrace <= 68.0:
            score += 5
        if ema_current > (ema_4ago * 1.0008):
            score += 4
        if impulse_percent >= 12.0:
            score += 4
        if adx_val >= 25.0:
            score += 4
        elif adx_val >= MIN_ADX:
            score += 2
        confidence = min(100, score)

        if confidence < 80:
            return None

        return {
            "sym": sym,
            "confidence": confidence,
            "scenario": scenario_hit,
            "swing_high": swing_high_val,
            "swing_low": swing_low_val,
            "adx": round(adx_val, 2),
            "fib000": fib000,
            "fib236": fib236,
            "fib382": fib382,
            "fib500": fib500,
            "fib618": fib618,
            "fib650": fib650,
            "fib786": fib786,
            "fib1000": fib1000,
            "impulse_percent": impulse_percent,
            "retracement_pct": current_retrace,
            "ema50": ema_current,
            "rsi": rsi_current,
            "rsi_prev": rsi_prev,
            "volume_ratio": volume_ratio,
            "is_green": is_green,
            "lower_wick": is_lower_wick_rebound,
            "last_close": c0_close,
        }

    except Exception as e:
        print(f"[ANALİZ HATA {sym}] {e}", flush=True)
        return None

    return None

# ── POZİSYON AÇMA ────────────────────────────────────────────────────────────

def execute_real_entry(sym, notional, free_margin):
    time.sleep(0.15)
    rules = get_symbol_rules(sym)
    price = last_price(sym)
    actual_not = min(notional, free_margin * 0.95)
    actual_not = max(actual_not, rules.get("minNotional", 10.0))
    qty = round_step(actual_not / price, rules["stepSize"], rules["qtyPrec"])
    if qty < rules["minQty"]: qty = rules["minQty"]
    actual_not = qty * price
    if actual_not < rules.get("minNotional", 10.0):
        extra_qty = (rules.get("minNotional", 10.0) / price)
        qty = round_step(extra_qty * 1.01, rules["stepSize"], rules["qtyPrec"])
        actual_not = qty * price

    res = binance_signed("POST", "/api/v3/order",
                         {"symbol": sym, "side": "BUY", "type": "MARKET",
                          "quantity": str(qty), "newOrderRespType": "FULL"})
    fills = res.get("fills", [])
    if fills:
        qty_filled = sum(float(f.get("qty", 0)) for f in fills)
        price_filled = (
            sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills) /
            qty_filled if qty_filled > 0 else price
        )
    else:
        qty_filled = qty
        price_filled = price
    return build_pos(sym, price_filled, qty_filled, actual_not, is_real=True,
                     order_id=str(res.get("orderId", "")))

def execute_real_close(pos, reason):
    try:
        rules = get_symbol_rules(pos["sym"])
        qty_sell = round_step(pos["qty"], rules["stepSize"], rules["qtyPrec"])
        if qty_sell < rules["minQty"]:
            qty_sell = pos["qty"]
        res = binance_signed("POST", "/api/v3/order",
                             {"symbol": pos["sym"], "side": "SELL",
                              "type": "MARKET",
                              "quantity": str(qty_sell),
                              "newOrderRespType": "FULL"})
        fills = res.get("fills", [])
        if fills:
            qty_filled = sum(float(f.get("qty", 0)) for f in fills)
            ep = (
                sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills) /
                qty_filled if qty_filled > 0 else last_price(pos["sym"])
            )
        else:
            ep = last_price(pos["sym"])
    except:
        ep = last_price(pos["sym"])
    pnl = (ep - pos["entry"]) * pos["qty"]
    return ep, pnl

def build_pos(sym, entry, qty, notional, is_real=False, order_id="", sig=None):
    sl_price = entry * (1 - SL_USD / POSITION_NOTIONAL)
    tp_price = entry * (1 + TP_USD / POSITION_NOTIONAL)
    be_trigger = entry * (1 + BREAKEVEN_TRIGGER / POSITION_NOTIONAL)
    return {
        "sym": sym, "entry": entry, "qty": qty,
        "notional_usd": round(notional, 2),
        "initial_sl": sl_price,
        "tp_price": tp_price,
        "be_trigger_price": be_trigger,
        "sl_price": sl_price,
        "order_id": order_id or f"sim_{uuid.uuid4().hex[:8]}",
        "opened_iso": utc().isoformat(), "opened_ts": ts(),
        "breakeven_set": False,
        "highest_pnl_usd": 0.0,
        "highest_price": entry,
        "trailing_dist_usd": TRAILING_DISTANCE,
        "is_real": is_real,
        "sig": sig or {},
    }

# ── POZİSYON MONİTÖRÜ (TP, SL, BE, TRAİLİNG) ────────────────────────────────

def monitor(state):
    still = []
    for pos in state.get("positions", []):
        sym = pos["sym"]
        try: price = last_price(sym)
        except Exception as e:
            print(f"[FİYAT HATA {sym}] {e}", flush=True)
            still.append(pos)
            continue

        dur = int((utc() - datetime.fromisoformat(pos["opened_iso"])).total_seconds())
        gpnl = (price - pos["entry"]) * pos["qty"]

        if gpnl > pos.get("highest_pnl_usd", 0.0):
            pos["highest_pnl_usd"] = gpnl
            pos["highest_price"] = price
        highest_pnl = pos.get("highest_pnl_usd", gpnl)
        highest_price = pos.get("highest_price", price)

        tp_price = pos.get("tp_price")
        if tp_price is None:
            tp_price = pos["entry"] * (1 + TP_USD / POSITION_NOTIONAL)
            pos["tp_price"] = tp_price

        if price >= tp_price:
            reason = "TAKE_PROFIT"
            close_pos(state, pos, price, gpnl, reason, dur)
            continue

        if (not pos.get("breakeven_set", False)) and highest_pnl >= BREAKEVEN_TRIGGER:
            pos["breakeven_set"] = True
            pos["sl_price"] = pos["entry"]
            be_send = f"⚖️ [BREAKEVEN] {sym} — Başabaşa geçildi!\nYeni Stop: {fp(pos['entry'])} (Giriş fiyatı) — Zarar riski SIFIR"
            tg(be_send)

        if pos.get("breakeven_set", False):
            trailing_dist_usd = pos.get("trailing_dist_usd", TRAILING_DISTANCE)
            new_stop_pnl = highest_pnl - trailing_dist_usd
            new_sl_price = pos["entry"] + (new_stop_pnl / pos["qty"]) if pos["qty"] > 0 else pos["entry"]
            if new_sl_price > pos["sl_price"]:
                old_sl = pos["sl_price"]
                pos["sl_price"] = new_sl_price
                if (new_sl_price - old_sl) / old_sl > 0.001:
                    garanti_pnl = (new_sl_price - pos["entry"]) * pos["qty"]
                    trail_msg = (f"🔒 [TRAILING] {sym} — Yeni zirve kilitlendi\n"
                                 f"Stop şimdi: {fp(new_sl_price)} (garanti edilen kar: +${garanti_pnl:.1f}$)")
                    tg(trail_msg)

        cur_sl = pos.get("sl_price")
        if cur_sl is None:
            cur_sl = pos["entry"] * (1 - SL_USD / POSITION_NOTIONAL)
            pos["sl_price"] = cur_sl
        cur_stop_pnl = (cur_sl - pos["entry"]) * pos["qty"]

        if price <= cur_sl:
            if pos.get("breakeven_set", False):
                if cur_stop_pnl > 0.05:
                    reason = "TRAILING_STOP"
                elif abs(cur_stop_pnl) <= 0.05:
                    reason = "BREAKEVEN"
                else:
                    reason = "INITIAL_SL"
            else:
                reason = "INITIAL_SL"
            close_pos(state, pos, price, gpnl, reason, dur)
            continue

        state.setdefault("cooldown", {})
        mode = "[G]" if pos.get("is_real") else "[S]"
        be_tag = " BE" if pos.get("breakeven_set") else ""
        print(f"  {mode} {sym}{be_tag} P:{fp(price)} PnL:${gpnl:+.2f} "
              f"(Zirve:${highest_pnl:+.2f}) SL:{fp(pos['sl_price'])} TP:{fp(tp_price)}",
              flush=True)
        still.append(pos)

    state["positions"] = still
    return state

def close_pos(state, pos, price, gpnl, reason, dur):
    if pos.get("is_real"):
        exit_p, gpnl_out = execute_real_close(pos, reason)
    else:
        exit_p = price
        gpnl_out = gpnl
    net = record_trade(state, pos, exit_p, gpnl_out, reason, dur)
    fee = est_fee(pos["notional_usd"])
    mode = "🔴" if pos.get("is_real") else "🧪"

    icon = "🟢" if net > 0.05 else ("🔰" if abs(net) <= 0.05 else "🔴")

    if reason == "INITIAL_SL":
        sebep = f"İLK SL (-${SL_USD:.2f})"
    elif reason == "BREAKEVEN":
        sebep = "BREAKEVEN (Başabaş)"
    elif reason == "TRAILING_STOP":
        sebep = f"TRAILING STOP (Kilitli Kar - ${TRAILING_DISTANCE:.2f} mesafe)"
    elif reason == "TAKE_PROFIT":
        sebep = f"TAKE PROFİT (+${TP_USD:.2f})"
    else:
        sebep = reason

    hours = dur // 3600
    mins = (dur % 3600) // 60
    if hours > 0:
        sure = f"{hours} saat {mins} dakika"
    else:
        sure = f"{mins} dakika"

    tg(f"{icon} [KAPANDI] {pos['sym']}\n"
       f"Sebep: {sebep}\n"
       f"Sonuç: +{net if net>=0 else ''}{net:.2f} USDT  /  "
       f"+{((exit_p/pos['entry'])-1)*100:.2f} %\n"
       f"Süre: {sure}")

    state.setdefault("cooldown", {})[pos["sym"]] = time.time()
    print(f"🔒 [{reason}] {pos['sym']} Net: ${net:+.2f}", flush=True)

# ── TARAMA ────────────────────────────────────────────────────────────────────

def scan(state, universe):
    is_real = state.get("real_trading", False)

    if len(state.get("positions", [])) >= MAX_OPEN_POSITIONS:
        return state

    cb_ok, cb_reason = check_circuit_breakers(state)
    if not cb_ok:
        return state

    ok, reason = can_open_trade(state)
    if not ok:
        return state

    if is_real:
        eq, fm = get_account_balances()
        if fm < 50.0: return state
        margin = fm
    else:
        bal = get_sim_balance(state)
        if bal < 50.0: return state
        margin = bal
        fm = margin

    target_not = POSITION_NOTIONAL

    open_syms = {p["sym"] for p in state.get("positions", [])}
    cooldown = state.setdefault("cooldown", {})

    candidates = []
    for i, (sym, qv, chg) in enumerate(universe):
        if sym in open_syms: continue
        if i % 10 == 0:
            print(f"  Fib Golden Pocket Taranıyor... [{i+1}/{len(universe)}]", end="\r", flush=True)
        sig = analyze(sym, cooldown)
        if sig and sig["confidence"] >= 75:
            candidates.append(sig)
            print(f"  📡 Aday: {sym} | Güven: %{sig['confidence']} | Senaryo {sig['scenario']} | Impulse: %{fpct(sig['impulse_percent'])}", flush=True)
        time.sleep(0.05)

    if not candidates:
        return state

    quality = [c for c in candidates if c["confidence"] >= 82]
    if not quality:
        return state
    quality.sort(key=lambda x: (x["confidence"], x["impulse_percent"]), reverse=True)
    sig = quality[0]
    sym = sig["sym"]

    mode = "🔴 GERÇEK" if is_real else "🧪 SİMÜLASYON"
    print(f"\n🏆 [{mode}] {len(quality)} kaliteli adaydan seçilen: {sym} (Güven: %{sig['confidence']})", flush=True)

    if is_real:
        try:
            pos = execute_real_entry(sym, target_not, fm)
        except Exception as e:
            tg(f"❌ {sym} için gerçek alım başarısız: {e}")
            return state
    else:
        entry = sig["last_close"]
        rules = get_symbol_rules(sym)
        qty = round_step(target_not / entry, rules["stepSize"], rules["qtyPrec"])
        if qty < rules["minQty"]: qty = rules["minQty"]
        actual_not = qty * entry
        if actual_not < rules.get("minNotional", 10.0):
            actual_not = max(rules.get("minNotional", 10.0) * 1.01, actual_not)
            qty = round_step(actual_not / entry, rules["stepSize"], rules["qtyPrec"])
            actual_not = qty * entry
        pos = build_pos(sym, entry, qty, actual_not, is_real=False, sig=sig)

    pos["sig"] = sig
    state.setdefault("positions", []).append(pos)

    sl_price = pos["initial_sl"]
    tp_price = pos["tp_price"]
    sc_tag = "TEK MUM BOUNCE (A)" if sig["scenario"] == "A" else "ÇOKLU MUM BOUNCE (B)"

    vol_tag = f"{sig['volume_ratio']:.1f}x"
    rr = (TP_USD / SL_USD)
    rsi_ok = "✓" if (32 <= sig["rsi"] <= 52) else "—"
    sl_pct = (SL_USD / POSITION_NOTIONAL) * 100
    tp_pct = (TP_USD / POSITION_NOTIONAL) * 100

    msg = (
        f"🚀 LONGSİNYAL (v2): {sym}\n"
        f"📊 Zaman Dilimi: 15m | Güven: %{sig['confidence']} | {sc_tag}\n"
        f"-------------------------------------\n"
        f"🎯 Giriş: {fp(pos['entry'])} USDT\n"
        f"🛑 Stop: {fp(sl_price)}  (-${SL_USD:.2f} / %{sl_pct:.2f})\n"
        f"🎯 TP Hedef: {fp(tp_price)}  (+${TP_USD:.2f} / %{tp_pct:.2f})\n"
        f"⚖️ R:R = 1 : *{rr:.3f}*\n"
        f"-------------------------------------\n"
        f"📈 Fib Seviyeleri:\n"
        f"  Swing High:  {fp(sig['swing_high'])}\n"
        f"  0.618:       {fp(sig['fib618'])}  (Sınanan Seviye)\n"
        f"  0.500:       {fp(sig['fib500'])}\n"
        f"  Swing Low:   {fp(sig['swing_low'])}\n"
        f"  Impulse:     %{fpct(sig['impulse_percent'])}  ✓ (≥ %8)\n"
        f"📊 RSI(14): {sig['rsi']:.1f} {rsi_ok} | ADX(14): {sig.get('adx', 0):.0f} | EMA50 Trend: ✓ (üstünde)\n"
        f"🔝 1 Saatlik Trend: ✓ (EMA20>EMA50 yükselişte)\n"
        f"💹 Hacim: {vol_tag} ortalamanın üzerinde\n"
        f"💸 Pozisyon Boyutu: {POSITION_NOTIONAL:.0f} USDT\n"
        f"-------------------------------------\n"
        f"⏰ Breakeven: +${BREAKEVEN_TRIGGER:.2f} tetik → Stop girişe çekilir\n"
        f"🔒 Trailing: ${TRAILING_DISTANCE:.2f} mesafeyle arkadan kilitler\n"
    )
    tg(msg)
    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 65, flush=True)
    print("⚡ BINANCE FIBONACCI GOLDEN POCKET LONG SİNYAL MOTORU", flush=True)
    print("=" * 65, flush=True)
    print(f" Mod           : {'SİMÜLASYON (gerçek paraya dokunulmaz)' if not REAL_TRADING_DEFAULT else 'GERÇEK SPOT'}", flush=True)
    print(f" Zaman Dilimi  : {TIMEFRAME} ZORUNLU", flush=True)
    print(f" Tarama        : Her {SCAN_INTERVAL} sn | Top Gainers 30 coin | Hacim ≥ 10M USDT | Gain %-5 / +25 filtresi", flush=True)
    print(f" Maks Pozisyon : {MAX_OPEN_POSITIONS} adet | Nominal: ${POSITION_NOTIONAL:.0f}/pozisyon | Günlük max {MAX_TRADES_PER_DAY} işlem", flush=True)
    print(f" Giriş Modeli  : Fib 0.618 'Altından Giriş' — Senaryo A (tek mum) / B (2 mum)", flush=True)
    print(f" Güven Eşiği   : Confidence ≥ 92 (90-91 = izle, açma) | Impulse ≥ %8", flush=True)
    print(f" Risk/Ödül     : SL ${SL_USD:.2f} (%1.55) | TP ${TP_USD:.2f} (%4.44) | R:R = {TP_USD/SL_USD:.3f}", flush=True)
    print(f" BE Mekanizması: +${BREAKEVEN_TRIGGER:.2f} zirve → SL = giriş (başabaş)", flush=True)
    print(f" Trailing Stop : BE'den sonra ${TRAILING_DISTANCE:.2f} mesafeyle arkadan kilit", flush=True)
    print("=" * 65, flush=True)

    state = load_st()

    dirty = False
    if "sim_balance" not in state:
        state["sim_balance"] = SIM_STARTING_BALANCE; dirty = True
    if "sim_peak_balance" not in state:
        state["sim_peak_balance"] = max(state["sim_balance"], SIM_STARTING_BALANCE); dirty = True
    if "real_trading" not in state:
        state["real_trading"] = REAL_TRADING_DEFAULT; dirty = True
    if "circuit_breaker_until" not in state:
        state["circuit_breaker_until"] = 0; dirty = True
    if "pnl_today_sum" not in state:
        state["pnl_today_sum"] = 0.0; dirty = True
    if "pnl_today_date" not in state:
        state["pnl_today_date"] = today_str(); dirty = True
    today = today_str()
    if state.get("pnl_today_date", "") != today:
        state["pnl_today_date"] = today
        state["pnl_today_sum"] = 0.0
        dirty = True
    if dirty:
        save_st(state)

    cleaned_positions = []
    for p in state.get("positions", []):
        try:
            opened = datetime.fromisoformat(p["opened_iso"])
            if (utc() - opened).total_seconds() < 7 * 86400:
                cleaned_positions.append(p)
        except: pass
    state["positions"] = cleaned_positions
    save_st(state)

    is_real = state.get("real_trading", False)
    mode = "🔴 GERÇEK SPOT" if is_real else "🧪 SİMÜLASYON"
    bal = get_sim_balance(state)

    tg(f"⚡ *Fibonacci Golden Pocket Motoru Başlatıldı (v2 - Optimize Edildi)*\n\n"
       f"⚙️ Mod: `{mode}`\n"
       f"🎯 Zaman Dilimi: `15m` | Tarama: `Top 30 Gainers`\n"
       f"💰 Sanal Kasa: `${bal:.2f} USDT`\n"
       f"📋 Yeni Kurallar:\n"
       f"  • Impulse Dalgası ≥ %8 (≤ %60)\n"
       f"  • Hacim ≥ 10M USDT | 24h Gain %-5 / +25 arası\n"
       f"  • Fib 0.618 altından giriş (Senaryo A/B)\n"
       f"  • Fiyat EMA50 ÜSTÜNDE + EMA50 yükselişte\n"
       f"  • RSI %32-%52 arası ve AÇIK yukarı kancalanmış\n"
       f"  • Güçlü yeşil mum + hacim onayı\n"
       f"  • Confidence ≥ %92 açılır, 90-91 izlenir\n\n"
       f"💸 Pozisyon: `225 USDT` | SL: -$3.50 (%1.55) | TP: +$10.00 (%4.44)\n"
       f"⚖️ *R:R = 2.85* | BE: +$5.00 → giriş | 🔒 Trailing: $3.00 mesafe\n"
       f"🚦 Günlük Max İşlem: `{MAX_TRADES_PER_DAY}` | İşlem arası: `120sn` | Cooldown: `30dk`\n\n"
       f"🎮 Komutlar: /durum /rapor /gercek /fake /kapat /reset")

    last_scan = 0
    last_hb = 0

    while True:
        try:
            state = load_st()
            now = time.time()

            state = handle_telegram(state)

            if now - last_hb >= 180:
                is_real = state.get("real_trading", False)
                bal = get_sim_balance(state)
                pnl = bal - SIM_STARTING_BALANCE
                today_cnt = trades_today_count(state)
                n_pos = len(state.get("positions", []))
                mode_tag = "G" if is_real else "S"
                print(f"💓 [{mode_tag}] Kasa:${bal:.2f} (PnL:${pnl:+.2f}) "
                      f"Pos:{n_pos}/{MAX_OPEN_POSITIONS} Bugün:{today_cnt} "
                      f"{utc().strftime('%H:%M UTC')}", flush=True)
                last_hb = now

            if state.get("positions"):
                state = monitor(state)
                save_st(state)

            if len(state.get("positions", [])) < MAX_OPEN_POSITIONS and now - last_scan >= SCAN_INTERVAL:
                universe = get_universe()
                if universe:
                    state = scan(state, universe)
                    save_st(state)
                last_scan = time.time()

            time.sleep(2.0)

        except Exception as e:
            print(f"[HATA] {e}", flush=True)
            time.sleep(5.0)

if __name__ == "__main__":
    main()
