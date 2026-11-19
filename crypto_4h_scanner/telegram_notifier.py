"""Telegram notification sender & alert deduplication state for 4H scanner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
import requests

from .binance_scanner import Coin4hResult
from .binance_trader import BinanceTrader, TradeExecution


def fmt_price(price: float | None) -> str:
    if price is None:
        return "-"
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.01:
        return f"{price:.5f}".rstrip("0").rstrip(".")
    return f"{price:.8f}".rstrip("0").rstrip(".")


def fmt_vol(vol: float) -> str:
    if vol >= 1_000_000_000:
        return f"${vol / 1_000_000_000:.2f}B"
    if vol >= 1_000_000:
        return f"${vol / 1_000_000:.1f}M"
    return f"${vol:,.0f}"


def _ind_row(ev, htf_label: str = "4S EMA50") -> str:
    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    return (
        f"• StochRSI: {mark(ev.cond1_buy)} | RSI+MA: {mark(ev.cond2_buy)} | Fisher: {mark(ev.cond3_buy)}\n"
        f"• WaveTrend: {mark(ev.cond4_buy)} | SMI>EMA9: {mark(ev.cond5_buy)} | {htf_label}: {mark(ev.trend_ok)}"
    )


def format_entry_message(
    res: Coin4hResult,
    kind: str,
    trade_exec: TradeExecution | None = None,
    trade_note: str | None = None,
) -> str:
    ev = res.evaluation
    market_label = (
        "PORTFOLIO MARGIN"
        if res.market == "papi"
        else "FUTURES (USDT-M)" if res.market == "futures" else "SPOT (USDT)"
    )
    if kind == "ORANGE":
        header = "🟠 <b>TURUNCU KESİN AL (5/5)</b>"
    else:
        header = "🟢 <b>YEŞİL AL (İlk 3/5)</b>"

    entry = trade_exec.price if trade_exec else (ev.entry_price or res.price)
    tp = ev.tp_level or ev.next_res
    sl = ev.sl_level or ev.prev_sup
    tp_pct = ((tp - entry) / entry * 100.0) if entry > 0 else 0.0
    sl_pct = ((sl - entry) / entry * 100.0) if entry > 0 else 0.0

    bar_dt = datetime.fromtimestamp(ev.bar_open_time / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    trade_block = ""
    if trade_exec:
        trade_block = (
            f"\n⚡ <b>OTOMATİK EMİR AÇILDI</b>\n"
            f"• Büyüklük: <b>${trade_exec.notional_usd:.2f}</b> | Miktar: <code>{trade_exec.qty:g}</code>\n"
            f"• Emir ID: <code>{trade_exec.order_id}</code>\n"
        )
    elif trade_note:
        trade_block = f"\nℹ️ <i>{trade_note}</i>\n"

    tf = getattr(res, "timeframe", "1h")
    tf_badge = "15dk (15m)" if tf == "15m" else "1S (1h)"
    htf_label = "1S EMA50" if tf == "15m" else "4S EMA50"
    stop_lbl = "1.50" if tf == "15m" else "2.20"
    be_lbl = "1.20" if tf == "15m" else "2.00"

    return (
        f"{header}\n"
        f"🪙 <b>{res.symbol}</b> | <code>{tf_badge}</code> | {market_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Giriş:</b> <code>{fmt_price(entry)}</code> (Anlık: <code>{fmt_price(res.price)}</code>)\n"
        f"🎯 <b>Kâr Al (Direnç):</b> <code>{fmt_price(tp)}</code> (<b>+{tp_pct:.2f}%</b>)\n"
        f"🛑 <b>Stop-Loss (Max -%{stop_lbl}):</b> <code>{fmt_price(sl)}</code> (<b>{sl_pct:.2f}%</b>)\n"
        f"🔒 <b>Kâr Kilidi / Trailing:</b> +%{be_lbl} görünce aktif (+%0.20 taban)\n"
        f"{trade_block}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>5'li İndikatör + Üst Trend:</b>\n"
        f"{_ind_row(ev, htf_label=htf_label)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>{tf_badge} Backtest:</b> %{ev.win_rate:.1f} Başarı ({ev.win_trades}/{ev.total_trades} işlem) | Getiri: %{ev.total_profit_pct:+.1f}\n"
        f"💧 <b>24s Hacim:</b> {fmt_vol(res.volume_24h_usdt)} | 24s: {res.change_24h:+.2f}%\n"
        f"🕒 <i>Mum: {bar_dt}</i>"
    )


def format_managed_close_message(ev_dict: dict) -> str:
    pnl_usd = float(ev_dict["pnl_usd"])
    pnl_pct = float(ev_dict["pnl_pct"])
    icon = "💰" if pnl_usd >= 0 else "🛑"
    cooldown_line = ""
    if ev_dict.get("cooldown_triggered"):
        cooldown_line = (
            f"\n⏸️ <b>MOLA:</b> Art arda 3 işlem stop oldu, "
            f"<b>{ev_dict.get('cooldown_minutes', 240)} dk</b> yeni işlem açılmayacak."
        )
    return (
        f"{icon} <b>POZİSYON KAPATILDI — {ev_dict['reason']}</b>\n"
        f"🪙 <b>{ev_dict['symbol']}</b> | <code>45dk</code>\n"
        f"💵 <b>Giriş:</b> <code>{fmt_price(ev_dict['entry_price'])}</code> → <b>Çıkış:</b> <code>{fmt_price(ev_dict['exit_price'])}</code>\n"
        f"📊 <b>Net Kâr/Zarar:</b> <b>${pnl_usd:+.2f} ({pnl_pct:+.2f}%)</b>\n"
        f"🧾 <b>Emir ID:</b> <code>{ev_dict['order_id']}</code>"
        f"{cooldown_line}"
    )


def format_exit_message(
    res: Coin4hResult,
    trade_exec: TradeExecution | None = None,
) -> str:
    ev = res.evaluation
    market_label = (
        "PORTFOLIO MARGIN"
        if res.market == "papi"
        else "FUTURES (USDT-M)" if res.market == "futures" else "SPOT (USDT)"
    )
    pct = ev.exit_trade_pct or 0.0
    icon = "💰" if pct > 0 else "🔴"
    reason = ev.exit_reason or "ÇIKIŞ"
    exit_px = trade_exec.price if trade_exec else (ev.exit_price or res.price)
    exec_line = (
        f"\n⚡ <b>Binance Pozisyonu Kapatıldı</b> | Emir ID: <code>{trade_exec.order_id}</code>"
        if trade_exec
        else ""
    )

    return (
        f"{icon} <b>POZİSYON KAPANDI — {reason}</b>\n"
        f"🪙 <b>{res.symbol}</b> | <code>4S (4H)</code> | {market_label}\n"
        f"💵 <b>Çıkış Fiyatı:</b> <code>{fmt_price(exit_px)}</code>\n"
        f"📊 <b>İşlem Sonucu:</b> <b>{pct:+.2f}%</b>{exec_line}\n"
        f"📈 <b>4S Başarı Oranı:</b> %{ev.win_rate:.1f} ({ev.win_trades}/{ev.total_trades})"
    )


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        state_file: Path,
        timeout: float = 10.0,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.state_file = state_file
        self.timeout = timeout
        self._last_update_id: int = 0
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("sent_alerts", {})
                    data.setdefault("tracked_positions", {})
                    return data
            except Exception:
                pass
        return {"sent_alerts": {}, "tracked_positions": {}}

    def _save_state(self) -> None:
        sent = self._state["sent_alerts"]
        if len(sent) > 2000:
            # Keep the most recent 1500 keys
            items = sorted(sent.items(), key=lambda kv: kv[1], reverse=True)[:1500]
            self._state["sent_alerts"] = dict(items)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def send_text(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik.")
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        resp = requests.post(
            url,
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        return bool(payload.get("ok"))

    def poll_commands(self, trader: BinanceTrader | None) -> None:
        """Handle simple Telegram commands (/devam, /durum) from the authorized chat."""
        if not self.bot_token or not self.chat_id or trader is None:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        params: dict = {"timeout": 0, "limit": 10}
        if self._last_update_id > 0:
            params["offset"] = self._last_update_id + 1
        try:
            resp = requests.get(url, params=params, timeout=5.0)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except Exception:
            return

        for upd in updates:
            uid = int(upd.get("update_id", 0))
            if uid > self._last_update_id:
                self._last_update_id = uid
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != self.chat_id:
                continue
            text = str(msg.get("text") or "").strip().lower()
            if text in ("/devam", "/reset", "/mola_iptal"):
                trader.reset_cooldown()
                self.send_text("▶️ <b>Mola iptal edildi!</b> Stop sayacı sıfırlandı, yeni 5/5 sinyallerde işlem açılmaya devam edilecek.")
            elif text == "/durum":
                open_syms = list(trader._state.get("open_positions", {}).keys())
                stops = int(trader._state.get("consecutive_stops", 0))
                cd = f"Aktif ({trader.cooldown_remaining_min()} dk kaldı)" if trader.is_in_cooldown() else "Kapalı"
                self.send_text(
                    f"📊 <b>Bot Durumu</b>\n"
                    f"• Açık Pozisyonlar: <code>{', '.join(open_syms) if open_syms else 'YOK'}</code>\n"
                    f"• Art Arda Stop Sayacı: <b>{stops}/{trader.max_consecutive_stops}</b>\n"
                    f"• Mola: <b>{cd}</b>"
                )

    def _try_open_trade(
        self,
        trader: BinanceTrader | None,
        res: Coin4hResult,
        target_usd: float,
        dry_run: bool,
    ) -> tuple[TradeExecution | None, str | None]:
        if trader is None or not trader.is_configured or dry_run:
            return None, None
        if trader.is_in_cooldown():
            rem = trader.cooldown_remaining_min()
            return (
                None,
                f"Mola aktif (art arda 3 stop) — {rem} dk boyunca yeni emir açılmayacak.",
            )
        try:
            exec_res = trader.open_position(res.symbol, res.price, target_usd)
            if exec_res is None:
                return (
                    None,
                    f"Otomatik emir açılmadı (Max {trader.max_positions} pozisyon limiti veya yetersiz bakiye).",
                )
            return exec_res, None
        except Exception as exc:
            return None, f"Otomatik emir hatası: {exc}"

    def process_results(
        self,
        results: list[Coin4hResult],
        notify_all_exits: bool = False,
        dry_run: bool = False,
        trader: BinanceTrader | None = None,
        trade_on_green: bool = False,
    ) -> dict:
        """
        1) Manage open Binance positions (-$3 SL & +1% Trailing Stop)
        2) Inspect 45m scan results, open orders on 5/5 ORANGE, and send Telegram alerts.
        """
        sent_alerts: dict[str, int] = self._state["sent_alerts"]
        tracked: dict[str, dict] = self._state["tracked_positions"]

        stats = {
            "green": 0,
            "orange": 0,
            "exit": 0,
            "orders_opened": 0,
            "orders_closed": 0,
            "errors": [],
        }

        if trader and trader.is_configured and not dry_run:
            price_map = {r.symbol: r.price for r in results}
            try:
                closed_events = trader.manage_open_positions(price_map)
                for ev_dict in closed_events:
                    stats["orders_closed"] += 1
                    stats["exit"] += 1
                    try:
                        self.send_text(format_managed_close_message(ev_dict))
                    except Exception as exc:
                        stats["errors"].append(f"{ev_dict['symbol']} CLOSE MSG: {exc}")
            except Exception as exc:
                stats["errors"].append(f"MANAGE_POS: {exc}")

        for res in results:
            ev = res.evaluation
            track_key = f"{res.market}:{res.symbol}"

            # 1. TURUNCU KESİN AL (5/5) -> $200-$300 (uses max_usd, clamped to balance)
            if ev.orange_trigger:
                alert_key = f"{track_key}:{res.timeframe}:ORANGE:{ev.bar_open_time}"
                if alert_key not in sent_alerts:
                    target_usd = trader.max_usd if trader else 300.0
                    trade_exec, trade_note = self._try_open_trade(
                        trader, res, target_usd, dry_run
                    )
                    if trade_exec:
                        stats["orders_opened"] += 1
                    msg = format_entry_message(
                        res, kind="ORANGE", trade_exec=trade_exec, trade_note=trade_note
                    )
                    try:
                        if dry_run:
                            print(f"[DRY-RUN ORANGE] {res.symbol} @ {res.price}")
                        else:
                            self.send_text(msg)
                        sent_alerts[alert_key] = int(time.time())
                        tracked[track_key] = {
                            "kind": "ORANGE",
                            "bar_open_time": ev.bar_open_time,
                            "entry_price": trade_exec.price if trade_exec else (ev.entry_price or res.price),
                        }
                        stats["orange"] += 1
                    except Exception as exc:
                        stats["errors"].append(f"{res.symbol} ORANGE: {exc}")

            # 2. YEŞİL AL (İlk 3/5) -> Ön uyarı (trade_on_green=False by default)
            elif ev.green_trigger:
                alert_key = f"{track_key}:{res.timeframe}:GREEN:{ev.bar_open_time}"
                if alert_key not in sent_alerts:
                    if trade_on_green:
                        target_usd = trader.min_usd if trader else 200.0
                        trade_exec, trade_note = self._try_open_trade(
                            trader, res, target_usd, dry_run
                        )
                        if trade_exec:
                            stats["orders_opened"] += 1
                    else:
                        trade_exec = None
                        trade_note = (
                            "Ön uyarı (3/5) — Emir sadece TURUNCU KESİN AL (5/5) onayında açılır."
                            if (trader and trader.is_configured)
                            else None
                        )
                    msg = format_entry_message(
                        res, kind="GREEN", trade_exec=trade_exec, trade_note=trade_note
                    )
                    try:
                        if dry_run:
                            print(f"[DRY-RUN GREEN] {res.symbol} @ {res.price}")
                        else:
                            self.send_text(msg)
                        sent_alerts[alert_key] = int(time.time())
                        tracked[track_key] = {
                            "kind": "GREEN",
                            "bar_open_time": ev.bar_open_time,
                            "entry_price": trade_exec.price if trade_exec else (ev.entry_price or res.price),
                        }
                        stats["green"] += 1
                    except Exception as exc:
                        stats["errors"].append(f"{res.symbol} GREEN: {exc}")

            # 3. İndikatör ÇIKIŞ bildirimi (only when trader is NOT managing live SL/Trailing, or notify_all_exits)
            elif ev.exit_trigger and (notify_all_exits or (track_key in tracked and not (trader and trader.is_configured))):
                alert_key = f"{track_key}:{res.timeframe}:EXIT:{ev.bar_open_time}"
                if alert_key not in sent_alerts:
                    msg = format_exit_message(res)
                    try:
                        if dry_run:
                            print(f"[DRY-RUN EXIT] {res.symbol} ({ev.exit_reason})")
                        else:
                            self.send_text(msg)
                        sent_alerts[alert_key] = int(time.time())
                        tracked.pop(track_key, None)
                        stats["exit"] += 1
                    except Exception as exc:
                        stats["errors"].append(f"{res.symbol} EXIT: {exc}")

        if not dry_run:
            self._save_state()

        return stats
