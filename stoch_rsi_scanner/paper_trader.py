"""Thread-safe Paper Trading position manager for $250 Stoch RSI signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import threading
import time
import uuid


@dataclass
class Position:
    id: str
    symbol: str
    side: str  # "LONG" | "SHORT"
    interval: str  # "1d" | "15m"
    notional_usdt: float
    entry_price: float
    quantity: float
    opened_at: int
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    stop_loss_price: float = 0.0
    close_condition: str = ""


@dataclass
class ClosedTrade:
    id: str
    symbol: str
    side: str
    interval: str
    notional_usdt: float
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float
    realized_pnl_pct: float
    opened_at: int
    closed_at: int
    close_reason: str


def _calc_pnl(side: str, entry_price: float, mark_price: float, qty: float, notional: float) -> tuple[float, float]:
    if entry_price <= 0 or mark_price <= 0:
        return 0.0, 0.0
    diff = (mark_price - entry_price) if side == "LONG" else (entry_price - mark_price)
    pnl = diff * qty
    pct = (pnl / notional) * 100.0 if notional > 0 else 0.0
    return round(pnl, 2), round(pct, 2)


def _format_price_str(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def _compute_sl_and_condition(
    side: str, entry_price: float, stop_loss_pct: float
) -> tuple[float, str]:
    if stop_loss_pct > 0 and entry_price > 0:
        sl_price = (
            entry_price * (1.0 - stop_loss_pct / 100.0)
            if side == "LONG"
            else entry_price * (1.0 + stop_loss_pct / 100.0)
        )
        sl_str = _format_price_str(sl_price)
        if side == "LONG":
            cond = f"SAT sinyali (Mavi ↓ Kırmızı) veya SL ≤ ${sl_str}"
        else:
            cond = f"AL sinyali (Mavi ↑ Kırmızı) veya SL ≥ ${sl_str}"
        return sl_price, cond

    if side == "LONG":
        return 0.0, "SAT sinyali (Mavi ↓ Kırmızı)"
    return 0.0, "AL sinyali (Mavi ↑ Kırmızı)"


class PaperTrader:
    def __init__(
        self,
        state_file: Path,
        default_notional: float = 250.0,
        default_stop_loss_pct: float = 2.0,
    ) -> None:
        self.state_file = state_file
        self.default_notional = default_notional
        self._lock = threading.Lock()
        self.auto_trade: bool = True
        self.notional_usdt: float = default_notional
        self.stop_loss_pct: float = default_stop_loss_pct
        self.open_positions: list[Position] = []
        self.closed_trades: list[ClosedTrade] = []
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.auto_trade = bool(raw.get("auto_trade", True))
            self.notional_usdt = float(raw.get("notional_usdt", self.default_notional))
            self.stop_loss_pct = float(raw.get("stop_loss_pct", self.stop_loss_pct))
            self.open_positions = [Position(**p) for p in raw.get("open_positions", [])]
            for pos in self.open_positions:
                sl, cond = _compute_sl_and_condition(
                    pos.side, pos.entry_price, self.stop_loss_pct
                )
                pos.stop_loss_price = sl
                pos.close_condition = cond
            self.closed_trades = [ClosedTrade(**t) for t in raw.get("closed_trades", [])]
        except Exception:
            pass

    def _save(self) -> None:
        payload = {
            "auto_trade": self.auto_trade,
            "notional_usdt": self.notional_usdt,
            "stop_loss_pct": self.stop_loss_pct,
            "open_positions": [asdict(p) for p in self.open_positions],
            "closed_trades": [asdict(t) for t in self.closed_trades[:200]],
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def configure(
        self,
        auto_trade: bool | None = None,
        notional_usdt: float | None = None,
        stop_loss_pct: float | None = None,
    ) -> dict:
        with self._lock:
            if auto_trade is not None:
                self.auto_trade = auto_trade
            if notional_usdt is not None and notional_usdt > 0:
                self.notional_usdt = round(notional_usdt, 2)
            if stop_loss_pct is not None and stop_loss_pct >= 0:
                self.stop_loss_pct = round(stop_loss_pct, 2)
                for pos in self.open_positions:
                    sl, cond = _compute_sl_and_condition(
                        pos.side, pos.entry_price, self.stop_loss_pct
                    )
                    pos.stop_loss_price = sl
                    pos.close_condition = cond
            self._save()
            return self._snapshot_locked()

    def process_scan_results(self, scan_results: list[dict], interval: str) -> dict:
        """Update mark prices for open positions and open/flip $250 positions on AL/SAT signals."""
        now = int(time.time())
        price_map = {r["symbol"]: float(r["price"]) for r in scan_results if r.get("price", 0) > 0}
        signal_map = {r["symbol"]: r["signal"] for r in scan_results}

        with self._lock:
            # 1. Update prices and check opposite signals to close existing positions
            remaining_open: list[Position] = []
            for pos in self.open_positions:
                mark = price_map.get(pos.symbol, pos.current_price or pos.entry_price)
                pos.current_price = mark
                sig = signal_map.get(pos.symbol)

                hit_sl = (
                    self.stop_loss_pct > 0
                    and pos.stop_loss_price > 0
                    and (
                        (pos.side == "LONG" and mark <= pos.stop_loss_price)
                        or (pos.side == "SHORT" and mark >= pos.stop_loss_price)
                    )
                )
                should_flip = (
                    self.auto_trade
                    and pos.interval == interval
                    and ((pos.side == "LONG" and sig == "SAT") or (pos.side == "SHORT" and sig == "AL"))
                )
                if hit_sl or should_flip:
                    pnl, pct = _calc_pnl(pos.side, pos.entry_price, mark, pos.quantity, pos.notional_usdt)
                    reason = (
                        f"Stop Loss (-{self.stop_loss_pct}%)"
                        if hit_sl
                        else f"Ters Sinyal ({sig})"
                    )
                    self.closed_trades.insert(
                        0,
                        ClosedTrade(
                            id=pos.id,
                            symbol=pos.symbol,
                            side=pos.side,
                            interval=pos.interval,
                            notional_usdt=pos.notional_usdt,
                            entry_price=pos.entry_price,
                            exit_price=mark,
                            quantity=pos.quantity,
                            realized_pnl=pnl,
                            realized_pnl_pct=pct,
                            opened_at=pos.opened_at,
                            closed_at=now,
                            close_reason=reason,
                        ),
                    )
                else:
                    pnl, pct = _calc_pnl(pos.side, pos.entry_price, mark, pos.quantity, pos.notional_usdt)
                    pos.unrealized_pnl = pnl
                    pos.unrealized_pnl_pct = pct
                    remaining_open.append(pos)

            self.open_positions = remaining_open

            # 2. Open new $250 positions for AL/SAT signals if not already open on this symbol+interval
            if self.auto_trade:
                open_keys = {(p.symbol, p.interval) for p in self.open_positions}
                for r in scan_results:
                    sig = r.get("signal")
                    sym = r.get("symbol")
                    price = float(r.get("price", 0.0))
                    if sig not in ("AL", "SAT") or price <= 0:
                        continue
                    if (sym, interval) in open_keys:
                        continue

                    side = "LONG" if sig == "AL" else "SHORT"
                    qty = self.notional_usdt / price
                    sl_price, close_cond = _compute_sl_and_condition(
                        side, price, self.stop_loss_pct
                    )
                    self.open_positions.insert(
                        0,
                        Position(
                            id=uuid.uuid4().hex[:8],
                            symbol=sym,
                            side=side,
                            interval=interval,
                            notional_usdt=self.notional_usdt,
                            entry_price=price,
                            quantity=qty,
                            opened_at=now,
                            current_price=price,
                            unrealized_pnl=0.0,
                            unrealized_pnl_pct=0.0,
                            stop_loss_price=sl_price,
                            close_condition=close_cond,
                        ),
                    )
                    open_keys.add((sym, interval))

            self._save()
            return self._snapshot_locked()

    def close_position(self, pos_id: str) -> dict:
        now = int(time.time())
        with self._lock:
            remaining: list[Position] = []
            for pos in self.open_positions:
                if pos.id == pos_id:
                    mark = pos.current_price or pos.entry_price
                    pnl, pct = _calc_pnl(pos.side, pos.entry_price, mark, pos.quantity, pos.notional_usdt)
                    self.closed_trades.insert(
                        0,
                        ClosedTrade(
                            id=pos.id,
                            symbol=pos.symbol,
                            side=pos.side,
                            interval=pos.interval,
                            notional_usdt=pos.notional_usdt,
                            entry_price=pos.entry_price,
                            exit_price=mark,
                            quantity=pos.quantity,
                            realized_pnl=pnl,
                            realized_pnl_pct=pct,
                            opened_at=pos.opened_at,
                            closed_at=now,
                            close_reason="Manuel Kapatma",
                        ),
                    )
                else:
                    remaining.append(pos)
            self.open_positions = remaining
            self._save()
            return self._snapshot_locked()

    def close_all(self) -> dict:
        now = int(time.time())
        with self._lock:
            for pos in self.open_positions:
                mark = pos.current_price or pos.entry_price
                pnl, pct = _calc_pnl(pos.side, pos.entry_price, mark, pos.quantity, pos.notional_usdt)
                self.closed_trades.insert(
                    0,
                    ClosedTrade(
                        id=pos.id,
                        symbol=pos.symbol,
                        side=pos.side,
                        interval=pos.interval,
                        notional_usdt=pos.notional_usdt,
                        entry_price=pos.entry_price,
                        exit_price=mark,
                        quantity=pos.quantity,
                        realized_pnl=pnl,
                        realized_pnl_pct=pct,
                        opened_at=pos.opened_at,
                        closed_at=now,
                        close_reason="Tümünü Kapat",
                    ),
                )
            self.open_positions = []
            self._save()
            return self._snapshot_locked()

    def clear_history(self) -> dict:
        with self._lock:
            self.closed_trades = []
            self._save()
            return self._snapshot_locked()

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _calc_stats(self, interval: str | None = None) -> dict:
        opens = [
            p for p in self.open_positions if interval is None or p.interval == interval
        ]
        closed = [
            t for t in self.closed_trades if interval is None or t.interval == interval
        ]
        open_wins = sum(1 for p in opens if p.unrealized_pnl > 0)
        closed_wins = sum(1 for t in closed if t.realized_pnl > 0)
        open_win_rate = round((open_wins / len(opens)) * 100.0, 1) if opens else 0.0
        closed_win_rate = (
            round((closed_wins / len(closed)) * 100.0, 1) if closed else 0.0
        )
        return {
            "open_count": len(opens),
            "closed_count": len(closed),
            "open_wins": open_wins,
            "open_win_rate": open_win_rate,
            "closed_wins": closed_wins,
            "closed_win_rate": closed_win_rate,
            "total_exposure_usdt": round(sum(p.notional_usdt for p in opens), 2),
            "unrealized_pnl": round(sum(p.unrealized_pnl for p in opens), 2),
            "realized_pnl": round(sum(t.realized_pnl for t in closed), 2),
        }

    def _snapshot_locked(self) -> dict:
        all_stats = self._calc_stats(None)
        stats_1d = self._calc_stats("1d")
        stats_15m = self._calc_stats("15m")
        return {
            "auto_trade": self.auto_trade,
            "notional_usdt": self.notional_usdt,
            "stop_loss_pct": self.stop_loss_pct,
            "open_count": all_stats["open_count"],
            "total_exposure_usdt": all_stats["total_exposure_usdt"],
            "unrealized_pnl": all_stats["unrealized_pnl"],
            "realized_pnl": all_stats["realized_pnl"],
            "stats": {
                "all": all_stats,
                "1d": stats_1d,
                "15m": stats_15m,
            },
            "open_positions": [asdict(p) for p in self.open_positions],
            "closed_trades": [asdict(t) for t in self.closed_trades[:100]],
        }
