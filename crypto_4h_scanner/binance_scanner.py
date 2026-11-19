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
    SystemEvaluation,
    evaluate_1h_15m_system,
    resample_15m_to_45m,
)

FAPI_BASE = "https://fapi.binance.com"
SPOT_BASE = "https://api.binance.com"

MarketType = Literal["futures", "papi", "spot"]
TimeframeType = Literal["45m", "1h", "15m"]


@dataclass
class Coin4hResult:
    symbol: str
    market: MarketType
    price: float
    change_24h: float
    volume_24h_usdt: float
    evaluation: SystemEvaluation
    timeframe: TimeframeType = "45m"


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
        max_workers: int = 12,
        timeout: float = 10.0,
    ) -> None:
        self.market = market
        self.timeframe = timeframe
        self.max_workers = max_workers
        self.timeout = timeout
        self._session_local = threading.local()

    def _get_session(self) -> requests.Session:
        sess = getattr(self._session_local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": "Crypto4HScanner/1.0"})
            self._session_local.session = sess
        return sess

    def get_active_usdt_symbols(
        self, min_volume_usdt: float = 5_000_000.0, limit: int = 0
    ) -> list[dict]:
        """Fetch active USDT pairs sorted by 24h quote volume descending."""
        sess = self._get_session()
        if self.market in ("futures", "papi"):
            info_url = f"{FAPI_BASE}/fapi/v1/exchangeInfo"
            ticker_url = f"{FAPI_BASE}/fapi/v1/ticker/24hr"
        else:
            info_url = f"{SPOT_BASE}/api/v3/exchangeInfo"
            ticker_url = f"{SPOT_BASE}/api/v3/ticker/24hr"

        info_resp = sess.get(info_url, timeout=self.timeout)
        info_resp.raise_for_status()
        info_data = info_resp.json()

        if self.market in ("futures", "papi"):
            active_symbols = {
                s["symbol"]
                for s in info_data.get("symbols", [])
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
            }
        else:
            active_symbols = {
                s["symbol"]
                for s in info_data.get("symbols", [])
                if s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
                and s.get("isSpotTradingAllowed", True)
                and not s["symbol"].endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))
            }

        ticker_resp = sess.get(ticker_url, timeout=self.timeout)
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
        resp.raise_for_status()
        return _klines_to_df(resp.json())

    def _scan_one(self, coin: dict) -> Coin4hResult | None:
        sym = coin["symbol"]
        if self.timeframe == "45m":
            # Fetch 2 pages of 15m (up to 3000 bars -> ~1000 45m bars / ~31 days)
            df_15m_recent = self.fetch_klines(sym, interval="15m", limit=1500)
            if not df_15m_recent.empty:
                first_ts = int(df_15m_recent["open_time"].iloc[0])
                try:
                    df_15m_older = self.fetch_klines(
                        sym, interval="15m", limit=1500, end_time=first_ts - 1
                    )
                    df_15m = (
                        pd.concat([df_15m_older, df_15m_recent], ignore_index=True)
                        .drop_duplicates(subset=["open_time"])
                        .sort_values("open_time")
                        .reset_index(drop=True)
                    )
                except Exception:
                    df_15m = df_15m_recent
            else:
                df_15m = df_15m_recent
            df_ltf = resample_15m_to_45m(df_15m)
            df_trend = self.fetch_klines(sym, interval="4h", limit=250)
            df_pivot = self.fetch_klines(sym, interval="1d", limit=45)
        elif self.timeframe == "15m":
            df_ltf = self.fetch_klines(sym, interval="15m", limit=300)
            df_trend = self.fetch_klines(sym, interval="1h", limit=120)
            df_pivot = self.fetch_klines(sym, interval="4h", limit=60)
        else:
            df_ltf = self.fetch_klines(sym, interval="1h", limit=300)
            df_trend = self.fetch_klines(sym, interval="4h", limit=120)
            df_pivot = self.fetch_klines(sym, interval="1d", limit=30)

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
