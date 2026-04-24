"""
trader_bot.py — VWAP Scalping Botu
Tüm Binance Futures USDT çiftleri
Leverage: 15x | Pos: 300 USD | SL: %1 | Max 3 pozisyon | Max 5 trades/gün
"""
import json, math, os, time, uuid
from datetime import datetime, timezone
from typing import Optional
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")
TK   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TC   = os.getenv("TELEGRAM_CHAT_ID", "")
SF   = os.getenv("STATE_FILE",  "trader_state.json")
DB   = os.getenv("TRADE_DB",    "trade_db.json")

# ── PARAMETRELER ─────────────────────────────────────────────────────────────
LEVERAGE      = 15
POSITION_USD  = 300.0
SL_PCT        = 0.01
TP_PCT        = 0.018
BE_PCT        = 0.008
MAX_OPEN      = 3        # aynı anda max açık pozisyon
MAX_TRADES    = 5        # günlük max işlem
MAX_CONSEC_L  = 2        # ardışık max kayıp
DAILY_LOSS_L  = 6.0      # günlük max kayıp (USD)
SCAN_EVERY    = int(os.getenv("SCAN_EVERY_SECONDS", "120"))
MIN_VOL_USD   = float(os.getenv("MIN_QUOTE_VOLUME_USD", "5000000"))
MAX_VOL_USD   = float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000"))
VOL_SPIKE     = 1.3
# ─────────────────────────────────────────────────────────────────────────────

STABLE = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","EURC"}

def utc():
    return datetime.now(timezone.utc)

def ts():
    return utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def fp(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def tg(txt):
    if not TK or not TC:
        print(txt); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id": TC, "text": txt, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=20
        ).raise_for_status()
    except Exception as e:
        print(f"[TG]{e}")

def get_json(url, p=None):
    r = requests.get(url, params=p, timeout=25)
    r.raise_for_status()
    return r.json()

def klines(sym, tf, n=200):
    raw = get_json(f"{BASE}/fapi/v1/klines", {"symbol": sym, "interval": tf, "limit": n})
    df = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def last_price(sym):
    return float(get_json(f"{BASE}/fapi/v1/ticker/price", {"symbol": sym})["price"])

# ── İNDİKATÖRLER ─────────────────────────────────────────────────────────────

def add_indicators(df):
    df = df.copy()
    c = df["c"]
    df["ema9"]   = c.ewm(span=9,  adjust=False).mean()
    df["ema21"]  = c.ewm(span=21, adjust=False).mean()
    tp           = (df["h"] + df["l"] + df["c"]) / 3
    df["vwap"]   = (tp * df["v"]).cumsum() / df["v"].cumsum().replace(0, np.nan)
    df["vol_ma"] = df["v"].rolling(20).mean()
    return df

def trend_5m(sym):
    try:
        df = add_indicators(klines(sym, "5m", 60))
        r  = df.iloc[-2]
        e9, e21 = float(r["ema9"]), float(r["ema21"])
        if e9 > e21 * 1.002: return "up"
        if e9 < e21 * 0.998: return "down"
        return "range"
    except:
        return "range"

# ── EVREN ─────────────────────────────────────────────────────────────────────

def get_universe():
    try:
        info   = get_json(f"{BASE}/fapi/v1/exchangeInfo")
        active = {r["symbol"] for r in info.get("symbols", [])
                  if r.get("status") == "TRADING"
                  and r.get("contractType") == "PERPETUAL"
                  and r.get("quoteAsset") == "USDT"
                  and r.get("symbol","")[:-4] not in STABLE}
        tickers = get_json(f"{BASE}/fapi/v1/ticker/24hr")
        out = []
        for t in tickers:
            sym = t.get("symbol","")
            if sym not in active: continue
            try:
                qv  = float(t.get("quoteVolume", 0))
            except:
                continue
            if MIN_VOL_USD <= qv <= MAX_VOL_USD:
                out.append((sym, qv))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        print(f"[Universe] {e}"); return []

# ── SİNYAL ───────────────────────────────────────────────────────────────────

def analyze(sym):
    try:
        df1m = add_indicators(klines(sym, "1m", 200))
    except:
        return None

    if len(df1m) < 50: return None

    r    = df1m.iloc[-2]
    c    = float(r["c"])
    e9   = float(r["ema9"])
    e21  = float(r["ema21"])
    vwap = float(r["vwap"])
    vol  = float(r["v"])
    vm   = float(r["vol_ma"]) if not math.isnan(float(r["vol_ma"])) else 1

    if c <= 0 or math.isnan(vwap): return None

    vol_spike = vol > vm * VOL_SPIKE
    vol_label = "high" if vol_spike else "low"
    prox      = c * 0.002
    near_ema9 = abs(c - e9) <= prox
    near_vwap = abs(c - vwap) <= prox * 2
    pullback  = near_ema9 or near_vwap

    if not vol_spike or not pullback:
        return None

    tr5 = trend_5m(sym)

    # LONG
    if c > vwap and e9 > e21 and tr5 != "down":
        entry = c
        return {
            "sym": sym, "side": "LONG", "entry": entry,
            "sl":  round(entry * (1 - SL_PCT), 8),
            "tp":  round(entry * (1 + TP_PCT), 8),
            "be":  round(entry * (1 + BE_PCT), 8),
            "rr":  round(TP_PCT / SL_PCT, 2),
            "vwap_pos": "above", "ema_bias": "bullish",
            "volume": vol_label, "trend_5m": tr5,
            "pullback": "yes", "vol_ratio": round(vol / vm, 2),
            "vwap": vwap, "ema9": e9, "ema21": e21
        }

    # SHORT
    if c < vwap and e9 < e21 and tr5 != "up":
        entry = c
        return {
            "sym": sym, "side": "SHORT", "entry": entry,
            "sl":  round(entry * (1 + SL_PCT), 8),
            "tp":  round(entry * (1 - TP_PCT), 8),
            "be":  round(entry * (1 - BE_PCT), 8),
            "rr":  round(TP_PCT / SL_PCT, 2),
            "vwap_pos": "below", "ema_bias": "bearish",
            "volume": vol_label, "trend_5m": tr5,
            "pullback": "yes", "vol_ratio": round(vol / vm, 2),
            "vwap": vwap, "ema9": e9, "ema21": e21
        }

    return None

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(sig, tid):
    icon = "🟢" if sig["side"] == "LONG" else "🔴"
    return (
        f"{icon} *TRADE AÇILDI*\n\n"
        f"Pair   : `{sig['sym']}`\n"
        f"Side   : `{sig['side']}`\n"
        f"Entry  : `{fp(sig['entry'])}`\n\n"
        f"Pozisyon: `${POSITION_USD:.0f}` | Kaldıraç: `{LEVERAGE}x`\n\n"
        f"Stop Loss  : `{fp(sig['sl'])}` (-1% / ~-$3)\n"
        f"Take Profit: `{fp(sig['tp'])}` (+1.8% / ~+$5.4)\n"
        f"Breakeven  : `{fp(sig['be'])}` (+0.8%)\n\n"
        f"*Strateji:*\n"
        f"- VWAP     : `{sig['vwap_pos']}`\n"
        f"- EMA 9/21 : `{sig['ema_bias']}`\n"
        f"- Pullback : `{sig['pullback']}`\n"
        f"- Hacim    : `{sig['volume']}` ({sig['vol_ratio']}x)\n\n"
        f"*5m Trend:* `{sig['trend_5m']}`\n\n"
        f"Zaman   : `{ts()}`\n"
        f"Trade ID: `{tid}`"
    )

def msg_close(pos, price, reason, duration_sec, tid):
    side  = pos["side"]
    pct   = (price - pos["entry"]) / pos["entry"] * (1 if side == "LONG" else -1)
    pnl   = POSITION_USD * pct
    rmult = pct / SL_PCT
    icon  = "🟢" if pnl >= 0 else "🔴"
    res_icons = {"TP": "✅ TP HIT", "SL": "❌ STOP", "BE": "🔰 BREAKEVEN"}
    mins  = duration_sec // 60
    secs  = duration_sec % 60
    return (
        f"{icon} *TRADE KAPANDI* — {res_icons.get(reason, reason)}\n\n"
        f"Pair   : `{pos['sym']}`\n"
        f"Side   : `{side}`\n\n"
        f"Entry  : `{fp(pos['entry'])}`\n"
        f"Exit   : `{fp(price)}`\n\n"
        f"Sonuç  : `{pct*100:+.2f}%`\n"
        f"P&L    : `${pnl:+.2f}`\n"
        f"R-Çarpan: `{rmult:+.2f}R`\n\n"
        f"Süre   : `{mins}dk {secs}sn`\n\n"
        f"*Strateji:*\n"
        f"- VWAP : `{pos.get('vwap_pos','?')}`\n"
        f"- EMA  : `{pos.get('ema_bias','?')}`\n"
        f"- Hacim: `{pos.get('volume','?')}`\n\n"
        f"Trade ID: `{tid}`"
    )

def msg_stats(stats):
    if stats["total"] == 0: return "📊 Henüz trade yok."
    wr = stats["wins"] / stats["total"] * 100
    return (
        f"📊 *BOT İSTATİSTİKLERİ*\n\n"
        f"Toplam Trade : `{stats['total']}`\n"
        f"Kazanan      : `{stats['wins']}` ({wr:.1f}%)\n"
        f"Kaybeden     : `{stats['losses']}`\n"
        f"Toplam P&L   : `${stats['total_pnl']:+.2f}`\n"
        f"Expectancy   : `${stats['expectancy']:+.4f}` per trade\n\n"
        f"*En İyi Çift:* `{stats.get('best_pair','—')}`\n"
        f"*En İyi Saat:* `{stats.get('best_hour','—')}:00 UTC`\n"
        f"*LONG Winrate:* `{stats.get('long_wr',0):.1f}%`\n"
        f"*SHORT Winrate:* `{stats.get('short_wr',0):.1f}%`"
    )

# ── TRADE DB ──────────────────────────────────────────────────────────────────

def load_db():
    if os.path.exists(DB):
        try:
            with open(DB) as f: return json.load(f)
        except: pass
    return []

def save_db(trades):
    with open(DB, "w") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)

def record_trade(pos, price, reason, duration_sec):
    trades = load_db()
    pct    = (price - pos["entry"]) / pos["entry"] * (1 if pos["side"] == "LONG" else -1)
    pnl    = POSITION_USD * pct
    trades.append({
        "id"       : pos.get("trade_id",""),
        "pair"     : pos["sym"],
        "side"     : pos["side"],
        "entry"    : pos["entry"],
        "exit"     : price,
        "result"   : reason,
        "pnl"      : round(pnl, 4),
        "vwap"     : pos.get("vwap_pos","?"),
        "ema"      : pos.get("ema_bias","?"),
        "volume"   : pos.get("volume","?"),
        "trend_5m" : pos.get("trend_5m","?"),
        "duration" : duration_sec,
        "hour_utc" : utc().hour,
        "date"     : utc().strftime("%Y-%m-%d"),
        "timestamp": ts()
    })
    save_db(trades)
    return trades

def calc_stats(trades):
    if not trades:
        return {"total":0,"wins":0,"losses":0,"total_pnl":0,"expectancy":0,
                "best_pair":"—","best_hour":"—","long_wr":0,"short_wr":0}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = len(trades)
    w, l   = len(wins), len(losses)
    total_pnl  = sum(t["pnl"] for t in trades)
    avg_win    = sum(t["pnl"] for t in wins)   / w if w else 0
    avg_loss   = sum(t["pnl"] for t in losses) / l if l else 0
    expectancy = (w/total * avg_win) + (l/total * avg_loss) if total else 0

    from collections import defaultdict
    pair_pnl = defaultdict(float)
    hour_pnl = defaultdict(float)
    for t in trades:
        pair_pnl[t["pair"]] += t["pnl"]
        hour_pnl[t["hour_utc"]] += t["pnl"]

    longs  = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]

    return {
        "total"    : total,
        "wins"     : w,
        "losses"   : l,
        "total_pnl": round(total_pnl, 4),
        "expectancy": round(expectancy, 4),
        "best_pair": max(pair_pnl, key=pair_pnl.get) if pair_pnl else "—",
        "best_hour": max(hour_pnl, key=hour_pnl.get) if hour_pnl else "—",
        "long_wr"  : round(len([t for t in longs  if t["pnl"]>0])/len(longs)*100,1)  if longs  else 0,
        "short_wr" : round(len([t for t in shorts if t["pnl"]>0])/len(shorts)*100,1) if shorts else 0,
    }

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except: pass
    return {
        "positions": [],   # liste — max 3
        "daily": {
            "date": utc().strftime("%Y-%m-%d"),
            "trades": 0, "loss_usd": 0.0, "consec_losses": 0
        }
    }

def save_st(s):
    with open(SF, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

def reset_daily(state):
    today = utc().strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "trades": 0, "loss_usd": 0.0, "consec_losses": 0}
    return state

def can_open(state):
    d = state["daily"]
    open_count = len(state.get("positions", []))
    if open_count >= MAX_OPEN:
        print(f"  ⏳ Max pozisyon dolu ({open_count}/{MAX_OPEN})")
        return False
    if d["trades"] >= MAX_TRADES:
        print(f"  ⛔ Günlük limit: {d['trades']}/{MAX_TRADES} işlem")
        return False
    if d["consec_losses"] >= MAX_CONSEC_L:
        print(f"  ⛔ {MAX_CONSEC_L} ardışık kayıp")
        return False
    if d["loss_usd"] >= DAILY_LOSS_L:
        print(f"  ⛔ Günlük kayıp: ${d['loss_usd']:.2f}/${DAILY_LOSS_L}")
        return False
    return True

# ── MONİTÖR ──────────────────────────────────────────────────────────────────

def monitor(state):
    positions     = state.get("positions", [])
    still_open    = []

    for pos in positions:
        try:
            price = last_price(pos["sym"])
        except Exception as e:
            print(f"  [Monitor] {pos['sym']}: {e}")
            still_open.append(pos)
            continue

        side   = pos["side"]
        entry  = pos["entry"]
        be_hit = pos.get("be_hit", False)

        try:
            opened_dt = datetime.fromisoformat(pos.get("opened_iso", utc().isoformat()))
            dur_sec   = int((utc() - opened_dt).total_seconds())
        except:
            dur_sec = 0

        # Breakeven kontrolü
        if not be_hit:
            if (side == "LONG"  and price >= pos["be"]) or \
               (side == "SHORT" and price <= pos["be"]):
                pos["sl"]     = entry
                pos["be_hit"] = True
                tg(f"🔰 *{pos['sym']}* | Breakeven'a taşındı | `{fp(price)}`")
                print(f"  🔰 [{pos['sym']}] BE @ {fp(price)}")

        # Çıkış kontrolü
        reason = None
        if side == "LONG":
            if   price <= pos["sl"]: reason = "BE" if pos.get("be_hit") else "SL"
            elif price >= pos["tp"]: reason = "TP"
        else:
            if   price >= pos["sl"]: reason = "BE" if pos.get("be_hit") else "SL"
            elif price <= pos["tp"]: reason = "TP"

        if reason:
            tid    = pos.get("trade_id","—")
            trades = record_trade(pos, price, reason, dur_sec)
            tg(msg_close(pos, price, reason, dur_sec, tid))

            pct = (price - entry) / entry * (1 if side == "LONG" else -1)
            pnl = POSITION_USD * pct
            d   = state["daily"]
            d["trades"] += 1
            if pnl < 0:
                d["loss_usd"]      += abs(pnl)
                d["consec_losses"] += 1
            else:
                d["consec_losses"] = 0

            print(f"  [{reason}] {pos['sym']} @ {fp(price)} | P&L: ${pnl:+.2f}")

            # Her 10 trade'de istatistik gönder
            if len(trades) % 10 == 0:
                tg(msg_stats(calc_stats(trades)))
        else:
            pct = (price - entry) / entry * (1 if side == "LONG" else -1)
            print(f"  [OPEN] {pos['sym']} {side} | {fp(price)} ({pct*100:+.2f}%) "
                  f"SL:{fp(pos['sl'])} TP:{fp(pos['tp'])} {dur_sec//60}dk")
            still_open.append(pos)

        time.sleep(0.1)

    state["positions"] = still_open
    return state

# ── TARAMA ───────────────────────────────────────────────────────────────────

def scan(state):
    slots = MAX_OPEN - len(state.get("positions", []))
    if slots <= 0:
        return state

    print(f"\n{'='*55}")
    print(f"🎯 VWAP SCALP — {utc().strftime('%H:%M:%S UTC')}")
    d = state["daily"]
    open_count = len(state.get("positions", []))
    print(f"  Açık: {open_count}/{MAX_OPEN} | "
          f"Bugün: {d['trades']}/{MAX_TRADES} | "
          f"Kayıp: ${d['loss_usd']:.2f}/${DAILY_LOSS_L}")
    print(f"{'='*55}")

    if not can_open(state):
        return state

    universe = get_universe()
    print(f"  {len(universe)} coin taranıyor (max {slots} yeni pozisyon açılabilir)...\n")

    # Zaten açık olan coinleri atla
    open_syms = {p["sym"] for p in state.get("positions", [])}

    candidates = []
    for i, (sym, qv) in enumerate(universe):
        print(f"  [{i+1}/{len(universe)}] {sym}", end="\r")
        if sym in open_syms:
            continue
        try:
            sig = analyze(sym)
            if sig:
                candidates.append(sig)
                print(f"\n  ✅ {sym} {sig['side']} | Vol:{sig['vol_ratio']}x | 5m:{sig['trend_5m']}")
        except:
            pass
        time.sleep(0.05)

    print(f"\n  {len(candidates)} sinyal bulundu.\n")

    # En yüksek hacim spike'lı top N sinyali aç
    candidates.sort(key=lambda x: x["vol_ratio"], reverse=True)

    opened = 0
    for sig in candidates:
        if opened >= slots: break
        if not can_open(state): break
        if sig["sym"] in {p["sym"] for p in state.get("positions",[])}:
            continue

        tid = str(uuid.uuid4())[:8].upper()
        new_pos = {
            **sig,
            "trade_id"  : tid,
            "be_hit"    : False,
            "opened_iso": utc().isoformat(),
            "opened_ts" : ts()
        }
        state.setdefault("positions", []).append(new_pos)
        tg(msg_open(sig, tid))
        print(f"  🚀 AÇILDI [{opened+1}/{slots}]: {sig['sym']} {sig['side']} @ {fp(sig['entry'])} | ID:{tid}")
        opened += 1
        time.sleep(0.3)

    if opened == 0:
        print(f"  🔍 Uygun setup bulunamadı.")

    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("🎯 VWAP SCALP BOT — Tüm Binance Futures")
    print(f"   SL:%1 | TP:%1.8 | BE:%0.8 | Kaldıraç:{LEVERAGE}x")
    print(f"   Max {MAX_OPEN} açık pozisyon | Max {MAX_TRADES}/gün | Max ${DAILY_LOSS_L} kayıp/gün")
    print(f"   Tarama: her {SCAN_EVERY}sn | TG: {'✅' if TK else '❌'}")
    print("=" * 55 + "\n")

    trades = load_db()
    if trades:
        stats = calc_stats(trades)
        print(f"  DB: {stats['total']} trade | Kazanan: {stats['wins']} | P&L: ${stats['total_pnl']:+.2f}\n")

    while True:
        try:
            state = load_st()
            state = reset_daily(state)

            # Açık pozisyonları izle
            if state.get("positions"):
                state = monitor(state)
                save_st(state)

            # Yeni sinyal ara (boş slot varsa)
            if can_open(state):
                state = scan(state)
                save_st(state)
            else:
                open_count = len(state.get("positions", []))
                print(f"  [Bekle] Açık:{open_count}/{MAX_OPEN} | "
                      f"Trade:{state['daily']['trades']}/{MAX_TRADES}")

        except Exception as e:
            print(f"[HATA] {e}")

        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    main()
