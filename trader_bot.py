"""
trader_bot.py — Yapay Zekalı (Gemini 2.5 Flash) & Dinamik Oransal Korumalı Binance Vadeli Botu
• Hesap: Portfolio Margin (PAPI) & Futures Uyumlu
• Kasa & Teminat Yönetimi: Gerçek serbest teminatı (Free Margin) kontrol eder, -2019 yetersiz teminat hatasını engeller
• Net Trailing Stop: +$2.00 kârda başlar ve stopu hemen +$1.00 kâra kilitler (Zirveden $1.00 geri çekilirse satar)
• Breakeven: +$1.00 kârda stop maliyete çekilir (Sıfır Risk)
• Stop Loss: -$1.50 sabit koruma
• Hızlı Anlık İzleme: 1.5 saniyelik ultra hızlı döngü ile kaymasız takip
• Koruma: BASEDUSDT dokunulmaz | BTC Düşüş Kalkanı aktif | Gemini 2.5 Flash AI Teyitli
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
DEFAULT_LEVERAGE = 20          # 20x Kaldıraç (Destekliyorsa 20x-25x ayarlanır)
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
MAX_VOL_USD      = 200_000_000.0
RSI_OVERSOLD     = 25.0
BB_PERIOD        = 20
BB_STD           = 2.0
MAX_STAGNATION_PCT = 6.0
MIN_VOL_MULTIPLIER = 3.5

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

def gemini_ai_validate(sym, mode, rsi, price, btc_status, last_candles_summary):
    if not GEMINI_KEY:
        return True, 80, "Gemini API anahtarı girilmedi, teknik sinyalle devam ediliyor."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    prompt = f"""
Sen dünyanın en disiplinli ve profesyonel Kripto Vadeli Scalp Uzmanısın.
Botumuz Binance vadeli işlemlerde 20x kaldıraç ile $250 büyüklüğünde LONG pozisyonuna girmek üzere.

Parite: {sym} | Mod: {mode} | Fiyat: {price} | 15m RSI: {rsi:.1f}
BTC Durumu: {btc_status}
Son Mumlar: {last_candles_summary}

GÖREV: Bu sinyalin sahte bir tuzak mı yoksa yüksek olasılıklı bir kâr fırsatı mı olduğunu ÇOK SIKI şekilde değerlendir.
Kararsızsan veya risk yüksekse REJECT ver. Sadece çok net ve güçlü fırsatları APPROVE et.

SADECE aşağıdaki JSON formatında yanıt ver:
{{
  "decision": "APPROVE" veya "REJECT",
  "confidence": 0 ile 100 arası sayı,
  "reason": "Türkçe net ve profesyonel 1-2 cümlelik açıklama"
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
    """
    Kullanıcının isteği: 20x kaldıraç ile $250'lık pozisyon açar.
    $250 notional için 20x'te sadece $12.50 teminat gerekir.
    """
    actual_lev = set_optimal_leverage(sym, target_lev=20)
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
    
    order_params = {
        "symbol": sym, "side": "BUY", "type": "MARKET", "quantity": str(qty)
    }
    
    actual_notional = qty * price
    print(f"⚡ [GERÇEK EMİR AÇILIYOR] {sym} | Kaldıraç: {actual_lev}x | Miktar: {qty} | Büyüklük: ${actual_notional:.2f} (Serbest Teminat: ${free_margin:.2f})", flush=True)
    order_res = binance_signed_request("POST", "/papi/v1/um/order", order_params)
    
    avg_price = float(order_res.get("avgPrice", 0))
    if avg_price <= 0: avg_price = price
        
    dyn_tp_trigger_usd = DEFAULT_TP_TRIGGER_USD
    dyn_trailing_drop_usd = DEFAULT_TRAILING_DROP
    dyn_be_trigger_usd = DEFAULT_BE_TRIGGER_USD
    dyn_sl_usd = DEFAULT_SL_USD
    
    sl_price = avg_price - (dyn_sl_usd / qty)
    be_trigger_price = avg_price + (dyn_be_trigger_usd / qty)
    be_sl_price = avg_price + (0.20 / qty)
    
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
        "be_hit": False
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

def msg_real_open(pos, sig, ai_conf, ai_reason):
    icon = "🎯" if sig.get("mode") == "DİP_AVCISI" else "🚀"
    lines = "\n".join(f"  • {r}" for r in sig.get("reasons", []))
    return (
        f"{icon} *GERÇEK POZİSYON AÇILDI!* | `{pos['sym']}`\n\n"
        f"Yön: *LONG (10x Kaldıraç)*\n"
        f"Giriş Fiyatı : `{fp(pos['entry'])}`\n"
        f"Pozisyon Büyüklüğü : `${pos['notional_usd']}` ({pos['qty']} adet)\n\n"
        f"📈 *Trailing Kâr:* `+${pos['dyn_tp_trigger_usd']:.2f}` geçilince başlar (+${pos['dyn_tp_trigger_usd'] - pos['dyn_trailing_drop_usd']:.2f} kilitlenir)\n"
        f"🛑 *Stop Loss:* `-${pos['dyn_sl_usd']:.2f}` (`{fp(pos['sl_price'])}`)\n"
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
    
    eq, free_m = get_account_balances()
    return (
        f"{icon} *{title}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(exit_price)}`\n"
        f"Net P&L : *`${pnl:+.2f} USDT`*\n"
        f"Görülen Zirve Kâr : `+${pos.get('highest_profit_usd', pnl):.2f}`\n"
        f"İşlem Süresi : `{dur_sec//60} dakika`\n"
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
        "id": pos.get("order_id", ""), "pair": pos["sym"], "side": "LONG",
        "entry": pos["entry"], "exit": exit_price, "qty": pos["qty"],
        "notional": pos["notional_usd"], "pnl": round(pnl, 2),
        "result": reason, "duration": dur_sec, "timestamp": ts()
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
    still = []
    
    for pos in state.get("positions", []):
        sym = pos["sym"]
        
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
        unrealized_pnl = (price - entry) * qty
        
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
                tg(f"🚀 *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! *Trailing Kâr Takibi Aktif Edildi!*\nStop seviyesi `+${locked_profit:.2f}` kâra kilitlendi. Fiyat yükseldikçe stop peşinden sürecek.")
                
            # Zirveden $1.00 geri çekilme stopu
            trailing_exit_pnl = highest_pnl - dyn_trailing_drop
            trailing_exit_price = entry + (trailing_exit_pnl / qty)
            pos["trailing_sl_price"] = max(pos.get("trailing_sl_price", pos["sl_price"]), trailing_exit_price)
            
        # 2. BREAKEVEN KORUMASI (+ $1.00 kârda stop maliyet seviyesine çekilir)
        if not pos.get("be_hit") and (price >= pos.get("be_trigger_price", entry * 1.005) or unrealized_pnl >= dyn_be_trigger):
            pos["sl_price"] = pos["be_sl_price"]
            pos["be_hit"] = True
            tg(f"🔰 *{sym}* `+${unrealized_pnl:.2f}` kâra ulaştı! Stop maliyete (`{fp(pos['sl_price'])}`) çekildi. *İşlem artık sıfır risklidir!*")

        reason = None
        # Trailing Kâr Tetiklenmesi (Zirveden $1.00 çekilirse sat)
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
            print(f"🔒 [{reason}] {sym} @ {fp(exit_price)} | Net P&L: ${real_pnl:+.2f}", flush=True)
        else:
            trail_str = f"| Trailing Stop: {fp(pos['trailing_sl_price'])}" if pos.get("trailing_active") else ""
            print(f"  [AÇIK POZİSYON] {sym} | Fiyat: {fp(price)} | PnL: ${unrealized_pnl:+.2f} (Zirve: ${highest_pnl:+.2f}) | SL: {fp(pos['sl_price'])} {trail_str}", flush=True)
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
        
    btc_ok, btc_reason = check_btc_shield()
    if not btc_ok:
        return state

    open_syms = {p["sym"] for p in state.get("positions", [])}
    open_syms.update(PROTECTED_SYMBOLS)
    
    for i, (sym, _) in enumerate(universe):
        if sym in open_syms: continue
        print(f"  [{i+1}/{len(universe)}] {sym} taranıyor...", end="\r", flush=True)
        try:
            sig = analyze_market_candidate(sym)
            if sig:
                print(f"\n🔍 [TEKNİK SİNYAL] {sym} ({sig['mode']})! Gemini 2.5 Flash analiz ediyor...", flush=True)
                
                ai_ok, ai_conf, ai_reason = gemini_ai_validate(
                    sym=sym, mode=sig["mode"], rsi=sig["rsi"],
                    price=sig["entry"], btc_status=btc_reason,
                    last_candles_summary=sig.get("candles_summary", "")
                )
                
                if not ai_ok:
                    print(f"❌ [GEMINI RED] {sym} (%{ai_conf}) — {ai_reason}", flush=True)
                    continue
                    
                print(f"✅ [GEMINI ONAY] {sym} (%{ai_conf})! Gerçek işlem açılıyor...", flush=True)
                
                if REAL_TRADING:
                    pos = execute_real_entry(sym, free_margin=free_margin)
                else:
                    pos = {
                        "sym": sym, "side": "LONG", "entry": sig["entry"],
                        "qty": round(200.0 / sig["entry"], 2),
                        "notional_usd": 200.0, "sl_price": sig["entry"] * 0.993,
                        "dyn_sl_usd": DEFAULT_SL_USD, "dyn_tp_trigger_usd": DEFAULT_TP_TRIGGER_USD,
                        "dyn_be_trigger_usd": DEFAULT_BE_TRIGGER_USD, "dyn_trailing_drop_usd": DEFAULT_TRAILING_DROP,
                        "be_trigger_price": sig["entry"] * 1.005,
                        "be_sl_price": sig["entry"] * 1.001, "trailing_active": False,
                        "highest_profit_usd": 0.0, "trailing_sl_price": sig["entry"] * 0.993,
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
    print("⚡ BİNANCE GERÇEK İŞLEM BOTU (GEMINI 2.5 FLASH & ORACLE CLOUD 7/24)", flush=True)
    print("="*65, flush=True)
    print(" 🧠 Yapay Zeka         : Google Gemini 2.5 Flash Onay Motoru", flush=True)
    print(" 💸 Trailing Stop      : +$2.00 kârda başlar, +$1.00 kârı kilitler ve sürer", flush=True)
    print(" 🔰 Başa Baş Koruma    : +$1.00 kârda stop maliyete çekilir (Sıfır Risk)", flush=True)
    print(" 🛑 Hızlı Stop Loss    : -$1.50 seviyesinde 1.5s anlık koruma", flush=True)
    print(" 🛡️ Koruma             : BASEDUSDT dokunulmaz | Serbest Teminat Koruması Aktif", flush=True)
    print("="*65, flush=True)
    
    eq, free_m = get_account_balances()
    print(f"✅ Toplam Varlık: ${eq:.2f} USDT | Serbest Teminat: ${free_m:.2f} USDT", flush=True)
    
    tg(f"🚀 *BINANCE VADELİ BOTU GÜNCELLENDİ (ORACLE CLOUD 7/24)!*\n\n"
       f"💰 *Toplam Varlık:* `${eq:.2f} USDT` (Serbest: `${free_m:.2f}`)\n"
       f"📈 *Trailing Kâr:* `+$2.00` kârda başlar, `+$1.00` kârı kilitler ve zirveyi takip eder.\n"
       f"🔰 *Başa Baş:* `+$1.00` kârda stop maliyete çekilir (Sıfır Risk).\n"
       f"🛑 *Stop Loss:* `-$1.50` anlık çıkış.\n"
       f"🛡️ *Korumalı:* `BASEDUSDT` dokunulmaz | Yetersiz teminat koruması aktif.\n\n"
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
