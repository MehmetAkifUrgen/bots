"""
trader_bot.py — Balina Takip Botu
Sadece balina aktivitesi sinyalleriyle pozisyon aç
OI | Top Trader Ratio | Taker | Büyük İşlem | Funding
"""
import json, math, os, time, uuid
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")
TK   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TC   = os.getenv("TELEGRAM_CHAT_ID", "")
SF   = os.getenv("STATE_FILE", "trader_state.json")
DB   = os.getenv("TRADE_DB",   "trade_db.json")

# ── PARAMETRELER ──────────────────────────────────────────────────────────────
MAX_OPEN        = 3
DAILY_LOSS_L    = 6.0
MAX_HOLD_MIN    = 120
POSITION_USD    = 300.0
SL_PCT          = 0.015      # %1.5 stop
TP1_PCT         = 0.03       # %3 hedef 1
TP2_PCT         = 0.05       # %5 hedef 2
SCAN_EVERY      = int(os.getenv("SCAN_EVERY_SECONDS", "120"))
MIN_VOL_USD     = float(os.getenv("MIN_QUOTE_VOLUME_USD", "10000000"))
MAX_VOL_USD     = float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000"))
WHALE_MIN_SCORE = 5          # pozisyon açmak için min skor
WHALE_OI_CHG    = 5.0        # OI %5+ = balina girişi
WHALE_TRADE_USD = 200_000    # $200K+ tek işlem = balina
_last_scan_t    = {"t": None}
# ─────────────────────────────────────────────────────────────────────────────

STABLE = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","EURC"}

def utc():  return datetime.now(timezone.utc)
def ts():   return utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def fp(v):
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def tg(txt):
    if not TK or not TC: print(txt); return
    try:
        requests.post(f"https://api.telegram.org/bot{TK}/sendMessage",
            json={"chat_id":TC,"text":txt,"parse_mode":"Markdown",
                  "disable_web_page_preview":True}, timeout=20).raise_for_status()
    except Exception as e: print(f"[TG]{e}")

def get_json(url, p=None):
    r = requests.get(url, params=p, timeout=25)
    r.raise_for_status()
    return r.json()

def klines(sym, tf, n=50):
    raw = get_json(f"{BASE}/fapi/v1/klines", {"symbol":sym,"interval":tf,"limit":n})
    df  = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v"]: df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

def last_price(sym):
    return float(get_json(f"{BASE}/fapi/v1/ticker/price", {"symbol":sym})["price"])

# ── EVREN ─────────────────────────────────────────────────────────────────────

def get_universe():
    try:
        info   = get_json(f"{BASE}/fapi/v1/exchangeInfo")
        active = {r["symbol"] for r in info.get("symbols",[])
                  if r.get("status")=="TRADING" and r.get("contractType")=="PERPETUAL"
                  and r.get("quoteAsset")=="USDT"
                  and r.get("symbol","")[:-4] not in STABLE}
        tickers = get_json(f"{BASE}/fapi/v1/ticker/24hr")
        out = []
        for t in tickers:
            sym = t.get("symbol","")
            if sym not in active: continue
            try: qv = float(t.get("quoteVolume",0))
            except: continue
            if MIN_VOL_USD <= qv <= MAX_VOL_USD:
                out.append((sym, qv))
        out.sort(key=lambda x: x[1], reverse=True)
        return out
    except Exception as e:
        print(f"[Universe]{e}"); return []

# ── BALİNA FONKSİYONLARI ─────────────────────────────────────────────────────

def whale_oi(sym):
    try:
        data = get_json(f"{BASE}/futures/data/openInterestHist",
                        {"symbol":sym,"period":"5m","limit":12})
        if len(data) < 2: return 0.0
        oi_new = float(data[-1]["sumOpenInterest"])
        oi_old = float(data[0]["sumOpenInterest"])
        if oi_old <= 0: return 0.0
        return (oi_new - oi_old) / oi_old * 100
    except: return 0.0

def whale_top_ratio(sym):
    try:
        data = get_json(f"{BASE}/futures/data/topLongShortPositionRatio",
                        {"symbol":sym,"period":"5m","limit":1})
        if not data: return 1.0
        return float(data[-1]["longShortRatio"])
    except: return 1.0

def whale_taker(sym):
    try:
        data = get_json(f"{BASE}/futures/data/takerlongshortRatio",
                        {"symbol":sym,"period":"5m","limit":3})
        if not data: return 1.0
        return sum(float(d["buySellRatio"]) for d in data) / len(data)
    except: return 1.0

def whale_large_trade(sym):
    try:
        trades = get_json(f"{BASE}/fapi/v1/aggTrades", {"symbol":sym,"limit":50})
        price  = last_price(sym)
        for t in trades:
            usd = float(t.get("q", 0)) * price
            if usd >= WHALE_TRADE_USD:
                return {"usd": usd, "side": "BUY" if not t.get("m") else "SELL"}
        return None
    except: return None

def whale_funding(sym):
    try:
        d = get_json(f"{BASE}/fapi/v1/premiumIndex", {"symbol":sym})
        return float(d.get("lastFundingRate", 0)) * 100
    except: return 0.0

def get_whale_signal(sym):
    oi_chg = whale_oi(sym)
    ratio  = whale_top_ratio(sym)
    taker  = whale_taker(sym)
    fund   = whale_funding(sym)
    big_t  = whale_large_trade(sym)

    signals = []
    score   = 0

    if oi_chg >= WHALE_OI_CHG:
        signals.append(f"📈 OI +{oi_chg:.1f}% (balina giriyor)")
        score += 3
    elif oi_chg <= -WHALE_OI_CHG:
        signals.append(f"📉 OI {oi_chg:.1f}% (balina çıkıyor)")
        score -= 3

    if ratio >= 2.0:
        signals.append(f"🐋 Top Trader L/S: {ratio:.2f} (LONG dominant)")
        score += 2
    elif ratio <= 0.5:
        signals.append(f"🐋 Top Trader L/S: {ratio:.2f} (SHORT dominant)")
        score -= 2

    if taker >= 1.5:
        signals.append(f"⚡ Taker alım: {taker:.2f}x (agresif ALIŞ)")
        score += 2
    elif taker <= 0.67:
        signals.append(f"⚡ Taker satım: {taker:.2f}x (agresif SATIŞ)")
        score -= 2

    if big_t:
        signals.append(f"💰 Büyük işlem: ${big_t['usd']/1000:.0f}K {big_t['side']}")
        score += 3 if big_t["side"] == "BUY" else -3

    if fund <= -0.05:
        signals.append(f"💸 Funding: {fund:.3f}% (short squeeze riski)")
        score += 2
    elif fund >= 0.1:
        signals.append(f"💸 Funding: {fund:.3f}% (long sıkışması riski)")
        score -= 2

    direction = "LONG" if score > 0 else "SHORT" if score < 0 else None
    return {
        "sym": sym, "score": score, "direction": direction,
        "signals": signals, "oi_chg": oi_chg,
        "ratio": ratio, "taker": taker, "funding": fund
    }

# ── MESAJLAR ──────────────────────────────────────────────────────────────────

def msg_open(pos, tid):
    icon    = "🟢" if pos["side"] == "LONG" else "🔴"
    side_tr = "LONG (AL)" if pos["side"] == "LONG" else "SHORT (SAT)"
    lines   = "\n".join(f"  • {s}" for s in pos.get("whale_signals", []))
    return (
        f"{icon} *TRADE AÇILDI* | `{pos['sym']}`\n\n"
        f"Yön    : *{side_tr}*\n"
        f"Giriş  : `{fp(pos['entry'])}`\n\n"
        f"Stop   : `{fp(pos['sl'])}` (-%{SL_PCT*100:.1f})\n"
        f"Hedef 1: `{fp(pos['tp1'])}` (+%{TP1_PCT*100:.1f})\n"
        f"Hedef 2: `{fp(pos['tp2'])}` (+%{TP2_PCT*100:.1f})\n\n"
        f"*🐋 Balina Sinyalleri (Skor: {pos['whale_score']:+d}):*\n{lines}\n\n"
        f"Pozisyon: `${POSITION_USD:.0f}` | Max Kayıp: `${POSITION_USD*SL_PCT:.2f}`\n"
        f"Zaman: `{ts()}` | ID: `{tid}`"
    )

def msg_close(pos, price, reason, dur_sec, tid):
    side = pos["side"]
    pct  = (price - pos["entry"]) / pos["entry"] * (1 if side == "LONG" else -1)
    pnl  = POSITION_USD * pct
    icon = "🟢" if pnl >= 0 else "🔴"
    labels = {"TP1":"✅ HEDEF 1","TP2":"✅✅ HEDEF 2",
              "SL":"❌ STOP","TIMEOUT":"⏱️ TIMEOUT","BE":"🔰 BREAKEVEN"}
    return (
        f"{icon} *{labels.get(reason,reason)}* | `{pos['sym']}`\n\n"
        f"Giriş : `{fp(pos['entry'])}` → Çıkış: `{fp(price)}`\n"
        f"Sonuç : `{pct*100:+.2f}%` | P&L: `${pnl:+.2f}`\n"
        f"Süre  : `{dur_sec//60} dakika`\n"
        f"ID: `{tid}`"
    )

def msg_stats(stats):
    if stats["total"] == 0: return "📊 Henüz trade yok."
    wr = stats["wins"] / stats["total"] * 100
    return (
        f"📊 *BOT İSTATİSTİKLERİ*\n\n"
        f"Toplam  : `{stats['total']}`\n"
        f"Kazanan : `{stats['wins']}` (%{wr:.1f})\n"
        f"P&L     : `${stats['total_pnl']:+.2f}`\n"
        f"Beklenti: `${stats['expectancy']:+.4f}` / trade\n\n"
        f"En İyi Çift : `{stats.get('best_pair','—')}`\n"
        f"LONG WR     : `%{stats.get('long_wr',0):.1f}`\n"
        f"SHORT WR    : `%{stats.get('short_wr',0):.1f}`"
    )

# ── DB ────────────────────────────────────────────────────────────────────────

def load_db():
    if os.path.exists(DB):
        try:
            with open(DB) as f: return json.load(f)
        except: pass
    return []

def save_db(t):
    with open(DB,"w") as f: json.dump(t,f,indent=2,ensure_ascii=False)

def record_trade(pos, price, reason, dur_sec):
    trades = load_db()
    pct = (price-pos["entry"])/pos["entry"]*(1 if pos["side"]=="LONG" else -1)
    pnl = POSITION_USD * pct
    trades.append({
        "id":pos.get("trade_id",""), "pair":pos["sym"], "side":pos["side"],
        "entry":pos["entry"], "exit":price, "result":reason,
        "pnl":round(pnl,4), "whale_score":pos.get("whale_score",0),
        "duration":dur_sec, "hour_utc":utc().hour,
        "date":utc().strftime("%Y-%m-%d"), "timestamp":ts()
    })
    save_db(trades)
    return trades

def calc_stats(trades):
    if not trades:
        return {"total":0,"wins":0,"losses":0,"total_pnl":0,
                "expectancy":0,"best_pair":"—","long_wr":0,"short_wr":0}
    wins   = [t for t in trades if t["pnl"]>0]
    losses = [t for t in trades if t["pnl"]<=0]
    total  = len(trades)
    w, l   = len(wins), len(losses)
    tp     = sum(t["pnl"] for t in trades)
    aw     = sum(t["pnl"] for t in wins)/w   if w else 0
    al     = sum(t["pnl"] for t in losses)/l if l else 0
    exp    = (w/total*aw)+(l/total*al)        if total else 0
    from collections import defaultdict
    pp = defaultdict(float)
    for t in trades: pp[t["pair"]] += t["pnl"]
    longs  = [t for t in trades if t["side"]=="LONG"]
    shorts = [t for t in trades if t["side"]=="SHORT"]
    return {
        "total":total, "wins":w, "losses":l, "total_pnl":round(tp,4),
        "expectancy":round(exp,4),
        "best_pair":max(pp,key=pp.get) if pp else "—",
        "long_wr":round(len([t for t in longs  if t["pnl"]>0])/len(longs)*100,1)  if longs  else 0,
        "short_wr":round(len([t for t in shorts if t["pnl"]>0])/len(shorts)*100,1) if shorts else 0,
    }

# ── STATE ─────────────────────────────────────────────────────────────────────

def load_st():
    if os.path.exists(SF):
        try:
            with open(SF) as f: return json.load(f)
        except: pass
    return {"positions":[],"daily":{"date":utc().strftime("%Y-%m-%d"),
            "loss_usd":0.0,"consec_losses":0}}

def save_st(s):
    with open(SF,"w") as f: json.dump(s,f,indent=2,ensure_ascii=False)

def reset_daily(state):
    today = utc().strftime("%Y-%m-%d")
    if state["daily"].get("date") != today:
        state["daily"] = {"date":today,"loss_usd":0.0,"consec_losses":0}
    return state

def can_open(state):
    d  = state["daily"]
    oc = len(state.get("positions",[]))
    if oc >= MAX_OPEN:
        print(f"  ⏳ Max pozisyon ({oc}/{MAX_OPEN})"); return False
    if d["loss_usd"] >= DAILY_LOSS_L:
        print(f"  ⛔ Günlük kayıp: ${d['loss_usd']:.2f}"); return False
    return True

# ── MONİTÖR ──────────────────────────────────────────────────────────────────

def monitor(state):
    still = []
    for pos in state.get("positions",[]):
        try:
            price = last_price(pos["sym"])
        except Exception as e:
            print(f"  [Mon]{pos['sym']}: {e}"); still.append(pos); continue

        side  = pos["side"]
        entry = pos["entry"]
        try:
            dur = int((utc()-datetime.fromisoformat(
                pos.get("opened_iso", utc().isoformat()))).total_seconds())
        except: dur = 0

        # Breakeven: TP1'in %50'sine gelince SL entry'e çek
        if not pos.get("be_hit") and pos.get("tp1"):
            be = entry + (pos["tp1"]-entry)*0.5 if side=="LONG" \
                 else entry - (entry-pos["tp1"])*0.5
            if (side=="LONG" and price >= be) or (side=="SHORT" and price <= be):
                pos["sl"] = entry; pos["be_hit"] = True
                tg(f"🔰 *{pos['sym']}* BE'ye taşındı | `{fp(price)}`")
                print(f"  🔰 [{pos['sym']}] BE @ {fp(price)}")

        reason = None
        if dur >= MAX_HOLD_MIN * 60:
            reason = "TIMEOUT"
        elif side == "LONG":
            if   price <= pos["sl"]:  reason = "BE" if pos.get("be_hit") else "SL"
            elif price >= pos["tp2"]: reason = "TP2"
            elif price >= pos["tp1"]: reason = "TP1"
        else:
            if   price >= pos["sl"]:  reason = "BE" if pos.get("be_hit") else "SL"
            elif price <= pos["tp2"]: reason = "TP2"
            elif price <= pos["tp1"]: reason = "TP1"

        if reason:
            tid    = pos.get("trade_id","—")
            trades = record_trade(pos, price, reason, dur)
            tg(msg_close(pos, price, reason, dur, tid))
            pct = (price-entry)/entry*(1 if side=="LONG" else -1)
            pnl = POSITION_USD * pct
            d   = state["daily"]
            if pnl < 0:
                d["loss_usd"]      += abs(pnl)
                d["consec_losses"] += 1
            else:
                d["consec_losses"] = 0
            print(f"  [{reason}] {pos['sym']} @ {fp(price)} | P&L: ${pnl:+.2f}")
            if len(trades) % 10 == 0:
                tg(msg_stats(calc_stats(trades)))
        else:
            pct = (price-entry)/entry*(1 if side=="LONG" else -1)
            print(f"  [OPEN] {pos['sym']} {side} | {fp(price)} ({pct*100:+.2f}%) "
                  f"SL:{fp(pos['sl'])} TP1:{fp(pos['tp1'])} {dur//60}dk")
            still.append(pos)
        time.sleep(0.1)

    state["positions"] = still
    return state

# ── BALINA TARAMASI + POZİSYON AÇ ────────────────────────────────────────────

def whale_scan_and_open(state, universe):
    if not can_open(state): return state

    open_syms = {p["sym"] for p in state.get("positions",[])}
    slots     = MAX_OPEN - len(open_syms)
    d         = state["daily"]

    print(f"\n  🐋 Balina taraması | {utc().strftime('%H:%M:%S UTC')}")
    print(f"  Açık:{len(open_syms)}/{MAX_OPEN} | Kayıp:${d['loss_usd']:.2f} | Ardışık:{d['consec_losses']}")

    candidates = []
    for sym, _ in universe[:50]:       # hacme göre top 50
        if sym in open_syms: continue
        try:
            w = get_whale_signal(sym)
            if abs(w["score"]) >= WHALE_MIN_SCORE and w["direction"]:
                candidates.append(w)
                print(f"  🐋 {sym} skor:{w['score']:+d} yön:{w['direction']}")
        except: pass
        time.sleep(0.08)

    candidates.sort(key=lambda x: abs(x["score"]), reverse=True)
    print(f"  {len(candidates)} balina sinyali bulundu.")

    opened = 0
    for w in candidates:
        if opened >= slots or not can_open(state): break
        if w["sym"] in {p["sym"] for p in state.get("positions",[])}: continue

        try: entry = last_price(w["sym"])
        except: continue

        side = w["direction"]
        if side == "LONG":
            sl  = round(entry * (1 - SL_PCT), 8)
            tp1 = round(entry * (1 + TP1_PCT), 8)
            tp2 = round(entry * (1 + TP2_PCT), 8)
        else:
            sl  = round(entry * (1 + SL_PCT), 8)
            tp1 = round(entry * (1 - TP1_PCT), 8)
            tp2 = round(entry * (1 - TP2_PCT), 8)

        tid = str(uuid.uuid4())[:8].upper()
        pos = {
            "sym": w["sym"], "side": side, "entry": entry,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "whale_score": w["score"],
            "whale_signals": w["signals"],
            "trade_id": tid, "be_hit": False,
            "opened_iso": utc().isoformat(), "opened_ts": ts()
        }
        state.setdefault("positions", []).append(pos)
        tg(msg_open(pos, tid))
        print(f"  🚀 AÇILDI: {w['sym']} {side} @ {fp(entry)} | Skor:{w['score']:+d} | ID:{tid}")
        opened += 1
        time.sleep(0.3)

    if opened == 0 and not candidates:
        print(f"  🔍 Dikkat çekici balina hareketi yok.")

    return state

# ── ANA DÖNGÜ ─────────────────────────────────────────────────────────────────

def main():
    print("="*55)
    print("🐋 BALİNA TAKİP BOTU")
    print(f"   Min Skor:{WHALE_MIN_SCORE} | Max {MAX_OPEN} pozisyon")
    print(f"   SL:%{SL_PCT*100:.1f} | TP1:%{TP1_PCT*100:.1f} | TP2:%{TP2_PCT*100:.1f}")
    print(f"   Timeout:{MAX_HOLD_MIN}dk | TG:{'✅' if TK else '❌'}")
    print("="*55+"\n")

    trades = load_db()
    if trades:
        s = calc_stats(trades)
        print(f"  DB: {s['total']} trade | WR:{s['wins']}/{s['total']} | P&L:${s['total_pnl']:+.2f}\n")

    while True:
        try:
            state    = load_st()
            state    = reset_daily(state)
            universe = get_universe()

            if state.get("positions"):
                state = monitor(state)
                save_st(state)

            state = whale_scan_and_open(state, universe)
            save_st(state)

        except Exception as e:
            print(f"[HATA]{e}")
        time.sleep(SCAN_EVERY)

if __name__ == "__main__":
    main()
