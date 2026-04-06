import os
import time
import math
import hmac
import hashlib
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: str
    symbols: list[str]
    intervals: list[str]          # ornek: ["15m", "1h", "4h"]
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
    binance_api_key: str
    binance_api_secret: str
    binance_api_futures_base: str
    use_futures: bool
    futures_order_enabled: bool
    futures_leverage: int
    futures_margin_type: str
    futures_risk_per_trade_pct: float
    max_notional_per_trade: float
    paper_trade_enabled: bool
    paper_initial_balance: float
    paper_risk_per_trade_pct: float
    paper_fee_rate: float
    paper_log_file: str
    signal_state_file: str
    symbol_refresh_hours: int
    whale_vol_multiplier: float


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


def sign_futures_params(secret: str, params: dict) -> str:
    query_string = urlencode(params, doseq=True)
    return hmac.new(secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()


def send_futures_signed_request(cfg: Config, method: str, path: str, params: dict | None = None) -> dict | list:
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query_string = urlencode(params, doseq=True)
    signature = hmac.new(cfg.binance_api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{cfg.binance_api_futures_base}{path}?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": cfg.binance_api_key}

    if method.upper() == "GET":
        response = requests.get(url, headers=headers, timeout=20)
    else:
        response = requests.post(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def get_futures_balance(cfg: Config) -> float:
    data = send_futures_signed_request(cfg, "GET", "/fapi/v2/balance")
    for item in data:
        if item.get("asset") == "USDT":
            return float(item.get("balance", 0.0))
    return 0.0


def set_futures_margin(cfg: Config, symbol: str) -> None:
    try:
        send_futures_signed_request(cfg, "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": cfg.futures_margin_type})
    except Exception as exc:
        print(f"{symbol}: futures margin type ayarlanamadi - {exc}")


def set_futures_leverage(cfg: Config, symbol: str) -> None:
    try:
        send_futures_signed_request(cfg, "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": cfg.futures_leverage})
    except Exception as exc:
        print(f"{symbol}: futures leverage ayarlanamadi - {exc}")


def place_futures_order(cfg: Config, symbol: str, side: str, quantity: float) -> dict:
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": f"{quantity:.6f}",
    }
    return send_futures_signed_request(cfg, "POST", "/fapi/v1/order", params)


def place_futures_close_order(cfg: Config, symbol: str, side: str, stop_price: float, order_type: str) -> dict:
    params = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "stopPrice": f"{stop_price:.8f}",
        "closePosition": "true",
        "reduceOnly": "true",
    }
    return send_futures_signed_request(cfg, "POST", "/fapi/v1/order", params)


def open_futures_position(cfg: Config, signal: dict) -> dict | None:
    if not cfg.futures_order_enabled:
        return None

    if not cfg.binance_api_key or not cfg.binance_api_secret:
        print("Futures API anahtarlariniz eksik, emir acilamadi.")
        return None

    symbol = signal["symbol"]
    entry = float(signal["entry"])
    stop_loss = float(signal["stop_loss"])
    take_profit = float(signal["take_profit"])
    side = "BUY" if signal["side"] == "LONG" else "SELL"
    risk_amount = get_futures_balance(cfg) * (cfg.futures_risk_per_trade_pct / 100.0)
    risk_per_unit = abs(entry - stop_loss)
    if risk_amount <= 0 or risk_per_unit <= 0:
        print(f"{symbol}: futures risk veya sl farki uygun degil.")
        return None

    quantity = risk_amount / risk_per_unit
    if quantity <= 0:
        return None

    # Limit notional exposure
    notional = quantity * entry
    if cfg.max_notional_per_trade > 0 and notional > cfg.max_notional_per_trade:
        quantity = cfg.max_notional_per_trade / entry

    quantity = float(f"{quantity:.3f}")
    if quantity <= 0:
        return None

    set_futures_margin(cfg, symbol)
    set_futures_leverage(cfg, symbol)

    try:
        market_order = place_futures_order(cfg, symbol, side, quantity)
        tp_side = "SELL" if side == "BUY" else "BUY"
        sl_order = place_futures_close_order(cfg, symbol, tp_side, stop_loss, "STOP_MARKET")
        tp_order = place_futures_close_order(cfg, symbol, tp_side, take_profit, "TAKE_PROFIT_MARKET")

        return {
            "symbol": symbol,
            "side": signal["side"],
            "quantity": quantity,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "order_id": market_order.get("orderId"),
            "sl_order_id": sl_order.get("orderId"),
            "tp_order_id": tp_order.get("orderId"),
            "leverage": cfg.futures_leverage,
            "order": market_order,
            "stop_order": sl_order,
            "take_profit_order": tp_order,
        }
    except Exception as exc:
        print(f"{symbol}: futures emir acma hatasi - {exc}")
        return None


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

    def has_open_position(self, key: str) -> bool:
        """key = 'SYMBOL_interval' ornegi 'ARBUSDT_1h'"""
        return key in self.open_positions

    def open_position(self, signal: dict, key: str) -> dict | None:
        """key = 'SYMBOL_interval'"""
        if key in self.open_positions:
            return None
        symbol = signal["symbol"]

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
        self.open_positions[key] = position

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

    def update_position(self, key: str, candle_row: pd.Series) -> dict | None:
        """key = 'SYMBOL_interval'"""
        position = self.open_positions.get(key)
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
        self.open_positions.pop(key, None)
        return trade


def load_config() -> Config:
    load_dotenv()
    symbols_raw = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    intervals_raw = os.getenv("INTERVALS", "15m,1h,4h")
    intervals = [i.strip() for i in intervals_raw.split(",") if i.strip()]
    if not intervals:
        intervals = ["15m", "1h", "4h"]

    return Config(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        symbols=symbols,
        intervals=intervals,
        scan_every_seconds=int(os.getenv("SCAN_EVERY_SECONDS", "60")),
        binance_api_base=os.getenv("BINANCE_API_BASE", "https://api.binance.com").strip(),
        lookback_bars=int(os.getenv("LOOKBACK_BARS", "250")),
        fib_lookback=int(os.getenv("FIB_LOOKBACK", "120")),
        risk_reward=float(os.getenv("RISK_REWARD", "2.0")),
        min_confidence=int(os.getenv("MIN_CONFIDENCE", "70")),
        max_price_usd=float(os.getenv("MAX_PRICE_USD", "100")),
        use_dynamic_symbols=parse_bool(os.getenv("USE_DYNAMIC_SYMBOLS", "true"), default=True),
        min_listing_year=int(os.getenv("MIN_LISTING_YEAR", "2021")),
        min_quote_volume_usd=float(os.getenv("MIN_QUOTE_VOLUME_USD", "100000")),
        max_quote_volume_usd=float(os.getenv("MAX_QUOTE_VOLUME_USD", "25000000")),
        dynamic_symbol_limit=int(os.getenv("DYNAMIC_SYMBOL_LIMIT", "120")),
        binance_api_key=os.getenv("BINANCE_API_KEY", "").strip(),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
        binance_api_futures_base=os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com").strip(),
        use_futures=parse_bool(os.getenv("USE_FUTURES", "true"), default=True),
        futures_order_enabled=parse_bool(os.getenv("FUTURES_ORDER_ENABLED", "false"), default=False),
        futures_leverage=int(os.getenv("FUTURES_LEVERAGE", "10")),
        futures_margin_type=os.getenv("FUTURES_MARGIN_TYPE", "ISOLATED").strip().upper(),
        futures_risk_per_trade_pct=float(os.getenv("FUTURES_RISK_PER_TRADE_PCT", "1.0")),
        max_notional_per_trade=float(os.getenv("MAX_NOTIONAL_PER_TRADE", "1000")),
        paper_trade_enabled=parse_bool(os.getenv("PAPER_TRADE_ENABLED", "true"), default=True),
        paper_initial_balance=float(os.getenv("PAPER_INITIAL_BALANCE", "10000")),
        paper_risk_per_trade_pct=float(os.getenv("PAPER_RISK_PER_TRADE_PCT", "1.0")),
        paper_fee_rate=float(os.getenv("PAPER_FEE_RATE", "0.0004")),
        paper_log_file=os.getenv("PAPER_LOG_FILE", "paper_trades.csv").strip(),
        signal_state_file=os.getenv("SIGNAL_STATE_FILE", "signal_state.json").strip(),
        symbol_refresh_hours=int(os.getenv("SYMBOL_REFRESH_HOURS", "6")),
        whale_vol_multiplier=float(os.getenv("WHALE_VOL_MULTIPLIER", "4.0")),
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


# Momentum onbellegi (5 dakika TTL)
_momentum_cache: tuple[float, dict[str, float]] = (0.0, {})
_MOMENTUM_CACHE_TTL = 300.0


def fetch_symbol_momentum(base_url: str) -> dict[str, float]:
    """24 saatlik fiyat degisim yuzdesini sembol bazinda dondurur (5 dk onbellek).
    Sifir donus: API hatasi veya onbellek bos.
    """
    global _momentum_cache
    now = time.monotonic()
    if now - _momentum_cache[0] < _MOMENTUM_CACHE_TTL and _momentum_cache[1]:
        return _momentum_cache[1]
    try:
        resp = requests.get(f"{base_url}/api/v3/ticker/24hr", timeout=15)
        resp.raise_for_status()
        mapping = {
            row["symbol"]: float(row.get("priceChangePercent", 0.0))
            for row in resp.json()
            if isinstance(row, dict) and "symbol" in row
        }
        _momentum_cache = (now, mapping)
        return mapping
    except Exception as exc:
        print(f"[WARN] Momentum verisi alinamadi: {exc}")
        return _momentum_cache[1]


def is_leveraged_token(symbol: str) -> bool:
    suffixes = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    return symbol.endswith(suffixes)


_STABLECOIN_BASES: frozenset[str] = frozenset({
    "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "USDD",
    "SUSD", "FRAX", "GUSD", "LUSD", "MIM", "USDN", "USDJ",
    "HUSD", "USDK", "VAI", "USTC", "UST", "CUSD", "EURC",
})


def is_stablecoin(base_asset: str) -> bool:
    """Dolar veya euro endeksli sabit coinleri filtreler (isim bazli)."""
    return base_asset.upper() in _STABLECOIN_BASES


# Futures sembol seti onbellegi (12 saatte bir yenilenir)
_futures_symbols_cache: tuple[float, frozenset[str]] = (0.0, frozenset())
_FUTURES_SYMBOLS_TTL = 12 * 3600.0


def fetch_futures_symbols(futures_base_url: str) -> frozenset[str]:
    """Binance Futures'ta aktif olarak islem goren USDT-marjin sembol setini dondurur.
    Sonuclar 12 saat onbelleklenir; hata durumunda bos set yerine son bilinen set kullanilir.
    """
    global _futures_symbols_cache
    now = time.monotonic()
    if now - _futures_symbols_cache[0] < _FUTURES_SYMBOLS_TTL and _futures_symbols_cache[1]:
        return _futures_symbols_cache[1]
    try:
        resp = requests.get(f"{futures_base_url}/fapi/v1/exchangeInfo", timeout=15)
        resp.raise_for_status()
        symbols = frozenset(
            s["symbol"]
            for s in resp.json().get("symbols", [])
            if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
        )
        _futures_symbols_cache = (now, symbols)
        print(f"[INFO] Futures sembol listesi guncellendi: {len(symbols)} coin")
        return symbols
    except Exception as exc:
        print(f"[WARN] Futures sembol listesi alinamadi: {exc}")
        return _futures_symbols_cache[1]


# Funding rate onbellegi: symbol -> (monotonic_ts, rate_float)
_funding_cache: dict[str, tuple[float, float]] = {}
_FUNDING_CACHE_TTL = 300.0  # 5 dakika


def fetch_funding_rate(symbol: str, futures_base_url: str) -> float | None:
    """Guncel funding rate'i dondurur (5 dk onbellek).
    Pozitif: longlar short'lara oduyor (asiri long -> zayif boga/short firsat).
    Negatif: shortlar longlara oduyor (asiri short -> zayif ayi/long firsat).
    """
    now = time.monotonic()
    if symbol in _funding_cache:
        ts, rate = _funding_cache[symbol]
        if now - ts < _FUNDING_CACHE_TTL:
            return rate
    try:
        resp = requests.get(
            f"{futures_base_url}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        rate = float(resp.json().get("lastFundingRate", 0.0))
        _funding_cache[symbol] = (now, rate)
        return rate
    except Exception:
        cached = _funding_cache.get(symbol)
        return cached[1] if cached else None


def _base_from_symbol(symbol: str) -> str:
    """Sembol adından baz varlığı çıkarır (BTCUSDT → BTC, TUSDUSDT → TUSD)."""
    for quote in ("USDT", "BUSD", "USDC", "BTC", "ETH", "BNB"):
        if symbol.endswith(quote):
            return symbol[:-len(quote)]
    return symbol


def _filter_static_list(symbols: list[str]) -> list[str]:
    """Statik sembol listesinden stablecoin ve kaldiraçlı token'ları temizler."""
    filtered = []
    for s in symbols:
        base = _base_from_symbol(s)
        if is_leveraged_token(s) or is_stablecoin(base):
            print(f"[INFO] Statik listeden filtrelendi: {s}")
            continue
        filtered.append(s)
    return filtered


def resolve_symbols(cfg: Config) -> list[str]:
    if not cfg.use_dynamic_symbols:
        return _filter_static_list(cfg.symbols)

    try:
        exchange_info = fetch_exchange_info(cfg.binance_api_base)
        ticker_24h = fetch_ticker_24hr(cfg.binance_api_base)
    except Exception as exc:
        print(f"[WARN] Dinamik sembol listesi alinamadi: {exc}")
        return _filter_static_list(cfg.symbols)

    # Futures'ta listeli olmayan coinlere sinyal verme
    futures_symbols = fetch_futures_symbols(cfg.binance_api_futures_base)
    futures_filter_active = bool(futures_symbols)

    ticker_map: dict[str, dict] = {row.get("symbol", ""): row for row in ticker_24h}
    min_listing_ts = int(datetime(cfg.min_listing_year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

    candidates: list[tuple[str, float]] = []
    for item in exchange_info.get("symbols", []):
        symbol = str(item.get("symbol", "")).upper()
        base_asset = str(item.get("baseAsset", "")).upper()
        if not symbol or is_leveraged_token(symbol) or is_stablecoin(base_asset):
            continue

        # Futures'ta listelenmeyen coinleri atla
        if futures_filter_active and symbol not in futures_symbols:
            continue

        if item.get("status") != "TRADING":
            continue

        if str(item.get("quoteAsset", "")).upper() != "USDT":
            continue

        if not bool(item.get("isSpotTradingAllowed", True)):
            continue

        onboard_date = int(item.get("onboardDate", 0) or 0)
        # onboardDate artik Binance API tarafindan cogunlukla 0 olarak donuyor,
        # bu durumda tarihe gore filtreleme yapamayiz; atlamamak icin devam ediyoruz.
        if onboard_date != 0 and onboard_date < min_listing_ts:
            continue

        ticker = ticker_map.get(symbol)
        if not ticker:
            continue

        try:
            last_price = float(ticker.get("lastPrice", 0.0))
            quote_volume = float(ticker.get("quoteVolume", 0.0))
            high_24h = float(ticker.get("highPrice", 0.0))
            low_24h = float(ticker.get("lowPrice", 0.0))
        except (TypeError, ValueError):
            continue

        if last_price <= 0 or last_price > cfg.max_price_usd:
            continue

        # 24 saatlik fiyat hareketi %0.5'ten azsa sabit coin gibi davraniyordur – atla.
        if (high_24h - low_24h) / last_price < 0.005:
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
            f"Hacim:[{cfg.min_quote_volume_usd:.0f}, {cfg.max_quote_volume_usd:.0f}] | "
            f"Futures filtresi: {'ACIK' if futures_filter_active else 'KAPALI'}"
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


def compute_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume: birikmeli alici/satici basinci."""
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def compute_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD cizgisi, sinyal cizgisi ve histogram dondurur."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_bollinger(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: (upper, mid, lower) dondurur."""
    mid = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index (ADX). 25+ = guclu trend, <20 = yatay piyasa."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()

    pos_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    neg_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )

    alpha = 1.0 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    pos_di = 100 * pos_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w.replace(0, np.nan)
    neg_di = 100 * neg_dm.ewm(alpha=alpha, adjust=False).mean() / atr_w.replace(0, np.nan)

    di_sum = (pos_di + neg_di).replace(0, np.nan)
    dx = (100 * (pos_di - neg_di).abs() / di_sum).fillna(0)
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx


def compute_supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[pd.Series, pd.Series]:
    """SuperTrend indikatoru. (st_line, st_bull) dondurur.
    st_bull = True: fiyat ST'nin uzerinde (yukari yonelim).
    """
    atr = compute_atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    basic_upper = (hl2 + multiplier * atr).values
    basic_lower = (hl2 - multiplier * atr).values
    close_arr = df["close"].values
    n = len(df)

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    st = np.full(n, np.nan)
    bull = np.zeros(n, dtype=bool)

    for i in range(1, n):
        if basic_upper[i] < final_upper[i - 1] or close_arr[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]

        if basic_lower[i] > final_lower[i - 1] or close_arr[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]

        if np.isnan(st[i - 1]):
            st[i] = final_upper[i]
            bull[i] = close_arr[i] > st[i]
        elif st[i - 1] == final_upper[i - 1]:
            if close_arr[i] > final_upper[i]:
                st[i] = final_lower[i]
                bull[i] = True
            else:
                st[i] = final_upper[i]
                bull[i] = False
        else:
            if close_arr[i] < final_lower[i]:
                st[i] = final_upper[i]
                bull[i] = False
            else:
                st[i] = final_lower[i]
                bull[i] = True

    return pd.Series(st, index=df.index), pd.Series(bull, index=df.index)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()
    out["rsi14"] = compute_rsi(out["close"], period=14)
    out["atr14"] = compute_atr(out, period=14)
    out["vol_ma20"] = out["volume"].rolling(20).mean()
    macd_line, macd_sig, macd_hist = compute_macd(out["close"])
    out["macd"] = macd_line
    out["macd_signal"] = macd_sig
    out["macd_hist"] = macd_hist
    bb_upper, bb_mid, bb_lower = compute_bollinger(out["close"])
    out["bb_upper"] = bb_upper
    out["bb_mid"] = bb_mid
    out["bb_lower"] = bb_lower
    out["obv"] = compute_obv(out)
    out["obv_ma20"] = out["obv"].rolling(20).mean()
    out["adx14"] = compute_adx(out, period=14)
    # BB band genisligi (squeeze/breakout tespiti icin)
    out["bb_bw"] = (out["bb_upper"] - out["bb_lower"]) / out["bb_mid"].replace(0, np.nan)
    out["bb_bw_ma20"] = out["bb_bw"].rolling(20).mean()
    out["bb_bw_min10"] = out["bb_bw"].rolling(10).min()
    st_line, st_bull = compute_supertrend(out, period=10, multiplier=3.0)
    out["st_line"] = st_line
    out["st_bull"] = st_bull
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


def compute_pivots(
    high: pd.Series,
    low: pd.Series,
    left: int = 5,
    right: int = 5,
) -> list[tuple[int, float, str]]:
    """Pivot Yuksek ('H') ve Pivot Dusuk ('L') noktalarini saptar.
    Bir bar, sol 'left' ve sag 'right' bardan daha yuksek/dusukse pivot kabul edilir.
    """
    h = high.values
    l = low.values
    n = len(h)
    pivots: list[tuple[int, float, str]] = []
    for i in range(left, n - right):
        h_window = np.concatenate([h[i - left:i], h[i + 1:i + right + 1]])
        if h[i] >= h_window.max():
            pivots.append((i, float(h[i]), "H"))
            continue
        l_window = np.concatenate([l[i - left:i], l[i + 1:i + right + 1]])
        if l[i] <= l_window.min():
            pivots.append((i, float(l[i]), "L"))
    return pivots


def detect_market_structure(
    df: pd.DataFrame,
    lookback: int = 120,
    left: int = 5,
    right: int = 5,
) -> dict:
    """Piyasa yapisini ve trend kirilimini (Break of Structure) saptar.

    Donus anahtarlari:
      structure      : 'BULLISH' | 'BEARISH' | 'RANGING'
      bos_bull       : True → ayı yapısında fiyat son Lower High'ı kırdı (trend donusu)
      bos_bear       : True → boğa yapısında fiyat son Higher Low'un altına kırdı
      last_resistance: son pivot yuksek degeri
      last_support   : son pivot dusuk degeri
    """
    _default: dict = {
        "structure": "RANGING",
        "bos_bull": False,
        "bos_bear": False,
        "last_resistance": None,
        "last_support": None,
    }
    analysis_df = df.iloc[-(lookback + right + 2):-2]
    if len(analysis_df) < left + right + 2:
        return _default

    pivots = compute_pivots(analysis_df["high"], analysis_df["low"], left=left, right=right)
    if len(pivots) < 4:
        return _default

    highs = [(idx, v) for idx, v, t in pivots if t == "H"]
    lows = [(idx, v) for idx, v, t in pivots if t == "L"]

    if len(highs) < 2 or len(lows) < 2:
        return {
            **_default,
            "last_resistance": highs[-1][1] if highs else None,
            "last_support": lows[-1][1] if lows else None,
        }

    prev_high, last_high = highs[-2][1], highs[-1][1]
    prev_low, last_low = lows[-2][1], lows[-1][1]

    hh = last_high > prev_high   # Higher High
    hl = last_low > prev_low     # Higher Low
    lh = last_high < prev_high   # Lower High
    ll = last_low < prev_low     # Lower Low

    if hh and hl:
        structure = "BULLISH"
    elif lh and ll:
        structure = "BEARISH"
    else:
        structure = "RANGING"

    close = float(df.iloc[-2]["close"])
    # BOS Boga: ayi yapisinda fiyat son LH'yi gecti → trend kirilimi
    bos_bull = lh and close > last_high
    # BOS Ayi: boga yapisinda fiyat son HL'in altina kırıldı → trend kirilimi
    bos_bear = hl and close < last_low

    return {
        "structure": structure,
        "bos_bull": bos_bull,
        "bos_bear": bos_bear,
        "last_resistance": last_high,
        "last_support": last_low,
    }


def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 30) -> str:
    """Son 'lookback' bardaki RSI-fiyat diverjansini tespit eder.
    Donen deger: 'BULL' | 'BEAR' | ''
      BULL: Fiyat LL yaparken RSI HL yapiyor → boga diverjans (zayifliyan satis baskisi)
      BEAR: Fiyat HH yaparken RSI LH yapiyor → ayi diverjans (zayiflayan alis baskisi)
    """
    window = df.iloc[-(lookback + 2):-2]
    if len(window) < 10:
        return ""

    close = window["close"].values
    rsi = window["rsi14"].values

    # Son iki lokal dip/zirve bulmak icin basit karsilastirma
    close_min1_idx = int(np.argmin(close[: len(close) // 2]))
    close_min2_idx = int(len(close) // 2 + np.argmin(close[len(close) // 2:]))
    close_max1_idx = int(np.argmax(close[: len(close) // 2]))
    close_max2_idx = int(len(close) // 2 + np.argmax(close[len(close) // 2:]))

    # Boga diverjans: fiyat daha dusuk dip, RSI daha yuksek dip
    if (close[close_min2_idx] < close[close_min1_idx]
            and rsi[close_min2_idx] > rsi[close_min1_idx] + 2):
        return "BULL"

    # Ayi diverjans: fiyat daha yuksek zirve, RSI daha dusuk zirve
    if (close[close_max2_idx] > close[close_max1_idx]
            and rsi[close_max2_idx] < rsi[close_max1_idx] - 2):
        return "BEAR"

    return ""


def detect_candle_patterns(row: pd.Series, prev_row: pd.Series) -> list[str]:
    """Son kapanan mumun teknik formasyonlarini saptar (OHLCV).
    Donen liste: eslesenlerin Turkce adlari.
    """
    patterns: list[str] = []
    o = float(row["open"])
    h = float(row["high"])
    l = float(row["low"])
    c = float(row["close"])
    po = float(prev_row["open"])
    ph = float(prev_row["high"])
    pl = float(prev_row["low"])
    pc = float(prev_row["close"])

    body = abs(c - o)
    candle_range = h - l
    if candle_range <= 0:
        return patterns

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # Doji: govde <= %5 aralik — belirsizlik
    if body / candle_range <= 0.05:
        patterns.append("Doji")
        return patterns   # doji tespit edildi, diger formasyonlara bakilmaz

    # Hammer (boğa): yesil govde, alt fitil >= 2× govde, ust fitil kucuk
    if c > o and lower_wick >= 2.0 * body and upper_wick <= 0.3 * body:
        patterns.append("Hammer")

    # Shooting Star (ayi): kirmizi govde, ust fitil >= 2× govde, alt fitil kucuk
    if c < o and upper_wick >= 2.0 * body and lower_wick <= 0.3 * body:
        patterns.append("Shooting Star")

    # Boga Yutmasi: onceki mum kirmizi, simdiki yesil ve oncekini tamamen yutuyor
    if pc < po and c > o and o <= pc and c >= po:
        patterns.append("Boga Yutmasi")

    # Ayi Yutmasi: onceki mum yesil, simdiki kirmizi ve oncekini tamamen yutuyor
    if pc > po and c < o and o >= pc and c <= po:
        patterns.append("Ayi Yutmasi")

    return patterns


def build_signal(symbol: str, df: pd.DataFrame, cfg: Config) -> dict | None:
    if len(df) < max(80, cfg.fib_lookback + 5):
        return None

    # Use last closed candle to avoid false trigger from live candle noise.
    row = df.iloc[-2]
    prev_row = df.iloc[-3]
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
    ema200 = float(row["ema200"])
    macd_hist_val = float(row["macd_hist"])
    bb_mid_val = float(row["bb_mid"])
    obv_val = float(row["obv"])
    obv_ma_val = float(row["obv_ma20"]) if not math.isnan(float(row["obv_ma20"])) else obv_val
    adx_val = float(row["adx14"]) if not math.isnan(float(row["adx14"])) else 0.0
    bb_bw_val = float(row["bb_bw"]) if not math.isnan(float(row["bb_bw"])) else 0.0
    bb_bw_ma_val = float(row["bb_bw_ma20"]) if not math.isnan(float(row["bb_bw_ma20"])) else 0.0
    bb_bw_min_val = float(row["bb_bw_min10"]) if not math.isnan(float(row["bb_bw_min10"])) else 0.0
    st_bull_val = bool(row["st_bull"])
    st_bear_val = not st_bull_val
    st_line_val = float(row["st_line"]) if not math.isnan(float(row["st_line"])) else 0.0

    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    range_size = swing_high - swing_low
    if range_size <= 0:
        return None

    # ADX hard gate: <18 ise piyasa yatay — sinyal uretme
    if adx_val < 18:
        return None

    # Tam EMA stack gerekli (yatay veya karisik EMA'larda sinyal yok)
    uptrend = close > ema20 > ema50 > ema200
    downtrend = close < ema20 < ema50 < ema200
    # Volume: 1.3x ortalama (gercek hareket olan coinleri sec)
    volume_ok = vol_ma20 > 0 and volume > 1.3 * vol_ma20
    candle_patterns = detect_candle_patterns(row, prev_row)
    ms = detect_market_structure(df)
    rsi_div = detect_rsi_divergence(df)
    funding_rate = fetch_funding_rate(symbol, cfg.binance_api_futures_base)

    if uptrend and st_bull_val:
        fib = fib_levels_from_swing(swing_low, swing_high)
        fib_ok = in_zone(close, fib["0.5"], fib["0.618"])
        rsi_ok = 42 <= rsi <= 70
        atr_ok = atr > 0

        macd_bull = not math.isnan(macd_hist_val) and macd_hist_val > 0
        bb_pullback = not math.isnan(bb_mid_val) and close <= bb_mid_val
        whale_alert = vol_ma20 > 0 and volume > cfg.whale_vol_multiplier * vol_ma20
        obv_rising = obv_val > obv_ma_val
        bullish_candle = any(p in candle_patterns for p in ("Hammer", "Boga Yutmasi"))
        bull_structure = ms["structure"] == "BULLISH"
        bos_signal = ms["bos_bull"]
        bull_div = rsi_div == "BULL"
        adx_strong = adx_val >= 25
        bb_squeeze_break = (
            bb_bw_ma_val > 0
            and bb_bw_min_val < bb_bw_ma_val * 0.75
            and bb_bw_val > bb_bw_min_val * 1.15
        )
        funding_ok_long = funding_rate is None or funding_rate <= 0.0005

        score = score_signal(
            base_conditions=[uptrend, st_bull_val, rsi_ok],
            bonus_conditions=[fib_ok, close > fib["0.382"], volume_ok, atr_ok, macd_bull, bb_pullback, whale_alert, obv_rising, bullish_candle, bull_structure, bos_signal, bull_div, adx_strong, bb_squeeze_break, funding_ok_long],
        )

        if score >= cfg.min_confidence and rsi_ok and atr_ok and volume_ok:
            entry = close
            # SL: SuperTrend cizgisi zemin gorevini goruyor; ATR bandi ile en yakin olan
            raw_sl = entry - 1.5 * atr
            stop_loss = max(st_line_val, raw_sl) if st_line_val > 0 and st_line_val < entry else raw_sl
            if stop_loss >= entry:
                stop_loss = entry - 1.5 * atr
            take_profit = entry + (entry - stop_loss) * cfg.risk_reward
            rr = (take_profit - entry) / (entry - stop_loss) if entry > stop_loss else 0.0

            note_parts = ["EMA Stack", "SuperTrend Bull"]
            if adx_strong:
                note_parts.append("ADX Guclu")
            if bb_squeeze_break:
                note_parts.append("BB Squeeze/Breakout")
            if fib_ok:
                note_parts.append("Fib 0.5-0.618")
            if macd_bull:
                note_parts.append("MACD+")
            if bb_pullback:
                note_parts.append("BB alt yari")
            if whale_alert:
                note_parts.append("Balina Hacmi")
            if obv_rising:
                note_parts.append("OBV+")
            if bullish_candle:
                note_parts.append(" | ".join(candle_patterns))
            if bull_structure:
                note_parts.append("Boga Yapisi")
            if bos_signal:
                note_parts.append("Trend Kirilimi")
            if bull_div:
                note_parts.append("RSI Boga Diverjans")
            funding_str = f"{funding_rate*100:.4f}%" if funding_rate is not None else "?"

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
                "macd_hist": 0.0 if math.isnan(macd_hist_val) else macd_hist_val,
                "market_structure": ms["structure"],
                "bos": ms["bos_bull"],
                "funding_rate": funding_str,
                "supertrend": "Bull",
                "notes": " | ".join(note_parts),
            }

    if downtrend and st_bear_val:
        fib = short_fib_levels_from_swing(swing_low, swing_high)
        fib_ok = in_zone(close, fib["0.5"], fib["0.618"])
        rsi_ok = 30 <= rsi <= 58
        atr_ok = atr > 0

        macd_bear = not math.isnan(macd_hist_val) and macd_hist_val < 0
        bb_rejection = not math.isnan(bb_mid_val) and close >= bb_mid_val
        whale_alert = vol_ma20 > 0 and volume > cfg.whale_vol_multiplier * vol_ma20
        obv_falling = obv_val < obv_ma_val
        bearish_candle = any(p in candle_patterns for p in ("Shooting Star", "Ayi Yutmasi"))
        bear_structure = ms["structure"] == "BEARISH"
        bos_signal = ms["bos_bear"]
        bear_div = rsi_div == "BEAR"
        adx_strong = adx_val >= 25
        bb_squeeze_break = (
            bb_bw_ma_val > 0
            and bb_bw_min_val < bb_bw_ma_val * 0.75
            and bb_bw_val > bb_bw_min_val * 1.15
        )
        funding_ok_short = funding_rate is None or funding_rate >= -0.0005

        score = score_signal(
            base_conditions=[downtrend, st_bear_val, rsi_ok],
            bonus_conditions=[fib_ok, close < fib["0.382"], volume_ok, atr_ok, macd_bear, bb_rejection, whale_alert, obv_falling, bearish_candle, bear_structure, bos_signal, bear_div, adx_strong, bb_squeeze_break, funding_ok_short],
        )

        if score >= cfg.min_confidence and rsi_ok and atr_ok and volume_ok:
            entry = close
            raw_sl = entry + 1.5 * atr
            stop_loss = min(st_line_val, raw_sl) if st_line_val > entry else raw_sl
            if stop_loss <= entry:
                stop_loss = entry + 1.5 * atr
            take_profit = entry - (stop_loss - entry) * cfg.risk_reward
            rr = (entry - take_profit) / (stop_loss - entry) if stop_loss > entry else 0.0

            note_parts = ["EMA Stack", "SuperTrend Bear"]
            if adx_strong:
                note_parts.append("ADX Guclu")
            if bb_squeeze_break:
                note_parts.append("BB Squeeze/Breakout")
            if fib_ok:
                note_parts.append("Fib 0.5-0.618")
            if macd_bear:
                note_parts.append("MACD-")
            if bb_rejection:
                note_parts.append("BB ust yari")
            if whale_alert:
                note_parts.append("Balina Hacmi")
            if obv_falling:
                note_parts.append("OBV-")
            if bearish_candle:
                note_parts.append(" | ".join(candle_patterns))
            if bear_structure:
                note_parts.append("Ayi Yapisi")
            if bos_signal:
                note_parts.append("Trend Kirilimi")
            if bear_div:
                note_parts.append("RSI Ayi Diverjans")
            funding_str = f"{funding_rate*100:.4f}%" if funding_rate is not None else "?"

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
                "macd_hist": 0.0 if math.isnan(macd_hist_val) else macd_hist_val,
                "market_structure": ms["structure"],
                "bos": ms["bos_bear"],
                "funding_rate": funding_str,
                "supertrend": "Bear",
                "notes": " | ".join(note_parts),
            }

    return None


# Zaman dilimleri kucukten buyuge sira numarasi
_INTERVAL_RANK: dict[str, int] = {
    "1m": 0, "3m": 1, "5m": 2, "15m": 3, "30m": 4,
    "1h": 5, "2h": 6, "4h": 7, "6h": 8, "8h": 9, "12h": 10, "1d": 11,
}


def check_mtf_alignment(
    side: str,
    signal_interval: str,
    dfs: dict[str, pd.DataFrame],
) -> tuple[bool, str]:
    """Sinyalin TF'sinden daha buyuk zaman dilimlerinin trendi teyit edip etmedigini kontrol eder.
    Doner: (hizalanmis, aciklama)
      True  -> tum ust TF'ler ayni yonü gosteriyor VEYA kontrol edilecek ust TF yok
      False -> en az bir ust TF zit trendde
    """
    signal_rank = _INTERVAL_RANK.get(signal_interval, 99)
    confirmations: list[str] = []
    conflicts: list[str] = []

    for tf, df in dfs.items():
        tf_rank = _INTERVAL_RANK.get(tf, -1)
        if tf_rank <= signal_rank or len(df) < 3:
            continue
        row = df.iloc[-2]
        ema20 = float(row["ema20"])
        ema50 = float(row["ema50"])
        if side == "LONG":
            if ema20 > ema50:
                confirmations.append(tf)
            elif ema20 < ema50:
                conflicts.append(tf)
        else:
            if ema20 < ema50:
                confirmations.append(tf)
            elif ema20 > ema50:
                conflicts.append(tf)

    if conflicts:
        return False, f"Zit TF: {','.join(sorted(conflicts))}"
    if confirmations:
        return True, f"MTF Hizali({','.join(sorted(confirmations))})"
    return True, ""


_INTERVAL_CATEGORY = {
    "1m":  "Scalp", "3m":  "Scalp",  "5m":  "Scalp",  "15m": "Scalp",
    "30m": "Intraday", "1h": "Intraday", "2h": "Intraday",
    "4h": "Swing",  "6h": "Swing",   "8h": "Swing",
    "12h": "Swing",  "1d": "Swing",
}

_CATEGORY_EMOJI = {
    "Scalp":    "\u26a1",   # ⚡
    "Intraday": "\U0001f4c6",  # 📆
    "Swing":    "\U0001f4c8",  # 📈
}


def signal_category(interval: str) -> str:
    return _INTERVAL_CATEGORY.get(interval, "Diger")


def format_signal_message(signal: dict, interval: str) -> str:
    dt = signal["close_time"]
    if isinstance(dt, pd.Timestamp):
        dt_str = dt.tz_convert(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    else:
        dt_str = str(dt)

    cat = signal_category(interval)
    cat_emoji = _CATEGORY_EMOJI.get(cat, "\U0001f4cc")
    return (
        f"*{cat_emoji} {cat} Sinyali*\n"
        f"Sembol: *{signal['symbol']}*\n"
        f"Yon: *{signal['side']}*\n"
        f"Zaman Dilimi: *{interval}*\n"
        f"Entry: `{signal['entry']:.4f}`\n"
        f"SL: `{signal['stop_loss']:.4f}`\n"
        f"TP: `{signal['take_profit']:.4f}`\n"
        f"Tahmini R/R: `{signal['rr']:.2f}`\n"
        f"RSI: `{signal['rsi']:.1f}`\n"
        f"MACD Hist: `{signal.get('macd_hist', 0):.6f}`\n"
        f"SuperTrend: `{signal.get('supertrend', '?')}`\n"
        f"Yapi: `{signal.get('market_structure', '?')}`{'  *[BOS]*' if signal.get('bos') else ''}\n"
        f"Funding: `{signal.get('funding_rate', '?')}`\n"
        f"Guven: *{signal['confidence']}%*\n"
        f"Korku/Acgozluluk: `{signal.get('fng', '?')}`\n"
        f"Not: {signal['notes']}\n"
        f"Kapanis: {dt_str}"
    )


def format_futures_order_message(order_event: dict, interval: str) -> str:
    return (
        "*Futures Pozisyonu Acildi*\n"
        f"Sembol: *{order_event['symbol']}*\n"
        f"Yon: *{order_event['side']}*\n"
        f"Zaman Dilimi: *{interval}*\n"
        f"Qty: `{order_event['quantity']:.3f}`\n"
        f"Entry: `{order_event['entry']:.4f}`\n"
        f"SL: `{order_event['stop_loss']:.4f}`\n"
        f"TP: `{order_event['take_profit']:.4f}`\n"
        f"Leverage: `{order_event['leverage']}x`\n"
        f"Order Id: `{order_event['order_id']}`\n"
        f"TP Order: `{order_event['tp_order_id']}`\n"
        f"SL Order: `{order_event['sl_order_id']}`"
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


# Fear & Greed Index onbellegi (alternative.me - ucretsiz, auth yok)
_fng_cache: tuple[float, str] = (0.0, "")
_FNG_CACHE_TTL = 3600.0  # gunluk guncelleniyor; 1 saatte bir kontrol yeterli


def fetch_fear_and_greed() -> str:
    """Alternative.me Fear & Greed Index'i getirir.
    Ornek: '72 (Açgözlülük)'. Hata durumunda son bilinen degeri dondurur.
    """
    global _fng_cache
    now = time.monotonic()
    if now - _fng_cache[0] < _FNG_CACHE_TTL and _fng_cache[1]:
        return _fng_cache[1]
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/",
            params={"limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        entry = resp.json()["data"][0]
        value = entry["value"]
        label = entry["value_classification"]
        text = f"{value} ({label})"
        _fng_cache = (now, text)
        return text
    except Exception as exc:
        print(f"[WARN] Fear & Greed alinamadi: {exc}")
        return _fng_cache[1] or "?"


def load_signal_state(path: str) -> dict[str, str]:
    """Son gonderilen sinyal anahtarlarini diskten yukle."""
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_signal_state(path: str, state: dict[str, str]) -> None:
    """Son gonderilen sinyal anahtarlarini diske kaydet."""
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as exc:
        print(f"[WARN] Sinyal durumu kaydedilemedi: {exc}")


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


# ---------------------------------------------------------------------------
# Telegram komut dinleyici (/status, /durum, /yardim)
# ---------------------------------------------------------------------------

_tg_update_offset: int = 0


def format_portfolio_status(paper_trader: "PaperTrader", last_prices: dict) -> str:
    """Tum acik paper pozisyonlari ve portfoy ozetini Markdown olarak dondurur."""
    balance_change_pct = (
        (paper_trader.balance - paper_trader.initial_balance)
        / paper_trader.initial_balance
        * 100
    )
    win_rate = (
        (paper_trader.wins / paper_trader.total_trades * 100)
        if paper_trader.total_trades > 0
        else 0.0
    )

    lines = [
        "\U0001f4ca *Portfoy Durumu*",
        f"\U0001f4b0 Bakiye: `{paper_trader.balance:.2f}` USDT ({balance_change_pct:+.1f}%)",
        (
            f"\U0001f4c8 Toplam: {paper_trader.total_trades} islem | "
            f"Kazanc: {paper_trader.wins} | Kayip: {paper_trader.losses} | "
            f"WR: *{win_rate:.1f}%*"
        ),
        f"\U0001f4c9 Max Drawdown: `{paper_trader.max_drawdown_pct:.2f}%`",
    ]

    open_pos = list(paper_trader.open_positions.items())
    if not open_pos:
        lines.append("\n_Acik pozisyon yok._")
    else:
        lines.append(f"\n\U0001f513 *Acik Pozisyonlar* ({len(open_pos)})")
        now_utc = datetime.now(timezone.utc)

        # Pozisyonlari kategoriye gore grupla: Scalp → Intraday → Swing → Diger
        def _pos_sort_key(item: tuple) -> tuple:
            key, pos = item
            parts = key.rsplit("_", 1)
            iv = parts[1] if len(parts) == 2 else "?"
            cat_order = {"Scalp": 0, "Intraday": 1, "Swing": 2, "Diger": 3}
            return (cat_order.get(signal_category(iv), 3), pos.side, pos.symbol)

        open_pos.sort(key=_pos_sort_key)

        prev_cat = None
        for i, (key, pos) in enumerate(open_pos, 1):
            parts = key.rsplit("_", 1)
            iv = parts[1] if len(parts) == 2 else "?"
            cat = signal_category(iv)
            if cat != prev_cat:
                cat_emoji = _CATEGORY_EMOJI.get(cat, "\U0001f4cc")
                lines.append(f"\n{cat_emoji} *{cat}*")
                prev_cat = cat

            yon_emoji = "\U0001f7e2" if pos.side == "LONG" else "\U0001f534"
            lines.append(f"\n{i}. {yon_emoji} *{pos.symbol}* {pos.side} [{iv}]")
            lines.append(
                f"   Giris: `{pos.entry:.6g}` | TP: `{pos.take_profit:.6g}` | SL: `{pos.stop_loss:.6g}`"
            )

            cur_price = last_prices.get(pos.symbol)
            if cur_price is not None:
                if pos.side == "LONG":
                    upnl_pct = (cur_price - pos.entry) / pos.entry * 100
                    tp_dist = (pos.take_profit - cur_price) / cur_price * 100
                    sl_dist = (cur_price - pos.stop_loss) / cur_price * 100
                else:
                    upnl_pct = (pos.entry - cur_price) / pos.entry * 100
                    tp_dist = (cur_price - pos.take_profit) / cur_price * 100
                    sl_dist = (pos.stop_loss - cur_price) / cur_price * 100

                profit_emoji = "\U0001f49a" if upnl_pct >= 0 else "\u2764\ufe0f"
                lines.append(
                    f"   Su An: `{cur_price:.6g}` | {profit_emoji} Kar: `{upnl_pct:+.2f}%`"
                )
                lines.append(f"   TP'ye: `{tp_dist:.2f}%` | SL'ye: `{sl_dist:.2f}%`")
            else:
                lines.append("   _(fiyat henuz taranmadi)_")

            # Pozisyon suresi
            try:
                opened_dt = pos.opened_at
                if hasattr(opened_dt, "tz_convert"):
                    opened_dt = opened_dt.tz_convert(timezone.utc).to_pydatetime()
                elif hasattr(opened_dt, "tzinfo") and opened_dt.tzinfo is None:
                    opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                total_min = int((now_utc - opened_dt).total_seconds() // 60)
                h, m = divmod(total_min, 60)
                dur_str = f"{h}s {m}dk" if h > 0 else f"{m}dk"
                lines.append(f"   \u23f1 Sure: {dur_str}")
            except Exception:
                pass

    return "\n".join(lines)


def poll_telegram_commands(
    cfg: "Config",
    paper_trader: "PaperTrader | None",
    last_prices: dict,
) -> None:
    """getUpdates ile Telegram komutlarini oku; /status ve /yardim'a yanitla."""
    global _tg_update_offset
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        return

    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/getUpdates"
    try:
        resp = requests.get(
            url,
            params={"offset": _tg_update_offset, "limit": 20, "timeout": 0},
            timeout=10,
        )
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as exc:
        print(f"[WARN] Telegram getUpdates hatasi: {exc}")
        return

    for upd in updates:
        _tg_update_offset = upd["update_id"] + 1
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        text = msg.get("text", "").strip().lower().split()[0] if msg.get("text") else ""
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != cfg.telegram_chat_id:
            continue  # baska bir chatin mesajini yoksay

        if text in ("/status", "/durum"):
            if paper_trader is not None:
                reply = format_portfolio_status(paper_trader, last_prices)
            else:
                reply = "Paper Trade kapali. Portfoy takibi aktif degil."
            send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, reply)
            print("[CMD] /status komutu yanitlandi")

        elif text in ("/yardim", "/help"):
            reply = (
                "*Bot Komutlari*\n"
                "/status ya da /durum \u2014 Acik pozisyonlar + portfoy ozeti\n"
                "/yardim \u2014 Bu yardim mesaji"
            )
            send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, reply)
            print("[CMD] /yardim komutu yanitlandi")


def main() -> None:
    cfg = load_config()
    symbols_to_scan = resolve_symbols(cfg)
    print(f"Basladi | Taranacak sembol sayisi: {len(symbols_to_scan)} | Zaman dilimleri: {', '.join(cfg.intervals)}")
    if not symbols_to_scan:
        print("[HATA] Taranacak sembol yok. .env ayarlarini kontrol et.")
        return
    print(f"Maksimum fiyat filtresi: {cfg.max_price_usd:.2f} USDT")
    print(f"Paper Trade: {'ACIK' if cfg.paper_trade_enabled else 'KAPALI'}")
    print(f"Futures emir modu: {'ACIK' if cfg.futures_order_enabled else 'KAPALI'}")
    print(f"Balina hacim esigi: {cfg.whale_vol_multiplier:.1f}x ortalama hacim")

    paper_trader = None
    if cfg.paper_trade_enabled:
        paper_trader = PaperTrader(
            initial_balance=cfg.paper_initial_balance,
            risk_per_trade_pct=cfg.paper_risk_per_trade_pct,
            fee_rate=cfg.paper_fee_rate,
            log_file=cfg.paper_log_file,
        )

    # Diskten onceki sinyal durumunu yukle (yeniden baslatmada tekrar gonderimi onler)
    last_sent_key: dict[str, str] = load_signal_state(cfg.signal_state_file)
    print(f"Sinyal durumu yuklendi: {len(last_sent_key)} kayit ({cfg.signal_state_file})")

    last_symbol_refresh = time.monotonic()
    symbol_refresh_interval = cfg.symbol_refresh_hours * 3600

    # 451 / kalici hata alan sembolleri bu oturumda atla
    banned_symbols: set[str] = set()

    # Her semboln son bilinen kapani fiyati (/status komutu icin)
    last_prices: dict[str, float] = {}

    while True:
        cycle_started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        active_symbols = [s for s in symbols_to_scan if s not in banned_symbols]
        print(f"\n[{cycle_started}] Tarama basliyor... ({len(active_symbols)} sembol x {len(cfg.intervals)} TF)")

        # Telegram komutlarini kontrol et (/status, /durum, /yardim)
        poll_telegram_commands(cfg, paper_trader, last_prices)

        # Fear & Greed Index her dongude bir kez guncellenir (1 saatlik onbellek)
        fng_text = fetch_fear_and_greed()
        print(f"[INFO] Fear & Greed: {fng_text}")

        # Momentum siralaması: en cok haraket eden coinler once taransin
        momentum_map = fetch_symbol_momentum(cfg.binance_api_base)
        active_symbols.sort(key=lambda s: abs(momentum_map.get(s, 0.0)), reverse=True)
        if momentum_map and active_symbols:
            top5 = active_symbols[:5]
            top5_info = ", ".join(f"{s}({momentum_map.get(s, 0.0):+.1f}%)" for s in top5)
            print(f"[INFO] En hareketli 5 coin: {top5_info}")

        # Sembol listesini periyodik olarak yenile
        if cfg.use_dynamic_symbols and (time.monotonic() - last_symbol_refresh) >= symbol_refresh_interval:
            print(f"[INFO] Sembol listesi yenileniyor ({cfg.symbol_refresh_hours}s arasinda bir)...")
            new_symbols = resolve_symbols(cfg)
            if new_symbols:
                symbols_to_scan = new_symbols
                banned_symbols.clear()   # yeni listede yasaklar sifirlansin
                last_symbol_refresh = time.monotonic()
                print(f"[INFO] Yeni sembol sayisi: {len(symbols_to_scan)}")

        for symbol in active_symbols:
            # Stablecoin / kaldiraçlı token son savunma hattı (resolve_symbols bypass'larına karşı)
            if is_leveraged_token(symbol) or is_stablecoin(_base_from_symbol(symbol)):
                print(f"[SKIP] Stablecoin/kaldiraçlı token atlandı: {symbol}")
                continue

            # ── Faz 1: Tum TF verileri cekiliyor ──────────────────────────
            dfs: dict[str, pd.DataFrame] = {}
            raw_dfs: dict[str, pd.DataFrame] = {}
            price_ok = True
            price_checked = False
            banned_by_error = False

            for interval in cfg.intervals:
                pt_key = f"{symbol}_{interval}"
                try:
                    raw_df = fetch_klines(
                        symbol=symbol,
                        interval=interval,
                        limit=cfg.lookback_bars,
                        base_url=cfg.binance_api_base,
                    )
                    df = add_indicators(raw_df)
                    raw_dfs[interval] = raw_df
                    dfs[interval] = df

                    # Paper trade: acik pozisyonu guncelle
                    if paper_trader is not None:
                        latest_closed = raw_df.iloc[-2]
                        closed_trade = paper_trader.update_position(pt_key, latest_closed)
                        if closed_trade is not None:
                            close_msg = format_paper_close_message(closed_trade, interval)
                            send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, close_msg)
                            print(
                                f"{symbol} [{interval}]: paper kapandi | PnL: {closed_trade['pnl_net']:.2f} | "
                                f"Bakiye: {closed_trade['balance']:.2f}"
                            )

                    if not price_checked:
                        latest_price = float(raw_df.iloc[-2]["close"])
                        price_checked = True
                        last_prices[symbol] = latest_price  # /status komutu icin guncelle
                        if latest_price > cfg.max_price_usd:
                            print(f"{symbol}: filtre disi, fiyat {latest_price:.4f} > {cfg.max_price_usd:.2f}")
                            price_ok = False
                    if not price_ok:
                        break

                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else 0
                    if status == 451:
                        banned_symbols.add(symbol)
                        print(f"{symbol}: bolgesel kisitlama (451) - listeden cikarildi")
                        banned_by_error = True
                    else:
                        print(f"{symbol} [{interval}]: HTTP hata {status} - {exc}")
                    break
                except Exception as exc:
                    print(f"{symbol} [{interval}]: veri cekme hatasi - {exc}")
                    break

            if not price_ok or banned_by_error:
                continue

            # ── Faz 2: Sinyal uret + Multi-TF hizalama kontrolu ───────────
            for interval in cfg.intervals:
                if interval not in dfs:
                    continue
                pt_key = f"{symbol}_{interval}"
                df = dfs[interval]

                try:
                    signal = build_signal(symbol, df, cfg)

                    if not signal:
                        print(f"{symbol} [{interval}]: sinyal yok")
                        continue

                    # Ust TF'ler ayni yonu gostermiyorsa sinyali yayma
                    aligned, align_info = check_mtf_alignment(signal["side"], interval, dfs)
                    if not aligned:
                        print(f"{symbol} [{interval}]: {signal['side']} - MTF hizalanmadi ({align_info})")
                        continue

                    signal["fng"] = fng_text
                    if align_info:
                        signal["notes"] += f" | {align_info}"

                    close_time_key = pd.Timestamp(signal["close_time"]).isoformat()
                    dedupe_key = f"{symbol}|{signal['side']}|{interval}|{close_time_key}"

                    if last_sent_key.get(pt_key) == dedupe_key:
                        print(f"{symbol} [{interval}]: ayni sinyal daha once gonderildi")
                        continue

                    message = format_signal_message(signal, interval)

                    send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, message)
                    last_sent_key[pt_key] = dedupe_key
                    print(f"{symbol} [{interval}]: {signal['side']} sinyali gonderildi (Guven: {signal['confidence']}%)")

                    if cfg.use_futures and cfg.futures_order_enabled:
                        futures_event = open_futures_position(cfg, signal)
                        if futures_event is not None:
                            futures_msg = format_futures_order_message(futures_event, interval)
                            send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, futures_msg)
                            print(
                                f"{symbol} [{interval}]: futures pozisyon acildi | {futures_event['side']} | "
                                f"Qty: {futures_event['quantity']:.3f} | Leverage: {futures_event['leverage']}x"
                            )

                    if paper_trader is not None and not paper_trader.has_open_position(pt_key):
                        open_event = paper_trader.open_position(signal, pt_key)
                        if open_event is not None:
                            open_msg = format_paper_open_message(open_event, interval)
                            send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, open_msg)
                            print(
                                f"{symbol} [{interval}]: paper acildi | {open_event['side']} | "
                                f"Qty: {open_event['quantity']:.6f}"
                            )

                except Exception as exc:
                    print(f"{symbol} [{interval}]: beklenmeyen hata - {exc}")

        # Her tarama dongusu sonunda sinyal durumunu kaydet (yeniden baslatmada duplikasyon onlenir)
        save_signal_state(cfg.signal_state_file, last_sent_key)

        time.sleep(cfg.scan_every_seconds)


if __name__ == "__main__":
    main()
