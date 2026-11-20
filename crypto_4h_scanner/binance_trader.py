"""Binance auto-trading executor (Futures USDT-M & Spot) for 4H scanner."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
from pathlib import Path
import threading
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
        min_usd: float = 200.0,
        max_usd: float = 300.0,
        max_positions: int = 3,
        leverage: int = 20,
        max_leverage: int = 125,
        stop_loss_pct: float = 2.5,
        trailing_trigger_pct: float = 2.0,
        trailing_callback_pct: float = 0.5,
        trailing_floor_pct: float = 1.5,
        use_trailing: bool = True,
        max_consecutive_stops: int = 3,
        cooldown_minutes: int = 0,
        symbol_stop_cooldown_minutes: int = 180,
        state_file: Path | None = None,
        timeout: float = 10.0,
        proxy_url: str = "",
    ) -> None:
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.market = market
        self.min_usd = min_usd
        self.max_usd = max_usd
        self.max_positions = max_positions
        self.max_leverage = max(1, max_leverage)
        self.leverage = min(self.max_leverage, max(1, leverage))
        self.stop_loss_pct = stop_loss_pct
        self.trailing_trigger_pct = trailing_trigger_pct
        self.trailing_callback_pct = trailing_callback_pct
        self.trailing_floor_pct = trailing_floor_pct
        self.use_trailing = use_trailing
        self.max_consecutive_stops = max_consecutive_stops
        self.cooldown_minutes = cooldown_minutes
        self.symbol_stop_cooldown_minutes = max(0, symbol_stop_cooldown_minutes)
        self.state_file = state_file or (
            Path(__file__).resolve().parent / "trade_state.json"
        )
        self.timeout = timeout
        self.proxy_url = proxy_url.strip()
        self._lock = threading.RLock()
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
                    data.setdefault("consecutive_stops", 0)
                    data.setdefault("cooldown_until", 0)
                    data.setdefault("symbol_cooldowns", {})
                    return data
            except Exception:
                pass
        return {
            "open_positions": {},
            "consecutive_stops": 0,
            "cooldown_until": 0,
            "symbol_cooldowns": {},
        }

    def is_symbol_in_cooldown(self, symbol: str) -> bool:
        if self.symbol_stop_cooldown_minutes <= 0:
            return False
        cds = self._state.get("symbol_cooldowns", {})
        until = int(cds.get(symbol, 0))
        if until <= 0:
            return False
        if time.time() < until:
            return True
        cds.pop(symbol, None)
        self._save_state()
        return False

    def is_in_cooldown(self) -> bool:
        if self.cooldown_minutes <= 0:
            return False
        until = int(self._state.get("cooldown_until", 0))
        if until <= 0:
            return False
        if time.time() < until:
            return True
        # Cooldown expired -> reset counter
        self._state["cooldown_until"] = 0
        self._state["consecutive_stops"] = 0
        self._save_state()
        return False

    def cooldown_remaining_min(self) -> int:
        until = int(self._state.get("cooldown_until", 0))
        rem = int(max(0, (until - time.time()) / 60))
        return rem

    def reset_cooldown(self) -> None:
        with self._lock:
            self._state["consecutive_stops"] = 0
            self._state["cooldown_until"] = 0
            self._save_state()

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

        proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url
            else None
        )
        resp = requests.request(
            method, url, headers=headers, proxies=proxies, timeout=self.timeout
        )
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
            tick_size = 0.0001
            for f in sym_info.get("filters", []):
                ftype = f.get("filterType")
                if ftype == "LOT_SIZE":
                    step_size = float(f.get("stepSize", step_size))
                    min_qty = float(f.get("minQty", min_qty))
                elif ftype == "PRICE_FILTER":
                    tick_size = float(f.get("tickSize", tick_size))
                elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(
                        f.get("notional") or f.get("minNotional") or min_notional
                    )
            self._filters_cache[sym] = {
                "stepSize": step_size,
                "minQty": min_qty,
                "minNotional": min_notional,
                "tickSize": tick_size,
            }

    def cancel_exchange_stop_losses(self, symbol: str) -> None:
        """Cancel all open algo/stop orders for a symbol on Binance."""
        if not self._is_um_futures() or not self.is_configured:
            return
        try:
            if self.market == "papi":
                self._signed_request(
                    "DELETE", "/papi/v1/um/algo/allOpenOrders", {"symbol": symbol}
                )
            else:
                self._signed_request(
                    "DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}
                )
        except Exception:
            pass

    def place_exchange_stop_loss(self, symbol: str, sl_price: float) -> str | None:
        """Place a real exchange-side STOP_MARKET order on Binance (millisecond execution)."""
        if not self._is_um_futures() or not self.is_configured or sl_price <= 0:
            return None
        self._ensure_filters()
        tick = float(self._filters_cache.get(symbol, {}).get("tickSize", 0.0001))
        rounded_sl = _round_step(sl_price, tick)
        if rounded_sl <= 0:
            rounded_sl = sl_price

        self.cancel_exchange_stop_losses(symbol)
        try:
            if self.market == "papi":
                res = self._signed_request(
                    "POST",
                    "/papi/v1/um/algo/order",
                    {
                        "symbol": symbol,
                        "side": "SELL",
                        "algoType": "CONDITIONAL",
                        "type": "STOP_MARKET",
                        "triggerPrice": f"{rounded_sl:g}",
                        "closePosition": "true",
                        "workingType": "CONTRACT_PRICE",
                    },
                )
                return str(res.get("algoId") or res.get("orderId") or "")
            else:
                res = self._signed_request(
                    "POST",
                    "/fapi/v1/order",
                    {
                        "symbol": symbol,
                        "side": "SELL",
                        "type": "STOP_MARKET",
                        "stopPrice": f"{rounded_sl:g}",
                        "closePosition": "true",
                        "workingType": "CONTRACT_PRICE",
                    },
                )
                return str(res.get("orderId") or "")
        except Exception:
            return None

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
        self,
        symbol: str,
        price: float,
        target_usd: float,
        tp_price: float | None = None,
    ) -> TradeExecution | None:
        """Open a BUY/LONG position if under max_positions, not in cooldown, and balance allows."""
        if self.is_in_cooldown() or self.is_symbol_in_cooldown(symbol):
            return None
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
        effective_lev = min(self.max_leverage, self.leverage)
        if self._is_um_futures() and free_usdt > 0:
            # Auto-pick leverage up to safe max_leverage (never exceeds 15x so -2.5% SL triggers long before liquidation)
            if free_usdt * effective_lev * 0.90 < self.min_usd:
                needed_lev = int(math.ceil(notional / (free_usdt * 0.90)))
                effective_lev = min(self.max_leverage, max(effective_lev, needed_lev))

        required_margin = (
            notional / effective_lev if self._is_um_futures() else notional
        )
        if free_usdt < required_margin:
            max_affordable_notional = (
                free_usdt * effective_lev * 0.90
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
            for try_lev in dict.fromkeys([effective_lev, 50, 40, 25, 20, 10, 5]):
                try:
                    self._signed_request(
                        "POST",
                        lev_path,
                        {"symbol": symbol, "leverage": try_lev},
                    )
                    break
                except Exception:
                    continue
            res = self._signed_request(
                "POST",
                ord_path,
                {
                    "symbol": symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": f"{qty:g}",
                    "newOrderRespType": "RESULT",
                },
            )
            exec_price = float(res.get("avgPrice") or 0.0)
            exec_qty = float(res.get("executedQty") or 0.0) or qty
            if exec_price <= 0:
                # Binance PAPI/Futures market orders often return avgPrice=0 before fill settles;
                # query the real Binance position entryPrice instead of using stale scan price!
                time.sleep(0.35)
                try:
                    live_after = self.get_live_open_positions().get(symbol)
                    if live_after and float(live_after.get("entry_price") or 0.0) > 0:
                        exec_price = float(live_after["entry_price"])
                        exec_qty = float(live_after.get("qty") or exec_qty)
                except Exception:
                    pass
            if exec_price <= 0:
                exec_price = self.fetch_public_price(symbol) or price
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
        actual_not = round(exec_qty * exec_price, 2)
        sl_price = exec_price * (1.0 - self.stop_loss_pct / 100.0)
        algo_sl_id = self.place_exchange_stop_loss(symbol, sl_price)
        self._state["open_positions"][symbol] = {
            "symbol": symbol,
            "order_id": order_id,
            "entry_price": exec_price,
            "qty": exec_qty,
            "notional_usd": actual_not,
            "sl_price": sl_price,
            "tp_price": float(tp_price) if (tp_price and tp_price > exec_price) else 0.0,
            "exchange_sl_price": sl_price if algo_sl_id else 0.0,
            "peak_price": exec_price,
            "trailing_active": False,
            "opened_at": int(time.time()),
        }
        self._save_state()

        return TradeExecution(
            symbol=symbol,
            side="BUY",
            order_id=order_id,
            price=exec_price,
            qty=exec_qty,
            notional_usd=actual_not,
        )

    def close_position(
        self,
        symbol: str,
        fallback_price: float,
        is_stop_loss: bool = False,
    ) -> TradeExecution | None:
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

        self.cancel_exchange_stop_losses(symbol)
        prev_pos = self._state["open_positions"].pop(symbol, None)
        entry_px = float((prev_pos or pos).get("entry_price") or exec_price)
        pnl_usd = (exec_price - entry_px) * exec_qty

        if is_stop_loss:
            self._state["consecutive_stops"] = int(self._state.get("consecutive_stops", 0)) + 1
            if self.symbol_stop_cooldown_minutes > 0:
                self._state.setdefault("symbol_cooldowns", {})[symbol] = int(
                    time.time() + self.symbol_stop_cooldown_minutes * 60
                )
            if (
                self.cooldown_minutes > 0
                and self._state["consecutive_stops"] >= self.max_consecutive_stops
            ):
                self._state["cooldown_until"] = int(time.time() + self.cooldown_minutes * 60)
        elif pnl_usd >= 0:
            self._state["consecutive_stops"] = 0
            self._state["cooldown_until"] = 0

        self._save_state()

        return TradeExecution(
            symbol=symbol,
            side="SELL",
            order_id=str(res.get("orderId", "")),
            price=exec_price,
            qty=exec_qty,
            notional_usd=round(exec_qty * exec_price, 2),
        )

    def has_open_positions(self) -> bool:
        with self._lock:
            return bool(self._state["open_positions"])

    def fetch_public_price(self, symbol: str) -> float | None:
        url = (
            f"{FAPI_BASE}/fapi/v1/ticker/price"
            if self._is_um_futures()
            else f"{SPOT_BASE}/api/v3/ticker/price"
        )
        try:
            resp = requests.get(url, params={"symbol": symbol}, timeout=self.timeout)
            resp.raise_for_status()
            return float(resp.json().get("price", 0.0)) or None
        except Exception:
            return None

    def sync_live_positions(self) -> dict[str, dict]:
        """
        Sync live open positions from Binance into local state.
        Crucial on Railway/ephemeral disks so restarts never drop SL/Trailing tracking!
        """
        if not self._is_um_futures() or not self.is_configured:
            with self._lock:
                return dict(self._state["open_positions"])

        live = self.get_live_open_positions()
        with self._lock:
            tracked = self._state["open_positions"]
            dirty = False

            # Adopt any live Binance LONG position missing from local state
            for sym, lpos in live.items():
                if lpos.get("side") != "LONG":
                    continue
                if sym not in tracked:
                    entry_px = float(lpos["entry_price"])
                    qty = float(lpos["qty"])
                    tracked[sym] = {
                        "symbol": sym,
                        "order_id": "synced",
                        "entry_price": entry_px,
                        "qty": qty,
                        "notional_usd": round(entry_px * qty, 2),
                        "sl_price": entry_px * (1.0 - self.stop_loss_pct / 100.0),
                        "peak_price": entry_px,
                        "trailing_active": False,
                        "opened_at": int(time.time()),
                    }
                    dirty = True
                else:
                    # Keep entry_price synced with Binance's real fill price
                    live_ep = float(lpos.get("entry_price") or 0.0)
                    if live_ep > 0 and abs(live_ep - float(tracked[sym].get("entry_price") or 0.0)) / live_ep > 0.001:
                        tracked[sym]["entry_price"] = live_ep
                        if not tracked[sym].get("trailing_active"):
                            tracked[sym]["sl_price"] = live_ep * (1.0 - self.stop_loss_pct / 100.0)
                        dirty = True

            # Remove positions no longer open on Binance
            for sym in list(tracked.keys()):
                if sym not in live:
                    tracked.pop(sym, None)
                    dirty = True

            if dirty:
                self._save_state()
            return dict(tracked)

    def manage_open_positions(
        self, price_map: dict[str, float] | None = None
    ) -> list[dict]:
        """
        Monitor open positions in state against live prices:
        - Close at -stop_loss_pct% (-1.0%) Stop-Loss
        - Activate Trailing Stop at +1.0% profit, trail by trailing_callback_pct (floor +0.3%)
        Returns list of closed trade event dicts.
        """
        pmap = price_map or {}
        events: list[dict] = []

        with self._lock:
            open_syms = list(self._state["open_positions"].keys())
            state_dirty = False

            for sym in open_syms:
                pos = self._state["open_positions"].get(sym)
                if not pos:
                    continue
                curr_price = pmap.get(sym)
                if curr_price is None or curr_price <= 0:
                    curr_price = self.fetch_public_price(sym)
                if curr_price is None or curr_price <= 0:
                    continue

                entry_px = float(pos["entry_price"])
                qty = float(pos["qty"])
                pnl_pct = (
                    ((curr_price - entry_px) / entry_px) * 100.0
                    if entry_px > 0
                    else 0.0
                )

                # Update peak & trailing state
                peak_px = max(float(pos.get("peak_price") or entry_px), curr_price)
                if peak_px > float(pos.get("peak_price") or 0.0):
                    pos["peak_price"] = peak_px
                    state_dirty = True

                trailing_active = bool(pos.get("trailing_active", False))
                if not trailing_active and pnl_pct >= self.trailing_trigger_pct:
                    trailing_active = True
                    pos["trailing_active"] = True
                    state_dirty = True

                reason: str | None = None
                tp_px = float(pos.get("tp_price") or 0.0)
                if (not self.use_trailing) and tp_px > 0 and curr_price >= tp_px:
                    reason = "KÂR AL (Direnç)"
                elif trailing_active:
                    if self.use_trailing:
                        peak_pct = (
                            ((peak_px - entry_px) / entry_px) * 100.0
                            if entry_px > 0
                            else 0.0
                        )
                        lock_pct = max(
                            self.trailing_floor_pct,
                            peak_pct - self.trailing_callback_pct,
                        )
                        effective_sl = entry_px * (1.0 + lock_pct / 100.0)
                    else:
                        effective_sl = entry_px * (1.0 + self.trailing_floor_pct / 100.0)
                    if effective_sl > float(pos.get("sl_price") or 0.0):
                        pos["sl_price"] = effective_sl
                        state_dirty = True
                        ex_sl = float(pos.get("exchange_sl_price") or 0.0)
                        if effective_sl > ex_sl * 1.002 and curr_price > effective_sl:
                            if self.place_exchange_stop_loss(sym, effective_sl):
                                pos["exchange_sl_price"] = effective_sl
                    if curr_price <= effective_sl:
                        reason = "TRAILING STOP" if self.use_trailing else "SAT (Kâr Kilidi)"
                else:
                    # At +1.0% profit, move Stop-Loss to 0% (break-even: entry_price)
                    peak_pct = (
                        ((peak_px - entry_px) / entry_px) * 100.0
                        if entry_px > 0
                        else 0.0
                    )
                    be_locked = bool(pos.get("be_locked", False))
                    if not be_locked and peak_pct >= 1.0:
                        be_locked = True
                        pos["be_locked"] = True
                        if entry_px > float(pos.get("sl_price") or 0.0):
                            pos["sl_price"] = entry_px
                            state_dirty = True
                            if curr_price > entry_px:
                                if self.place_exchange_stop_loss(sym, entry_px):
                                    pos["exchange_sl_price"] = entry_px

                    sl_px = float(
                        pos.get("sl_price")
                        or (entry_px * (1.0 - self.stop_loss_pct / 100.0))
                    )
                    if curr_price <= sl_px or (not be_locked and pnl_pct <= -self.stop_loss_pct):
                        reason = "SAT (Başabaş %0)" if be_locked else f"STOP (-%{self.stop_loss_pct:g})"

                if reason:
                    closed = self.close_position(
                        sym,
                        fallback_price=curr_price,
                        is_stop_loss=reason.startswith("STOP"),
                    )
                    if closed:
                        realized_usd = (closed.price - entry_px) * closed.qty
                        realized_pct = (
                            ((closed.price - entry_px) / entry_px) * 100.0
                            if entry_px > 0
                            else 0.0
                        )
                        events.append(
                            {
                                "symbol": sym,
                                "reason": reason,
                                "entry_price": entry_px,
                                "exit_price": closed.price,
                                "pnl_usd": round(realized_usd, 2),
                                "pnl_pct": round(realized_pct, 2),
                                "order_id": closed.order_id,
                                "cooldown_triggered": self.is_in_cooldown(),
                                "cooldown_minutes": self.cooldown_minutes,
                                "consecutive_stops": int(
                                    self._state.get("consecutive_stops", 0)
                                ),
                            }
                        )

            if state_dirty:
                self._save_state()

        return events
