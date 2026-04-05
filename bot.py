import os
import time
import math
import csv
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    symbols: list[str]
    interval: str
    scan_every_seconds: int
    binance_api_base: str
    lookback_bars: int
    fib_lookback: int
    risk_reward: float
    min_confidence: int
    max_price_usd: float
    use_dynamic_symbols: bool
    min_listing_year: int
    min_quote_volume_usd: float
    max_quote_volume_usd: float
    dynamic_symbol_limit: int
    paper_trade_enabled: bool
    paper_initial_balance: float
    paper_risk_per_trade_pct: float
    paper_fee_rate: float
    paper_log_file: str


@dataclass
class PaperPosition:
    symbol: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    quantity: float
    opened_at: pd.Timestamp
    confidence: int
    fee_open: float


def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class PaperTrader:
    def __init__(
        self,
        initial_balance: float,
        risk_per_trade_pct: float,
        fee_rate: float,
        log_file: str,
    ) -> None:
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.fee_rate = fee_rate
        self.log_file = log_file

        self.open_positions: dict[str, PaperPosition] = {}
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.gross_profit = 0.0
        self.gross_loss = 0.0
        self.peak_balance = initial_balance
        self.max_drawdown_pct = 0.0

        if self.log_file and not os.path.exists(self.log_file):
            with open(self.log_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "symbol",
                        "side",
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
                )

    def _update_drawdown(self) -> None:
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance

        if self.peak_balance > 0:
            drawdown_pct = (self.peak_balance - self.balance) / self.peak_balance * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown_pct)

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self.open_positions

    def open_position(self, signal: dict) -> dict | None:
        symbol = signal["symbol"]
        if symbol in self.open_positions:
            return None

        entry = float(signal["entry"])
        stop_loss = float(signal["stop_loss"])
        take_profit = float(signal["take_profit"])
        side = signal["side"]
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit <= 0:
            return None

        risk_amount = self.balance * (self.risk_per_trade_pct / 100.0)
        if risk_amount <= 0:
            return None

        quantity = risk_amount / risk_per_unit
        if quantity <= 0:
            return None

        fee_open = quantity * entry * self.fee_rate

        position = PaperPosition(
            symbol=symbol,
            side=side,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quantity=quantity,
            opened_at=pd.Timestamp(signal["close_time"]),
            confidence=int(signal["confidence"]),
            fee_open=fee_open,
        )
        self.open_positions[symbol] = position

        return {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "quantity": quantity,
            "risk_amount": risk_amount,
            "fee_open": fee_open,
            "balance": self.balance,
            "opened_at": position.opened_at,
        }

    def _close_position(
        self,
        position: PaperPosition,
        exit_price: float,
        exit_reason: str,
        closed_at: pd.Timestamp,
    ) -> dict:
        if position.side == "LONG":
            pnl_raw = (exit_price - position.entry) * position.quantity
        else:
            pnl_raw = (position.entry - exit_price) * position.quantity

        fee_close = position.quantity * exit_price * self.fee_rate
        pnl_net = pnl_raw - position.fee_open - fee_close

        self.balance += pnl_net
        self.total_trades += 1
        if pnl_net >= 0:
            self.wins += 1
            self.gross_profit += pnl_net
        else:
            self.losses += 1
            self.gross_loss += abs(pnl_net)

        self._update_drawdown()

        trade = {
            "symbol": position.symbol,
            "side": position.side,
            "entry": position.entry,
            "exit": exit_price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "quantity": position.quantity,
            "opened_at": position.opened_at,
            "closed_at": closed_at,
            "exit_reason": exit_reason,
            "pnl_net": pnl_net,
            "balance": self.balance,
            "confidence": position.confidence,
            "win_rate": (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0.0,
            "max_drawdown_pct": self.max_drawdown_pct,
            "total_trades": self.total_trades,
        }
        self._append_trade_log(trade)
        return trade

    def _append_trade_log(self, trade: dict) -> None:
        if not self.log_file:
            return

        with open(self.log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    trade["symbol"],
                    trade["side"],
                    f"{trade['entry']:.8f}",
                    f"{trade['exit']:.8f}",
                    f"{trade['stop_loss']:.8f}",
                    f"{trade['take_profit']:.8f}",
                    f"{trade['quantity']:.8f}",
                    pd.Timestamp(trade["opened_at"]).isoformat(),
                    pd.Timestamp(trade["closed_at"]).isoformat(),
                    trade["exit_reason"],
                    f"{trade['pnl_net']:.8f}",
                    f"{trade['balance']:.8f}",
                    trade["confidence"],
                ]
            )

    def update_symbol(self, symbol: str, candle_row: pd.Series) -> dict | None:
        position = self.open_positions.get(symbol)
        if position is None:
            return None

        high = float(candle_row["high"])
        low = float(candle_row["low"])
        close_time = pd.Timestamp(candle_row["close_time"])

        if position.side == "LONG":
            hit_sl = low <= position.stop_loss
            hit_tp = high >= position.take_profit
            if not hit_sl and not hit_tp:
                return None

            # Conservative handling when both levels are touched in same candle.
            if hit_sl and hit_tp:
                exit_price = position.stop_loss
                exit_reason = "BOTH_HIT_ASSUME_SL"
            elif hit_sl:
                exit_price = position.stop_loss
                exit_reason = "STOP_LOSS"
            else:
                exit_price = position.take_profit
                exit_reason = "TAKE_PROFIT"

        else:
            hit_sl = high >= position.stop_loss
            hit_tp = low <= position.take_profit
            if not hit_sl and not hit_tp:
                return None

            if hit_sl and hit_tp:
                exit_price = position.stop_loss
                exit_reason = "BOTH_HIT_ASSUME_SL"
            elif hit_sl:
                exit_price = position.stop_loss
                exit_reason = "STOP_LOSS"
            else:
                exit_price = position.take_profit
                exit_reason = "TAKE_PROFIT"

        trade = self._close_position(position, exit_price, exit_reason, close_time)
        self.open_positions.pop(symbol, None)
        return trade


def load_config() -> Config:
    load_dotenv()
    symbols_raw = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    return Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        symbols=symbols,
        interval=os.getenv("INTERVAL", "15m").strip(),
        scan_every_seconds=int(os.getenv("SCAN_EVERY_SECONDS", "60")),
        binance_api_base=os.getenv("BINANCE_API_BASE", "https://api.binance.com").strip(),
        lookback_bars=int(os.getenv("LOOKBACK_BARS", "250")),
        fib_lookback=int(os.getenv("FIB_LOOKBACK", "120")),
        risk_reward=float(os.getenv("RISK_REWARD", "2.0")),
        min_confidence=int(os.getenv("MIN_CONFIDENCE", "60")),
        max_price_usd=float(os.getenv("MAX_PRICE_USD", "100")),
        use_dynamic_symbols=parse_bool(os.getenv("USE_DYNAMIC_SYMBOLS", "true"), default=True),
        min_listing_year=int(os.getenv("MIN_LISTING_YEAR", "2021")),
        min_quote_volume_usd=float(os.getenv("MIN_QUOTE_VOLUME_USD", "100000")),
        max_quote_volume_usd=float(os.getenv("MAX_QUOTE_VOLUME_USD", "25000000")),
        dynamic_symbol_limit=int(os.getenv("DYNAMIC_SYMBOL_LIMIT", "120")),
        paper_trade_enabled=parse_bool(os.getenv("PAPER_TRADE_ENABLED", "true"), default=True),
        paper_initial_balance=float(os.getenv("PAPER_INITIAL_BALANCE", "10000")),
        paper_risk_per_trade_pct=float(os.getenv("PAPER_RISK_PER_TRADE_PCT", "1.0")),
        paper_fee_rate=float(os.getenv("PAPER_FEE_RATE", "0.0004")),
        paper_log_file=os.getenv("PAPER_LOG_FILE", "paper_trades.csv").strip(),
    )


def fetch_klines(symbol: str, interval: str, limit: int, base_url: str) -> pd.DataFrame:
    url = f"{base_url}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    raw = response.json()

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

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def fetch_exchange_info(base_url: str) -> dict:
    url = f"{base_url}/api/v3/exchangeInfo"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.json()


def fetch_ticker_24hr(base_url: str) -> list[dict]:
    url = f"{base_url}/api/v3/ticker/24hr"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def is_leveraged_token(symbol: str) -> bool:
    suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    return symbol.endswith(suffixes)


def resolve_symbols(cfg: Config) -> list[str]:
    if not cfg.use_dynamic_symbols:
        return cfg.symbols

    try:
        exchange_info = fetch_exchange_info(cfg.binance_api_base)
        ticker_24h = fetch_ticker_24hr(cfg.binance_api_base)
    except Exception as exc:
        print(f"[WARN] Dinamik sembol listesi alinamadi: {exc}")
        return cfg.symbols

    ticker_map: dict[str, dict] = {row.get("symbol", ""): row for row in ticker_24h}
    min_listing_ts = int(datetime(cfg.min_listing_year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

    candidates: list[tuple[str, float]] = []
    for item in exchange_info.get("symbols", []):
        symbol = str(item.get("symbol", "")).upper()
        if not symbol or is_leveraged_token(symbol):
            continue

        if item.get("status") != "TRADING":
            continue

        if str(item.get("quoteAsset", "")).upper() != "USDT":
            continue

        if not bool(item.get("isSpotTradingAllowed", True)):
            continue

        onboard_date = int(item.get("onboardDate", 0) or 0)
        if onboard_date and onboard_date < min_listing_ts:
            continue

        if onboard_date == 0:
            # Strict filter: if listing date is not known, skip to keep 2021+ rule.
            continue

        ticker = ticker_map.get(symbol)
        if not ticker:
            continue

        try:
            last_price = float(ticker.get("lastPrice", 0.0))
            quote_volume = float(ticker.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            continue

        if last_price <= 0 or last_price > cfg.max_price_usd:
            continue

        if quote_volume < cfg.min_quote_volume_usd or quote_volume > cfg.max_quote_volume_usd:
            continue

        candidates.append((symbol, quote_volume))

    candidates.sort(key=lambda x: x[1])
    symbols = [s for s, _ in candidates[: cfg.dynamic_symbol_limit]]

    if symbols:
        print(
            "Dinamik sembol filtresi aktif | "
            f"Secilen: {len(symbols)} | "
            f"Yil>={cfg.min_listing_year}, Fiyat<={cfg.max_price_usd}, "
            f"Hacim:[{cfg.min_quote_volume_usd:.0f}, {cfg.max_quote_volume_usd:.0f}]"
        )
        return symbols

    print("[WARN] Dinamik filtreyle sembol bulunamadi, statik SYMBOLS listesine donuluyor.")
    return cfg.symbols


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
    high_close_prev = (df["high"] - df["close"].shift(1)).abs()
    low_close_prev = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close_prev, low_close_prev], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["rsi14"] = compute_rsi(out["close"], period=14)
    out["atr14"] = compute_atr(out, period=14)
    out["vol_ma20"] = out["volume"].rolling(20).mean()
    return out


def fib_levels_from_swing(swing_low: float, swing_high: float) -> dict[str, float]:
    diff = swing_high - swing_low
    return {
        "0.236": swing_high - diff * 0.236,
        "0.382": swing_high - diff * 0.382,
        "0.5": swing_high - diff * 0.5,
        "0.618": swing_high - diff * 0.618,
        "0.786": swing_high - diff * 0.786,
        "1.272_ext": swing_high + diff * 0.272,
    }


def short_fib_levels_from_swing(swing_low: float, swing_high: float) -> dict[str, float]:
    diff = swing_high - swing_low
    return {
        "0.236": swing_low + diff * 0.236,
        "0.382": swing_low + diff * 0.382,
        "0.5": swing_low + diff * 0.5,
        "0.618": swing_low + diff * 0.618,
        "0.786": swing_low + diff * 0.786,
        "1.272_ext": swing_low - diff * 0.272,
    }


def in_zone(value: float, a: float, b: float, tolerance_ratio: float = 0.0025) -> bool:
    low = min(a, b)
    high = max(a, b)
    tol = value * tolerance_ratio
    return (low - tol) <= value <= (high + tol)


def score_signal(base_conditions: list[bool], bonus_conditions: list[bool]) -> int:
    base = sum(1 for c in base_conditions if c)
    bonus = sum(1 for c in bonus_conditions if c)
    raw = (base * 18) + (bonus * 14)
    return min(100, raw)


def build_signal(symbol: str, df: pd.DataFrame, cfg: Config) -> dict | None:
    if len(df) < max(80, cfg.fib_lookback + 5):
        return None

    # Use last closed candle to avoid false trigger from live candle noise.
    row = df.iloc[-2]
    recent = df.iloc[-(cfg.fib_lookback + 2):-2]

    if recent.empty or recent["high"].max() <= recent["low"].min():
        return None

    close = float(row["close"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    rsi = float(row["rsi14"])
    atr = float(row["atr14"])
    volume = float(row["volume"])
    vol_ma20 = float(row["vol_ma20"]) if not math.isnan(float(row["vol_ma20"])) else 0.0

    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    range_size = swing_high - swing_low
    if range_size <= 0:
        return None

    uptrend = ema20 > ema50 and close > ema20
    downtrend = ema20 < ema50 and close < ema20
    volume_ok = vol_ma20 > 0 and volume > vol_ma20

    if uptrend:
        fib = fib_levels_from_swing(swing_low, swing_high)
        fib_ok = in_zone(close, fib["0.5"], fib["0.618"])
        rsi_ok = 42 <= rsi <= 70
        atr_ok = atr > 0

        score = score_signal(
            base_conditions=[uptrend, fib_ok, rsi_ok],
            bonus_conditions=[volume_ok, atr_ok, close > fib["0.382"]],
        )

        if score >= cfg.min_confidence and fib_ok and rsi_ok and atr_ok:
            entry = close
            stop_loss = min(fib["0.786"], entry - 1.2 * atr)
            take_profit = max(entry + (entry - stop_loss) * cfg.risk_reward, fib["1.272_ext"])
            rr = (take_profit - entry) / (entry - stop_loss) if entry > stop_loss else 0.0

            return {
                "symbol": symbol,
                "side": "LONG",
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": score,
                "rsi": rsi,
                "rr": rr,
                "close_time": row["close_time"],
                "notes": "EMA trend yukari, fiyat Fibonacci 0.5-0.618 bolgesinde",
            }

    if downtrend:
        fib = short_fib_levels_from_swing(swing_low, swing_high)
        fib_ok = in_zone(close, fib["0.5"], fib["0.618"])
        rsi_ok = 30 <= rsi <= 58
        atr_ok = atr > 0

        score = score_signal(
            base_conditions=[downtrend, fib_ok, rsi_ok],
            bonus_conditions=[volume_ok, atr_ok, close < fib["0.382"]],
        )

        if score >= cfg.min_confidence and fib_ok and rsi_ok and atr_ok:
            entry = close
            stop_loss = max(fib["0.786"], entry + 1.2 * atr)
            take_profit = min(entry - (stop_loss - entry) * cfg.risk_reward, fib["1.272_ext"])
            rr = (entry - take_profit) / (stop_loss - entry) if stop_loss > entry else 0.0

            return {
                "symbol": symbol,
                "side": "SHORT",
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "confidence": score,
                "rsi": rsi,
                "rr": rr,
                "close_time": row["close_time"],
                "notes": "EMA trend asagi, fiyat Fibonacci 0.5-0.618 bolgesinde",
            }

    return None


def format_signal_message(signal: dict, interval: str) -> str:
    dt = signal["close_time"]
    if isinstance(dt, pd.Timestamp):
        dt_str = dt.tz_convert(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    else:
        dt_str = str(dt)

    return (
        "*Kripto Sinyal*\n"
        f"Sembol: *{signal['symbol']}*\n"
        f"Yon: *{signal['side']}*\n"
        f"Zaman Dilimi: *{interval}*\n"
        f"Entry: `{signal['entry']:.4f}`\n"
        f"SL: `{signal['stop_loss']:.4f}`\n"
        f"TP: `{signal['take_profit']:.4f}`\n"
        f"Tahmini R/R: `{signal['rr']:.2f}`\n"
        f"RSI: `{signal['rsi']:.1f}`\n"
        f"Guven: *{signal['confidence']}%*\n"
        f"Not: {signal['notes']}\n"
        f"Kapanis: {dt_str}"
    )


def format_paper_open_message(event: dict, interval: str) -> str:
    dt = pd.Timestamp(event["opened_at"]).tz_convert(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "*Paper Trade Acildi*\n"
        f"Sembol: *{event['symbol']}*\n"
        f"Yon: *{event['side']}*\n"
        f"Zaman Dilimi: *{interval}*\n"
        f"Entry: `{event['entry']:.4f}`\n"
        f"SL: `{event['stop_loss']:.4f}`\n"
        f"TP: `{event['take_profit']:.4f}`\n"
        f"Qty: `{event['quantity']:.6f}`\n"
        f"Risk: `{event['risk_amount']:.2f}`\n"
        f"Ucret(acilis): `{event['fee_open']:.4f}`\n"
        f"Sanal Bakiye: `{event['balance']:.2f}`\n"
        f"Acilis: {dt}"
    )


def format_paper_close_message(trade: dict, interval: str) -> str:
    opened = pd.Timestamp(trade["opened_at"]).tz_convert(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    closed = pd.Timestamp(trade["closed_at"]).tz_convert(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "*Paper Trade Kapandi*\n"
        f"Sembol: *{trade['symbol']}*\n"
        f"Yon: *{trade['side']}*\n"
        f"Zaman Dilimi: *{interval}*\n"
        f"Entry: `{trade['entry']:.4f}` -> Exit: `{trade['exit']:.4f}`\n"
        f"Sebep: *{trade['exit_reason']}*\n"
        f"PnL (net): `{trade['pnl_net']:.2f}`\n"
        f"Sanal Bakiye: `{trade['balance']:.2f}`\n"
        f"Toplam Islem: `{trade['total_trades']}`\n"
        f"Win Rate: `{trade['win_rate']:.1f}%`\n"
        f"Max DD: `{trade['max_drawdown_pct']:.2f}%`\n"
        f"Acilis: {opened}\n"
        f"Kapanis: {closed}"
    )


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        print("[WARN] Telegram ayarlari yok, mesaj konsola yazdiriliyor:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def main() -> None:
    cfg = load_config()
    symbols_to_scan = resolve_symbols(cfg)
    print(f"Basladi | Taranacak sembol sayisi: {len(symbols_to_scan)} | Interval: {cfg.interval}")
    if not symbols_to_scan:
        print("[HATA] Taranacak sembol yok. .env ayarlarini kontrol et.")
        return
    print(f"Maksimum fiyat filtresi: {cfg.max_price_usd:.2f} USDT")
    print(f"Paper Trade: {'ACIK' if cfg.paper_trade_enabled else 'KAPALI'}")

    paper_trader = None
    if cfg.paper_trade_enabled:
        paper_trader = PaperTrader(
            initial_balance=cfg.paper_initial_balance,
            risk_per_trade_pct=cfg.paper_risk_per_trade_pct,
            fee_rate=cfg.paper_fee_rate,
            log_file=cfg.paper_log_file,
        )

    last_sent_key: dict[str, str] = {}

    while True:
        cycle_started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n[{cycle_started}] Tarama basliyor...")

        for symbol in symbols_to_scan:
            try:
                raw_df = fetch_klines(
                    symbol=symbol,
                    interval=cfg.interval,
                    limit=cfg.lookback_bars,
                    base_url=cfg.binance_api_base,
                )
                df = add_indicators(raw_df)

                if paper_trader is not None:
                    latest_closed = raw_df.iloc[-2]
                    closed_trade = paper_trader.update_symbol(symbol, latest_closed)
                    if closed_trade is not None:
                        close_msg = format_paper_close_message(closed_trade, cfg.interval)
                        send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, close_msg)
                        print(
                            f"{symbol}: paper pozisyon kapandi | PnL: {closed_trade['pnl_net']:.2f} | "
                            f"Bakiye: {closed_trade['balance']:.2f}"
                        )

                latest_price = float(raw_df.iloc[-2]["close"])
                if latest_price > cfg.max_price_usd:
                    print(f"{symbol}: filtre disi, fiyat {latest_price:.4f} > {cfg.max_price_usd:.2f}")
                    continue

                signal = build_signal(symbol, df, cfg)

                if not signal:
                    print(f"{symbol}: sinyal yok")
                    continue

                close_time_key = pd.Timestamp(signal["close_time"]).isoformat()
                dedupe_key = f"{signal['symbol']}|{signal['side']}|{close_time_key}"

                if last_sent_key.get(symbol) == dedupe_key:
                    print(f"{symbol}: ayni sinyal daha once gonderildi")
                    continue

                message = format_signal_message(signal, cfg.interval)
                send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, message)
                last_sent_key[symbol] = dedupe_key
                print(f"{symbol}: {signal['side']} sinyali gonderildi (Guven: {signal['confidence']}%)")

                if paper_trader is not None and not paper_trader.has_open_position(symbol):
                    open_event = paper_trader.open_position(signal)
                    if open_event is not None:
                        open_msg = format_paper_open_message(open_event, cfg.interval)
                        send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, open_msg)
                        print(
                            f"{symbol}: paper pozisyon acildi | {open_event['side']} | "
                            f"Qty: {open_event['quantity']:.6f}"
                        )

            except requests.HTTPError as exc:
                print(f"{symbol}: HTTP hata - {exc}")
            except Exception as exc:
                print(f"{symbol}: beklenmeyen hata - {exc}")

        time.sleep(cfg.scan_every_seconds)


if __name__ == "__main__":
    main()
