"""Binance auto-trading executor (Futures USDT-M & Spot) for 4H scanner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
BOT_CLIENT_ID_PREFIX = "BOT4H_"

MarketType = Literal["futures", "spot", "papi"]


@dataclass(frozen=True)
class TradeExecution:
    symbol: str
    side: Literal["BUY", "SELL"]
    order_id: str
    price: float
    qty: float
    notional_usd: float
    fee_usd: float = 0.0
    paper_balance: float = 0.0
    is_paper: bool = False
    wallet_name: str = "KRİPTO"


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    precision = max(0, int(round(-math.log10(step))))
    floored = math.floor(value / step + 1e-12) * step
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
        leverage: int = 15,
        max_leverage: int = 15,
        stop_loss_pct: float = 2.5,
        trailing_trigger_pct: float = 2.0,
        trailing_callback_pct: float = 0.4,
        trailing_floor_pct: float = 0.2,
        max_consecutive_stops: int = 3,
        cooldown_minutes: int = 0,
        symbol_stop_cooldown_minutes: int = 240,
        signal_cancel_stop_usd: float = 1.5,
        risk_per_trade_pct: float = 0.0,
        state_file: Path | None = None,
        timeout: float = 10.0,
        proxy_url: str = "",
        paper_mode: bool = False,
        paper_balance_usd: float = 100.0,
        taker_fee_pct: float = 0.05,
        wallet_name: str = "KRİPTO",
        high_lev_fixed_usd: bool = False,
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
        self.max_consecutive_stops = max_consecutive_stops
        self.cooldown_minutes = cooldown_minutes
        self.symbol_stop_cooldown_minutes = max(0, symbol_stop_cooldown_minutes)
        self.signal_cancel_stop_usd = max(0.1, float(signal_cancel_stop_usd))
        self.risk_per_trade_pct = max(0.0, float(risk_per_trade_pct))
        self.paper_mode = bool(paper_mode)
        self.paper_balance_usd = max(1.0, float(paper_balance_usd))
        self.taker_fee_pct = max(0.0, float(taker_fee_pct))
        self.wallet_name = wallet_name.strip() or "KRİPTO"
        self.high_lev_fixed_usd = bool(high_lev_fixed_usd)
        self.last_reject_reason: str = ""
        self.state_file = state_file or (
            Path(__file__).resolve().parent / "trade_state.json"
        )
        self.timeout = timeout
        self.proxy_url = proxy_url.strip()
        self._lock = threading.RLock()
        self._filters_cache: dict[str, dict] = {}
        self._pending_close_events: list[dict] = []
        self._state = self._load_state()

    @property
    def is_configured(self) -> bool:
        return True if self.paper_mode else bool(self.api_key and self.api_secret)

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
                    data.setdefault("paused", False)
                    data.setdefault("paper_balance", self.paper_balance_usd)
                    data.setdefault("paper_initial_balance", self.paper_balance_usd)
                    data.setdefault("paper_total_trades", 0)
                    data.setdefault("paper_win_trades", 0)
                    data.setdefault("paper_total_fees", 0.0)
                    data.setdefault("paper_realized_pnl", 0.0)
                    data.setdefault("closed_history", [])
                    data.setdefault("opened_signals", {})
                    return data
            except Exception:
                pass
        return {
            "open_positions": {},
            "consecutive_stops": 0,
            "cooldown_until": 0,
            "symbol_cooldowns": {},
            "paused": False,
            "paper_balance": self.paper_balance_usd,
            "paper_initial_balance": self.paper_balance_usd,
            "paper_total_trades": 0,
            "paper_win_trades": 0,
            "paper_total_fees": 0.0,
            "paper_realized_pnl": 0.0,
            "closed_history": [],
            "opened_signals": {},
        }

    def get_paper_summary(self) -> dict:
        with self._lock:
            bal = float(self._state.get("paper_balance", self.paper_balance_usd))
            init_bal = float(self._state.get("paper_initial_balance", self.paper_balance_usd))
            net_usd = bal - init_bal
            net_pct = (net_usd / init_bal * 100.0) if init_bal > 0 else 0.0
            return {
                "balance": round(bal, 2),
                "initial_balance": round(init_bal, 2),
                "net_usd": round(net_usd, 2),
                "net_pct": round(net_pct, 2),
                "total_trades": int(self._state.get("paper_total_trades", 0)),
                "win_trades": int(self._state.get("paper_win_trades", 0)),
                "total_fees": round(float(self._state.get("paper_total_fees", 0.0)), 3),
                "taker_fee_pct": self.taker_fee_pct,
            }

    def reset_paper_account(self, balance: float | None = None) -> dict:
        new_bal = float(balance) if balance is not None else self.paper_balance_usd
        with self._lock:
            self._state["open_positions"] = {}
            self._state["consecutive_stops"] = 0
            self._state["cooldown_until"] = 0
            self._state["symbol_cooldowns"] = {}
            self._state["paper_balance"] = new_bal
            self._state["paper_initial_balance"] = new_bal
            self._state["paper_total_trades"] = 0
            self._state["paper_win_trades"] = 0
            self._state["paper_total_fees"] = 0.0
            self._state["paper_realized_pnl"] = 0.0
            self._state["closed_history"] = []
            self._save_state()
        return self.get_paper_summary()

    def get_daily_report(self, date_tr: str | None = None) -> dict:
        tz_tr = timezone(timedelta(hours=3))
        target_day = date_tr or datetime.now(tz_tr).strftime("%Y-%m-%d")
        with self._lock:
            hist: list[dict] = list(self._state.get("closed_history", []))
            today_rows = [r for r in hist if r.get("date_tr") == target_day]
            wins = [r for r in today_rows if float(r.get("pnl_usd", 0.0)) > 0]
            losses = [r for r in today_rows if float(r.get("pnl_usd", 0.0)) <= 0]
            stops = [r for r in today_rows if bool(r.get("is_stop")) or float(r.get("pnl_usd", 0.0)) < 0]
            gross_win = sum(float(r.get("pnl_usd", 0.0)) for r in wins)
            gross_loss = sum(float(r.get("pnl_usd", 0.0)) for r in losses)
            fees = sum(float(r.get("fee_usd", 0.0)) for r in today_rows)
            return {
                "date_tr": target_day,
                "wallet_name": self.wallet_name,
                "today_trades": len(today_rows),
                "today_wins": len(wins),
                "today_stops": len(stops),
                "today_profit_usd": round(gross_win, 2),
                "today_loss_usd": round(gross_loss, 2),
                "today_net_usd": round(gross_win + gross_loss, 2),
                "today_fees_usd": round(fees, 3),
                "recent_trades": hist[-15:],
                "open_positions": dict(self._state.get("open_positions", {})),
                "summary": self.get_paper_summary(),
            }

    def export_cloud_snapshot(self) -> dict:
        with self._lock:
            return {
                "wallet_name": self.wallet_name,
                "paper_balance": round(float(self._state.get("paper_balance", self.paper_balance_usd)), 4),
                "paper_initial_balance": round(float(self._state.get("paper_initial_balance", self.paper_balance_usd)), 2),
                "paper_total_trades": int(self._state.get("paper_total_trades", 0)),
                "paper_win_trades": int(self._state.get("paper_win_trades", 0)),
                "paper_total_fees": round(float(self._state.get("paper_total_fees", 0.0)), 4),
                "paper_realized_pnl": round(float(self._state.get("paper_realized_pnl", 0.0)), 4),
                "paused": bool(self._state.get("paused", False)),
                "open_positions": dict(self._state.get("open_positions", {})),
                "closed_history": list(self._state.get("closed_history", []))[-20:],
            }

    def import_cloud_snapshot(self, snap: dict) -> None:
        if not isinstance(snap, dict):
            return
        with self._lock:
            for k in (
                "paper_balance",
                "paper_initial_balance",
                "paper_total_trades",
                "paper_win_trades",
                "paper_total_fees",
                "paper_realized_pnl",
                "paused",
            ):
                if k in snap:
                    self._state[k] = snap[k]
            if isinstance(snap.get("open_positions"), dict):
                self._state["open_positions"] = snap["open_positions"]
            if isinstance(snap.get("closed_history"), list):
                self._state["closed_history"] = snap["closed_history"]
            self._save_state()

    def is_paused(self) -> bool:
        with self._lock:
            return bool(self._state.get("paused", False))

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._state["paused"] = bool(paused)
            self._save_state()

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
        if self.paper_mode or not self._is_um_futures() or not self.is_configured:
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

    def place_exchange_stop_loss(
        self, symbol: str, sl_price: float, pos_side: str = "LONG"
    ) -> str | None:
        """Place a real exchange-side STOP_MARKET order on Binance (millisecond execution)."""
        if self.paper_mode:
            return "PAPER_SL"
        if not self._is_um_futures() or not self.is_configured or sl_price <= 0:
            return None
        self._ensure_filters()
        tick = float(self._filters_cache.get(symbol, {}).get("tickSize", 0.0001))
        rounded_sl = _round_step(sl_price, tick)
        if rounded_sl <= 0:
            rounded_sl = sl_price

        close_side = "SELL" if str(pos_side).upper() == "LONG" else "BUY"
        self.cancel_exchange_stop_losses(symbol)
        try:
            if self.market == "papi":
                res = self._signed_request(
                    "POST",
                    "/papi/v1/um/algo/order",
                    {
                        "symbol": symbol,
                        "side": close_side,
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
                        "side": close_side,
                        "type": "STOP_MARKET",
                        "stopPrice": f"{rounded_sl:g}",
                        "closePosition": "true",
                        "workingType": "CONTRACT_PRICE",
                    },
                )
                return str(res.get("orderId") or "")
        except Exception:
            return None

    def get_account_equity(self) -> float:
        """Return total account equity in USDT (used for dynamic small-account risk sizing)."""
        if self.paper_mode:
            return float(self._state.get("paper_balance", self.paper_balance_usd))
        try:
            if self.market == "papi":
                acct = self._signed_request("GET", "/papi/v1/account")
                return float(
                    acct.get("accountEquity")
                    or acct.get("virtualMaxWithdrawAmount")
                    or 0.0
                )
        except Exception:
            pass
        return self.get_free_usdt()

    def get_free_usdt(self) -> float:
        if self.paper_mode:
            bal = float(self._state.get("paper_balance", self.paper_balance_usd))
            used_margin = sum(
                float(p.get("notional_usd", 0.0)) / max(1, int(p.get("leverage", self.leverage)))
                for p in self._state.get("open_positions", {}).values()
            )
            return max(0.0, bal - used_margin)
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
        """Return currently open positions (from Binance on futures/papi, or state on spot/paper)."""
        if self.paper_mode:
            return dict(self._state["open_positions"])
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
        sl_price: float | None = None,
        side: Literal["LONG", "SHORT"] = "LONG",
        use_trailing: bool = True,
        be_trigger_pct: float = 0.0,
        entry_bar_open_time: int = 0,
        allow_wide_sl: bool = False,
        v3_mode: bool = False,
        tp1_pct: float = 2.00,
        fee_buffer_pct: float = 0.15,
        trail_dist_pct: float = 1.10,
        close_stop_pct: float = 2.20,
        hard_stop_pct: float = 2.20,
    ) -> TradeExecution | None:
        """Open a LONG or SHORT position if under max_positions, not in cooldown, and balance allows."""
        self.last_reject_reason = ""
        pos_side: Literal["LONG", "SHORT"] = "SHORT" if str(side).upper() == "SHORT" else "LONG"
        if pos_side == "SHORT" and not self._is_um_futures():
            self.last_reject_reason = "Spot piyasada SHORT açılamaz."
            return None
        if self.is_paused():
            self.last_reject_reason = "Bot /durdur ile durduruldu."
            return None
        if self.is_in_cooldown():
            self.last_reject_reason = f"Genel mola aktif ({self.cooldown_remaining_min()} dk kaldı)."
            return None
        if self.is_symbol_in_cooldown(symbol):
            self.last_reject_reason = f"{symbol} stop bekleme süresinde."
            return None
        live_open = self.get_live_open_positions()
        tracked = self._state["open_positions"]

        # Count unique open symbols across Binance live + local state
        all_open_syms = set(live_open.keys()) | set(tracked.keys())
        if symbol in all_open_syms:
            self.last_reject_reason = f"{symbol} zaten açık pozisyonda."
            return None
        if len(all_open_syms) >= self.max_positions:
            self.last_reject_reason = (
                f"{self.wallet_name} Max {self.max_positions} pozisyon dolu "
                f"(Açık: {', '.join(sorted(all_open_syms))})."
            )
            return None

        effective_min_usd = self.min_usd
        notional = max(self.min_usd, min(self.max_usd, target_usd))
        est_sl_pct = close_stop_pct if v3_mode else self.stop_loss_pct
        if not v3_mode and allow_wide_sl and sl_price is not None and price > 0:
            if pos_side == "LONG" and 0 < sl_price < price * 0.995:
                est_sl_pct = ((price - sl_price) / price) * 100.0
            elif pos_side == "SHORT" and sl_price > price * 1.005:
                est_sl_pct = ((sl_price - price) / price) * 100.0

        free_usdt = self.get_free_usdt()
        effective_lev = min(self.max_leverage, self.leverage)

        if self.high_lev_fixed_usd and not self.paper_mode and self._is_um_futures():
            # Set highest supported leverage on Binance FIRST so small accounts ($19) easily open $100-$150 x 3 slots
            lev_path = "/papi/v1/um/leverage" if self.market == "papi" else "/fapi/v1/leverage"
            brk_path = "/papi/v1/um/leverageBracket" if self.market == "papi" else "/fapi/v1/leverageBracket"
            cand_levs: list[int] = []
            try:
                brk_res = self._signed_request("GET", brk_path, {"symbol": symbol})
                brk_item = brk_res[0] if isinstance(brk_res, list) and brk_res else brk_res
                brackets = brk_item.get("brackets", []) if isinstance(brk_item, dict) else []
                if brackets:
                    max_sym_lev = max(
                        int(b.get("initialLeverage") or 0)
                        for b in brackets
                        if float(b.get("notionalCap") or 1e9) >= self.max_usd
                    )
                    if max_sym_lev > 0:
                        cand_levs.append(min(self.max_leverage, max_sym_lev))
            except Exception:
                pass
            for lv in (125, 100, 75, 50, 45, 40, 35, 30, 25, 20, 16, 15, 12, 10, 8, 5):
                if lv <= self.max_leverage and lv not in cand_levs:
                    cand_levs.append(lv)
            if not cand_levs:
                cand_levs = [self.max_leverage]
            actual_lev = 0
            for try_lev in cand_levs:
                try:
                    lev_res = self._signed_request(
                        "POST", lev_path, {"symbol": symbol, "leverage": try_lev}
                    )
                    actual_lev = int(lev_res.get("leverage") or try_lev)
                    break
                except Exception:
                    continue
            if actual_lev <= 0:
                self.last_reject_reason = f"{symbol} kaldıraç ayarlanamadı."
                return None
            effective_lev = max(1, actual_lev)
            rem_slots = max(1, self.max_positions - len(all_open_syms))
            slot_margin = (free_usdt / rem_slots) * 0.92
            slot_notional = slot_margin * effective_lev
            max_afford = free_usdt * effective_lev * 0.95
            # STRICT MIN $100 .. MAX $150: NEVER open $30 or $50 positions!
            if max_afford < self.min_usd:
                self.last_reject_reason = (
                    f"{self.wallet_name} marj min ${self.min_usd:g} için yetersiz "
                    f"(Serbest: ${free_usdt:.2f}, {effective_lev}x -> Max ${max_afford:.2f})."
                )
                return None
            if slot_notional >= self.min_usd:
                notional = min(self.max_usd, slot_notional)
            else:
                notional = self.min_usd
            effective_min_usd = self.min_usd
        else:
            if self.risk_per_trade_pct > 0 and est_sl_pct > 0:
                equity = self.get_account_equity()
                if equity > 0:
                    risk_notional = equity * (self.risk_per_trade_pct / est_sl_pct)
                    if self.paper_mode:
                        # Strictly honor min_usd ($100) .. max_usd ($150) bounds in paper simulation
                        notional = max(self.min_usd, min(self.max_usd, risk_notional))
                        effective_min_usd = self.min_usd
                    else:
                        min_floor = 12.0 if allow_wide_sl else 20.0
                        notional = max(min_floor, min(self.max_usd, risk_notional))
                        effective_min_usd = min(self.min_usd, max(10.0 if allow_wide_sl else 15.0, notional * 0.85))

            if not self.paper_mode and allow_wide_sl and est_sl_pct > 2.5:
                # Cap leverage safely on real exchange so wide ATR stops never get liquidated before stop hits
                effective_lev = min(effective_lev, max(2, int(80.0 / est_sl_pct)))
            if self._is_um_futures() and free_usdt > 0:
                if free_usdt * effective_lev * 0.90 < effective_min_usd:
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
                if max_affordable_notional < effective_min_usd:
                    self.last_reject_reason = f"{self.wallet_name} yetersiz serbest marj (${free_usdt:.2f})."
                    return None
                notional = max(effective_min_usd, min(self.max_usd, max_affordable_notional))

        if self.paper_mode:
            qty = round(notional / price, 6) if price > 0 else 0.0
            if qty <= 0:
                self.last_reject_reason = "Geçersiz fiyat/miktar."
                return None
        else:
            self._ensure_filters()
            rules = self._filters_cache.get(
                symbol, {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
            )
            qty = _round_step(notional / price, rules["stepSize"])
            if self.high_lev_fixed_usd and qty * price < self.min_usd:
                bumped_qty = _round_step(qty + rules["stepSize"], rules["stepSize"])
                max_cap = min(self.max_usd, free_usdt * effective_lev * 0.95)
                if bumped_qty * price <= max_cap * 1.01:
                    qty = bumped_qty
            if qty < rules["minQty"]:
                self.last_reject_reason = f"Miktar minQty altında ({qty:g} < {rules['minQty']:g})."
                return None
            actual_notional = qty * price
            min_notional_gate = (
                self.min_usd * 0.98 if self.high_lev_fixed_usd else effective_min_usd * 0.90
            )
            if actual_notional < min_notional_gate or actual_notional < rules["minNotional"]:
                self.last_reject_reason = f"Pozisyon tutarı min sınırın altında (${actual_notional:.2f} < ${min_notional_gate:.2f})."
                return None

        order_side: Literal["BUY", "SELL"] = "BUY" if pos_side == "LONG" else "SELL"
        entry_fee = 0.0
        if self.paper_mode:
            exec_price = price
            exec_qty = qty
            order_id = f"PAPER_{symbol}_{int(time.time())}"
            actual_not = round(exec_qty * exec_price, 2)
            entry_fee = round(actual_not * (self.taker_fee_pct / 100.0), 4)
            self._state["paper_balance"] = round(
                float(self._state.get("paper_balance", self.paper_balance_usd)) - entry_fee, 4
            )
            self._state["paper_total_fees"] = round(
                float(self._state.get("paper_total_fees", 0.0)) + entry_fee, 4
            )
        elif self._is_um_futures():
            lev_path = "/papi/v1/um/leverage" if self.market == "papi" else "/fapi/v1/leverage"
            ord_path = "/papi/v1/um/order" if self.market == "papi" else "/fapi/v1/order"
            if not self.high_lev_fixed_usd:
                for try_lev in dict.fromkeys([effective_lev, min(15, effective_lev), 10, 5]):
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
                    "side": order_side,
                    "type": "MARKET",
                    "quantity": f"{qty:g}",
                    "newClientOrderId": f"{BOT_CLIENT_ID_PREFIX}{symbol}_{int(time.time())}",
                    "newOrderRespType": "RESULT",
                },
            )
            exec_price = float(res.get("avgPrice") or 0.0)
            exec_qty = float(res.get("executedQty") or 0.0) or qty
            if exec_price <= 0:
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
            order_id = str(res.get("orderId", ""))
            actual_not = round(exec_qty * exec_price, 2)
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
        if v3_mode and pos_side == "LONG":
            final_sl = exec_price * (1.0 - hard_stop_pct / 100.0)
            final_tp = exec_price * (1.0 + tp1_pct / 100.0)
            close_sl_px = exec_price * (1.0 - close_stop_pct / 100.0)
        elif pos_side == "LONG":
            close_sl_px = 0.0
            default_sl = exec_price * (1.0 - self.stop_loss_pct / 100.0)
            if allow_wide_sl and sl_price is not None and 0 < sl_price < exec_price * 0.995:
                final_sl = sl_price
            elif sl_price is not None and default_sl <= sl_price < exec_price * 0.995:
                final_sl = sl_price
            else:
                final_sl = default_sl
            final_tp = (
                tp_price
                if (tp_price is not None and tp_price > exec_price * 1.005)
                else 0.0
            )
        else:
            close_sl_px = 0.0
            default_sl = exec_price * (1.0 + self.stop_loss_pct / 100.0)
            if allow_wide_sl and sl_price is not None and sl_price > exec_price * 1.005:
                final_sl = sl_price
            elif sl_price is not None and exec_price * 1.005 < sl_price <= default_sl:
                final_sl = sl_price
            else:
                final_sl = default_sl
            final_tp = (
                tp_price
                if (tp_price is not None and 0 < tp_price < exec_price * 0.99)
                else 0.0
            )

        algo_sl_id = self.place_exchange_stop_loss(symbol, final_sl, pos_side=pos_side)
        self._state["open_positions"][symbol] = {
            "symbol": symbol,
            "side": pos_side,
            "order_id": order_id,
            "entry_price": exec_price,
            "qty": exec_qty,
            "initial_qty": exec_qty,
            "notional_usd": actual_not,
            "initial_notional_usd": actual_not,
            "sl_price": final_sl,
            "close_sl_price": close_sl_px,
            "tp_price": final_tp,
            "exchange_sl_price": final_sl if algo_sl_id else 0.0,
            "peak_price": exec_price,
            "trailing_active": False,
            "use_trailing": bool(use_trailing),
            "be_trigger_pct": float(be_trigger_pct),
            "be_locked": False,
            "allow_wide_sl": bool(allow_wide_sl or v3_mode),
            "v3_mode": bool(v3_mode),
            "tp1_hit": False,
            "tp1_pct": float(tp1_pct),
            "fee_buffer_pct": float(fee_buffer_pct),
            "trail_dist_pct": float(trail_dist_pct),
            "tp1_realized_pnl_usd": 0.0,
            "tp1_fee_usd": 0.0,
            "entry_bar_open_time": int(entry_bar_open_time),
            "entry_fee_usd": entry_fee,
            "leverage": effective_lev,
            "is_paper": self.paper_mode,
            "opened_at": int(time.time()),
        }
        self._save_state()

        return TradeExecution(
            symbol=symbol,
            side=order_side,
            order_id=order_id,
            price=exec_price,
            qty=exec_qty,
            notional_usd=actual_not,
            fee_usd=entry_fee,
            paper_balance=round(float(self._state.get("paper_balance", self.paper_balance_usd)), 2) if self.paper_mode else 0.0,
            is_paper=self.paper_mode,
            wallet_name=self.wallet_name,
        )

    def close_position(
        self,
        symbol: str,
        fallback_price: float,
        is_stop_loss: bool = False,
        close_reason: str = "",
    ) -> TradeExecution | None:
        """Close an open LONG or SHORT position on Binance."""
        live_open = self.get_live_open_positions()
        tracked_pos = self._state["open_positions"].get(symbol)
        pos = live_open.get(symbol) or tracked_pos
        if not pos:
            return None

        pos_side = str((tracked_pos or pos).get("side") or "LONG").upper()
        close_side: Literal["BUY", "SELL"] = "SELL" if pos_side == "LONG" else "BUY"

        if self.paper_mode:
            qty = float(pos["qty"])
        else:
            self._ensure_filters()
            rules = self._filters_cache.get(symbol, {"stepSize": 0.001, "minQty": 0.001})
            qty = _round_step(float(pos["qty"]), rules["stepSize"])
            if qty < rules["minQty"]:
                qty = float(pos["qty"])

        if self.paper_mode:
            exec_price = fallback_price
            exec_qty = qty
            close_order_id = f"PAPER_CLOSE_{symbol}_{int(time.time())}"
        elif self._is_um_futures():
            ord_path = "/papi/v1/um/order" if self.market == "papi" else "/fapi/v1/order"
            res = self._signed_request(
                "POST",
                ord_path,
                {
                    "symbol": symbol,
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": f"{qty:g}",
                    "reduceOnly": "true",
                },
            )
            exec_price = float(res.get("avgPrice") or 0.0) or fallback_price
            exec_qty = float(res.get("executedQty") or 0.0) or qty
            close_order_id = str(res.get("orderId", ""))
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
            close_order_id = str(res.get("orderId", ""))

        self.cancel_exchange_stop_losses(symbol)
        prev_pos = self._state["open_positions"].pop(symbol, None)
        entry_px = float((prev_pos or pos).get("entry_price") or exec_price)
        gross_pnl = (
            (exec_price - entry_px) * exec_qty
            if pos_side == "LONG"
            else (entry_px - exec_price) * exec_qty
        )

        tp1_prev_net = float((prev_pos or {}).get("tp1_realized_pnl_usd") or 0.0)
        tp1_prev_fee = float((prev_pos or {}).get("tp1_fee_usd") or 0.0)
        init_notional = float((prev_pos or {}).get("initial_notional_usd") or (entry_px * exec_qty))

        total_fee = 0.0
        if self.paper_mode:
            entry_fee = float((prev_pos or {}).get("entry_fee_usd") or 0.0)
            exit_fee = round((exec_qty * exec_price) * (self.taker_fee_pct / 100.0), 4)
            leg_fee = round(entry_fee + exit_fee, 4)
            leg_net = gross_pnl - leg_fee
            total_fee = round(tp1_prev_fee + leg_fee, 4)
            pnl_usd = round(tp1_prev_net + leg_net, 4)
            self._state["paper_balance"] = round(
                float(self._state.get("paper_balance", self.paper_balance_usd)) + gross_pnl - exit_fee, 4
            )
            self._state["paper_total_fees"] = round(
                float(self._state.get("paper_total_fees", 0.0)) + exit_fee, 4
            )
            self._state["paper_total_trades"] = int(self._state.get("paper_total_trades", 0)) + 1
            if pnl_usd > 0:
                self._state["paper_win_trades"] = int(self._state.get("paper_win_trades", 0)) + 1
            self._state["paper_realized_pnl"] = round(
                float(self._state.get("paper_realized_pnl", 0.0)) + leg_net, 4
            )
        else:
            pnl_usd = round(tp1_prev_net + gross_pnl, 4)

        tz_tr = timezone(timedelta(hours=3))
        now_tr = datetime.now(tz_tr)
        if init_notional > 0 and tp1_prev_fee > 0:
            net_pct_rec = (pnl_usd / init_notional) * 100.0
        else:
            raw_pct = (
                ((exec_price - entry_px) / entry_px) * 100.0
                if (entry_px > 0 and pos_side == "LONG")
                else (((entry_px - exec_price) / entry_px) * 100.0 if entry_px > 0 else 0.0)
            )
            net_pct_rec = raw_pct - (2.0 * self.taker_fee_pct) if self.paper_mode else raw_pct
        hist_list: list[dict] = self._state.setdefault("closed_history", [])
        hist_list.append(
            {
                "ts": int(time.time()),
                "date_tr": now_tr.strftime("%Y-%m-%d"),
                "time_tr": now_tr.strftime("%H:%M"),
                "symbol": symbol,
                "wallet": self.wallet_name,
                "side": pos_side,
                "reason": close_reason or ("STOP" if is_stop_loss else "KAPANIŞ"),
                "is_stop": bool(is_stop_loss and pnl_usd < 0),
                "notional_usd": round(init_notional, 2),
                "entry_price": entry_px,
                "exit_price": exec_price,
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(net_pct_rec, 2),
                "fee_usd": round(total_fee, 3),
                "balance_after": round(float(self._state.get("paper_balance", self.paper_balance_usd)), 2),
            }
        )
        if len(hist_list) > 200:
            self._state["closed_history"] = hist_list[-200:]

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
            side=close_side,
            order_id=close_order_id,
            price=exec_price,
            qty=exec_qty,
            notional_usd=round(exec_qty * exec_price, 2),
            fee_usd=total_fee,
            paper_balance=round(float(self._state.get("paper_balance", self.paper_balance_usd)), 2) if self.paper_mode else 0.0,
            is_paper=self.paper_mode,
            wallet_name=self.wallet_name,
        )

    def has_open_positions(self) -> bool:
        with self._lock:
            return bool(self._state["open_positions"])

    def has_pending_close_events(self) -> bool:
        with self._lock:
            return bool(self._pending_close_events)

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

    def _inspect_bot_position(self, symbol: str, pos_side: str, current_qty: float) -> tuple[bool, bool, float]:
        """Return (is_bot_opened, tp1_already_hit, initial_qty) from Binance recent orders."""
        entry_order_side = "BUY" if pos_side.upper() == "LONG" else "SELL"
        exit_order_side = "SELL" if pos_side.upper() == "LONG" else "BUY"
        path = "/papi/v1/um/allOrders" if self.market == "papi" else "/fapi/v1/allOrders"
        try:
            orders = self._signed_request("GET", path, {"symbol": symbol, "limit": 10})
            if not isinstance(orders, list):
                return False, False, current_qty
            matching = [
                o
                for o in orders
                if str(o.get("side", "")).upper() == entry_order_side
                and float(o.get("executedQty") or 0.0) > 0
                and not bool(o.get("reduceOnly", False))
            ]
            if not matching:
                return False, False, current_qty
            latest = max(matching, key=lambda o: int(o.get("updateTime") or o.get("time") or 0))
            is_bot = str(latest.get("clientOrderId") or "").startswith(BOT_CLIENT_ID_PREFIX)
            if not is_bot:
                return False, False, current_qty
            entry_ts = int(latest.get("updateTime") or latest.get("time") or 0)
            entry_qty = float(latest.get("executedQty") or current_qty)
            partial_exits = [
                o
                for o in orders
                if str(o.get("side", "")).upper() == exit_order_side
                and float(o.get("executedQty") or 0.0) > 0
                and bool(o.get("reduceOnly", False))
                and int(o.get("updateTime") or o.get("time") or 0) >= entry_ts
            ]
            tp1_done = bool(partial_exits) or (entry_qty > 0 and current_qty < entry_qty * 0.75)
            return True, tp1_done, max(current_qty, entry_qty)
        except Exception:
            return False, False, current_qty

    def _is_bot_opened_position(self, symbol: str, pos_side: str) -> bool:
        """Check Binance recent orders to see if an untracked live position carries our BOT4H_ tag."""
        is_bot, _, _ = self._inspect_bot_position(symbol, pos_side, 0.0)
        return is_bot

    def sync_live_positions(self) -> dict[str, dict]:
        """
        Sync ONLY bot-opened positions against Binance:
        - In paper_mode, returns tracked paper positions without touching Binance.
        - Re-adopt bot positions tagged with BOT4H_ after a restart/redeploy.
        - NEVER adopt or touch positions opened manually by the user!
        - Keep entry_price accurate for bot-opened positions.
        - Remove bot-opened positions that were already closed on Binance.
        """
        if self.paper_mode or not self._is_um_futures() or not self.is_configured:
            with self._lock:
                return dict(self._state["open_positions"])

        live = self.get_live_open_positions()
        with self._lock:
            tracked = self._state["open_positions"]
            dirty = False

            for sym in list(tracked.keys()):
                if sym not in live:
                    prev = tracked.pop(sym, None)
                    dirty = True
                    if prev:
                        self._record_external_close(sym, prev)
                    continue
                live_ep = float(live[sym].get("entry_price") or 0.0)
                if live_ep > 0 and abs(live_ep - float(tracked[sym].get("entry_price") or 0.0)) / live_ep > 0.001:
                    tracked[sym]["entry_price"] = live_ep
                    pos_side = str(tracked[sym].get("side") or "LONG").upper()
                    if (
                        not tracked[sym].get("trailing_active")
                        and not tracked[sym].get("be_locked")
                        and not tracked[sym].get("allow_wide_sl")
                        and not tracked[sym].get("signal_lost")
                    ):
                        tracked[sym]["sl_price"] = (
                            live_ep * (1.0 - self.stop_loss_pct / 100.0)
                            if pos_side == "LONG"
                            else live_ep * (1.0 + self.stop_loss_pct / 100.0)
                        )
                    dirty = True

            for sym, lpos in live.items():
                if sym in tracked:
                    continue
                pos_side = str(lpos.get("side") or "LONG").upper()
                ep = float(lpos.get("entry_price") or 0.0)
                qty = float(lpos.get("qty") or 0.0)
                if ep <= 0 or qty <= 0:
                    continue
                is_bot, tp1_done, init_qty = self._inspect_bot_position(sym, pos_side, qty)
                if not is_bot:
                    continue
                is_long = pos_side == "LONG"
                sl_px = (
                    (ep * 1.0015 if tp1_done else ep * 0.978)
                    if is_long
                    else ep * (1.0 + self.stop_loss_pct / 100.0)
                )
                tracked[sym] = {
                    "symbol": sym,
                    "side": pos_side,
                    "order_id": "recovered",
                    "entry_price": ep,
                    "qty": qty,
                    "initial_qty": init_qty,
                    "notional_usd": round(qty * ep, 2),
                    "initial_notional_usd": round(init_qty * ep, 2),
                    "sl_price": sl_px,
                    "close_sl_price": ep * 0.978 if is_long else 0.0,
                    "tp_price": ep * 1.02 if is_long else ep * 0.955,
                    "exchange_sl_price": sl_px,
                    "peak_price": ep,
                    "trailing_active": False,
                    "use_trailing": False,
                    "be_trigger_pct": 0.0 if is_long else 1.5,
                    "be_locked": bool(is_long and tp1_done),
                    "allow_wide_sl": is_long,
                    "v3_mode": is_long,
                    "tp1_hit": bool(is_long and tp1_done),
                    "tp1_pct": 2.00,
                    "fee_buffer_pct": 0.15,
                    "trail_dist_pct": 1.10,
                    "tp1_realized_pnl_usd": 0.0,
                    "tp1_fee_usd": 0.0,
                    "entry_bar_open_time": 0,
                    "opened_at": int(time.time()),
                }
                dirty = True

            if dirty:
                self._save_state()
            return dict(tracked)

    def _record_external_close(self, symbol: str, prev: dict) -> None:
        """Build a close event when Binance's own STOP_MARKET/TP closed a tracked bot position."""
        entry_px = float(prev.get("entry_price") or 0.0)
        qty = float(prev.get("qty") or 0.0)
        pos_side = str(prev.get("side") or "LONG").upper()
        sl_px = float(prev.get("sl_price") or 0.0)
        exit_px = sl_px if sl_px > 0 else (self.fetch_public_price(symbol) or entry_px)
        order_id = str(prev.get("order_id") or "exchange")
        pnl_usd: float | None = None

        try:
            ut_path = "/papi/v1/um/userTrades" if self.market == "papi" else "/fapi/v1/userTrades"
            close_side = "SELL" if pos_side == "LONG" else "BUY"
            trades = self._signed_request("GET", ut_path, {"symbol": symbol, "limit": 5})
            if isinstance(trades, list):
                closing = [
                    t
                    for t in trades
                    if str(t.get("side", "")).upper() == close_side
                    and int(t.get("time") or 0) >= int(prev.get("opened_at") or 0) * 1000
                ]
                if closing:
                    tot_q = sum(float(t.get("qty") or 0.0) for t in closing)
                    if tot_q > 0:
                        exit_px = sum(float(t.get("price") or 0.0) * float(t.get("qty") or 0.0) for t in closing) / tot_q
                    pnl_usd = sum(float(t.get("realizedPnl") or 0.0) for t in closing)
                    order_id = str(closing[-1].get("orderId") or order_id)
        except Exception:
            pass

        if pnl_usd is None:
            if pos_side == "LONG":
                pnl_usd = (exit_px - entry_px) * qty
            else:
                pnl_usd = (entry_px - exit_px) * qty

        if entry_px > 0:
            pnl_pct = (
                ((exit_px - entry_px) / entry_px) * 100.0
                if pos_side == "LONG"
                else ((entry_px - exit_px) / entry_px) * 100.0
            )
        else:
            pnl_pct = 0.0

        is_trailing = bool(prev.get("trailing_active"))
        is_be = bool(prev.get("be_locked"))
        if is_trailing:
            reason = "TRAILING STOP (Binance)"
        elif is_be:
            reason = (
                "KAPAT (Binance Kâr Kilidi +%0.40)"
                if pos_side == "SHORT"
                else "SAT (Binance Kâr Kilidi +%0.30)"
            )
        else:
            reason = f"STOP (Binance -%{self.stop_loss_pct:g})"
        if pnl_usd < 0 and not is_trailing and not is_be:
            self._state["consecutive_stops"] = int(self._state.get("consecutive_stops", 0)) + 1
            if self.symbol_stop_cooldown_minutes > 0:
                self._state.setdefault("symbol_cooldowns", {})[symbol] = int(
                    time.time() + self.symbol_stop_cooldown_minutes * 60
                )
        elif pnl_usd >= 0:
            self._state["consecutive_stops"] = 0
            self._state["cooldown_until"] = 0

        self._pending_close_events.append(
            {
                "symbol": symbol,
                "side": pos_side,
                "reason": reason,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round(pnl_pct, 2),
                "order_id": order_id,
                "cooldown_triggered": self.is_in_cooldown(),
                "cooldown_minutes": self.cooldown_minutes,
                "consecutive_stops": int(self._state.get("consecutive_stops", 0)),
            }
        )

    def apply_signal_cancel_stop(self, symbol: str, current_price: float) -> dict | None:
        """
        When 3/5 buy signal disappears on an open position (before trailing activates):
        - Never close early at $0 if in profit or small loss (< signal_cancel_stop_usd).
        - Tighten SL to -$signal_cancel_stop_usd (default -$1.50) and update Binance STOP_MARKET.
        - If current price is already at or below that -$1.50 stop, close immediately.
        """
        with self._lock:
            pos = self._state["open_positions"].get(symbol)
            if not pos or pos.get("trailing_active"):
                return None
            entry_px = float(pos.get("entry_price") or 0.0)
            qty = float(pos.get("qty") or 0.0)
            if entry_px <= 0 or qty <= 0 or current_price <= 0:
                return None

            target_sl = max(entry_px * 0.5, entry_px - (self.signal_cancel_stop_usd / qty))
            prev_sl = float(pos.get("sl_price") or 0.0)
            new_sl = max(prev_sl, target_sl)

            if current_price <= new_sl:
                closed = self.close_position(
                    symbol, fallback_price=current_price, is_stop_loss=True
                )
                if not closed:
                    return None
                pnl_usd = (closed.price - entry_px) * closed.qty
                pnl_pct = ((closed.price - entry_px) / entry_px) * 100.0
                return {
                    "action": "closed",
                    "event": {
                        "symbol": symbol,
                        "reason": f"STOP (Sinyal Bozuldu -${self.signal_cancel_stop_usd:g})",
                        "entry_price": entry_px,
                        "exit_price": closed.price,
                        "pnl_usd": round(pnl_usd, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "order_id": closed.order_id,
                    },
                }

            if pos.get("signal_lost") and new_sl <= prev_sl * 1.0001:
                return None

            pos["sl_price"] = new_sl
            pos["signal_lost"] = True
            if self.place_exchange_stop_loss(symbol, new_sl):
                pos["exchange_sl_price"] = new_sl
            self._save_state()
            return {
                "action": "tightened",
                "symbol": symbol,
                "entry_price": entry_px,
                "sl_price": new_sl,
                "current_price": current_price,
                "stop_usd": self.signal_cancel_stop_usd,
            }

    def _partial_close_v3_tp1(self, symbol: str, pos: dict, curr_price: float) -> dict | None:
        """Close 50% of a v3_mode LONG position at TP1 (+1.0%) and lock SL to +0.15% / peak trailing."""
        entry_px = float(pos.get("entry_price") or 0.0)
        full_qty = float(pos.get("qty") or 0.0)
        if entry_px <= 0 or full_qty <= 0:
            return None

        if self.paper_mode:
            half_qty = round(full_qty * 0.5, 6)
            exec_px = curr_price
            tp1_order_id = f"PAPER_TP1_{symbol}_{int(time.time())}"
        else:
            self._ensure_filters()
            rules = self._filters_cache.get(symbol, {"stepSize": 0.001, "minQty": 0.001})
            half_qty = _round_step(full_qty * 0.5, rules["stepSize"])
            if half_qty < rules["minQty"] or (full_qty - half_qty) < rules["minQty"]:
                return None
            ord_path = "/papi/v1/um/order" if self.market == "papi" else "/fapi/v1/order"
            res = self._signed_request(
                "POST",
                ord_path,
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": f"{half_qty:g}",
                    "reduceOnly": "true",
                },
            )
            exec_px = float(res.get("avgPrice") or 0.0) or curr_price
            half_qty = float(res.get("executedQty") or 0.0) or half_qty
            tp1_order_id = str(res.get("orderId", ""))

        rem_qty = max(0.0, full_qty - half_qty)
        gross_leg1 = (exec_px - entry_px) * half_qty
        entry_fee_full = float(pos.get("entry_fee_usd") or 0.0)
        leg1_entry_fee = round(entry_fee_full * 0.5, 4)
        rem_entry_fee = round(max(0.0, entry_fee_full - leg1_entry_fee), 4)

        if self.paper_mode:
            leg1_exit_fee = round((half_qty * exec_px) * (self.taker_fee_pct / 100.0), 4)
            leg1_fee = round(leg1_entry_fee + leg1_exit_fee, 4)
            leg1_net = round(gross_leg1 - leg1_fee, 4)
            self._state["paper_balance"] = round(
                float(self._state.get("paper_balance", self.paper_balance_usd)) + gross_leg1 - leg1_exit_fee, 4
            )
            self._state["paper_total_fees"] = round(
                float(self._state.get("paper_total_fees", 0.0)) + leg1_exit_fee, 4
            )
            self._state["paper_realized_pnl"] = round(
                float(self._state.get("paper_realized_pnl", 0.0)) + leg1_net, 4
            )
        else:
            leg1_fee = 0.0
            leg1_net = round(gross_leg1, 4)

        fee_buf_pct = float(pos.get("fee_buffer_pct") or 0.15)
        trail_pct = float(pos.get("trail_dist_pct") or 1.10)
        peak_px = max(float(pos.get("peak_price") or curr_price), curr_price)
        locked_sl = max(
            entry_px * (1.0 + fee_buf_pct / 100.0),
            peak_px * (1.0 - trail_pct / 100.0),
        )

        pos["qty"] = rem_qty
        pos["notional_usd"] = round(rem_qty * entry_px, 2)
        pos["entry_fee_usd"] = rem_entry_fee
        pos["tp1_hit"] = True
        pos["be_locked"] = True
        pos["tp1_realized_pnl_usd"] = leg1_net
        pos["tp1_fee_usd"] = leg1_fee
        pos["peak_price"] = peak_px
        pos["sl_price"] = locked_sl
        if curr_price > locked_sl and self.place_exchange_stop_loss(symbol, locked_sl, pos_side="LONG"):
            pos["exchange_sl_price"] = locked_sl

        raw_pct = ((exec_px - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
        net_pct = raw_pct - (2.0 * self.taker_fee_pct) if self.paper_mode else raw_pct
        return {
            "symbol": symbol,
            "side": "LONG",
            "is_partial": True,
            "reason": "TP1 (%50 KÂR AL — Kalan %50 Trailing)",
            "entry_price": entry_px,
            "exit_price": exec_px,
            "new_sl_price": locked_sl,
            "pnl_usd": round(leg1_net, 2),
            "pnl_pct": round(net_pct, 2),
            "fee_usd": round(leg1_fee, 3),
            "is_paper": self.paper_mode,
            "wallet_name": self.wallet_name,
            "paper_balance": round(float(self._state.get("paper_balance", self.paper_balance_usd)), 2),
            "paper_initial": float(self._state.get("paper_initial_balance", self.paper_balance_usd)),
            "paper_trades": int(self._state.get("paper_total_trades", 0)),
            "paper_wins": int(self._state.get("paper_win_trades", 0)),
            "order_id": tp1_order_id,
        }

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
        with self._lock:
            events: list[dict] = list(self._pending_close_events)
            self._pending_close_events.clear()
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
                pos_side = str(pos.get("side") or "LONG").upper()
                if pos_side == "LONG":
                    pnl_pct = (
                        ((curr_price - entry_px) / entry_px) * 100.0
                        if entry_px > 0
                        else 0.0
                    )
                    peak_px = max(float(pos.get("peak_price") or entry_px), curr_price)
                    if peak_px > float(pos.get("peak_price") or 0.0):
                        pos["peak_price"] = peak_px
                        state_dirty = True
                else:
                    pnl_pct = (
                        ((entry_px - curr_price) / entry_px) * 100.0
                        if entry_px > 0
                        else 0.0
                    )
                    prev_peak = float(pos.get("peak_price") or entry_px)
                    peak_px = min(prev_peak, curr_price)
                    if peak_px < prev_peak:
                        pos["peak_price"] = peak_px
                        state_dirty = True

                use_trailing = bool(pos.get("use_trailing", True))
                trailing_active = bool(pos.get("trailing_active", False))
                if use_trailing and not trailing_active and pnl_pct >= self.trailing_trigger_pct:
                    trailing_active = True
                    pos["trailing_active"] = True
                    state_dirty = True

                reason: str | None = None
                tp_px = float(pos.get("tp_price") or 0.0)
                if pos_side == "LONG" and pos.get("v3_mode"):
                    tp1_hit = bool(pos.get("tp1_hit", False))
                    if not tp1_hit and tp_px > 0 and curr_price >= tp_px:
                        tp1_ev = self._partial_close_v3_tp1(sym, pos, curr_price)
                        if tp1_ev is not None:
                            events.append(tp1_ev)
                            state_dirty = True
                            tp1_hit = True
                    if tp1_hit:
                        fee_buf_pct = float(pos.get("fee_buffer_pct") or 0.15)
                        trail_pct = float(pos.get("trail_dist_pct") or 1.10)
                        floor_sl = entry_px * (1.0 + fee_buf_pct / 100.0)
                        trail_sl = peak_px * (1.0 - trail_pct / 100.0)
                        new_sl = max(float(pos.get("sl_price") or 0.0), floor_sl, trail_sl)
                        if new_sl > float(pos.get("sl_price") or 0.0):
                            pos["sl_price"] = new_sl
                            state_dirty = True
                            ex_sl = float(pos.get("exchange_sl_price") or 0.0)
                            if new_sl > ex_sl * 1.002 and curr_price > new_sl:
                                if self.place_exchange_stop_loss(sym, new_sl, pos_side="LONG"):
                                    pos["exchange_sl_price"] = new_sl
                        if curr_price <= float(pos.get("sl_price") or 0.0):
                            reason = "TRAILING KÂR (%50)"
                    else:
                        hard_sl = float(pos.get("sl_price") or (entry_px * 0.978))
                        if curr_price <= hard_sl:
                            reason = "ACİL STOP (-%2.20 Fitil)"
                elif pos_side == "LONG":
                    be_trig = float(pos.get("be_trigger_pct") or 0.0)
                    if not use_trailing and be_trig > 0 and not pos.get("be_locked") and pnl_pct >= be_trig:
                        be_sl = entry_px * 1.003
                        prev_sl = float(pos.get("sl_price") or 0.0)
                        new_sl = max(prev_sl, be_sl)
                        pos["be_locked"] = True
                        pos["sl_price"] = new_sl
                        state_dirty = True
                        if curr_price > new_sl and self.place_exchange_stop_loss(sym, new_sl, pos_side="LONG"):
                            pos["exchange_sl_price"] = new_sl

                    if tp_px > 0 and curr_price >= tp_px:
                        reason = "KÂR AL (%2-%3)" if pos.get("allow_wide_sl") else "KÂR AL (Direnç)"
                    elif use_trailing and trailing_active:
                        floor_sl = entry_px * (1.0 + self.trailing_floor_pct / 100.0)
                        trail_sl = peak_px * (1.0 - self.trailing_callback_pct / 100.0)
                        effective_sl = max(floor_sl, trail_sl)
                        if effective_sl > float(pos.get("sl_price") or 0.0):
                            pos["sl_price"] = effective_sl
                            state_dirty = True
                            ex_sl = float(pos.get("exchange_sl_price") or 0.0)
                            if effective_sl > ex_sl * 1.002 and curr_price > effective_sl:
                                if self.place_exchange_stop_loss(sym, effective_sl, pos_side="LONG"):
                                    pos["exchange_sl_price"] = effective_sl
                        if curr_price <= effective_sl:
                            reason = "SAT (Kâr Kilidi)" if tp_px > 0 else "TRAILING STOP"
                    else:
                        sl_px = float(
                            pos.get("sl_price")
                            or (entry_px * (1.0 - self.stop_loss_pct / 100.0))
                        )
                        if pos.get("be_locked"):
                            if curr_price <= sl_px:
                                reason = "SAT (Kâr Kilidi +%0.30)"
                        elif pos.get("allow_wide_sl"):
                            if curr_price <= sl_px:
                                reason = "STOP (ATR+Destek)"
                        elif pnl_pct <= -self.stop_loss_pct or curr_price <= sl_px:
                            if pos.get("signal_lost") and curr_price <= sl_px:
                                reason = f"STOP (Sinyal Bozuldu -${self.signal_cancel_stop_usd:g})"
                            else:
                                reason = f"STOP (-%{self.stop_loss_pct:g})"
                else:
                    be_trig = float(pos.get("be_trigger_pct") or 0.0)
                    if not use_trailing and be_trig > 0 and not pos.get("be_locked") and pnl_pct >= be_trig:
                        be_sl = entry_px * 0.996
                        prev_sl = float(pos.get("sl_price") or 0.0)
                        new_sl = min(prev_sl, be_sl) if prev_sl > 0 else be_sl
                        pos["be_locked"] = True
                        pos["sl_price"] = new_sl
                        state_dirty = True
                        if curr_price < new_sl and self.place_exchange_stop_loss(sym, new_sl, pos_side="SHORT"):
                            pos["exchange_sl_price"] = new_sl

                    if tp_px > 0 and curr_price <= tp_px:
                        reason = "KÂR AL (-%4.5 Hedef)"
                    elif use_trailing and trailing_active:
                        floor_sl = entry_px * (1.0 - self.trailing_floor_pct / 100.0)
                        trail_sl = peak_px * (1.0 + self.trailing_callback_pct / 100.0)
                        effective_sl = min(floor_sl, trail_sl)
                        prev_sl = float(pos.get("sl_price") or 0.0)
                        if prev_sl <= 0 or effective_sl < prev_sl:
                            pos["sl_price"] = effective_sl
                            state_dirty = True
                            ex_sl = float(pos.get("exchange_sl_price") or 0.0)
                            if (ex_sl <= 0 or effective_sl < ex_sl * 0.998) and curr_price < effective_sl:
                                if self.place_exchange_stop_loss(sym, effective_sl, pos_side="SHORT"):
                                    pos["exchange_sl_price"] = effective_sl
                        if curr_price >= effective_sl:
                            reason = "TRAILING STOP"
                    else:
                        sl_px = float(
                            pos.get("sl_price")
                            or (entry_px * (1.0 + self.stop_loss_pct / 100.0))
                        )
                        if pos.get("be_locked"):
                            if curr_price >= sl_px:
                                reason = "KAPAT (Kâr Kilidi +%0.40)"
                        elif pnl_pct <= -self.stop_loss_pct or curr_price >= sl_px:
                            reason = f"STOP (-%{self.stop_loss_pct:g})"

                if reason:
                    closed = self.close_position(
                        sym,
                        fallback_price=curr_price,
                        is_stop_loss=reason.startswith("STOP"),
                        close_reason=reason,
                    )
                    if closed:
                        last_hist = (
                            self._state.get("closed_history", [])[-1]
                            if self._state.get("closed_history")
                            else None
                        )
                        if last_hist and last_hist.get("symbol") == sym:
                            net_usd = float(last_hist["pnl_usd"])
                            net_pct = float(last_hist["pnl_pct"])
                            tot_fee = float(last_hist["fee_usd"])
                        else:
                            if pos_side == "LONG":
                                realized_usd = (closed.price - entry_px) * closed.qty
                                realized_pct = (
                                    ((closed.price - entry_px) / entry_px) * 100.0
                                    if entry_px > 0
                                    else 0.0
                                )
                            else:
                                realized_usd = (entry_px - closed.price) * closed.qty
                                realized_pct = (
                                    ((entry_px - closed.price) / entry_px) * 100.0
                                    if entry_px > 0
                                    else 0.0
                                )
                            net_usd = realized_usd - closed.fee_usd if closed.is_paper else realized_usd
                            net_pct = (
                                realized_pct - (2.0 * self.taker_fee_pct)
                                if closed.is_paper
                                else realized_pct
                            )
                            tot_fee = closed.fee_usd
                        events.append(
                            {
                                "symbol": sym,
                                "side": pos_side,
                                "reason": reason,
                                "entry_price": entry_px,
                                "exit_price": closed.price,
                                "pnl_usd": round(net_usd, 2),
                                "pnl_pct": round(net_pct, 2),
                                "fee_usd": round(tot_fee, 3),
                                "is_paper": closed.is_paper,
                                "wallet_name": self.wallet_name,
                                "paper_balance": closed.paper_balance,
                                "paper_initial": float(self._state.get("paper_initial_balance", self.paper_balance_usd)),
                                "paper_trades": int(self._state.get("paper_total_trades", 0)),
                                "paper_wins": int(self._state.get("paper_win_trades", 0)),
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
