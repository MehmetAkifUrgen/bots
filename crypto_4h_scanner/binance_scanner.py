"""Binance 4H scanner for 'Gün İçi Anlık Sistem' (5-indicator confirmation)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import threading
import time
from typing import Literal

import pandas as pd
import requests

from .indicators import (
    ShortPeakEvaluation,
    SystemEvaluation,
    check_1h_short_zone,
    evaluate_1h_15m_system,
    evaluate_4h_atr_long_system,
    evaluate_4h_system,
    evaluate_futures_v3_system,
    evaluate_short_15m_system,
    resample_15m_to_45m,
    resample_1h_to_4h,
)

FAPI_BASE = "https://fapi.binance.com"
SPOT_BASE = "https://api.binance.com"

MarketType = Literal["futures", "papi", "spot"]
TimeframeType = Literal["45m", "1h", "15m", "4h"]


@dataclass
class Coin4hResult:
    symbol: str
    market: MarketType
    price: float
    change_24h: float
    volume_24h_usdt: float
    evaluation: SystemEvaluation
    timeframe: TimeframeType = "45m"
    long_evaluation: SystemEvaluation | None = None
    prev_change_24h: float = 0.0
    short_15m_evaluation: ShortPeakEvaluation | None = None


def _klines_to_df(raw: list[list]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open_time": [int(r[0]) for r in raw],
            "open": [float(r[1]) for r in raw],
            "high": [float(r[2]) for r in raw],
            "low": [float(r[3]) for r in raw],
            "close": [float(r[4]) for r in raw],
            "volume": [float(r[5]) for r in raw],
            "close_time": [int(r[6]) for r in raw],
        }
    )


class Binance4hScanner:
    def __init__(
        self,
        market: MarketType = "futures",
        timeframe: TimeframeType = "45m",
        max_workers: int = 4,
        timeout: float = 10.0,
        contract_type: str = "PERPETUAL",
    ) -> None:
        self.market = market
        self.timeframe = timeframe
        self.max_workers = max_workers
        self.timeout = timeout
        self.contract_type = contract_type
        self._session_local = threading.local()
        self._cache_lock = threading.Lock()
        self._active_symbols: set[str] = set()
        self._active_symbols_ts: float = 0.0
        self._kline_cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._used_weight_1m: int = 0

    def _get_session(self) -> requests.Session:
        sess = getattr(self._session_local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": "Crypto4HScanner/1.0"})
            self._session_local.session = sess
        return sess

    def _respect_rate_limit(self, resp: requests.Response) -> None:
        w = resp.headers.get("X-MBX-USED-WEIGHT-1M") or resp.headers.get("x-mbx-used-weight-1m")
        if w:
            try:
                self._used_weight_1m = int(w)
            except ValueError:
                pass
        if self._used_weight_1m >= 1800:
            # Pause briefly so 1-minute weight window cools down before hitting 2400
            time.sleep(5.0)
        elif self._used_weight_1m >= 1400:
            time.sleep(0.35)

    def get_active_usdt_symbols(
        self, min_volume_usdt: float = 5_000_000.0, limit: int = 0
    ) -> list[dict]:
        """Fetch active USDT pairs sorted by 24h quote volume descending (exchangeInfo cached 1h)."""
        sess = self._get_session()
        if self.market in ("futures", "papi"):
            info_url = f"{FAPI_BASE}/fapi/v1/exchangeInfo"
            ticker_url = f"{FAPI_BASE}/fapi/v1/ticker/24hr"
        else:
            info_url = f"{SPOT_BASE}/api/v3/exchangeInfo"
            ticker_url = f"{SPOT_BASE}/api/v3/ticker/24hr"

        if not self._active_symbols or (time.time() - self._active_symbols_ts) > 3600:
            info_resp = sess.get(info_url, timeout=self.timeout)
            self._respect_rate_limit(info_resp)
            info_resp.raise_for_status()
            info_data = info_resp.json()

            if self.market in ("futures", "papi"):
                self._active_symbols = {
                    s["symbol"]
                    for s in info_data.get("symbols", [])
                    if s.get("contractType") == self.contract_type
                    and s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"
                }
            else:
                self._active_symbols = {
                    s["symbol"]
                    for s in info_data.get("symbols", [])
                    if s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"
                    and s.get("isSpotTradingAllowed", True)
                    and not s["symbol"].endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))
                }
            self._active_symbols_ts = time.time()

        active_symbols = self._active_symbols
        ticker_resp = sess.get(ticker_url, timeout=self.timeout)
        self._respect_rate_limit(ticker_resp)
        ticker_resp.raise_for_status()
        tickers = ticker_resp.json()

        coins: list[dict] = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym not in active_symbols:
                continue
            quote_vol = float(t.get("quoteVolume", 0.0))
            if quote_vol < min_volume_usdt:
                continue
            coins.append(
                {
                    "symbol": sym,
                    "price": float(t.get("lastPrice", 0.0)),
                    "change_24h": float(t.get("priceChangePercent", 0.0)),
                    "volume_24h_usdt": quote_vol,
                }
            )

        coins.sort(key=lambda c: c["volume_24h_usdt"], reverse=True)
        if limit > 0:
            coins = coins[:limit]
        return coins

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        end_time: int | None = None,
    ) -> pd.DataFrame:
        sess = self._get_session()
        if self.market in ("futures", "papi"):
            url = f"{FAPI_BASE}/fapi/v1/klines"
        else:
            url = f"{SPOT_BASE}/api/v3/klines"

        params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time is not None:
            params["endTime"] = end_time
        resp = sess.get(url, params=params, timeout=self.timeout)
        self._respect_rate_limit(resp)
        resp.raise_for_status()
        return _klines_to_df(resp.json())

    def fetch_klines_cached(
        self,
        symbol: str,
        interval: str,
        initial_limit: int,
        pages: int = 1,
        max_rows: int = 2000,
    ) -> pd.DataFrame:
        """
        Incrementally update cached klines:
        - First call fetches `pages * initial_limit` bars
        - Subsequent calls fetch only last 8 bars (weight=1!) and merge.
        """
        key = (symbol, interval)
        with self._cache_lock:
            cached = self._kline_cache.get(key)

        if cached is not None and not cached.empty:
            recent = self.fetch_klines(symbol, interval=interval, limit=8)
            merged = (
                pd.concat([cached, recent], ignore_index=True)
                .drop_duplicates(subset=["open_time"], keep="last")
                .sort_values("open_time")
                .tail(max_rows)
                .reset_index(drop=True)
            )
            with self._cache_lock:
                self._kline_cache[key] = merged
            return merged

        # Initial load
        df = self.fetch_klines(symbol, interval=interval, limit=initial_limit)
        for _ in range(max(0, pages - 1)):
            if df.empty:
                break
            first_ts = int(df["open_time"].iloc[0])
            try:
                older = self.fetch_klines(
                    symbol, interval=interval, limit=initial_limit, end_time=first_ts - 1
                )
                if older.empty:
                    break
                df = (
                    pd.concat([older, df], ignore_index=True)
                    .drop_duplicates(subset=["open_time"], keep="last")
                    .sort_values("open_time")
                    .reset_index(drop=True)
                )
            except Exception:
                break

        with self._cache_lock:
            self._kline_cache[key] = df.tail(max_rows).reset_index(drop=True)
        return self._kline_cache[key]

    def _scan_one(self, coin: dict) -> Coin4hResult | None:
        sym = coin["symbol"]
        if self.timeframe == "45m":
            # 2 pages x 1000 (weight 5 each on first load, weight 1 on subsequent scans!)
            df_15m = self.fetch_klines_cached(
                sym, interval="15m", initial_limit=1000, pages=2, max_rows=2000
            )
            df_ltf = resample_15m_to_45m(df_15m)
            df_trend = self.fetch_klines_cached(
                sym, interval="4h", initial_limit=200, pages=1, max_rows=250
            )
            df_pivot = self.fetch_klines_cached(
                sym, interval="1d", initial_limit=45, pages=1, max_rows=60
            )
        elif self.timeframe == "15m":
            df_ltf = self.fetch_klines_cached(
                sym, interval="15m", initial_limit=300, pages=1, max_rows=400
            )
            df_trend = self.fetch_klines_cached(
                sym, interval="1h", initial_limit=120, pages=1, max_rows=150
            )
            df_pivot = self.fetch_klines_cached(
                sym, interval="4h", initial_limit=60, pages=1, max_rows=80
            )
        else:
            df_ltf = self.fetch_klines_cached(
                sym, interval="1h", initial_limit=300, pages=1, max_rows=400
            )
            df_trend = self.fetch_klines_cached(
                sym, interval="4h", initial_limit=120, pages=1, max_rows=150
            )
            df_pivot = self.fetch_klines_cached(
                sym, interval="1d", initial_limit=30, pages=1, max_rows=45
            )

        ev = evaluate_1h_15m_system(
            df_ltf=df_ltf,
            df_trend=df_trend,
            df_pivot=df_pivot,
            timeframe=self.timeframe,
        )
        if ev is None:
            return None
        return Coin4hResult(
            symbol=sym,
            market=self.market,
            price=ev.price,
            change_24h=round(coin["change_24h"], 2),
            volume_24h_usdt=round(coin["volume_24h_usdt"], 0),
            evaluation=ev,
            timeframe=self.timeframe,
        )

    def scan_market(
        self,
        min_volume_usdt: float = 5_000_000.0,
        limit: int = 0,
    ) -> tuple[list[Coin4hResult], list[str], float]:
        """Scan all matching Binance USDT pairs on 4H timeframe."""
        started = time.time()
        coins = self.get_active_usdt_symbols(
            min_volume_usdt=min_volume_usdt, limit=limit
        )

        results: list[Coin4hResult] = []
        errors: list[str] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {pool.submit(self._scan_one, coin): coin["symbol"] for coin in coins}
            for fut in as_completed(future_map):
                sym = future_map[fut]
                try:
                    res = fut.result()
                    if res is not None:
                        results.append(res)
                except Exception as exc:
                    errors.append(f"{sym}: {exc}")

        duration = round(time.time() - started, 2)
        return results, errors, duration

    def _scan_one_4h_bist(
        self,
        coin: dict,
        interval: str = "1h",
        allow_short_scan: bool = True,
        force_15m: bool = False,
    ) -> Coin4hResult | None:
        sym = coin["symbol"]
        if interval == "1h":
            df_1h = self.fetch_klines_cached(
                sym, interval="1h", initial_limit=1500, pages=4, max_rows=5000
            )
            df_1m = self.fetch_klines_cached(
                sym, interval="1M", initial_limit=24, pages=1, max_rows=30
            )
            ev_1h = evaluate_4h_system(
                df_1h, df_1m=df_1m, pivot_len=7, be_trigger_pct=2.5, ema200_filter=False
            )
            if ev_1h is None:
                return None
            df_4h = resample_1h_to_4h(df_1h)
            ev_4h = evaluate_4h_system(
                df_4h, df_1m=df_1m, pivot_len=7, be_trigger_pct=2.5, ema200_filter=False
            )
            prev_chg_24h = 0.0
            if len(df_1h) >= 49:
                c_48 = float(df_1h["close"].iloc[-49])
                c_24 = float(df_1h["close"].iloc[-25])
                if c_48 > 0:
                    prev_chg_24h = round((c_24 - c_48) / c_48 * 100.0, 2)
            return Coin4hResult(
                symbol=sym,
                market=self.market,
                price=ev_1h.price,
                change_24h=round(coin["change_24h"], 2),
                volume_24h_usdt=round(coin["volume_24h_usdt"], 0),
                evaluation=ev_1h,
                timeframe="1h",
                long_evaluation=ev_4h,
                prev_change_24h=prev_chg_24h,
            )

        df_ohlcv = self.fetch_klines_cached(
            sym, interval="4h", initial_limit=1500, pages=4, max_rows=5000
        )
        ev = evaluate_futures_v3_system(
            df_ohlcv,
            use_local_trend=False,
            use_anti_top=True,
            tp1_pct=1.00,
            fee_buffer_pct=0.15,
            trail_dist_pct=1.10,
            max_stop_pct=2.20,
            hard_stop_pct=3.00,
        )
        if ev is None:
            return None

        # 2-Stage SHORT (ONLY Top 10/20 Gainers when enabled): 1H/4H Wall + Red Candle -> 15m Early Reversal
        in_1h_zone = False
        htf_reason = ""
        if allow_short_scan:
            df_1m = self.fetch_klines_cached(
                sym, interval="1M", initial_limit=36, pages=1, max_rows=48
            )
            df_1h_short = self.fetch_klines_cached(
                sym, interval="1h", initial_limit=350, pages=1, max_rows=450
            )
            in_1h_zone, htf_reason = check_1h_short_zone(
                df_1h_short, df_4h=df_ohlcv, df_1m=df_1m
            )

        short_15m_ev: ShortPeakEvaluation | None = None
        if (allow_short_scan and in_1h_zone) or force_15m:
            df_15m_short = self.fetch_klines_cached(
                sym, interval="15m", initial_limit=450, pages=1, max_rows=550
            )
            short_15m_ev = evaluate_short_15m_system(
                df_15m_short,
                in_1h_zone=(allow_short_scan and in_1h_zone),
                htf_reason=htf_reason,
                max_stop_pct=1.5,
                be_trigger_pct=1.5,
            )

        return Coin4hResult(
            symbol=sym,
            market=self.market,
            price=short_15m_ev.price if short_15m_ev is not None else ev.price,
            change_24h=round(coin["change_24h"], 2),
            volume_24h_usdt=round(coin["volume_24h_usdt"], 0),
            evaluation=ev,
            timeframe="4h",
            short_15m_evaluation=short_15m_ev,
        )

    def scan_4h_bist_alerts(
        self,
        min_volume_usdt: float = 5_000_000.0,
        limit: int = 0,
        interval: str = "1h",
        short_top_gainers: int = 20,
        extra_15m_symbols: set[str] | None = None,
        enable_short: bool = False,
    ) -> tuple[list[Coin4hResult], list[str], float]:
        """Scan Binance USDT pairs on 4H (LONG-only when enable_short=False)."""
        started = time.time()
        coins = self.get_active_usdt_symbols(
            min_volume_usdt=min_volume_usdt, limit=limit
        )
        extra_syms = extra_15m_symbols or set()
        if not enable_short:
            gainer_syms: set[str] = set()
        elif short_top_gainers > 0:
            gainer_syms = {
                c["symbol"]
                for c in sorted(coins, key=lambda x: x["change_24h"], reverse=True)[:short_top_gainers]
                if c["change_24h"] > 0
            }
        else:
            gainer_syms = {c["symbol"] for c in coins}
        results: list[Coin4hResult] = []
        errors: list[str] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {
                pool.submit(
                    self._scan_one_4h_bist,
                    coin,
                    interval,
                    coin["symbol"] in gainer_syms,
                    coin["symbol"] in extra_syms,
                ): coin["symbol"]
                for coin in coins
            }
            for fut in as_completed(future_map):
                sym = future_map[fut]
                try:
                    res = fut.result()
                    if res is not None:
                        results.append(res)
                except Exception as exc:
                    errors.append(f"{sym}: {exc}")

        return results, errors, round(time.time() - started, 2)
