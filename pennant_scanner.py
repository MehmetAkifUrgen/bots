"""
pennant_scanner.py — Yükselen Flama (Rising Pennant) Pattern Tarayıcı

5dk ve 15dk grafiklerde rising pennant (yükselen flama) pattern'ini tespit eder.
Tespit edilen coinleri Telegram'a bildirim olarak gönderir.

Yükselen Flama Kriterleri:
  1. Güçlü yükseliş (flagpole): Son N barda minimum %X yükseliş
  2. Daralma (consolidation): Ardından higher-lows + lower-highs (kama)
  3. Hacim azalması: Konsolidasyon sırasında hacim düşmeli
  4. Kırılım eşiğinde: Fiyat üst trend çizgisine yakın olmalı
  5. Çift timeframe teyidi: Hem 5m hem 15m'de geçerliyse daha güçlü sinyal
"""

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


# ─── Yardımcı fonksiyonlar ────────────────────────────────────────────────────

def _safe(val) -> float:
    """NaN/Inf güvenli float dönüşümü."""
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except Exception:
        return 0.0


def _linreg_slope(series: pd.Series) -> float:
    """Doğrusal regresyon eğimi (normalized %)."""
    y = series.dropna().values.astype(float)
    if len(y) < 3:
        return 0.0
    x = np.arange(len(y))
    slope = np.polyfit(x, y, 1)[0]
    baseline = abs(y.mean()) if abs(y.mean()) > 0 else 1.0
    return slope / baseline * 100  # % olarak döner


def _fetch_json(url: str, params: dict = None, timeout: int = 20):
    """Basit GET isteği."""
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _fetch_klines(base_url: str, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    """Binance Futures kline verisi çek."""
    raw = _fetch_json(
        f"{base_url}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
    )
    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore",
    ]
    df = pd.DataFrame(raw, columns=columns)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


# ─── Pattern Tespit Motoru ────────────────────────────────────────────────────

@dataclass
class PennantResult:
    symbol: str
    timeframe: str
    score: int                        # 0-100 güven skoru
    signals: list[str]                # Tespit edilen sinyaller
    current_price: float
    flagpole_gain_pct: float          # Direk yükseliş %
    consolidation_bars: int           # Daralma bar sayısı
    volume_decline_pct: float         # Konsolidasyonda hacim düşüşü %
    upper_trendline: float            # Üst trend çizgisi (kırılım seviyesi)
    distance_to_breakout_pct: float   # Kırılıma uzaklık %
    dual_tf_confirmed: bool = False   # 5m + 15m çift teyit
    price_change_24h: float = 0.0


def detect_pennant(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    price_change_24h: float = 0.0,
    # Parametreler
    flagpole_min_bars: int = 3,
    flagpole_max_bars: int = 12,
    flagpole_min_gain_pct: float = 2.5,
    consolidation_min_bars: int = 4,
    consolidation_max_bars: int = 20,
    max_dist_to_breakout_pct: float = 1.5,
) -> Optional[PennantResult]:
    """
    Tek bir timeframe'de Rising Pennant tespiti.

    Adımlar:
      1. Flagpole bul: Güçlü yükseliş bölgesi
      2. Konsolidasyon bul: Higher-lows + Lower-highs kama
      3. Hacim azalıyor mu kontrol et
      4. Kırılım eşiğine yakınlığı hesapla
      5. Skor hesapla
    """
    if len(df) < 40:
        return None

    score = 0
    signals: list[str] = []

    close_arr = df["close"].values.astype(float)
    high_arr = df["high"].values.astype(float)
    low_arr = df["low"].values.astype(float)
    vol_arr = df["volume"].values.astype(float)

    current_price = _safe(close_arr[-2])
    if current_price <= 0:
        return None

    # ─── 1. FLAGPOLE TESPİTİ ─────────────────────────────────────────────────
    # Son konsolidasyondan önce güçlü bir yükseliş segmenti ara
    # Geriye doğru T... T+f kadar bak (flagpole bölgesi)
    best_flagpole_gain = 0.0
    best_flagpole_start = -1
    best_flagpole_end = -1

    # Arama penceresi: son 40 barın ilk yarısında flagpole olabilir
    search_end = len(df) - 2  # En son completed bar
    search_start = max(0, search_end - 40)

    for fp_end in range(search_end - consolidation_min_bars, search_start + flagpole_min_bars, -1):
        for fp_len in range(flagpole_min_bars, flagpole_max_bars + 1):
            fp_start = fp_end - fp_len
            if fp_start < search_start:
                break
            fp_low = _safe(low_arr[fp_start])
            fp_high = _safe(high_arr[fp_end])
            if fp_low <= 0:
                continue
            gain_pct = (fp_high - fp_low) / fp_low * 100

            if gain_pct >= flagpole_min_gain_pct and gain_pct > best_flagpole_gain:
                best_flagpole_gain = gain_pct
                best_flagpole_start = fp_start
                best_flagpole_end = fp_end

    if best_flagpole_start < 0 or best_flagpole_gain < flagpole_min_gain_pct:
        return None  # Flagpole bulunamadı

    # ─── 2. KONSOLİDASYON (KAMA) TESPİTİ ────────────────────────────────────
    # Flagpole bitiş noktasından sonra kama arayalım
    cons_start = best_flagpole_end
    cons_end = search_end  # En son bar
    cons_bars = cons_end - cons_start

    if not (consolidation_min_bars <= cons_bars <= consolidation_max_bars):
        return None  # Konsolidasyon penceresi dışında

    cons_highs = high_arr[cons_start:cons_end + 1]
    cons_lows = low_arr[cons_start:cons_end + 1]
    cons_vols = vol_arr[cons_start:cons_end + 1]

    if len(cons_highs) < 4:
        return None

    # Higher lows kontrolü (düşükler yükseliyor)
    lows_slope = _linreg_slope(pd.Series(cons_lows))
    # Lower highs kontrolü (yüksekler düşüyor)
    highs_slope = _linreg_slope(pd.Series(cons_highs))

    # Kama koşulu: lows_slope > 0 (düşükler yükseliyor), highs_slope < 0 (yüksekler düşüyor)
    is_wedge = lows_slope > 0.03 and highs_slope < -0.03

    # Alternatif: Yatay konsolidasyon da kabul et ama daha düşük skor ver
    is_flat_cons = abs(lows_slope) < 0.5 and abs(highs_slope) < 0.5

    if not (is_wedge or is_flat_cons):
        return None

    # ─── 3. HAC;M ANALİZİ ────────────────────────────────────────────────────
    # Flagpole hacmi vs konsolidasyon hacmi
    fp_vols = vol_arr[best_flagpole_start:best_flagpole_end + 1]
    fp_avg_vol = float(np.mean(fp_vols)) if len(fp_vols) > 0 else 0.0
    cons_avg_vol = float(np.mean(cons_vols)) if len(cons_vols) > 0 else 0.0

    vol_decline_pct = 0.0
    if fp_avg_vol > 0:
        vol_decline_pct = (fp_avg_vol - cons_avg_vol) / fp_avg_vol * 100

    # ─── 4. KIRILIM EŞİĞİ HESAPLA ────────────────────────────────────────────
    # Üst trend çizgisini lineer regresyonla tahmin et
    xs = np.arange(len(cons_highs))
    coeffs = np.polyfit(xs, cons_highs, 1)
    # Bir sonraki bar için üst trend çizgisi değeri
    upper_trendline = float(np.polyval(coeffs, len(cons_highs)))

    dist_to_breakout_pct = 0.0
    if current_price > 0:
        dist_to_breakout_pct = (upper_trendline - current_price) / current_price * 100

    # ─── 5. SKOR HESAPLA ─────────────────────────────────────────────────────

    # Flagpole gücü (max 30 puan)
    if best_flagpole_gain >= 8.0:
        score += 30
        signals.append(f"🚀 Güçlü direk: +%{best_flagpole_gain:.1f} yükseliş")
    elif best_flagpole_gain >= 5.0:
        score += 22
        signals.append(f"📈 Direk: +%{best_flagpole_gain:.1f} yükseliş")
    elif best_flagpole_gain >= flagpole_min_gain_pct:
        score += 14
        signals.append(f"📈 Direk: +%{best_flagpole_gain:.1f} yükseliş")

    # Kama kalitesi (max 25 puan)
    if is_wedge:
        score += 25
        signals.append(f"📐 Kama konsolidasyonu (↑lows: {lows_slope:+.2f}%, ↓highs: {highs_slope:+.2f}%)")
    elif is_flat_cons:
        score += 10
        signals.append("➡️ Yatay konsolidasyon (zayıf flama)")

    # Hacim azalması (max 20 puan)
    if vol_decline_pct >= 40:
        score += 20
        signals.append(f"📉 Hacim kuruyor: -%{vol_decline_pct:.0f} azalma")
    elif vol_decline_pct >= 25:
        score += 12
        signals.append(f"📉 Hacim azalıyor: -%{vol_decline_pct:.0f}")
    elif vol_decline_pct >= 10:
        score += 6
        signals.append(f"↘️ Hafif hacim düşüşü: -%{vol_decline_pct:.0f}")

    # Konsolidasyon süresi (max 15 puan, ideal 5-12 bar)
    if 5 <= cons_bars <= 12:
        score += 15
        signals.append(f"⏱️ İdeal konsolidasyon süresi: {cons_bars} bar")
    elif 3 <= cons_bars <= 16:
        score += 8
        signals.append(f"⏱️ Konsolidasyon: {cons_bars} bar")

    # Kırılıma yakınlık (max 10 puan)
    if 0 <= dist_to_breakout_pct <= max_dist_to_breakout_pct:
        score += 10
        signals.append(f"🎯 Kırılım eşiğinde (%{dist_to_breakout_pct:.2f} uzakta)")
    elif dist_to_breakout_pct < 0:
        # Zaten kırdı mı?
        if dist_to_breakout_pct >= -1.0:
            score += 8
            signals.append(f"✅ Üst çizgi yeni kırıldı (%{abs(dist_to_breakout_pct):.2f} üstünde)")
        else:
            score += 3
            signals.append(f"⚠️ Kırılım gerçekleşti, geç kalınmış olabilir")

    score = min(score, 100)

    return PennantResult(
        symbol=symbol,
        timeframe=timeframe,
        score=score,
        signals=signals,
        current_price=current_price,
        flagpole_gain_pct=best_flagpole_gain,
        consolidation_bars=cons_bars,
        volume_decline_pct=vol_decline_pct,
        upper_trendline=upper_trendline,
        distance_to_breakout_pct=dist_to_breakout_pct,
        price_change_24h=price_change_24h,
    )


# ─── Telegram Formatı ─────────────────────────────────────────────────────────

def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def format_pennant_alert(result: PennantResult) -> str:
    """Telegram mesajı formatla."""
    dual = " | 🔥 *ÇİFT TF TEYİT*" if result.dual_tf_confirmed else ""
    signal_lines = "\n".join(f"  • {s}" for s in result.signals)
    score_bar = "█" * (result.score // 10) + "░" * (10 - result.score // 10)

    dist_text = (
        f"`✅ Kırıldı (+%{abs(result.distance_to_breakout_pct):.2f})`"
        if result.distance_to_breakout_pct < 0
        else f"`%{result.distance_to_breakout_pct:.2f} uzakta`"
    )

    return (
        f"🏳️ *YÜKSELİŞ FLAMASI* | *{result.symbol}* [{result.timeframe}]{dual}\n"
        f"Skor: `{result.score}/100`  `{score_bar}`\n"
        f"24s Değişim: `{result.price_change_24h:+.2f}%`\n\n"
        f"*Tespit edilen sinyaller:*\n{signal_lines}\n\n"
        f"*Teknik Özet:*\n"
        f"Fiyat: `{_fmt_price(result.current_price)}`\n"
        f"Direk Yükselişi: `+%{result.flagpole_gain_pct:.1f}`\n"
        f"Kons. Süresi: `{result.consolidation_bars} bar`\n"
        f"Hacim Azalması: `%{result.volume_decline_pct:.0f}`\n"
        f"Kırılım Hedefi: `{_fmt_price(result.upper_trendline)}`\n"
        f"Kırılıma Uzaklık: {dist_text}"
    )


def format_pennant_summary(results: list[PennantResult]) -> str:
    """Birden fazla sonuç için özet."""
    if not results:
        return "🔍 Bu turda yükselen flama tespit edilmedi."

    lines = ["🏳️ *FLAMA RADAR ÖZET*\n"]
    for r in results[:15]:
        dual = "🔥" if r.dual_tf_confirmed else ""
        bar = "█" * (r.score // 10)
        dist = f"+%{abs(r.distance_to_breakout_pct):.1f}✅" if r.distance_to_breakout_pct < 0 else f"%{r.distance_to_breakout_pct:.1f}🎯"
        lines.append(
            f"{dual}*{r.symbol}* [{r.timeframe}] `{r.score}/100` {bar}\n"
            f"  Direk: `+%{r.flagpole_gain_pct:.1f}` | Kons: `{r.consolidation_bars}bar` | "
            f"Vol↓: `%{r.volume_decline_pct:.0f}` | Kırılım: {dist}"
        )
    return "\n".join(lines)


# ─── Ana Scanner ──────────────────────────────────────────────────────────────

STABLECOIN_BASES = {
    "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "USDD",
    "SUSD", "FRAX", "GUSD", "LUSD", "MIM", "USDN", "USDJ",
    "HUSD", "USDK", "VAI", "USTC", "UST", "CUSD", "EURC",
}


def _is_valid_symbol(symbol: str) -> bool:
    if not symbol.endswith("USDT") or not symbol.isascii():
        return False
    base = symbol[:-4].upper()
    return base.isalnum() and base not in STABLECOIN_BASES


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        print(text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram Error] {e}")


def run_pennant_scan(
    base_url: str,
    token: str,
    chat_id: str,
    min_score: int = 55,
    min_volume_usd: float = 10_000_000,
    max_volume_usd: float = 5_000_000_000,
    top_limit: int = 0,
    timeframes: tuple[str, ...] = ("5m", "15m"),
) -> list[PennantResult]:
    """
    Tüm Binance Futures coinlerini tarar, yükselen flama arar.

    Args:
        base_url: Binance Futures API base URL
        token: Telegram bot token
        chat_id: Telegram chat ID
        min_score: Minimum flama skoru (0-100)
        min_volume_usd: Minimum 24s hacim filtresi
        max_volume_usd: Maksimum 24s hacim filtresi
        top_limit: Sadece en iyi N sonucu gönder (0 = hepsini)
        timeframes: Taranacak zaman dilimleri

    Returns:
        Tespit edilen PennantResult listesi
    """
    print(f"\n{'='*60}")
    print(f"🏳️  FLAMA TARAYICI BAŞLADI — {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"   Timeframes: {', '.join(timeframes)}")
    print(f"   Min Skor: {min_score} | Min Hacim: ${min_volume_usd/1e6:.0f}M")
    print(f"{'='*60}\n")

    # 1. Aktif futures sembolleri çek
    try:
        exchange_info = _fetch_json(f"{base_url}/fapi/v1/exchangeInfo", timeout=30)
    except Exception as e:
        print(f"[Error] exchangeInfo alınamadı: {e}")
        return []

    active_symbols: set[str] = set()
    for row in exchange_info.get("symbols", []):
        if row.get("status") != "TRADING":
            continue
        if row.get("contractType") != "PERPETUAL":
            continue
        if row.get("quoteAsset") != "USDT":
            continue
        sym = str(row.get("symbol", "")).upper()
        if _is_valid_symbol(sym):
            active_symbols.add(sym)

    print(f"  ✅ {len(active_symbols)} aktif futures sembolü bulundu.")

    # 2. 24s ticker ile hacim filtrele
    try:
        tickers = _fetch_json(f"{base_url}/fapi/v1/ticker/24hr", timeout=30)
    except Exception as e:
        print(f"[Error] ticker alınamadı: {e}")
        return []

    candidates = []
    for row in tickers:
        sym = str(row.get("symbol", "")).upper()
        if sym not in active_symbols:
            continue
        try:
            qvol = float(row.get("quoteVolume", 0))
            pct = float(row.get("priceChangePercent", 0))
        except (TypeError, ValueError):
            continue
        if min_volume_usd <= qvol <= max_volume_usd:
            candidates.append((sym, pct, qvol))

    # Hacme göre sırala (en yüksek hacim önce)
    candidates.sort(key=lambda x: x[2], reverse=True)
    print(f"  ✅ {len(candidates)} coin hacim filtresini geçti.\n")

    # 3. Her coin için kline çek ve pattern tara
    all_results: dict[str, list[PennantResult]] = {}  # symbol -> [results]
    found_count = 0

    for idx, (sym, pct_24h, qvol) in enumerate(candidates):
        print(f"  [{idx+1}/{len(candidates)}] {sym} taranıyor...", end="\r")

        tf_results: list[PennantResult] = []

        for tf in timeframes:
            try:
                df = _fetch_klines(base_url, sym, tf, limit=200)
                if len(df) < 40:
                    continue

                result = detect_pennant(
                    df=df,
                    symbol=sym,
                    timeframe=tf,
                    price_change_24h=pct_24h,
                )
                if result and result.score >= min_score:
                    tf_results.append(result)
                    found_count += 1

            except Exception as e:
                # Sessizce geç, her coin için hata mesajı spam olur
                pass

            time.sleep(0.05)  # Rate limit için küçük bekleme

        if tf_results:
            all_results[sym] = tf_results

            # Çift TF teyidi
            tfs_found = {r.timeframe for r in tf_results}
            if "5m" in tfs_found and "15m" in tfs_found:
                for r in tf_results:
                    r.dual_tf_confirmed = True

    print(f"\n\n  ✅ Tarama tamamlandı. {found_count} flama tespit edildi.\n")

    # 4. En iyi sonuçları toplayıp sırala
    flat_results: list[PennantResult] = []
    for sym_results in all_results.values():
        flat_results.extend(sym_results)

    # Çift TF teyitli olanlar önce, sonra skora göre
    flat_results.sort(key=lambda r: (r.dual_tf_confirmed, r.score), reverse=True)

    if top_limit > 0:
        flat_results = flat_results[:top_limit]

    # 5. Telegram'a gönder
    if flat_results:
        # Önce özet mesajı gönder
        summary_msg = format_pennant_summary(flat_results)
        print("\n" + summary_msg.replace("*", "").replace("`", ""))
        send_telegram(token, chat_id, summary_msg)
        time.sleep(1)

        # Sonra detaylı mesajlar (max 10)
        for result in flat_results[:10]:
            detail_msg = format_pennant_alert(result)
            print("\n" + detail_msg.replace("*", "").replace("`", ""))
            send_telegram(token, chat_id, detail_msg)
            time.sleep(0.5)
    else:
        msg = f"🔍 Flama taraması tamamlandı. {len(candidates)} coin incelendi, eşik üstü sonuç bulunamadı. (Min skor: {min_score})"
        print(msg)
        send_telegram(token, chat_id, msg)

    return flat_results


# ─── Sürekli Döngü Modu ───────────────────────────────────────────────────────

def run_continuous(scan_interval_minutes: int = 15):
    """Belirli aralıklarla sürekli tarama yapar."""
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    base_url = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com").strip()
    min_score = int(os.getenv("PENNANT_MIN_SCORE", "55"))
    min_vol = float(os.getenv("MIN_QUOTE_VOLUME_USD", "10000000"))
    max_vol = float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000"))
    interval_sec = scan_interval_minutes * 60

    print("🏳️  Flama Tarayıcı — Sürekli Mod")
    print(f"   Her {scan_interval_minutes} dakikada bir tarama yapılacak.")
    print(f"   Telegram: {'✅ Bağlı' if token and chat_id else '❌ Bağlı değil (konsola yazdırılacak)'}\n")

    while True:
        try:
            run_pennant_scan(
                base_url=base_url,
                token=token,
                chat_id=chat_id,
                min_score=min_score,
                min_volume_usd=min_vol,
                max_volume_usd=max_vol,
                timeframes=("5m", "15m"),
            )
        except KeyboardInterrupt:
            print("\n⛔ Tarama durduruldu.")
            break
        except Exception as e:
            print(f"\n[Error] Tarama hatası: {e}")

        print(f"\n⏳ Sonraki tarama {scan_interval_minutes} dakika sonra...\n")
        time.sleep(interval_sec)


# ─── CLI Giriş Noktası ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Yükselen Flama (Rising Pennant) Tarayıcı")
    parser.add_argument("--once", action="store_true", help="Tek seferlik tarama yap ve çık")
    parser.add_argument("--interval", type=int, default=15, help="Tarama aralığı (dk, varsayılan: 15)")
    parser.add_argument("--min-score", type=int, default=55, help="Minimum flama skoru (varsayılan: 55)")
    parser.add_argument("--timeframes", nargs="+", default=["5m", "15m"], help="Taranacak TF'ler")
    args = parser.parse_args()

    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    base_url = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com").strip()
    min_vol = float(os.getenv("MIN_QUOTE_VOLUME_USD", "10000000"))
    max_vol = float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000"))

    if args.once:
        run_pennant_scan(
            base_url=base_url,
            token=token,
            chat_id=chat_id,
            min_score=args.min_score,
            min_volume_usd=min_vol,
            max_volume_usd=max_vol,
            timeframes=tuple(args.timeframes),
        )
    else:
        run_continuous(scan_interval_minutes=args.interval)
