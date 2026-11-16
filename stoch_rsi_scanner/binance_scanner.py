"""Binance Futures (USDT-M) daily scanner for Stoch RSI (3, 3, 14, 14, close)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import threading
import time
from typing import Literal

import pandas as pd
import requests

from .indicators import evaluate_signal

FAPI_BASE = "https://fapi.binance.com"


@dataclass
class CoinScanResult:
    symbol: str
    price: float
    change_24h: float
    volume_24h_usdt: float
    signal: str  # "AL" | "SAT" | "NÖTR"
    k_curr: float
    d_curr: float
    k_prev: float
    d_prev: float
    k_series: list[float]
    d_series: list[float]
    timestamps: list[int]


class BinanceFuturesScanner:
    def __init__(self, max_workers: int = 12, timeout: float = 10.0) -> None:
        self.max_workers = max_workers
        self.timeout = timeout
        self._session_local = threading.local()

    def _get_session(self) -> requests.Session:
        sess = getattr(self._session_local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": "StochRSIScanner/1.0"})
            self._session_local.session = sess
        return sess

    def get_active_usdt_perpetuals(
        self, min_volume_usdt: float = 0.0, limit: int = 0
    ) -> list[dict]:
        """Fetch active USDT-M perpetual contracts with 24h ticker stats."""
        sess = self._get_session()
        info_resp = sess.get(f"{FAPI_BASE}/fapi/v1/exchangeInfo", timeout=self.timeout)
        info_resp.raise_for_status()
        info_data = info_resp.json()

        active_symbols = {
            s["symbol"]
            for s in info_data.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        }

        ticker_resp = sess.get(f"{FAPI_BASE}/fapi/v1/ticker/24hr", timeout=self.timeout)
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
        self, symbol: str, interval: Literal["1d", "15m"] = "1d", limit: int = 150
    ) -> tuple[pd.Series, pd.Series]:
        """Fetch klines from Binance Futures for the given interval."""
        sess = self._get_session()
        resp = sess.get(
            f"{FAPI_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        raw = resp.json()
        # Kline format: [open_time, open, high, low, close, volume, close_time, ...]
        timestamps = pd.Series([int(row[0]) for row in raw], dtype="int64")
        closes = pd.Series([float(row[4]) for row in raw], dtype="float64")
        return closes, timestamps

    def _scan_one(
        self,
        coin: dict,
        interval: Literal["1d", "15m"],
        mode: Literal["live", "closed"],
    ) -> CoinScanResult | None:
        closes, timestamps = self.fetch_klines(
            coin["symbol"], interval=interval, limit=150
        )
        sig = evaluate_signal(closes, timestamps=timestamps, mode=mode, history_points=30)
        if sig is None:
            return None
        return CoinScanResult(
            symbol=coin["symbol"],
            price=coin["price"],
            change_24h=round(coin["change_24h"], 2),
            volume_24h_usdt=round(coin["volume_24h_usdt"], 0),
            signal=sig.signal,
            k_curr=sig.k_curr,
            d_curr=sig.d_curr,
            k_prev=sig.k_prev,
            d_prev=sig.d_prev,
            k_series=sig.k_series,
            d_series=sig.d_series,
            timestamps=sig.timestamps,
        )

    def scan_market(
        self,
        interval: Literal["1d", "15m"] = "1d",
        mode: Literal["live", "closed"] = "live",
        min_volume_usdt: float = 5_000_000.0,
        limit: int = 150,
    ) -> dict:
        """Scan Binance USDT-M Futures and return structured signal results."""
        started = time.time()
        coins = self.get_active_usdt_perpetuals(
            min_volume_usdt=min_volume_usdt, limit=limit
        )

        results: list[CoinScanResult] = []
        errors: list[str] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_map = {
                pool.submit(self._scan_one, coin, interval, mode): coin["symbol"]
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

        signal_rank = {"AL": 0, "SAT": 1, "NÖTR": 2}
        results.sort(
            key=lambda r: (signal_rank.get(r.signal, 3), -r.volume_24h_usdt)
        )

        al_count = sum(1 for r in results if r.signal == "AL")
        sat_count = sum(1 for r in results if r.signal == "SAT")

        return {
            "interval": interval,
            "mode": mode,
            "scanned_at": int(time.time()),
            "duration_sec": round(time.time() - started, 2),
            "total_scanned": len(results),
            "al_count": al_count,
            "sat_count": sat_count,
            "error_count": len(errors),
            "errors": errors[:10],
            "results": [asdict(r) for r in results],
        }
