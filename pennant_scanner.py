"""
pennant_scanner.py — Yükselen Flama Botu
5m + 15m grafiklerde rising pennant tespit eder, pozisyon açar, takip eder.
"""

import json, math, os, time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")
TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT        = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_SCORE      = int(os.getenv("PENNANT_MIN_SCORE", "55"))
COOLDOWN_MIN   = int(os.getenv("PENNANT_COOLDOWN_MINUTES", "15"))
MIN_VOL        = float(os.getenv("MIN_QUOTE_VOLUME_USD", "5000000"))
MAX_VOL        = float(os.getenv("MAX_QUOTE_VOLUME_USD", "1000000000"))
STATE_FILE     = os.getenv("PENNANT_STATE_FILE", "pennant_state.json")
MAX_POSITIONS  = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
TIMEFRAMES     = ("5m", "15m")

STABLECOINS = {"USDC","BUSD","DAI","TUSD","USDP","FDUSD","USDD","FRAX","GUSD","LUSD","USTC","UST","EURC"}

# ── Helpers ───────────────────────────────────────────────────────────────────
def _s(v) -> float:
    try:
        r = float(v)
        return 0.0 if (math.isnan(r) or math.isinf(r)) else r
    except: return 0.0

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def fmt_price(v: float) -> str:
    if v >= 1000: return f"{v:.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"

def send_tg(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print(text); return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=20,
        ).raise_for_status()
    except Exception as e:
        print(f"[TG Error] {e}")

def fetch_json(url, params=None):
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    raw = fetch_json(f"{BASE_URL}/fapi/v1/klines",
                     {"symbol": symbol, "interval": interval, "limit": limit})
    cols = ["open_time","open","high","low","close","volume",
            "close_time","qav","trades","tbbav","tbqav","ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def fetch_last_price(symbol: str) -> float:
    raw = fetch_json(f"{BASE_URL}/fapi/v1/ticker/price", {"symbol": symbol})
    return float(raw.get("price", 0))

def linreg_slope(arr) -> float:
    y = np.array(arr, dtype=float)
    if len(y) < 3: return 0.0
    x = np.arange(len(y))
    s = np.polyfit(x, y, 1)[0]
    base = abs(y.mean()) or 1.0
    return s / base * 100

# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except: pass
    return {"positions": [], "last_scan_at": None, "closed_trades": []}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# ── Dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class PennantSignal:
    symbol: str
    timeframe: str
    score: int
    signals: list
    current_price: float
    flagpole_gain_pct: float
    flagpole_height_abs: float   # Direk yüksekliği (fiyat farkı)
    consolidation_bars: int
    volume_decline_pct: float
    upper_trendline: float       # Kırılım seviyesi
    cons_low: float              # Konsolidasyonun en dibi (stop için)
    dist_to_breakout_pct: float
    dual_tf: bool = False
    price_change_24h: float = 0.0

@dataclass
class Position:
    symbol: str
    timeframe: str
    entry_price: float
    stop_loss: float
    target1: float
    target2: float
    flagpole_gain_pct: float
    score: int
    opened_at: str
    highest_price: float
    status: str = "OPEN"         # OPEN | TP1 | TP2 | SL | TIMEOUT

# ── Pattern Detection ─────────────────────────────────────────────────────────
def detect_pennant(df: pd.DataFrame, symbol: str, tf: str,
                   pct24h: float = 0.0) -> Optional[PennantSignal]:
    if len(df) < 40: return None

    closes = df["close"].values.astype(float)
    highs  = df["high"].values.astype(float)
    lows   = df["low"].values.astype(float)
    vols   = df["volume"].values.astype(float)

    cur = _s(closes[-2])
    if cur <= 0: return None

    # 1. Flagpole
    search_end = len(df) - 2
    search_start = max(0, search_end - 40)
    best_gain, fp_s, fp_e = 0.0, -1, -1

    for fe in range(search_end - 4, search_start + 3, -1):
        for flen in range(3, 13):
            fs = fe - flen
            if fs < search_start: break
            fl = _s(lows[fs]); fh = _s(highs[fe])
            if fl <= 0: continue
            g = (fh - fl) / fl * 100
            if g >= 2.5 and g > best_gain:
                best_gain, fp_s, fp_e = g, fs, fe

    if fp_s < 0 or best_gain < 2.5: return None

    # Direk yüksekliği (abs fiyat farkı)
    fp_height = _s(highs[fp_e]) - _s(lows[fp_s])

    # 2. Consolidation
    cs, ce = fp_e, search_end
    cb = ce - cs
    if not (4 <= cb <= 20): return None

    ch = highs[cs:ce+1]; cl = lows[cs:ce+1]; cv = vols[cs:ce+1]
    if len(ch) < 4: return None

    ls = linreg_slope(cl)  # lows slope
    hs = linreg_slope(ch)  # highs slope
    is_wedge = ls > 0.03 and hs < -0.03
    is_flat  = abs(ls) < 0.5 and abs(hs) < 0.5
    if not (is_wedge or is_flat): return None

    # 3. Volume
    fp_vol_avg  = float(np.mean(vols[fp_s:fp_e+1])) if fp_e > fp_s else 1.0
    cons_vol_avg = float(np.mean(cv)) if len(cv) > 0 else 1.0
    vol_dec = (fp_vol_avg - cons_vol_avg) / fp_vol_avg * 100 if fp_vol_avg > 0 else 0.0

    # 4. Upper trendline & breakout dist
    xs = np.arange(len(ch))
    coef = np.polyfit(xs, ch, 1)
    upper_tl = float(np.polyval(coef, len(ch)))
    cons_low  = float(np.min(cl))
    dist = (upper_tl - cur) / cur * 100 if cur > 0 else 99.0

    # 5. Score
    score = 0; sigs = []

    if best_gain >= 8:   score += 30; sigs.append(f"🚀 Güçlü direk +%{best_gain:.1f}")
    elif best_gain >= 5: score += 22; sigs.append(f"📈 Direk +%{best_gain:.1f}")
    else:                score += 14; sigs.append(f"📈 Direk +%{best_gain:.1f}")

    if is_wedge:   score += 25; sigs.append(f"📐 Kama (↑low:{ls:+.2f}% ↓high:{hs:+.2f}%)")
    elif is_flat:  score += 10; sigs.append("➡️ Yatay konsolidasyon")

    if vol_dec >= 40:   score += 20; sigs.append(f"📉 Hacim -%{vol_dec:.0f}")
    elif vol_dec >= 25: score += 12; sigs.append(f"📉 Hacim -%{vol_dec:.0f}")
    elif vol_dec >= 10: score += 6;  sigs.append(f"↘️ Hacim -%{vol_dec:.0f}")

    if 5 <= cb <= 12:  score += 15; sigs.append(f"⏱️ İdeal kons: {cb} bar")
    elif 3 <= cb <= 16: score += 8; sigs.append(f"⏱️ Kons: {cb} bar")

    if 0 <= dist <= 1.5:  score += 10; sigs.append(f"🎯 Kırılım %{dist:.2f} uzakta")
    elif dist < 0:
        if dist >= -1.0: score += 8;  sigs.append(f"✅ Kırıldı (+%{abs(dist):.2f})")
        else:            score += 3;  sigs.append("⚠️ Kırılım geçti")

    return PennantSignal(
        symbol=symbol, timeframe=tf, score=min(score, 100), signals=sigs,
        current_price=cur, flagpole_gain_pct=best_gain,
        flagpole_height_abs=fp_height, consolidation_bars=cb,
        volume_decline_pct=vol_dec, upper_trendline=upper_tl,
        cons_low=cons_low, dist_to_breakout_pct=dist,
        price_change_24h=pct24h,
    )

# ── Position Levels ───────────────────────────────────────────────────────────
def calc_levels(sig: PennantSignal):
    """Giriş, SL, TP1, TP2 hesapla."""
    entry = sig.upper_trendline * 1.001          # Kırılım + %0.1 buffer
    sl    = sig.cons_low * 0.998                 # Kons dip - %0.2 buffer
    risk  = entry - sl
    if risk <= 0: risk = entry * 0.02            # fallback %2 risk
    tp1   = entry + risk * 1.5                   # 1.5R
    tp2   = entry + sig.flagpole_height_abs      # Direk boyu projeksiyon
    return entry, sl, tp1, tp2

# ── Telegram Messages ─────────────────────────────────────────────────────────
def msg_signal(sig: PennantSignal) -> str:
    entry, sl, tp1, tp2 = calc_levels(sig)
    dual = " 🔥*ÇİFT TF*" if sig.dual_tf else ""
    bar  = "█" * (sig.score // 10) + "░" * (10 - sig.score // 10)
    sigs = "\n".join(f"  • {s}" for s in sig.signals)
    rr   = (tp1 - entry) / (entry - sl) if entry > sl else 0
    return (
        f"🏳️ *FLAMA SİNYALİ* | *{sig.symbol}* [{sig.timeframe}]{dual}\n"
        f"Skor: `{sig.score}/100` `{bar}`\n\n"
        f"*Sinyaller:*\n{sigs}\n\n"
        f"*📌 Pozisyon:*\n"
        f"Giriş:   `{fmt_price(entry)}`\n"
        f"Stop:    `{fmt_price(sl)}`\n"
        f"Hedef 1: `{fmt_price(tp1)}` _(1.5R)_\n"
        f"Hedef 2: `{fmt_price(tp2)}` _(Direk boyu)_\n"
        f"Risk/Ödül: `{rr:.1f}R`\n\n"
        f"Direk: `+%{sig.flagpole_gain_pct:.1f}` | "
        f"Kons: `{sig.consolidation_bars} bar` | "
        f"Hacim↓: `%{sig.volume_decline_pct:.0f}`"
    )

def msg_summary(signals: list) -> str:
    if not signals: return "🔍 Flama bulunamadı."
    lines = ["🏳️ *FLAMA RADAR ÖZET*\n"]
    for s in signals[:12]:
        entry, sl, tp1, _ = calc_levels(s)
        dual = "🔥" if s.dual_tf else "  "
        rr = (tp1 - entry) / (entry - sl) if entry > sl else 0
        lines.append(
            f"{dual}*{s.symbol}* [{s.timeframe}] `{s.score}/100`\n"
            f"  Giriş: `{fmt_price(entry)}` | SL: `{fmt_price(sl)}` | "
            f"TP1: `{fmt_price(tp1)}` | R/R: `{rr:.1f}R`"
        )
    return "\n".join(lines)

def msg_exit(pos: Position, exit_price: float) -> str:
    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
    emoji = "✅" if pos.status in ("TP1","TP2") else "❌" if pos.status == "SL" else "⏱️"
    label = {"TP1":"HEDEF 1 VURDU","TP2":"HEDEF 2 VURDU",
             "SL":"STOP OLDU","TIMEOUT":"SÜRE DOLDU"}.get(pos.status, pos.status)
    return (
        f"{emoji} *{label}* | *{pos.symbol}* [{pos.timeframe}]\n"
        f"Giriş: `{fmt_price(pos.entry_price)}` → Çıkış: `{fmt_price(exit_price)}`\n"
        f"Değişim: `{pnl_pct:+.2f}%`\n"
        f"SL: `{fmt_price(pos.stop_loss)}` | "
        f"TP1: `{fmt_price(pos.target1)}` | TP2: `{fmt_price(pos.target2)}`\n"
        f"Direk: `+%{pos.flagpole_gain_pct:.1f}` | Skor: `{pos.score}`"
    )

# ── Position Monitoring ───────────────────────────────────────────────────────
def monitor_positions(state: dict) -> dict:
    positions = [Position(**p) for p in state.get("positions", [])]
    if not positions:
        return state

    open_pos = [p for p in positions if p.status == "OPEN"]
    closed   = [p for p in positions if p.status != "OPEN"]

    still_open = []
    for pos in open_pos:
        try:
            price = fetch_last_price(pos.symbol)
            pos.highest_price = max(pos.highest_price, price)

            # Timeout: 24 saat
            opened = datetime.fromisoformat(pos.opened_at)
            age_h  = (utcnow() - opened).total_seconds() / 3600
            if age_h > 24:
                pos.status = "TIMEOUT"
                send_tg(msg_exit(pos, price))
                closed.append(pos)
                print(f"  [TIMEOUT] {pos.symbol} [{pos.timeframe}]")
                continue

            if price >= pos.target2:
                pos.status = "TP2"
                send_tg(msg_exit(pos, price))
                closed.append(pos)
                print(f"  [TP2 ✅] {pos.symbol} {price}")
            elif price >= pos.target1:
                pos.status = "TP1"
                send_tg(msg_exit(pos, price))
                closed.append(pos)
                print(f"  [TP1 ✅] {pos.symbol} {price}")
            elif price <= pos.stop_loss:
                pos.status = "SL"
                send_tg(msg_exit(pos, price))
                closed.append(pos)
                print(f"  [SL ❌] {pos.symbol} {price}")
            else:
                still_open.append(pos)
                print(f"  [OPEN] {pos.symbol} [{pos.timeframe}] fiyat={fmt_price(price)} "
                      f"SL={fmt_price(pos.stop_loss)} TP1={fmt_price(pos.target1)}")
        except Exception as e:
            print(f"  [Monitor Error] {pos.symbol}: {e}")
            still_open.append(pos)
        time.sleep(0.1)

    state["positions"] = [asdict(p) for p in still_open]
    state["closed_trades"] = [asdict(p) for p in closed][-200:]  # son 200
    return state

# ── Market Scanner ────────────────────────────────────────────────────────────
def get_candidates() -> list:
    try:
        info = fetch_json(f"{BASE_URL}/fapi/v1/exchangeInfo")
        active = set()
        for row in info.get("symbols", []):
            if row.get("status") != "TRADING": continue
            if row.get("contractType") != "PERPETUAL": continue
            if row.get("quoteAsset") != "USDT": continue
            sym = str(row.get("symbol", "")).upper()
            if sym.endswith("USDT") and sym[:-4].isalnum() and sym[:-4] not in STABLECOINS:
                active.add(sym)

        tickers = fetch_json(f"{BASE_URL}/fapi/v1/ticker/24hr")
        cands = []
        for row in tickers:
            sym = str(row.get("symbol","")).upper()
            if sym not in active: continue
            try:
                qv  = float(row.get("quoteVolume", 0))
                pct = float(row.get("priceChangePercent", 0))
            except: continue
            if MIN_VOL <= qv <= MAX_VOL:
                cands.append((sym, pct, qv))

        cands.sort(key=lambda x: x[2], reverse=True)
        return cands
    except Exception as e:
        print(f"[Market Error] {e}")
        return []

# ── Main Scan ─────────────────────────────────────────────────────────────────
def run_scan(state: dict) -> dict:
    print(f"\n{'='*55}")
    print(f"🏳️  FLAMA TARAMASI — {utcnow().strftime('%H:%M:%S UTC')}")
    print(f"{'='*55}")

    candidates = get_candidates()
    print(f"  {len(candidates)} coin hacim filtresini geçti.\n")

    open_syms = {p["symbol"] for p in state.get("positions", [])}
    all_signals: list[PennantSignal] = []
    seen: dict[str, list] = {}  # symbol -> [signals per tf]

    for idx, (sym, pct24h, _) in enumerate(candidates):
        print(f"  [{idx+1}/{len(candidates)}] {sym}...", end="\r")
        sym_sigs = []
        for tf in TIMEFRAMES:
            try:
                df  = fetch_klines(sym, tf, 200)
                sig = detect_pennant(df, sym, tf, pct24h)
                if sig and sig.score >= MIN_SCORE:
                    sym_sigs.append(sig)
            except: pass
            time.sleep(0.05)

        if sym_sigs:
            tfs = {s.timeframe for s in sym_sigs}
            if "5m" in tfs and "15m" in tfs:
                for s in sym_sigs: s.dual_tf = True
            all_signals.extend(sym_sigs)

    all_signals.sort(key=lambda s: (s.dual_tf, s.score), reverse=True)
    print(f"\n  ✅ {len(all_signals)} flama tespit edildi.\n")

    # Özet gönder
    if all_signals:
        send_tg(msg_summary(all_signals))
        time.sleep(0.5)

    # Yeni pozisyonlar aç (slot varsa, aynı sembol yoksa)
    open_count = len([p for p in state.get("positions", []) if p.get("status") == "OPEN"])
    new_opened = 0

    for sig in all_signals:
        if open_count + new_opened >= MAX_POSITIONS:
            break
        # En iyi sinyal (çift TF veya yüksek skor) + zaten açık değil
        if sig.symbol in open_syms:
            continue
        if not sig.dual_tf and sig.score < MIN_SCORE + 15:
            continue

        entry, sl, tp1, tp2 = calc_levels(sig)
        if entry <= 0 or sl >= entry:
            continue

        pos = Position(
            symbol=sig.symbol, timeframe=sig.timeframe,
            entry_price=entry, stop_loss=sl,
            target1=tp1, target2=tp2,
            flagpole_gain_pct=sig.flagpole_gain_pct,
            score=sig.score,
            opened_at=utcnow().isoformat(),
            highest_price=entry,
            status="OPEN",
        )
        state.setdefault("positions", []).append(asdict(pos))
        open_syms.add(sig.symbol)
        new_opened += 1

        send_tg(msg_signal(sig))
        print(f"  [AÇILDI] {sig.symbol} [{sig.timeframe}] "
              f"Giriş={fmt_price(entry)} SL={fmt_price(sl)} "
              f"TP1={fmt_price(tp1)} TP2={fmt_price(tp2)}")
        time.sleep(0.3)

    state["last_scan_at"] = utcnow().isoformat()
    return state

# ── Loop ──────────────────────────────────────────────────────────────────────
def main():
    print("🏳️  Flama Botu Başladı")
    print(f"   Min Skor: {MIN_SCORE} | Cooldown: {COOLDOWN_MIN}dk | Max Pozisyon: {MAX_POSITIONS}")
    print(f"   Telegram: {'✅' if TG_TOKEN else '❌ (konsola yazdırılır)'}\n")

    while True:
        state = load_state()
        try:
            # 1. Açık pozisyonları izle
            if state.get("positions"):
                print("[Pozisyon İzleme]")
                state = monitor_positions(state)
                save_state(state)

            # 2. Cooldown kontrolü
            last = state.get("last_scan_at")
            do_scan = True
            if last:
                elapsed = (utcnow() - datetime.fromisoformat(last)).total_seconds()
                if elapsed < COOLDOWN_MIN * 60:
                    wait = int(COOLDOWN_MIN * 60 - elapsed)
                    print(f"[Cooldown] {wait//60}dk {wait%60}sn sonra taranacak.")
                    do_scan = False

            # 3. Tara
            if do_scan:
                state = run_scan(state)
                save_state(state)

        except Exception as e:
            print(f"[Döngü Hatası] {e}")

        time.sleep(60)

if __name__ == "__main__":
    main()
