import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from pump_scanner import (
    detect_pump_candidate,
    format_pump_alert,
    format_pump_summary,
)


TIMEFRAMES = ("15m", "1h", "4h")
STABLECOIN_BASES = {
    "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "USDD",
    "SUSD", "FRAX", "GUSD", "LUSD", "MIM", "USDN", "USDJ",
    "HUSD", "USDK", "VAI", "USTC", "UST", "CUSD", "EURC",
}


def retry_on_failure(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    Decorator: HTTP isteklerinde basarisizlikta tekrar dene.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.RequestException, Exception) as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        print(f"  [Retry {attempt+1}/{max_retries}] {func.__name__}: {e}")
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        raise last_exception
        return wrapper
    return decorator


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    futures_base_url: str
    scan_every_seconds: int
    min_scan_interval_seconds: int
    top_gainers_limit: int
    min_quote_volume_usd: float
    max_quote_volume_usd: float
    lookback_bars: int
    min_ready_confidence: int
    send_wait_setups: bool
    max_wait_setups: int
    analysis_state_file: str
    analysis_log_file: str
    paper_trades_file: str
    max_position_hold_hours: int
    position_size_usd: float
    position_size_pct: float
    starting_balance_usd: float
    max_risk_pct: float
    trailing_stop_atr_mult: float
    max_drawdown_pct: float
    max_open_positions: int
    commission_pct: float
    slippage_pct: float
    max_position_age_hours: int
    # Pump radar
    pump_scan_enabled: bool
    pump_min_score: int
    pump_cooldown_minutes: int
    pump_send_summary: bool


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
    strategy_key: str
    confidence: int
    price_change_pct: float
    funding_rate_pct: float | None
    open_interest: float | None
    oi_trend: str | None
    oi_strength: int | None
    entry_low: float | None
    entry_high: float | None
    stop_loss: float | None
    target_1: float | None
    target_2: float | None
    ready: bool
    invalidation: str
    summary: str


@dataclass
class StrategyStats:
    strategy_key: str
    closed_trades: int
    win_rate: float
    avg_pnl_net: float
    score_bonus: float


@dataclass
class ActivePosition:
    symbol: str
    side: str
    setup_type: str
    strategy_key: str
    confidence: int
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    opened_at: str
    max_hold_until: str | None
    price_change_pct: float
    highest_price: float
    lowest_price: float
    trailing_stop: float | None


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    load_dotenv()
    requested_scan_seconds = int(os.getenv("SCAN_EVERY_SECONDS", "900"))
    min_scan_interval_seconds = int(os.getenv("MIN_SCAN_INTERVAL_SECONDS", "900"))
    return Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        futures_base_url=os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com").strip(),
        scan_every_seconds=max(requested_scan_seconds, min_scan_interval_seconds),
        min_scan_interval_seconds=min_scan_interval_seconds,
        top_gainers_limit=int(os.getenv("TOP_GAINERS_LIMIT", "0")),
        min_quote_volume_usd=float(os.getenv("MIN_QUOTE_VOLUME_USD", "15000000")),
        max_quote_volume_usd=float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000")),
        lookback_bars=int(os.getenv("LOOKBACK_BARS", "260")),
        min_ready_confidence=int(os.getenv("MIN_READY_CONFIDENCE", "82")),
        send_wait_setups=parse_bool(os.getenv("SEND_WAIT_SETUPS", "false"), default=False),
        max_wait_setups=int(os.getenv("MAX_WAIT_SETUPS", "3")),
        analysis_state_file=os.getenv("ANALYSIS_STATE_FILE", "analysis_state.json").strip(),
        analysis_log_file=os.getenv("ANALYSIS_LOG_FILE", "analysis_log.csv").strip(),
        paper_trades_file=os.getenv("PAPER_TRADES_FILE", "paper_trades.csv").strip(),
        max_position_hold_hours=int(os.getenv("MAX_POSITION_HOLD_HOURS", "0")),
        position_size_usd=float(os.getenv("POSITION_SIZE_USD", "100")),
        position_size_pct=float(os.getenv("POSITION_SIZE_PCT", "2")),
        starting_balance_usd=float(os.getenv("STARTING_BALANCE_USD", "1000")),
        max_risk_pct=float(os.getenv("MAX_RISK_PCT", "5")),
        trailing_stop_atr_mult=float(os.getenv("TRAILING_STOP_ATR_MULT", "1.5")),
        max_drawdown_pct=float(os.getenv("MAX_DRAWDOWN_PCT", "20")),
        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "3")),
        commission_pct=float(os.getenv("COMMISSION_PCT", "0.08")),
        slippage_pct=float(os.getenv("SLIPPAGE_PCT", "0.05")),
        max_position_age_hours=int(os.getenv("MAX_POSITION_AGE_HOURS", "4")),
        # Pump radar
        pump_scan_enabled=parse_bool(os.getenv("PUMP_SCAN_ENABLED", "true"), default=True),
        pump_min_score=int(os.getenv("PUMP_MIN_SCORE", "60")),
        pump_cooldown_minutes=int(os.getenv("PUMP_COOLDOWN_MINUTES", "60")),
        pump_send_summary=parse_bool(os.getenv("PUMP_SEND_SUMMARY", "false"), default=False),
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@retry_on_failure(max_retries=3, delay=1.0, backoff=2.0)
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


def build_strategy_key(decision: str, setup_type: str) -> str:
    return f"{decision.lower()}::{setup_type.lower()}"


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


def fetch_market_candidates(cfg: Config) -> list[MarketTicker]:
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

    candidates.sort(key=lambda item: (abs(item.price_change_pct), item.quote_volume), reverse=True)
    if cfg.top_gainers_limit > 0:
        return candidates[: cfg.top_gainers_limit]
    return candidates


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


def fetch_open_interest(cfg: Config, symbol: str) -> dict | None:
    """
    Open Interest verisini cek.
    Returns: {"openInterest": float, "symbol": str, "time": int}
    """
    try:
        raw = fetch_json(
            f"{cfg.futures_base_url}/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=20,
        )
        return {
            "symbol": str(raw.get("symbol", symbol)),
            "openInterest": float(raw.get("openInterest", 0)),
            "time": int(raw.get("time", 0)),
        }
    except Exception as e:
        print(f"  [OI Warning] {symbol}: {e}")
        return None


def fetch_open_interest_history(
    cfg: Config,
    symbol: str,
    period: str = "5m",
    limit: int = 100,
) -> pd.DataFrame:
    """
    Open Interest gecmis verilerini cek (Binance'de sinirli desteklenir).
    Alternatif olarak funding rate ve OI change oranlarini kullanabiliriz.
    """
    try:
        # Binance'de OI history endpoint'i yok, ama global configs OI değişimini takip edebiliriz
        # Şimdilik mevcut OI'yi dönelim
        current_oi = fetch_open_interest(cfg, symbol)
        if current_oi:
            return pd.DataFrame([current_oi])
        return pd.DataFrame()
    except Exception as e:
        print(f"  [OI History Warning] {symbol}: {e}")
        return pd.DataFrame()


def analyze_open_interest_trend(
    current_oi: float,
    historical_oi: pd.DataFrame,
    price_change_pct: float,
) -> tuple[str, float]:
    """
    Open Interest trendini analiz et.
    Returns: (trend_direction, strength)
    - "INCREASING": Yeni para girisi, trend guclu
    - "DECREASING": Para cikisi, trend zayifliyor
    - "STABLE": Degisim yok
    """
    if current_oi <= 0 or historical_oi.empty:
        return ("UNKNOWN", 0.0)

    # OI change hesaplama
    if len(historical_oi) >= 2:
        prev_oi = float(historical_oi.iloc[-2].get("openInterest", current_oi))
        oi_change_pct = ((current_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0
    else:
        oi_change_pct = 0.0

    # Yorumla
    if oi_change_pct > 2:
        trend = "INCREASING"
        strength = min(abs(oi_change_pct) / 10, 1.0)  # Normalize to 0-1
    elif oi_change_pct < -2:
        trend = "DECREASING"
        strength = min(abs(oi_change_pct) / 10, 1.0)
    else:
        trend = "STABLE"
        strength = 0.5

    # Price-OI divergence kontrolu
    divergence = False
    if price_change_pct > 2 and trend == "DECREASING":
        divergence = True  # Fiyat yukseliyor ama OI dusuyor - zayif trend
    elif price_change_pct < -2 and trend == "INCREASING":
        divergence = True  # Fiyat dusuyor ama OI artiyor - short build-up

    if divergence:
        strength *= 0.5  # Divergence durumunda gucu azalt

    return (trend, round(strength * 100))


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


def detect_market_regime(df: pd.DataFrame) -> tuple[str, float]:
    """
    Piyasa kosullarini tespit et: TRENDING_UP, TRENDING_DOWN, veya RANGING
    Returns: (regime, strength) where strength is 0-100
    """
    if len(df) < 50:
        return ("RANGING", 0.0)

    last_row = df.iloc[-1]
    close = safe_float(last_row["close"])
    ema20 = safe_float(last_row["ema20"])
    ema50 = safe_float(last_row["ema50"])
    ema200 = safe_float(last_row["ema200"])
    adx = safe_float(last_row["adx14"])
    atr = safe_float(last_row["atr14"])

    # EMA alignment scoring
    ema_score = 0
    if close > ema20 > ema50 > ema200:
        ema_score = 100  # Strong uptrend
    elif close < ema20 < ema50 < ema200:
        ema_score = -100  # Strong downtrend
    elif close > ema20 and ema20 > ema50:
        ema_score = 50  # Weak uptrend
    elif close < ema20 and ema20 < ema50:
        ema_score = -50  # Weak downtrend
    else:
        ema_score = 0  # Mixed/ranging

    # ADX tells trend strength
    adx_strength = min(adx / 50, 1.0)  # Normalize to 0-1

    # ATR relative to price (volatility)
    atr_pct = (atr / close * 100) if close > 0 else 0
    volatility_score = min(atr_pct / 2, 1.0)  # Normalize, cap at 2%

    # Final regime determination
    if abs(ema_score) >= 50 and adx >= 20:
        if ema_score > 0:
            regime = "TRENDING_UP"
            strength = min((abs(ema_score) * 0.5 + adx * 30 + volatility_score * 20) / 100, 1.0)
        else:
            regime = "TRENDING_DOWN"
            strength = min((abs(ema_score) * 0.5 + adx * 30 + volatility_score * 20) / 100, 1.0)
    else:
        regime = "RANGING"
        strength = 1.0 - adx_strength  # High ADX means NOT ranging

    return (regime, round(strength * 100))


def update_trailing_stop(
    position: ActivePosition,
    current_price: float,
    atr: float,
    trailing_mult: float,
) -> ActivePosition:
    """Trailing stop'u guncelle - kararti pozisyon lehine ilerlet."""
    if position.side == "LONG":
        # Update highest price seen
        new_highest = max(position.highest_price, current_price)
        
        # Calculate new trailing stop
        if position.trailing_stop is not None:
            new_trailing = new_highest - (trailing_mult * atr)
            # Only move stop up, never down
            updated_trailing_stop = max(position.trailing_stop, new_trailing)
        else:
            updated_trailing_stop = new_highest - (trailing_mult * atr)

        return ActivePosition(
            symbol=position.symbol,
            side=position.side,
            setup_type=position.setup_type,
            strategy_key=position.strategy_key,
            confidence=position.confidence,
            entry_price=position.entry_price,
            stop_loss=max(position.stop_loss, updated_trailing_stop),  # Use the higher stop
            take_profit=position.take_profit,
            quantity=position.quantity,
            opened_at=position.opened_at,
            max_hold_until=position.max_hold_until,
            price_change_pct=position.price_change_pct,
            highest_price=new_highest,
            lowest_price=position.lowest_price,
            trailing_stop=updated_trailing_stop,
        )
    else:  # SHORT
        # Update lowest price seen
        new_lowest = min(position.lowest_price, current_price)
        
        # Calculate new trailing stop
        if position.trailing_stop is not None:
            new_trailing = new_lowest + (trailing_mult * atr)
            # Only move stop down, never up
            updated_trailing_stop = min(position.trailing_stop, new_trailing)
        else:
            updated_trailing_stop = new_lowest + (trailing_mult * atr)

        return ActivePosition(
            symbol=position.symbol,
            side=position.side,
            setup_type=position.setup_type,
            strategy_key=position.strategy_key,
            confidence=position.confidence,
            entry_price=position.entry_price,
            stop_loss=min(position.stop_loss, updated_trailing_stop),  # Use the lower stop
            take_profit=position.take_profit,
            quantity=position.quantity,
            opened_at=position.opened_at,
            max_hold_until=position.max_hold_until,
            price_change_pct=position.price_change_pct,
            highest_price=position.highest_price,
            lowest_price=new_lowest,
            trailing_stop=updated_trailing_stop,
        )


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


def create_trade_setup(
    market: MarketTicker,
    decision: str,
    setup_type: str,
    confidence: int,
    entry_price: float,
    stop_loss: float,
    target_1: float,
    target_2: float,
    summary: str,
    invalidation: str,
    funding_pct: float | None = None,
    open_interest: float | None = None,
    oi_trend: str | None = None,
    oi_strength: int | None = None,
) -> Setup:
    return Setup(
        symbol=market.symbol,
        decision=decision,
        setup_type=setup_type,
        strategy_key=build_strategy_key(decision, setup_type),
        confidence=confidence,
        price_change_pct=market.price_change_pct,
        funding_rate_pct=funding_pct,
        open_interest=open_interest,
        oi_trend=oi_trend,
        oi_strength=oi_strength,
        entry_low=entry_price,  # Simplified to single point for cleaner logic
        entry_high=entry_price * 1.0005,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        ready=True,
        invalidation=invalidation,
        summary=summary,
    )


def build_candidate_setups(
    market: MarketTicker,
    frames: dict[str, pd.DataFrame],
    funding_rate: float | None,
    open_interest: float | None = None,
    oi_trend: str | None = None,
    oi_strength: int | None = None,
) -> list[Setup]:
    tf_15 = frames["15m"]
    tf_1h = frames["1h"]
    tf_4h = frames["4h"]

    row_15 = tf_15.iloc[-2]
    row_1h = tf_1h.iloc[-2]
    row_4h = tf_4h.iloc[-2]
    recent_15 = tf_15.iloc[-22:-2]

    close_15 = safe_float(row_15["close"])
    ema20_15 = safe_float(row_15["ema20"])
    ema50_15 = safe_float(row_15["ema50"])
    atr_15 = max(safe_float(row_15["atr14"]), close_15 * 0.003)
    rsi_15 = safe_float(row_15["rsi14"])
    adx_15 = safe_float(row_15["adx14"])
    vol_ma_15 = safe_float(row_15["vol_ma20"])
    volume_15 = safe_float(row_15["volume"])

    swing_low_15 = safe_float(recent_15["low"].min())
    swing_high_15 = safe_float(recent_15["high"].max())
    funding_pct = funding_rate * 100 if funding_rate is not None else None
    
    candidates: list[Setup] = []

    # 1. A+ TREND PULLBACK (Güvenli İşlem)
    # BTC trend kontrolü analyze_market içinde yapılıyor, burada teknik uyum bakıyoruz
    is_bullish = trend_up(row_4h) and trend_up(row_1h)
    is_bearish = trend_down(row_4h) and trend_down(row_1h)

    # Pullback kontrolü: 15m EMA20 retest
    dist_ema20 = (close_15 - ema20_15) / atr_15 if atr_15 > 0 else 0.0
    
    if is_bullish and 0.0 <= dist_ema20 <= 0.6:
        # Long Setup
        stop_loss = min(swing_low_15, ema50_15) - 0.5 * atr_15
        risk = close_15 - stop_loss
        if risk > 0:
            target_1 = close_15 + 1.5 * risk
            target_2 = close_15 + 2.5 * risk
            candidates.append(create_trade_setup(
                market, "LONG", "A+ Trend Pullback", 85, close_15, stop_loss, target_1, target_2,
                "4h/1h trend yukari, 15m EMA20 retest basarili.", "15m kapanis EMA50 altina inerse.",
                funding_pct, open_interest, oi_trend, oi_strength
            ))

    if is_bearish and -0.6 <= dist_ema20 <= 0.0:
        # Short Setup
        stop_loss = max(swing_high_15, ema50_15) + 0.5 * atr_15
        risk = stop_loss - close_15
        if risk > 0:
            target_1 = close_15 - 1.5 * risk
            target_2 = close_15 - 2.5 * risk
            candidates.append(create_trade_setup(
                market, "SHORT", "A+ Trend Pullback", 85, close_15, stop_loss, target_1, target_2,
                "4h/1h trend asagi, 15m EMA20 retest basarili.", "15m kapanis EMA50 ustune cikarsa.",
                funding_pct, open_interest, oi_trend, oi_strength
            ))

    # 2. S-TIER PUMP BREAKOUT (Hacimli Patlama)
    pump_candidate = detect_pump_candidate(market.symbol, market.price_change_pct, tf_15, tf_1h, tf_4h, funding_rate, open_interest)
    if pump_candidate and pump_candidate.score >= 78:
        stop_loss = close_15 - 0.8 * atr_15
        risk = close_15 - stop_loss
        if risk > 0:
            target_1 = close_15 + 1.8 * risk
            target_2 = close_15 + 3.0 * risk
            signals_text = ", ".join(pump_candidate.signals[:2])
            candidates.append(create_trade_setup(
                market, "LONG", "S-Tier Pump Breakout", pump_candidate.score, close_15, stop_loss, target_1, target_2,
                f"PUMP SINYALI (Skor {pump_candidate.score}): {signals_text}", "Hacim duserse veya ivme tersine donerse.",
                funding_pct, open_interest, oi_trend, oi_strength
            ))

    # 3. FAST MOMENTUM (Hizli Yakalama)
    # Son 3 bar hacim ve fiyat ivmesi
    recent_bars = tf_15.iloc[-4:-2]
    vol_spike = volume_15 / vol_ma_15 if vol_ma_15 > 0 else 1.0
    price_momentum = (close_15 - safe_float(recent_bars.iloc[0]["close"])) / safe_float(recent_bars.iloc[0]["close"]) * 100 if safe_float(recent_bars.iloc[0]["close"]) > 0 else 0
    
    if vol_spike > 2.0 and abs(price_momentum) > 1.2:
        side = "LONG" if price_momentum > 0 else "SHORT"
        if (side == "LONG" and trend_up(row_1h)) or (side == "SHORT" and trend_down(row_1h)):
            sl_dist = 0.9 * atr_15
            stop_loss = close_15 - sl_dist if side == "LONG" else close_15 + sl_dist
            target_1 = close_15 + 1.2 * sl_dist if side == "LONG" else close_15 - 1.2 * sl_dist
            target_2 = close_15 + 2.0 * sl_dist if side == "LONG" else close_15 - 2.0 * sl_dist
            candidates.append(create_trade_setup(
                market, side, "Fast Momentum", 82, close_15, stop_loss, target_1, target_2,
                f"HIZLI MOMENTUM: {vol_spike:.1f}x hacim artisi ve %{price_momentum:.1f} ivme.", "Ivme kaybolursa.",
                funding_pct, open_interest, oi_trend, oi_strength
            ))

    if candidates:
        return candidates

    return [
        create_trade_setup(
            market, "WAIT", "no clean setup", 0, close_15, 0, 0, 0,
            "Temiz setup yok, pusuya devam.", "Retest beklenebilir.",
            funding_pct, open_interest, oi_trend, oi_strength
        )
    ]

    if candidates:
        return candidates

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

    return [
        Setup(
            symbol=market.symbol,
            decision="WAIT",
            setup_type="no clean setup",
            strategy_key=build_strategy_key("WAIT", "no clean setup"),
            confidence=max(long_score, trend_short_score, exhaustion_short_score, scalp_long_score, scalp_short_score, fast_long_score, fast_short_score),
            price_change_pct=market.price_change_pct,
            funding_rate_pct=funding_pct,
            open_interest=open_interest,
            oi_trend=oi_trend,
            oi_strength=oi_strength,
            entry_low=None,
            entry_high=None,
            stop_loss=None,
            target_1=None,
            target_2=None,
            ready=False,
            invalidation="Temiz retest veya yon teyidi gelmeden islem yok.",
            summary="; ".join(wait_reasons),
        )
    ]


def load_trade_history(path: str) -> pd.DataFrame:
    columns = [
        "symbol",
        "side",
        "setup_type",
        "strategy_key",
        "entry",
        "exit",
        "stop_loss",
        "take_profit",
        "quantity",
        "opened_at",
        "closed_at",
        "exit_reason",
        "pnl_net",
        "balance_after",
        "confidence",
    ]
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame(columns=columns)
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame(columns=columns)
    for column in columns:
        if column not in df.columns:
            df[column] = np.nan
    return df[columns]


def compute_strategy_stats(path: str) -> dict[str, StrategyStats]:
    trade_history = load_trade_history(path)
    if trade_history.empty:
        return {}

    trade_history["pnl_net"] = pd.to_numeric(trade_history["pnl_net"], errors="coerce")
    if trade_history["strategy_key"].isna().all():
        trade_history["strategy_key"] = trade_history.apply(
            lambda row: build_strategy_key(str(row.get("side", "WAIT")), str(row.get("setup_type", "unknown"))),
            axis=1,
        )

    closed_trades = trade_history.dropna(subset=["closed_at", "pnl_net", "strategy_key"])
    if closed_trades.empty:
        return {}

    stats: dict[str, StrategyStats] = {}
    for strategy_key, group in closed_trades.groupby("strategy_key"):
        closed_count = len(group)
        win_rate = float((group["pnl_net"] > 0).mean())
        avg_pnl_net = float(group["pnl_net"].mean())
        sample_factor = min(closed_count, 10) / 10
        bounded_pnl = max(min(avg_pnl_net, 15.0), -15.0)
        score_bonus = sample_factor * (((win_rate - 0.5) * 24) + bounded_pnl)
        stats[str(strategy_key)] = StrategyStats(
            strategy_key=str(strategy_key),
            closed_trades=closed_count,
            win_rate=win_rate,
            avg_pnl_net=avg_pnl_net,
            score_bonus=score_bonus,
        )
    return stats


def compute_trade_summary(path: str) -> dict[str, float]:
    trade_history = load_trade_history(path)
    if trade_history.empty:
        return {"closed_trades": 0.0, "win_rate": 0.0, "avg_pnl_net": 0.0}

    trade_history["pnl_net"] = pd.to_numeric(trade_history["pnl_net"], errors="coerce")
    closed_trades = trade_history.dropna(subset=["closed_at", "pnl_net"])
    if closed_trades.empty:
        return {"closed_trades": 0.0, "win_rate": 0.0, "avg_pnl_net": 0.0}

    return {
        "closed_trades": float(len(closed_trades)),
        "win_rate": float((closed_trades["pnl_net"] > 0).mean()),
        "avg_pnl_net": float(closed_trades["pnl_net"].mean()),
    }


def score_setup(setup: Setup, strategy_stats: dict[str, StrategyStats]) -> float:
    stats = strategy_stats.get(setup.strategy_key)
    bonus = stats.score_bonus if stats else 0.0
    return float(setup.confidence) + bonus


def select_best_ready_setup(
    setups: list[Setup],
    min_ready_confidence: int,
    strategy_stats: dict[str, StrategyStats],
) -> Setup | None:
    ready_setups = [setup for setup in setups if setup.ready and setup.confidence >= min_ready_confidence]
    if not ready_setups:
        return None
    return max(
        ready_setups,
        key=lambda setup: (
            score_setup(setup, strategy_stats),
            setup.confidence,
            setup.price_change_pct,
        ),
    )


def setup_entry_price(setup: Setup) -> float:
    if setup.entry_low is not None and setup.entry_high is not None:
        return (setup.entry_low + setup.entry_high) / 2
    if setup.entry_high is not None:
        return setup.entry_high
    if setup.entry_low is not None:
        return setup.entry_low
    raise ValueError(f"{setup.symbol} icin entry price hesaplanamadi.")


def load_active_positions(state: dict) -> list[ActivePosition]:
    raw_list = state.get("active_positions")
    if not isinstance(raw_list, list):
        # Backward compatibility: migrate old single position format
        old_position = state.get("active_position")
        if isinstance(old_position, dict):
            try:
                return [ActivePosition(
                    symbol=str(old_position["symbol"]),
                    side=str(old_position["side"]),
                    setup_type=str(old_position["setup_type"]),
                    strategy_key=str(old_position["strategy_key"]),
                    confidence=int(old_position["confidence"]),
                    entry_price=float(old_position["entry_price"]),
                    stop_loss=float(old_position["stop_loss"]),
                    take_profit=float(old_position["take_profit"]),
                    quantity=float(old_position["quantity"]),
                    opened_at=str(old_position["opened_at"]),
                    max_hold_until=str(old_position["max_hold_until"]) if old_position.get("max_hold_until") is not None else None,
                    price_change_pct=float(old_position["price_change_pct"]),
                    highest_price=float(old_position.get("highest_price", old_position["entry_price"])),
                    lowest_price=float(old_position.get("lowest_price", old_position["entry_price"])),
                    trailing_stop=float(old_position["trailing_stop"]) if old_position.get("trailing_stop") is not None else None,
                )]
            except (KeyError, TypeError, ValueError):
                pass
        return []

    result: list[ActivePosition] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        try:
            result.append(ActivePosition(
                symbol=str(raw["symbol"]),
                side=str(raw["side"]),
                setup_type=str(raw["setup_type"]),
                strategy_key=str(raw["strategy_key"]),
                confidence=int(raw["confidence"]),
                entry_price=float(raw["entry_price"]),
                stop_loss=float(raw["stop_loss"]),
                take_profit=float(raw["take_profit"]),
                quantity=float(raw["quantity"]),
                opened_at=str(raw["opened_at"]),
                max_hold_until=str(raw["max_hold_until"]) if raw.get("max_hold_until") is not None else None,
                price_change_pct=float(raw["price_change_pct"]),
                highest_price=float(raw.get("highest_price", raw["entry_price"])),
                lowest_price=float(raw.get("lowest_price", raw["entry_price"])),
                trailing_stop=float(raw["trailing_stop"]) if raw.get("trailing_stop") is not None else None,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return result


def load_active_position(state: dict) -> ActivePosition | None:
    """Backward compatibility: return first active position."""
    positions = load_active_positions(state)
    return positions[0] if positions else None


def cleanup_stale_positions(
    positions: list[ActivePosition],
    cfg: Config,
    now: datetime,
    paper_balance: float,
    cfg_path: str,
) -> tuple[list[ActivePosition], float, list[dict]]:
    """
    Bot restart'inde veya cycle'da eski pozisyonlari tespit et ve kapat.
    Returns: (remaining_positions, updated_balance, closed_trade_records)
    """
    remaining: list[ActivePosition] = []
    closed_records: list[dict] = []
    updated_balance = paper_balance

    for position in positions:
        opened_at = parse_datetime(position.opened_at)
        if opened_at is None:
            # Invalid date, force close
            exit_reason = "stale_position_invalid_date"
        else:
            age_hours = (now - opened_at).total_seconds() / 3600
            if age_hours > cfg.max_position_age_hours:
                exit_reason = f"stale_position_age_{int(age_hours)}h"
            else:
                remaining.append(position)
                continue  # Position is still valid

        # Force close stale position at entry price (unknown real exit price)
        current_price = position.entry_price  # Fallback
        try:
            current_price = fetch_last_price(cfg, position.symbol)
        except Exception as e:
            print(f"  [Force Close Warning] {position.symbol}: fiyat cekilemedi, entry price ile kapatildi. {e}")

        pnl_net = compute_position_pnl(position, current_price, cfg)
        updated_balance += pnl_net

        append_paper_trade(
            cfg_path,
            position,
            current_price,
            exit_reason,
            now,
            pnl_net,
            updated_balance,
        )
        closed_records.append({
            "position": position,
            "exit_price": current_price,
            "exit_reason": exit_reason,
            "pnl": pnl_net,
        })
        print(f"  [Force Close] {position.symbol} | {exit_reason} | PnL: {pnl_net:+.2f} USD")

    return remaining, updated_balance, closed_records


def open_position_from_setup(setup: Setup, cfg: Config, now: datetime, balance: float) -> ActivePosition:
    entry_price = setup_entry_price(setup)
    
    # Percentage-based position sizing
    risk_amount = balance * (cfg.position_size_pct / 100)
    
    # Calculate position size based on risk
    if setup.stop_loss is not None and setup.stop_loss != entry_price:
        # Risk-based sizing: position_size = risk_amount / (entry - stop)
        risk_per_unit = abs(entry_price - setup.stop_loss)
        if risk_per_unit > 0:
            quantity = risk_amount / risk_per_unit
            # Cap at max risk percentage
            max_position_value = balance * (cfg.max_risk_pct / 100) * 10  # 10x max leverage equivalent
            quantity = min(quantity, max_position_value / entry_price)
        else:
            quantity = cfg.position_size_usd / entry_price
    else:
        quantity = cfg.position_size_usd / entry_price
    
    stop_loss = setup.stop_loss if setup.stop_loss is not None else entry_price * 0.98
    take_profit = setup.target_1 or setup.target_2 or entry_price * 1.02
    
    max_hold_until = None
    if cfg.max_position_hold_hours > 0:
        max_hold_until = (now + timedelta(hours=cfg.max_position_hold_hours)).isoformat()
    
    return ActivePosition(
        symbol=setup.symbol,
        side=setup.decision,
        setup_type=setup.setup_type,
        strategy_key=setup.strategy_key,
        confidence=setup.confidence,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        quantity=quantity,
        opened_at=now.isoformat(),
        max_hold_until=max_hold_until,
        price_change_pct=setup.price_change_pct,
        highest_price=entry_price,
        lowest_price=entry_price,
        trailing_stop=None,
    )


def fetch_last_price(cfg: Config, symbol: str) -> float:
    raw = fetch_json(
        f"{cfg.futures_base_url}/fapi/v1/ticker/price",
        params={"symbol": symbol},
        timeout=20,
    )
    return float(raw.get("price", 0.0))


def evaluate_position_exit(
    position: ActivePosition,
    current_price: float,
    now: datetime,
) -> tuple[str, float] | None:
    if position.side == "LONG":
        if current_price <= position.stop_loss:
            return ("stop_loss", current_price)
        if current_price >= position.take_profit:
            return ("take_profit", current_price)
    else:
        if current_price >= position.stop_loss:
            return ("stop_loss", current_price)
        if current_price <= position.take_profit:
            return ("take_profit", current_price)

    max_hold_until = parse_datetime(position.max_hold_until)
    if max_hold_until is not None and now >= max_hold_until:
        return ("max_hold_1d", current_price)
    return None


def compute_position_pnl(
    position: ActivePosition,
    exit_price: float,
    cfg: Config,
) -> float:
    direction = 1 if position.side == "LONG" else -1
    gross_pnl = (exit_price - position.entry_price) * position.quantity * direction

    # Commission: entry + exit
    notional_entry = position.entry_price * position.quantity
    notional_exit = exit_price * position.quantity
    commission = (notional_entry + notional_exit) * (cfg.commission_pct / 100)

    # Slippage on exit
    slippage = notional_exit * (cfg.slippage_pct / 100)

    return gross_pnl - commission - slippage


def append_paper_trade(
    path: str,
    position: ActivePosition,
    exit_price: float,
    exit_reason: str,
    closed_at: datetime,
    pnl_net: float,
    balance_after: float,
) -> None:
    trade_history = load_trade_history(path)
    new_row = pd.DataFrame(
        [
            {
                "symbol": position.symbol,
                "side": position.side,
                "setup_type": position.setup_type,
                "strategy_key": position.strategy_key,
                "entry": position.entry_price,
                "exit": exit_price,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
                "quantity": position.quantity,
                "opened_at": position.opened_at,
                "closed_at": closed_at.isoformat(),
                "exit_reason": exit_reason,
                "pnl_net": pnl_net,
                "balance_after": balance_after,
                "confidence": position.confidence,
            }
        ]
    )
    trade_history = pd.concat([trade_history, new_row], ignore_index=True)
    trade_history.to_csv(path, index=False)


def format_strategy_edge(setup: Setup, strategy_stats: dict[str, StrategyStats]) -> str:
    stats = strategy_stats.get(setup.strategy_key)
    if stats is None:
        return "gecmis veri yok"
    return (
        f"{stats.closed_trades} kapanmis islem | "
        f"win rate %{stats.win_rate * 100:.0f} | "
        f"ort pnl {stats.avg_pnl_net:+.2f} USD"
    )


def format_entry_message(setup: Setup, position: ActivePosition, strategy_stats: dict[str, StrategyStats]) -> str:
    funding_text = "?" if setup.funding_rate_pct is None else f"{setup.funding_rate_pct:+.4f}%"
    hold_text = "sinirsiz" if position.max_hold_until is None else (
        parse_datetime(position.max_hold_until) or utc_now()
    ).strftime('%Y-%m-%d %H:%M UTC')
    return (
        f"*PAPER ENTRY* | *{position.symbol}* | *{position.side}* `{position.confidence}`\n"
        f"Tip: `{position.setup_type}` | 24s degisim: `{setup.price_change_pct:+.2f}%`\n"
        f"Entry: `{format_price(position.entry_price)}` | SL: `{format_price(position.stop_loss)}` | TP: `{format_price(position.take_profit)}`\n"
        f"Boyut: `{position.quantity:.4f}` | Notional: `{position.quantity * position.entry_price:.2f} USD`\n"
        f"Funding: `{funding_text}` | Strateji edge: {format_strategy_edge(setup, strategy_stats)}\n"
        f"Maks elde tutma: `{hold_text}`\n"
        f"Not: {setup.summary}"
    )


def format_exit_message(
    position: ActivePosition,
    exit_price: float,
    exit_reason: str,
    pnl_net: float,
    balance_after: float,
    closed_at: datetime,
    summary: dict[str, float],
) -> str:
    reason_map = {
        "take_profit": "hedefe ulasti",
        "stop_loss": "stop oldu",
        "max_hold_1d": "1 gunluk sure doldu",
    }
    opened_at = parse_datetime(position.opened_at) or closed_at
    held_minutes = int((closed_at - opened_at).total_seconds() // 60)
    return (
        f"*PAPER EXIT* | *{position.symbol}* | *{position.side}*\n"
        f"Cikis: `{format_price(exit_price)}` | Sebep: `{reason_map.get(exit_reason, exit_reason)}`\n"
        f"PnL: `{pnl_net:+.2f} USD` | Bakiye: `{balance_after:.2f} USD`\n"
        f"Sure: `{held_minutes} dk` | Acilis: `{format_price(position.entry_price)}`\n"
        f"Basari orani: `%{summary['win_rate'] * 100:.0f}` | Kapanan islem: `{int(summary['closed_trades'])}` | Ortalama pnl: `{summary['avg_pnl_net']:+.2f} USD`"
    )


def write_cycle_state(cfg: Config, state: dict) -> None:
    state["updated_at"] = utc_now().isoformat()
    write_state(cfg.analysis_state_file, state)


def format_price(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


# format_setup_line artik build_report icinde inline yapiliyor.


def build_report(setups: list[Setup], cfg: Config) -> str:
    now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ready_setups = [s for s in setups if s.ready and s.confidence >= cfg.min_ready_confidence]
    
    sections = [
        "🎯 *UZMAN TRADER - SINYAL RAPORU*",
        f"Zaman: `{now_text}`",
        f"Tarama: `{len(setups)}` coin | Hazir: `{len(ready_setups)}`",
        "",
    ]

    if ready_setups:
        sections.append("*🔥 YÜKSEK GÜVENLİ FIRSATLAR*")
        for setup in ready_setups:
            sections.append(
                f"*{setup.symbol}* | `{setup.decision}` `{setup.confidence}/100`\n"
                f"Tip: `{setup.setup_type}`\n"
                f"Giris: `{format_price(setup.entry_low)}` | SL: `{format_price(setup.stop_loss)}` | TP: `{format_price(setup.target_1)}`\n"
                f"Analiz: _{setup.summary}_"
            )
    else:
        sections.append("🔍 Su an pusuya devam, temiz firsat yok.")

    return "\n\n".join(sections)


def build_report_hash(report_text: str) -> str:
    return hashlib.sha256(report_text.encode("utf-8")).hexdigest()


def detect_btc_trend(cfg: Config) -> str | None:
    """
    BTC trend'ini tespit et.
    Returns: 'TRENDING_UP', 'TRENDING_DOWN', 'RANGING', or None (error)
    """
    btc_symbol = "BTCUSDT"
    try:
        df_1h = fetch_klines(cfg, btc_symbol, "1h")
        if len(df_1h) < 50:
            return None
        df_1h = add_indicators(df_1h)
        regime, _ = detect_market_regime(df_1h)
        return regime
    except Exception as e:
        print(f"[BTC Trend Warning] {e}")
        return None


# btc_trend filtreleme mantigi artik analyze_market icinde hard filter olarak yapiliyor.


def analyze_market(cfg: Config) -> list[Setup]:
    market_candidates = fetch_market_candidates(cfg)
    if not market_candidates:
        raise RuntimeError("Filtrelere uyan futures sembolu bulunamadi.")

    # BTC trend'ini tespit et
    btc_trend = detect_btc_trend(cfg)
    if btc_trend:
        print(f"BTC Trend: {btc_trend}")

    funding_map = fetch_funding_rates(cfg)
    print(
        "Tarama evreni: "
        + ", ".join(f"{item.symbol}({item.price_change_pct:+.2f}%)" for item in market_candidates[:20])
    )

    setups: list[Setup] = []
    for market in market_candidates:
        try:
            frames: dict[str, pd.DataFrame] = {}
            for interval in TIMEFRAMES:
                df = fetch_klines(cfg, market.symbol, interval)
                if len(df) < 220:
                    print(f"[UYARI] {market.symbol} {interval} icin yetersiz mum verisi, atlandi.")
                    break
                frames[interval] = add_indicators(df)

            if len(frames) == len(TIMEFRAMES):
                # Fetch Open Interest data
                oi_data = fetch_open_interest(cfg, market.symbol)
                oi_value = oi_data.get("openInterest") if oi_data else None
                oi_history = pd.DataFrame([oi_data]) if oi_data else pd.DataFrame()
                oi_trend, oi_strength = analyze_open_interest_trend(
                    oi_value or 0,
                    oi_history,
                    market.price_change_pct,
                )

                candidates = build_candidate_setups(
                    market,
                    frames,
                    funding_map.get(market.symbol),
                    open_interest=oi_value,
                    oi_trend=oi_trend,
                    oi_strength=oi_strength,
                )

                # BTC trend filtresini uygula (HARD FILTER)
                if btc_trend:
                    filtered = []
                    for setup in candidates:
                        skip = False
                        if btc_trend == "TRENDING_DOWN" and setup.decision == "LONG":
                            skip = True
                        if btc_trend == "TRENDING_UP" and setup.decision == "SHORT":
                            skip = True
                        
                        if not skip:
                            filtered.append(setup)
                    setups.extend(filtered)
                else:
                    setups.extend(candidates)
        except Exception as exc:
            print(f"[UYARI] {market.symbol} taranamadi: {exc}")
            continue

    setups.sort(key=lambda item: (not item.ready, -item.confidence, -item.price_change_pct))
    return setups


def run_pump_scan(cfg: Config, market_candidates: list[MarketTicker], funding_map: dict[str, float], state: dict) -> list:
    """
    Piyasadaki tüm coinleri pump sinyalleri için tara.
    Eşiği geçen coinleri Telegram'a bildir, cooldown ile spam önle.
    """
    now = utc_now()
    pump_cooldowns: dict = state.get("pump_cooldowns", {})
    alerts_sent: list = []
    candidates_found: list = []

    print(f"[Pump Radar] {len(market_candidates)} coin taranıyor... (min skor: {cfg.pump_min_score})")

    for market in market_candidates:
        # Cooldown kontrolü
        last_alert_str = pump_cooldowns.get(market.symbol)
        if last_alert_str:
            last_alert = parse_datetime(last_alert_str)
            if last_alert and (now - last_alert).total_seconds() < cfg.pump_cooldown_minutes * 60:
                continue  # Bu coin için henüz cooldown bitmedi

        try:
            frames: dict[str, pd.DataFrame] = {}
            for interval in TIMEFRAMES:
                df = fetch_klines(cfg, market.symbol, interval)
                if len(df) < 50:
                    break
                frames[interval] = add_indicators(df)

            if len(frames) != len(TIMEFRAMES):
                continue

            oi_data = fetch_open_interest(cfg, market.symbol)
            oi_value = oi_data.get("openInterest") if oi_data else None

            candidate = detect_pump_candidate(
                symbol=market.symbol,
                price_change_24h=market.price_change_pct,
                df_15m=frames["15m"],
                df_1h=frames["1h"],
                df_4h=frames["4h"],
                funding_rate=funding_map.get(market.symbol),
                open_interest=float(oi_value) if oi_value else None,
            )

            if candidate and candidate.score >= cfg.pump_min_score and candidate.signals:
                candidates_found.append(candidate)
                pump_cooldowns[market.symbol] = now.isoformat()
                msg = format_pump_alert(candidate)
                send_telegram(cfg, msg)
                alerts_sent.append(market.symbol)
                print(
                    f"  [Pump Radar] 🚀 {market.symbol} | Skor: {candidate.score}/100 | "
                    f"{len(candidate.signals)} sinyal | {', '.join(s[:30] for s in candidate.signals[:3])}"
                )

        except Exception as exc:
            print(f"  [Pump Radar Hata] {market.symbol}: {exc}")
            continue

    # Özet mesajı (opsiyonel)
    if cfg.pump_send_summary and candidates_found:
        summary_msg = format_pump_summary(sorted(candidates_found, key=lambda c: c.score, reverse=True))
        send_telegram(cfg, summary_msg)

    if not alerts_sent:
        print(f"[Pump Radar] Bu turda eşiği geçen pump adayı bulunamadı.")
    else:
        print(f"[Pump Radar] {len(alerts_sent)} uyarı gönderildi: {', '.join(alerts_sent)}")

    # Eski cooldown kayıtlarını temizle (24 saat geçmişse sil)
    stale_symbols = [
        sym for sym, ts in pump_cooldowns.items()
        if (dt := parse_datetime(ts)) and (now - dt).total_seconds() > 86400
    ]
    for sym in stale_symbols:
        del pump_cooldowns[sym]

    state["pump_cooldowns"] = pump_cooldowns
    return candidates_found


def main() -> None:
    cfg = load_config()
    state = read_state(cfg.analysis_state_file)
    paper_balance = float(state.get("paper_balance_usd", cfg.starting_balance_usd))

    print(
        f"Basladi | Scan every: {cfg.scan_every_seconds}s | Symbol limit: {cfg.top_gainers_limit or 'all'} | "
        f"Volume filter: {cfg.min_quote_volume_usd:.0f}-{cfg.max_quote_volume_usd:.0f} | "
        f"Ready threshold: {cfg.min_ready_confidence} | Max hold: {cfg.max_position_hold_hours or 0}h(disabled if 0) | "
        f"Max open positions: {cfg.max_open_positions} | Position sizing: {cfg.position_size_pct}% risk | "
        f"Trailing stop: {cfg.trailing_stop_atr_mult}x ATR | "
        f"Commission: {cfg.commission_pct}% | Slippage: {cfg.slippage_pct}%"
    )

    if cfg.pump_scan_enabled:
        print(f"[Pump Radar] AKTİF | Min skor: {cfg.pump_min_score} | Cooldown: {cfg.pump_cooldown_minutes} dk")
    else:
        print("[Pump Radar] Devre dışı (PUMP_SCAN_ENABLED=false)")

    while True:
        try:
            active_positions = load_active_positions(state)
            now = utc_now()

            # === PUMP RADAR PHASE: Her cycle'da pump sinyalleri tara ===
            if cfg.pump_scan_enabled:
                try:
                    pump_candidates = fetch_market_candidates(cfg)
                    pump_funding_map = fetch_funding_rates(cfg)
                    run_pump_scan(cfg, pump_candidates, pump_funding_map, state)
                    write_cycle_state(cfg, state)  # pump_cooldowns'ı kaydet
                except Exception as pump_exc:
                    print(f"[Pump Radar Genel Hata] {pump_exc}")

            # === PHASE 0: Detect bot restart & cleanup stale positions ===
            last_cycle_status = state.get("last_cycle_status")
            last_success_at = parse_datetime(state.get("last_success_at"))
            is_restart = (
                last_cycle_status in ("error", "exit", None)
                or (last_success_at is not None and (now - last_success_at).total_seconds() > 3600)
            )

            if is_restart and active_positions:
                print(f"Bot restart detected veya uzun sure gecmis. Pozisyonlar kontrol ediliyor...")
                fresh_positions, paper_balance, closed_records = cleanup_stale_positions(
                    active_positions,
                    cfg,
                    now,
                    paper_balance,
                    cfg.paper_trades_file,
                )

                # Send Telegram notifications for force-closed positions
                for record in closed_records:
                    trade_summary = compute_trade_summary(cfg.paper_trades_file)
                    send_telegram(
                        cfg,
                        format_exit_message(
                            record["position"],
                            record["exit_price"],
                            record["exit_reason"],
                            record["pnl"],
                            paper_balance,
                            now,
                            trade_summary,
                        ),
                    )

                # Update state with cleaned positions
                if fresh_positions:
                    state["active_positions"] = [asdict(p) for p in fresh_positions]
                    remaining_positions = fresh_positions
                else:
                    state.pop("active_positions", None)
                    remaining_positions = []

                state["paper_balance_usd"] = paper_balance
                state["last_cycle_status"] = "ok"
                state["last_success_at"] = now.isoformat()
                write_cycle_state(cfg, state)

                if closed_records:
                    print(f"{len(closed_records)} eski pozisyon temizlendi. Bakiye: {paper_balance:.2f} USD")
            else:
                remaining_positions = active_positions

            # === PHASE 1: Check exits & update trailing stops for ALL positions ===
            closed_positions: list[tuple[ActivePosition, str, float, float]] = []  # (pos, reason, exit_price, pnl)
            updated_positions: list[ActivePosition] = []

            for position in remaining_positions:
                current_price = fetch_last_price(cfg, position.symbol)

                # Fetch current ATR for trailing stop calculation
                try:
                    df_15m = fetch_klines(cfg, position.symbol, "15m")
                    if len(df_15m) >= 50:
                        df_15m = add_indicators(df_15m)
                        current_atr = safe_float(df_15m.iloc[-1]["atr14"])

                        # Update trailing stop
                        updated_position = update_trailing_stop(
                            position,
                            current_price,
                            current_atr,
                            cfg.trailing_stop_atr_mult,
                        )
                        updated_positions.append(updated_position)
                        position = updated_position  # Use updated position for exit check

                        print(
                            f"Trailing stop guncellendi: {position.symbol} | "
                            f"Yeni SL: {position.stop_loss:.6f}"
                        )
                except Exception as e:
                    print(f"Trailing stop guncelleme hatasi ({position.symbol}): {e}")

                # Check exit conditions
                exit_signal = evaluate_position_exit(position, current_price, now)
                if exit_signal is not None:
                    exit_reason, exit_price = exit_signal
                    pnl_net = compute_position_pnl(position, exit_price, cfg)

                    # Check max drawdown limit
                    pnl_pct = (pnl_net / (position.entry_price * position.quantity)) * 100
                    if abs(pnl_pct) > cfg.max_drawdown_pct and exit_reason != "stop_loss":
                        exit_reason = "max_drawdown_limit"

                    closed_positions.append((position, exit_reason, exit_price, pnl_net))
                else:
                    pos_index = len(updated_positions) + 1
                    print(
                        f"Aktif pozisyon [{pos_index}/{len(active_positions)}]: "
                        f"{position.symbol} {position.side} | "
                        f"anlik fiyat {current_price:.6f} | "
                        f"Trailing SL: {position.stop_loss:.6f}"
                    )

            # === PHASE 2: Process closed positions ===
            for position, exit_reason, exit_price, pnl_net in closed_positions:
                paper_balance += pnl_net
                append_paper_trade(
                    cfg.paper_trades_file,
                    position,
                    exit_price,
                    exit_reason,
                    now,
                    pnl_net,
                    paper_balance,
                )
                trade_summary = compute_trade_summary(cfg.paper_trades_file)
                send_telegram(
                    cfg,
                    format_exit_message(
                        position,
                        exit_price,
                        exit_reason,
                        pnl_net,
                        paper_balance,
                        now,
                        trade_summary,
                    ),
                )
                print(
                    f"Pozisyon kapandi: {position.symbol} | {exit_reason} | "
                    f"PnL {pnl_net:+.2f} USD"
                )

            # === PHASE 3: Update state with remaining active positions ===
            # Remove closed positions, keep updated ones
            closed_symbols = {pos.symbol for pos, _, _, _ in closed_positions}
            remaining_positions = [p for p in updated_positions if p.symbol not in closed_symbols]

            if remaining_positions:
                state["active_positions"] = [asdict(p) for p in remaining_positions]
            else:
                state.pop("active_positions", None)

            state["paper_balance_usd"] = paper_balance
            trade_summary = compute_trade_summary(cfg.paper_trades_file)
            state["win_rate"] = trade_summary["win_rate"]
            state["closed_trades"] = int(trade_summary["closed_trades"])

            # === PHASE 4: Open new positions if slots available ===
            open_slots = cfg.max_open_positions - len(remaining_positions)
            if open_slots > 0:
                strategy_stats = compute_strategy_stats(cfg.paper_trades_file)
                setups = analyze_market(cfg)

                # Filter out symbols already in active positions
                active_symbols = {p.symbol for p in remaining_positions}
                available_setups = [s for s in setups if s.symbol not in active_symbols and s.ready and s.confidence >= cfg.min_ready_confidence]

                opened_count = 0
                skipped_symbols: list[str] = []
                for setup in sorted(available_setups, key=lambda s: score_setup(s, strategy_stats), reverse=True):
                    if opened_count >= open_slots:
                        break

                    new_position = open_position_from_setup(setup, cfg, now, paper_balance)
                    state["active_positions"] = [asdict(p) for p in remaining_positions] + [asdict(new_position)]
                    remaining_positions.append(new_position)
                    state["last_action"] = "entry"
                    state["last_selected_strategy"] = setup.strategy_key
                    send_telegram(cfg, format_entry_message(setup, new_position, strategy_stats))
                    print(
                        f"Pozisyon acildi: {new_position.symbol} {new_position.side} | "
                        f"{new_position.setup_type} | skor {score_setup(setup, strategy_stats):.2f} | "
                        f"Boyut: {new_position.quantity:.4f} ({paper_balance * cfg.position_size_pct / 100:.2f} USD risk)"
                    )
                    opened_count += 1

                if opened_count == 0:
                    if available_setups:
                        print(f"{len(available_setups)} uygun setup var ama slot dolu veya esik altinda.")
                    else:
                        print("Temiz ve esik ustu setup bulunamadi, yeni pozisyon acilmadi.")

                state["paper_balance_usd"] = paper_balance
            else:
                print(f"Tum slotlar dolu ({len(remaining_positions)}/{cfg.max_open_positions}), yeni pozisyon acilmadi.")

            state["last_cycle_status"] = "ok"
            state["last_success_at"] = now.isoformat()
            write_cycle_state(cfg, state)
        except Exception as exc:
            state["last_cycle_status"] = "error"
            state["last_error"] = str(exc)
            write_cycle_state(cfg, state)
            print(f"[HATA] {utc_now().isoformat()} | {exc}")

        time.sleep(cfg.scan_every_seconds)


if __name__ == "__main__":
    main()
