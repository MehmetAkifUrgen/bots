import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


TIMEFRAMES = ("15m", "1h", "4h")
STABLECOIN_BASES = {
    "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "USDD",
    "SUSD", "FRAX", "GUSD", "LUSD", "MIM", "USDN", "USDJ",
    "HUSD", "USDK", "VAI", "USTC", "UST", "CUSD", "EURC",
}


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    futures_base_url: str
    scan_every_seconds: int
    top_gainers_limit: int
    min_quote_volume_usd: float
    max_quote_volume_usd: float
    lookback_bars: int
    min_ready_confidence: int
    send_wait_setups: bool
    max_wait_setups: int
    analysis_state_file: str


@dataclass
class MarketTicker:
    symbol: str
    price_change_pct: float
    last_price: float
    quote_volume: float


@dataclass
class Setup:
    symbol: str
    decision: str
    setup_type: str
    confidence: int
    price_change_pct: float
    funding_rate_pct: float | None
    entry_low: float | None
    entry_high: float | None
    stop_loss: float | None
    target_1: float | None
    target_2: float | None
    ready: bool
    invalidation: str
    summary: str


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    load_dotenv()
    return Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        futures_base_url=os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com").strip(),
        scan_every_seconds=int(os.getenv("SCAN_EVERY_SECONDS", "120")),
        top_gainers_limit=int(os.getenv("TOP_GAINERS_LIMIT", "10")),
        min_quote_volume_usd=float(os.getenv("MIN_QUOTE_VOLUME_USD", "15000000")),
        max_quote_volume_usd=float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000")),
        lookback_bars=int(os.getenv("LOOKBACK_BARS", "260")),
        min_ready_confidence=int(os.getenv("MIN_READY_CONFIDENCE", "78")),
        send_wait_setups=parse_bool(os.getenv("SEND_WAIT_SETUPS", "true"), default=True),
        max_wait_setups=int(os.getenv("MAX_WAIT_SETUPS", "3")),
        analysis_state_file=os.getenv("ANALYSIS_STATE_FILE", "analysis_state.json").strip(),
    )


def read_state(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
    return {}


def write_state(path: str, state: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=True, indent=2)


def fetch_json(url: str, *, params: dict | None = None, timeout: int = 20) -> dict | list:
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def send_telegram(cfg: Config, text: str) -> None:
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        print(text)
        return

    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()


def symbol_base(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


def is_plain_symbol(symbol: str) -> bool:
    if not symbol.endswith("USDT") or not symbol.isascii():
        return False
    return symbol_base(symbol).isalnum()


def is_excluded_symbol(symbol: str) -> bool:
    base = symbol_base(symbol).upper()
    return (not is_plain_symbol(symbol)) or base in STABLECOIN_BASES


def fetch_active_futures_symbols(cfg: Config) -> set[str]:
    data = fetch_json(f"{cfg.futures_base_url}/fapi/v1/exchangeInfo", timeout=30)
    symbols: set[str] = set()
    for row in data.get("symbols", []):
        if row.get("status") != "TRADING":
            continue
        if row.get("contractType") != "PERPETUAL":
            continue
        if row.get("quoteAsset") != "USDT":
            continue
        symbol = str(row.get("symbol", "")).upper()
        if symbol and not is_excluded_symbol(symbol):
            symbols.add(symbol)
    return symbols


def fetch_top_gainers(cfg: Config) -> list[MarketTicker]:
    active_symbols = fetch_active_futures_symbols(cfg)
    tickers = fetch_json(f"{cfg.futures_base_url}/fapi/v1/ticker/24hr", timeout=30)
    candidates: list[MarketTicker] = []

    for row in tickers:
        symbol = str(row.get("symbol", "")).upper()
        if symbol not in active_symbols:
            continue

        try:
            price_change_pct = float(row.get("priceChangePercent", 0.0))
            last_price = float(row.get("lastPrice", 0.0))
            quote_volume = float(row.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            continue

        if price_change_pct <= 0:
            continue
        if quote_volume < cfg.min_quote_volume_usd or quote_volume > cfg.max_quote_volume_usd:
            continue
        if last_price <= 0:
            continue

        candidates.append(
            MarketTicker(
                symbol=symbol,
                price_change_pct=price_change_pct,
                last_price=last_price,
                quote_volume=quote_volume,
            )
        )

    candidates.sort(key=lambda item: (item.price_change_pct, item.quote_volume), reverse=True)
    return candidates[: cfg.top_gainers_limit]


def fetch_funding_rates(cfg: Config) -> dict[str, float]:
    raw = fetch_json(f"{cfg.futures_base_url}/fapi/v1/premiumIndex", timeout=30)
    mapping: dict[str, float] = {}
    if not isinstance(raw, list):
        return mapping

    for row in raw:
        symbol = str(row.get("symbol", "")).upper()
        try:
            mapping[symbol] = float(row.get("lastFundingRate", 0.0))
        except (TypeError, ValueError):
            continue
    return mapping


def fetch_klines(cfg: Config, symbol: str, interval: str) -> pd.DataFrame:
    raw = fetch_json(
        f"{cfg.futures_base_url}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": cfg.lookback_bars},
        timeout=30,
    )
    columns = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "number_of_trades",
        "taker_buy_base_asset_volume",
        "taker_buy_quote_asset_volume",
        "ignore",
    ]
    df = pd.DataFrame(raw, columns=columns)
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()
    pos_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    neg_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    alpha = 1 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    pos_di = 100 * pos_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)
    neg_di = 100 * neg_dm.ewm(alpha=alpha, adjust=False).mean() / atr.replace(0, np.nan)
    dx = (100 * (pos_di - neg_di).abs() / (pos_di + neg_di).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=alpha, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()
    out["rsi14"] = compute_rsi(out["close"])
    out["atr14"] = compute_atr(out)
    out["adx14"] = compute_adx(out)
    out["vol_ma20"] = out["volume"].rolling(20).mean()

    ema_fast = out["close"].ewm(span=12, adjust=False).mean()
    ema_slow = out["close"].ewm(span=26, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    out["macd_signal"] = macd_line.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = macd_line - out["macd_signal"]
    return out


def safe_float(value: float | int | np.floating) -> float:
    result = float(value)
    if math.isnan(result) or math.isinf(result):
        return 0.0
    return result


def trend_up(row: pd.Series) -> bool:
    close = safe_float(row["close"])
    ema20 = safe_float(row["ema20"])
    ema50 = safe_float(row["ema50"])
    ema200 = safe_float(row["ema200"])
    return close > ema20 > ema50 > ema200


def trend_down(row: pd.Series) -> bool:
    close = safe_float(row["close"])
    ema20 = safe_float(row["ema20"])
    ema50 = safe_float(row["ema50"])
    ema200 = safe_float(row["ema200"])
    return close < ema20 < ema50 < ema200


def build_setup(market: MarketTicker, frames: dict[str, pd.DataFrame], funding_rate: float | None) -> Setup:
    tf_15 = frames["15m"]
    tf_1h = frames["1h"]
    tf_4h = frames["4h"]

    row_15 = tf_15.iloc[-2]
    row_15_prev = tf_15.iloc[-3]
    row_1h = tf_1h.iloc[-2]
    row_4h = tf_4h.iloc[-2]
    recent_15 = tf_15.iloc[-22:-2]

    close_15 = safe_float(row_15["close"])
    ema20_15 = safe_float(row_15["ema20"])
    ema50_15 = safe_float(row_15["ema50"])
    atr_15 = max(safe_float(row_15["atr14"]), close_15 * 0.003)
    rsi_15 = safe_float(row_15["rsi14"])
    adx_15 = safe_float(row_15["adx14"])
    macd_15 = safe_float(row_15["macd_hist"])
    macd_15_prev = safe_float(row_15_prev["macd_hist"])
    volume_15 = safe_float(row_15["volume"])
    vol_ma_15 = safe_float(row_15["vol_ma20"])

    rsi_1h = safe_float(row_1h["rsi14"])
    adx_1h = safe_float(row_1h["adx14"])
    macd_1h = safe_float(row_1h["macd_hist"])

    swing_low_15 = safe_float(recent_15["low"].min())
    swing_high_15 = safe_float(recent_15["high"].max())
    dist_from_ema20 = (close_15 - ema20_15) / atr_15 if atr_15 > 0 else 0.0
    funding_pct = funding_rate * 100 if funding_rate is not None else None

    long_conditions = [
        trend_up(row_4h),
        trend_up(row_1h),
        close_15 >= ema20_15,
        50 <= rsi_1h <= 68,
        adx_1h >= 18,
        macd_1h > 0,
        volume_15 >= vol_ma_15,
        0.0 <= dist_from_ema20 <= 0.9,
        funding_rate is None or funding_rate <= 0.0008,
    ]
    long_score = int(round(100 * sum(long_conditions) / len(long_conditions)))

    trend_short_conditions = [
        trend_down(row_4h),
        trend_down(row_1h),
        close_15 <= ema20_15,
        32 <= rsi_1h <= 50,
        adx_1h >= 18,
        macd_1h < 0,
        volume_15 >= vol_ma_15,
        -0.9 <= dist_from_ema20 <= 0.1,
    ]
    trend_short_score = int(round(100 * sum(trend_short_conditions) / len(trend_short_conditions)))

    exhaustion_short_conditions = [
        market.price_change_pct >= 10,
        rsi_1h >= 72,
        rsi_15 >= 68,
        dist_from_ema20 >= 1.25,
        macd_15 < macd_15_prev,
        close_15 < safe_float(row_15_prev["close"]),
        funding_rate is None or funding_rate >= 0.0005,
    ]
    exhaustion_short_score = int(round(100 * sum(exhaustion_short_conditions) / len(exhaustion_short_conditions)))

    if long_score >= 78:
        entry_low = min(close_15, ema20_15)
        entry_high = max(close_15, ema20_15 + 0.25 * atr_15)
        stop_loss = min(swing_low_15, ema50_15) - 0.6 * atr_15
        if stop_loss >= entry_low:
            stop_loss = entry_low - atr_15
        entry_mid = (entry_low + entry_high) / 2
        risk = max(entry_mid - stop_loss, atr_15 * 0.8)
        target_1 = entry_mid + 1.5 * risk
        target_2 = entry_mid + 2.5 * risk
        return Setup(
            symbol=market.symbol,
            decision="LONG",
            setup_type="trend continuation",
            confidence=long_score,
            price_change_pct=market.price_change_pct,
            funding_rate_pct=funding_pct,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            ready=True,
            invalidation="15m kapanis EMA20 altina inerse setup zayiflar.",
            summary="4h ve 1h trend yukari, 15m tarafinda geri cekilme sonrasi devam setup'i var.",
        )

    if trend_short_score >= 78:
        entry_low = min(close_15, ema20_15)
        entry_high = max(close_15, ema20_15 + 0.25 * atr_15)
        stop_loss = max(swing_high_15, ema50_15) + 0.6 * atr_15
        if stop_loss <= entry_high:
            stop_loss = entry_high + atr_15
        entry_mid = (entry_low + entry_high) / 2
        risk = max(stop_loss - entry_mid, atr_15 * 0.8)
        target_1 = entry_mid - 1.5 * risk
        target_2 = entry_mid - 2.5 * risk
        return Setup(
            symbol=market.symbol,
            decision="SHORT",
            setup_type="trend continuation",
            confidence=trend_short_score,
            price_change_pct=market.price_change_pct,
            funding_rate_pct=funding_pct,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            ready=True,
            invalidation="15m kapanis EMA20 ustune geri cikarsa setup bozulur.",
            summary="Yukselenler icinde olsa da ust zaman dilimlerinde zayiflik var ve trend short setup'i olusmus.",
        )

    if exhaustion_short_score >= 72:
        entry_low = close_15
        entry_high = close_15 + 0.45 * atr_15
        stop_loss = max(swing_high_15, entry_high + 0.8 * atr_15)
        entry_mid = (entry_low + entry_high) / 2
        risk = max(stop_loss - entry_mid, atr_15 * 0.8)
        target_1 = entry_mid - 1.2 * risk
        target_2 = entry_mid - 2.0 * risk
        return Setup(
            symbol=market.symbol,
            decision="SHORT",
            setup_type="exhaustion fade",
            confidence=exhaustion_short_score,
            price_change_pct=market.price_change_pct,
            funding_rate_pct=funding_pct,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            target_1=target_1,
            target_2=target_2,
            ready=True,
            invalidation="Yeni 15m zirve gelirse counter-trend short iptal edilir.",
            summary="Coin cok sisli, RSI yuksek ve momentum zayifliyor; sadece hizli short denemesi olarak dusun.",
        )

    wait_reasons: list[str] = []
    if not trend_up(row_4h):
        wait_reasons.append("4h trend tam net degil")
    if dist_from_ema20 > 1.0:
        wait_reasons.append("fiyat 15m EMA20'den cok uzak")
    if adx_15 < 16:
        wait_reasons.append("15m trend gucu zayif")
    if volume_15 < vol_ma_15:
        wait_reasons.append("hacim teyidi zayif")
    if not wait_reasons:
        wait_reasons.append("setup kosullari eksik")

    return Setup(
        symbol=market.symbol,
        decision="WAIT",
        setup_type="no clean setup",
        confidence=max(long_score, trend_short_score, exhaustion_short_score),
        price_change_pct=market.price_change_pct,
        funding_rate_pct=funding_pct,
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        target_1=None,
        target_2=None,
        ready=False,
        invalidation="Temiz retest veya yon teyidi gelmeden islem yok.",
        summary="; ".join(wait_reasons),
    )


def format_price(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def format_setup_line(setup: Setup) -> str:
    funding_text = "?" if setup.funding_rate_pct is None else f"{setup.funding_rate_pct:+.4f}%"
    if setup.decision == "WAIT":
        return (
            f"*{setup.symbol}* | {setup.price_change_pct:+.2f}% | *WAIT* | Guven `{setup.confidence}`\n"
            f"Sebep: {setup.summary}\n"
            f"Funding: `{funding_text}`"
        )

    return (
        f"*{setup.symbol}* | {setup.price_change_pct:+.2f}% | *{setup.decision}* `{setup.confidence}` | `READY`\n"
        f"Tip: `{setup.setup_type}` | Funding: `{funding_text}`\n"
        f"Entry: `{format_price(setup.entry_low)}` - `{format_price(setup.entry_high)}`\n"
        f"SL: `{format_price(setup.stop_loss)}` | TP1: `{format_price(setup.target_1)}` | TP2: `{format_price(setup.target_2)}`\n"
        f"Not: {setup.summary}\n"
        f"Iptal: {setup.invalidation}"
    )


def build_report(setups: list[Setup], cfg: Config) -> str:
    now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ready_setups = [setup for setup in setups if setup.ready and setup.confidence >= cfg.min_ready_confidence]
    watchlist = [setup for setup in setups if not setup.ready or setup.confidence < cfg.min_ready_confidence]
    sections = [
        "*Binance Futures Top Gainers Analizi*",
        f"Zaman: `{now_text}`",
        f"Toplam coin: `{len(setups)}` | Hazir setup: `{len(ready_setups)}` | Esik: `{cfg.min_ready_confidence}`",
        "",
    ]

    if ready_setups:
        sections.append("*Isleme Uygunlar*")
        sections.extend(format_setup_line(setup) for setup in ready_setups)
    else:
        sections.append("*Isleme Uygunlar*")
        sections.append("Bu turda esigin ustunde temiz setup cikmadi.")

    if cfg.send_wait_setups and watchlist:
        sections.append("*Izleme Listesi*")
        sections.extend(format_setup_line(setup) for setup in watchlist[: cfg.max_wait_setups])

    return "\n\n".join(sections)


def build_report_hash(report_text: str) -> str:
    return hashlib.sha256(report_text.encode("utf-8")).hexdigest()


def analyze_market(cfg: Config) -> list[Setup]:
    top_gainers = fetch_top_gainers(cfg)
    if not top_gainers:
        raise RuntimeError("Filtrelere uyan futures sembolu bulunamadi.")

    funding_map = fetch_funding_rates(cfg)
    print(
        "Top gainers: "
        + ", ".join(f"{item.symbol}({item.price_change_pct:+.2f}%)" for item in top_gainers)
    )

    setups: list[Setup] = []
    for market in top_gainers:
        frames: dict[str, pd.DataFrame] = {}
        for interval in TIMEFRAMES:
            df = fetch_klines(cfg, market.symbol, interval)
            if len(df) < 220:
                raise RuntimeError(f"{market.symbol} {interval} icin yetersiz mum verisi geldi.")
            frames[interval] = add_indicators(df)

        setups.append(build_setup(market, frames, funding_map.get(market.symbol)))

    setups.sort(key=lambda item: (not item.ready, -item.confidence, -item.price_change_pct))
    return setups


def main() -> None:
    cfg = load_config()
    state = read_state(cfg.analysis_state_file)
    last_hash = str(state.get("last_report_hash", ""))

    print(
        f"Basladi | Scan every: {cfg.scan_every_seconds}s | Top limit: {cfg.top_gainers_limit} | "
        f"Volume filter: {cfg.min_quote_volume_usd:.0f}-{cfg.max_quote_volume_usd:.0f} | "
        f"Ready threshold: {cfg.min_ready_confidence}"
    )

    while True:
        try:
            setups = analyze_market(cfg)
            report_text = build_report(setups, cfg)
            report_hash = build_report_hash(report_text)

            if report_hash != last_hash:
                send_telegram(cfg, report_text)
                last_hash = report_hash
                write_state(
                    cfg.analysis_state_file,
                    {
                        "last_report_hash": last_hash,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                print("Telegram'a rapor gonderildi.")
            else:
                print("Rapor degismedi, mesaj gonderilmedi.")
        except Exception as exc:
            print(f"[HATA] {datetime.now(timezone.utc).isoformat()} | {exc}")

        time.sleep(cfg.scan_every_seconds)


if __name__ == "__main__":
    main()
