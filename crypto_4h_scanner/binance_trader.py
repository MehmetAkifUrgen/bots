"""Binance auto-trading executor (Futures USDT-M & Spot) for 4H scanner."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
from pathlib import Path
import time
from typing import Literal
from urllib.parse import urlencode

import requests

FAPI_BASE = "https://fapi.binance.com"
PAPI_BASE = "https://papi.binance.com"
SPOT_BASE = "https://api.binance.com"

MarketType = Literal["futures", "spot", "papi"]


@dataclass(frozen=True)
class TradeExecution:
    symbol: str
    side: Literal["BUY", "SELL"]
    order_id: str
    price: float
    qty: float
    notional_usd: float


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    precision = max(0, int(round(-math.log10(step))))
    floored = math.floor(value / step) * step
    return round(floored, precision)


class BinanceTrader:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        market: MarketType = "futures",
        min_usd: float = 250.0,
        max_usd: float = 350.0,
        max_positions: int = 3,
        leverage: int = 1,
        state_file: Path | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.market = market
        self.min_usd = min_usd
        self.max_usd = max_usd
        self.max_positions = max_positions
        self.leverage = max(1, leverage)
        self.state_file = state_file or (
            Path(__file__).resolve().parent / "trade_state.json"
        )
        self.timeout = timeout
        self._filters_cache: dict[str, dict] = {}
        self._state = self._load_state()

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _base_url(self) -> str:
        if self.market == "papi":
            return PAPI_BASE
        return FAPI_BASE if self.market == "futures" else SPOT_BASE

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("open_positions", {})
                    return data
            except Exception:
                pass
        return {"open_positions": {}}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _signed_request(
        self, method: str, path: str, params: dict | None = None
    ) -> dict | list:
        if not self.is_configured:
            raise RuntimeError("BINANCE_API_KEY veya BINANCE_API_SECRET eksik.")

        payload = dict(params or {})
        payload["timestamp"] = int(time.time() * 1000)
        payload.setdefault("recvWindow", 5000)
        query = urlencode(payload)
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self._base_url()}{path}?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": self.api_key}

        resp = requests.request(method, url, headers=headers, timeout=self.timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Binance API {resp.status_code}: {resp.text}")
        return resp.json()

    def _is_um_futures(self) -> bool:
        return self.market in ("futures", "papi")

    def _ensure_filters(self) -> None:
        if self._filters_cache:
            return
        if self._is_um_futures():
            url = f"{FAPI_BASE}/fapi/v1/exchangeInfo"
        else:
            url = f"{SPOT_BASE}/api/v3/exchangeInfo"
        resp = requests.get(url, timeout=self.timeout)
        resp.raise_for_status()
        for sym_info in resp.json().get("symbols", []):
            sym = sym_info.get("symbol")
            if not sym:
                continue
            step_size = 0.001
            min_qty = 0.001
            min_notional = 5.0
            for f in sym_info.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "LOT_SIZE":
                    step_size = float(f.get("stepSize", step_size))
                    min_qty = float(f.get("minQty", min_qty))
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(
                        f.get("notional") or f.get("minNotional") or min_notional
                    )
            self._filters_cache[sym] = {
                "stepSize": step_size,
                "minQty": min_qty,
                "minNotional": min_notional,
            }

    def get_free_usdt(self) -> float:
        if self.market == "papi":
            acct = self._signed_request("GET", "/papi/v1/account")
            return float(acct.get("virtualMaxWithdrawAmount") or acct.get("accountEquity") or 0.0)
        if self.market == "futures":
            balances = self._signed_request("GET", "/fapi/v2/balance")
            for b in balances:
                if b.get("asset") == "USDT":
                    return float(b.get("availableBalance", 0.0))
            return 0.0
        acct = self._signed_request("GET", "/api/v3/account")
        for b in acct.get("balances", []):
            if b.get("asset") == "USDT":
                return float(b.get("free", 0.0))
        return 0.0

    def get_live_open_positions(self) -> dict[str, dict]:
        """Return currently open positions (from Binance on futures/papi, or state on spot)."""
        if self._is_um_futures():
            path = "/papi/v1/um/positionRisk" if self.market == "papi" else "/fapi/v2/positionRisk"
            rows = self._signed_request("GET", path)
            live: dict[str, dict] = {}
            for r in rows:
                amt = float(r.get("positionAmt", 0.0))
                if abs(amt) > 0:
                    sym = r["symbol"]
                    live[sym] = {
                        "symbol": sym,
                        "qty": abs(amt),
                        "entry_price": float(r.get("entryPrice", 0.0)),
                        "side": "LONG" if amt > 0 else "SHORT",
                    }
            return live
        return dict(self._state["open_positions"])

    def open_position(
        self, symbol: str, price: float, target_usd: float
    ) -> TradeExecution | None:
        """Open a BUY/LONG position if under max_positions and balance allows."""
        live_open = self.get_live_open_positions()
        tracked = self._state["open_positions"]

        # Count unique open symbols across Binance live + local state
        all_open_syms = set(live_open.keys()) | set(tracked.keys())
        if symbol in all_open_syms:
            return None
        if len(all_open_syms) >= self.max_positions:
            return None

        notional = max(self.min_usd, min(self.max_usd, target_usd))
        free_usdt = self.get_free_usdt()
        required_margin = (
            notional / self.leverage if self._is_um_futures() else notional
        )
        if free_usdt < required_margin:
            # Clamp down if still >= min_usd
            max_affordable_notional = (
                free_usdt * self.leverage * 0.98
                if self._is_um_futures()
                else free_usdt * 0.98
            )
            if max_affordable_notional < self.min_usd:
                return None
            notional = max(self.min_usd, min(self.max_usd, max_affordable_notional))

        self._ensure_filters()
        rules = self._filters_cache.get(
            symbol, {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
        )
        qty = _round_step(notional / price, rules["stepSize"])
        if qty < rules["minQty"]:
            return None
        actual_notional = qty * price
        if actual_notional < self.min_usd * 0.95 or actual_notional < rules["minNotional"]:
            return None

        if self._is_um_futures():
            lev_path = "/papi/v1/um/leverage" if self.market == "papi" else "/fapi/v1/leverage"
            ord_path = "/papi/v1/um/order" if self.market == "papi" else "/fapi/v1/order"
            try:
                self._signed_request(
                    "POST",
                    lev_path,
                    {"symbol": symbol, "leverage": self.leverage},
                )
            except Exception:
                pass
            res = self._signed_request(
                "POST",
                ord_path,
                {
                    "symbol": symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": f"{qty:g}",
                },
            )
            exec_price = float(res.get("avgPrice") or 0.0) or price
            exec_qty = float(res.get("executedQty") or 0.0) or qty
        else:
            res = self._signed_request(
                "POST",
                "/api/v3/order",
                {
                    "symbol": symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": f"{qty:g}",
                    "newOrderRespType": "FULL",
                },
            )
            fills = res.get("fills", [])
            if fills:
                exec_qty = sum(float(f.get("qty", 0.0)) for f in fills)
                exec_price = (
                    sum(float(f.get("price", 0.0)) * float(f.get("qty", 0.0)) for f in fills)
                    / exec_qty
                    if exec_qty > 0
                    else price
                )
            else:
                exec_qty = float(res.get("executedQty") or qty)
                exec_price = price

        order_id = str(res.get("orderId", ""))
        self._state["open_positions"][symbol] = {
            "symbol": symbol,
            "order_id": order_id,
            "entry_price": exec_price,
            "qty": exec_qty,
            "notional_usd": round(exec_qty * exec_price, 2),
            "opened_at": int(time.time()),
        }
        self._save_state()

        return TradeExecution(
            symbol=symbol,
            side="BUY",
            order_id=order_id,
            price=exec_price,
            qty=exec_qty,
            notional_usd=round(exec_qty * exec_price, 2),
        )

    def close_position(self, symbol: str, fallback_price: float) -> TradeExecution | None:
        """Close an open LONG/BUY position on Binance."""
        live_open = self.get_live_open_positions()
        pos = live_open.get(symbol) or self._state["open_positions"].get(symbol)
        if not pos:
            return None

        self._ensure_filters()
        rules = self._filters_cache.get(symbol, {"stepSize": 0.001, "minQty": 0.001})
        qty = _round_step(float(pos["qty"]), rules["stepSize"])
        if qty < rules["minQty"]:
            qty = float(pos["qty"])

        if self._is_um_futures():
            ord_path = "/papi/v1/um/order" if self.market == "papi" else "/fapi/v1/order"
            res = self._signed_request(
                "POST",
                ord_path,
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": f"{qty:g}",
                    "reduceOnly": "true",
                },
            )
            exec_price = float(res.get("avgPrice") or 0.0) or fallback_price
            exec_qty = float(res.get("executedQty") or 0.0) or qty
        else:
            res = self._signed_request(
                "POST",
                "/api/v3/order",
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": f"{qty:g}",
                    "newOrderRespType": "FULL",
                },
            )
            fills = res.get("fills", [])
            if fills:
                exec_qty = sum(float(f.get("qty", 0.0)) for f in fills)
                exec_price = (
                    sum(float(f.get("price", 0.0)) * float(f.get("qty", 0.0)) for f in fills)
                    / exec_qty
                    if exec_qty > 0
                    else fallback_price
                )
            else:
                exec_qty = float(res.get("executedQty") or qty)
                exec_price = fallback_price

        self._state["open_positions"].pop(symbol, None)
        self._save_state()

        return TradeExecution(
            symbol=symbol,
            side="SELL",
            order_id=str(res.get("orderId", "")),
            price=exec_price,
            qty=exec_qty,
            notional_usd=round(exec_qty * exec_price, 2),
        )
