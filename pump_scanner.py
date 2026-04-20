"""
pump_scanner.py — Pump öncesi sinyal radar modülü.

Bollinger sıkışması, sessiz hacim birikimi, MACD dönüşü, OI birikimi ve
funding rate gibi 8 farklı sinyali birleştirerek 0-100 arası pump skor üretir.
Eşiği geçen coinler Telegram'a bildirim gönderir, pozisyon açmaz.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


# ─── Yardımcı ────────────────────────────────────────────────────────────────

def _safe(val) -> float:
    """NaN/Inf güvenli float dönüşümü."""
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except Exception:
        return 0.0


# ─── Teknik hesaplamalar ──────────────────────────────────────────────────────

def compute_bollinger_width(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> float:
    """
    Bollinger Band genişliği (normalized).
    Düşük değer = bantlar sıkışmış = sert hareket yakın.
    Döner: bb_width / orta_band (0.0x tipik değer)
    """
    if len(df) < period + 5:
        return 999.0

    close = df["close"].astype(float)
    middle = close.rolling(period).mean()
    std = close.rolling(period).std()

    upper = middle + std_mult * std
    lower = middle - std_mult * std

    last_middle = _safe(middle.iloc[-2])
    last_width = _safe(upper.iloc[-2] - lower.iloc[-2])

    if last_middle <= 0:
        return 999.0
    return last_width / last_middle


def compute_volume_accumulation(df: pd.DataFrame, lookback: int = 20, recent: int = 5) -> tuple[float, float]:
    """
    Sessiz hacim birikimi tespiti.
    Döner: (volume_spike_ratio, price_move_pct)
    Yüksek spike + düşük fiyat hareketi = birikim sinyali.
    """
    if len(df) < lookback + recent + 5:
        return 1.0, 0.0

    base = df.iloc[-(lookback + recent + 2):-(recent + 2)]
    recent_bars = df.iloc[-(recent + 2):-2]

    vol_base = _safe(base["volume"].mean())
    vol_recent = _safe(recent_bars["volume"].mean())

    price_start = _safe(recent_bars.iloc[0]["close"])
    price_end = _safe(recent_bars.iloc[-1]["close"])
    price_move = abs((price_end - price_start) / price_start * 100) if price_start > 0 else 999.0

    spike = vol_recent / vol_base if vol_base > 0 else 1.0
    return spike, price_move


def compute_rsi_acceleration(df: pd.DataFrame, lookback: int = 5) -> tuple[float, float]:
    """
    RSI ivmesi: son N bar önce RSI ve şimdiki RSI.
    Döner: (prev_rsi, current_rsi)
    """
    if len(df) < lookback + 5:
        return 50.0, 50.0

    prev_rsi = _safe(df.iloc[-(lookback + 2)]["rsi14"])
    curr_rsi = _safe(df.iloc[-2]["rsi14"])
    return prev_rsi, curr_rsi


def compute_macd_4h_turn(df_4h: pd.DataFrame) -> tuple[float, float]:
    """
    4h MACD histogram dönüşü.
    Döner: (prev_hist, curr_hist)
    """
    if len(df_4h) < 5:
        return 0.0, 0.0

    prev = _safe(df_4h.iloc[-3]["macd_hist"])
    curr = _safe(df_4h.iloc[-2]["macd_hist"])
    return prev, curr


def resistance_proximity(df_4h: pd.DataFrame, current_price: float, lookback: int = 20) -> float:
    """
    Fiyatın son N bar yükseklerine olan uzaklığı (%).
    Düşük değer = direncin hemen altında.
    """
    if len(df_4h) < lookback + 3:
        return 999.0

    recent_high = _safe(df_4h.iloc[-(lookback + 2):-2]["high"].max())
    if recent_high <= 0 or current_price <= 0:
        return 999.0
    return (recent_high - current_price) / current_price * 100


# ─── Ana dataclass ────────────────────────────────────────────────────────────

@dataclass
class PumpCandidate:
    symbol: str
    score: int                    # 0-100 pump skor
    signals: list[str]            # Tetiklenen sinyal açıklamaları
    current_price: float
    price_change_24h: float
    volume_spike: float           # Hacim çarpanı (örn. 2.3 → 2.3x ortalama)
    rsi_15m: float
    rsi_1h: float
    funding_pct: float | None
    bb_width: float               # Bollinger genişliği
    resistance_gap_pct: float     # Direnç eşiğine uzaklık %
    open_interest: float | None


# ─── Ana tespit fonksiyonu ────────────────────────────────────────────────────

def detect_pump_candidate(
    symbol: str,
    price_change_24h: float,
    df_15m: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    funding_rate: float | None = None,
    open_interest: float | None = None,
) -> PumpCandidate | None:
    """
    Tek bir coin için pump öncesi sinyalleri analiz eder.

    Returns:
        PumpCandidate if sinyaller mevcutsa, None if yetersiz veri.
    """
    # Minimum veri kontrolü
    if len(df_15m) < 50 or len(df_1h) < 50 or len(df_4h) < 30:
        return None

    # Gerekli sütunların varlığını kontrol et
    required = ["close", "high", "low", "volume", "rsi14", "macd_hist", "ema20", "ema50", "atr14"]
    for col in required:
        if col not in df_15m.columns or col not in df_1h.columns:
            return None

    score = 0
    signals: list[str] = []

    close = _safe(df_15m.iloc[-2]["close"])
    if close <= 0:
        return None

    # ── Sinyal 1: Bollinger Band Sıkışması ───────────────────────────────────
    bb_width = compute_bollinger_width(df_15m)
    if bb_width < 0.025:
        signals.append(f"🔴 BB sıkışması ({bb_width:.3f})")
        score += 20
    elif bb_width < 0.04:
        signals.append(f"🟡 BB daralıyor ({bb_width:.3f})")
        score += 10

    # ── Sinyal 2: Sessiz Hacim Birikimi ──────────────────────────────────────
    vol_spike, price_move_pct = compute_volume_accumulation(df_15m)
    if vol_spike >= 2.0 and price_move_pct < 1.5:
        signals.append(f"📊 Sessiz birikim ({vol_spike:.1f}x hacim, %{price_move_pct:.1f} hareket)")
        score += 25
    elif vol_spike >= 1.5 and price_move_pct < 2.0:
        signals.append(f"📊 Hacim artışı ({vol_spike:.1f}x)")
        score += 12

    # ── Sinyal 3: RSI İvmesi ──────────────────────────────────────────────────
    prev_rsi, curr_rsi = compute_rsi_acceleration(df_15m, lookback=5)
    if 38 <= prev_rsi <= 58 and curr_rsi > prev_rsi + 7:
        signals.append(f"⚡ RSI ivmeleniyor ({prev_rsi:.0f}→{curr_rsi:.0f})")
        score += 15
    elif 40 <= prev_rsi <= 60 and curr_rsi > prev_rsi + 4:
        signals.append(f"⚡ RSI yükseliyor ({prev_rsi:.0f}→{curr_rsi:.0f})")
        score += 8

    # 1h RSI de iyiyse bonus
    rsi_1h = _safe(df_1h.iloc[-2]["rsi14"])
    if 45 <= rsi_1h <= 65:
        score += 5  # Sessiz bonus, sinyal listeleme

    # ── Sinyal 4: 4h MACD Dönüşü ─────────────────────────────────────────────
    prev_macd, curr_macd = compute_macd_4h_turn(df_4h)
    threshold = close * 0.00005  # Fiyata göre sıfır eşiği
    if prev_macd < -threshold and curr_macd > -threshold:
        signals.append("🔁 4h MACD pozitife dönüyor")
        score += 20
    elif curr_macd > prev_macd and curr_macd > -threshold * 3 and prev_macd < 0:
        signals.append("📈 4h MACD toparlanıyor")
        score += 10

    # ── Sinyal 5: OI Birikimi (fiyat sessizken) ───────────────────────────────
    if open_interest is not None and open_interest > 0:
        if abs(price_change_24h) < 3.0:
            signals.append(f"🐋 OI artıyor, fiyat sessiz (%{price_change_24h:+.1f})")
            score += 15
        elif price_change_24h > 0 and price_change_24h < 5:
            # OI var, fiyat yavaş yükseliyor = sağlıklı birikim
            score += 8

    # ── Sinyal 6: Funding Rate ────────────────────────────────────────────────
    funding_pct = funding_rate * 100 if funding_rate is not None else None
    if funding_rate is not None:
        if funding_rate < -0.0001:
            signals.append(f"💸 Funding negatif ({funding_pct:.4f}%)")
            score += 15
        elif funding_rate < 0.0002:
            signals.append(f"💚 Funding düşük ({funding_pct:.4f}%)")
            score += 7

    # ── Sinyal 7: Direnç Kırılımı Eşiğinde ───────────────────────────────────
    res_gap = resistance_proximity(df_4h, close)
    if res_gap < -0.5:  # Zaten direncin üstünde (kırılmış)
        signals.append(f"✅ Direnç kırıldı, yeni alan açıldı (%{abs(res_gap):.1f} üstünde)")
        score += 12
    elif 0 <= res_gap <= 1.5:
        signals.append(f"🎯 Direnç eşiğinde (%{res_gap:.1f} uzakta)")
        score += 18
    elif 1.5 < res_gap <= 3.5:
        signals.append(f"🎯 Direncine yakın (%{res_gap:.1f} uzakta)")
        score += 8

    # ── Sinyal 8: EMA Dizilişi Başlıyor ──────────────────────────────────────
    ema20_1h = _safe(df_1h.iloc[-2]["ema20"])
    ema50_1h = _safe(df_1h.iloc[-2]["ema50"])
    close_1h = _safe(df_1h.iloc[-2]["close"])
    if close_1h > ema20_1h > 0 and ema20_1h >= ema50_1h * 0.998:
        signals.append("📐 1h EMA dizilişi oluşuyor")
        score += 10

    # ── Bonus: 15m > 1h > 4h hepsi yukarı bakıyor ────────────────────────────
    ema20_15 = _safe(df_15m.iloc[-2]["ema20"])
    ema20_4h = _safe(df_4h.iloc[-2]["ema20"])
    if close > ema20_15 > 0 and ema20_15 > ema20_1h > 0 and ema20_1h > ema20_4h > 0:
        signals.append("🟢 Multi-TF momentum")
        score += 10

    # ── Ceza: Zaten çok pump yapmış coinlere skor düşür ─────────────────────
    if price_change_24h > 25:
        score = max(0, score - 25)  # Ciddi pump, geri dönüş riski yüksek
        signals.append(f"⚠️ Uyarı: 24s %{price_change_24h:.0f} artış (geç kalınmış olabilir)")
    elif price_change_24h > 15:
        score = max(0, score - 12)

    # Skor üst sınırı
    score = min(score, 100)

    return PumpCandidate(
        symbol=symbol,
        score=score,
        signals=signals,
        current_price=close,
        price_change_24h=price_change_24h,
        volume_spike=vol_spike,
        rsi_15m=curr_rsi,
        rsi_1h=rsi_1h,
        funding_pct=funding_pct,
        bb_width=bb_width,
        resistance_gap_pct=res_gap,
        open_interest=open_interest,
    )


# ─── Telegram mesaj formatı ───────────────────────────────────────────────────

def _format_price(value: float) -> str:
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def format_pump_alert(candidate: PumpCandidate) -> str:
    """Telegram için pump uyarı mesajı formatla."""
    signal_lines = "\n".join(f"  • {s}" for s in candidate.signals)
    funding_text = (
        f"`{candidate.funding_pct:+.4f}%`"
        if candidate.funding_pct is not None
        else "`?`"
    )
    if candidate.resistance_gap_pct < -0.5:
        res_text = f"`✅ Kırıldı (+%{abs(candidate.resistance_gap_pct):.1f})`"
    elif candidate.resistance_gap_pct < 100:
        res_text = f"`%{candidate.resistance_gap_pct:.1f} uzakta`"
    else:
        res_text = "`?`"

    score_bar = "█" * (candidate.score // 10) + "░" * (10 - candidate.score // 10)

    return (
        f"🚀 *PUMP RADARI* | *{candidate.symbol}*\n"
        f"Skor: `{candidate.score}/100`  `{score_bar}`\n"
        f"24s Değişim: `{candidate.price_change_24h:+.2f}%`\n\n"
        f"*Tespit edilen sinyaller:*\n{signal_lines}\n\n"
        f"*Teknik Özet:*\n"
        f"Fiyat: `{_format_price(candidate.current_price)}`"
        f" | RSI 15m: `{candidate.rsi_15m:.0f}`"
        f" | RSI 1h: `{candidate.rsi_1h:.0f}`\n"
        f"Hacim: `{candidate.volume_spike:.1f}x`"
        f" | Funding: {funding_text}"
        f" | Direnç: {res_text}"
    )


def format_pump_summary(candidates: list[PumpCandidate]) -> str:
    """Birden fazla pump adayı için özet mesaj."""
    if not candidates:
        return "🔍 Bu turda pump adayı tespit edilmedi."

    lines = ["🔭 *PUMP RADAR ÖZET*\n"]
    for c in candidates[:10]:  # Max 10
        bar = "█" * (c.score // 10)
        lines.append(
            f"*{c.symbol}* `{c.score}/100` {bar} | "
            f"`{c.price_change_24h:+.2f}%` | "
            f"Vol: `{c.volume_spike:.1f}x` | "
            f"{len(c.signals)} sinyal"
        )
    return "\n".join(lines)
