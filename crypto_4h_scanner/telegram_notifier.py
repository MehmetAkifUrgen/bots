"""Telegram notification sender & alert deduplication state for 4H scanner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import html
import json
from pathlib import Path
import time
import zlib
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
    raw_sl = ev.sl_level or ev.prev_sup
    # Show the real clamped stop-loss (max -1.50% safety floor used on Binance)
    if entry > 0 and (raw_sl < entry * 0.985 or raw_sl >= entry * 0.995):
        sl = entry * 0.985
    else:
        sl = raw_sl
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

    tf = getattr(res, "timeframe", "45m")
    tf_badge = "15dk (15m)" if tf == "15m" else ("45dk (45m)" if tf == "45m" else "1S (1h)")
    htf_label = "1S EMA50" if tf == "15m" else "4S EMA50"

    return (
        f"{header}\n"
        f"🪙 <b>{res.symbol}</b> | <code>{tf_badge}</code> | {market_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Giriş:</b> <code>{fmt_price(entry)}</code> (Anlık: <code>{fmt_price(res.price)}</code>)\n"
        f"🎯 <b>Kâr Al (Direnç):</b> <code>{fmt_price(tp)}</code> (<b>+{tp_pct:.2f}%</b>)\n"
        f"🛑 <b>Stop-Loss:</b> <b>-%2.50</b> (Oto Zarar Kes)\n"
        f"🔒 <b>Trailing Stop:</b> <b>+%2.00</b> kârda başlar (+%0.20 taban)\n"
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
    side_tag = f" | <b>{ev_dict['side']}</b>" if ev_dict.get("side") else ""
    tf_badge = ev_dict.get("tf_label") or "1S (1H)"
    is_paper = bool(ev_dict.get("is_paper"))
    is_partial = bool(ev_dict.get("is_partial"))
    wname = str(ev_dict.get("wallet_name") or "KRİPTO")
    title_prefix = f"🧪 [{wname} SANAL] " if is_paper else ""
    if is_partial:
        fee_usd = float(ev_dict.get("fee_usd") or 0.0)
        bal = float(ev_dict.get("paper_balance") or 0.0)
        new_sl = float(ev_dict.get("new_sl_price") or 0.0)
        paper_line = (
            f"\n💸 <b>Komisyon (%50):</b> <code>-${fee_usd:.3f}</code> | 🏦 <b>{wname} Kasa:</b> <b>${bal:.2f}</b>"
            if is_paper
            else ""
        )
        return (
            f"🎯 <b>{title_prefix}{ev_dict['reason']}</b>\n"
            f"🪙 <b>{ev_dict['symbol']}</b>{side_tag} | <code>{tf_badge}</code>\n"
            f"💵 <b>Giriş:</b> <code>{fmt_price(ev_dict['entry_price'])}</code> → <b>TP1:</b> <code>{fmt_price(ev_dict['exit_price'])}</code>\n"
            f"📊 <b>Kilitlenen İlk %50 Net Kâr:</b> <b>${pnl_usd:+.2f} ({pnl_pct:+.2f}%)</b>\n"
            f"🛡️ <b>Kalan %50 Yeni Stop:</b> <code>{fmt_price(new_sl)}</code> (+%0.15 taban / %1.10 zirve takip)"
            f"{paper_line}"
        )
    paper_block = ""
    if is_paper:
        fee_usd = float(ev_dict.get("fee_usd") or 0.0)
        bal = float(ev_dict.get("paper_balance") or 0.0)
        init_bal = float(ev_dict.get("paper_initial") or 100.0)
        bal_pct = ((bal - init_bal) / init_bal * 100.0) if init_bal > 0 else 0.0
        trades = int(ev_dict.get("paper_trades") or 0)
        wins = int(ev_dict.get("paper_wins") or 0)
        paper_block = (
            f"\n💸 <b>Binance Komisyon:</b> <code>-${fee_usd:.3f}</code> (Giriş+Çıkış %0.10)\n"
            f"🏦 <b>{wname} Sanal Kasa:</b> <b>${bal:.2f}</b> (<b>{bal_pct:+.2f}%</b>) | <b>İşlem:</b> {wins}/{trades}"
        )
    return (
        f"{icon} <b>{title_prefix}POZİSYON KAPATILDI — {ev_dict['reason']}</b>\n"
        f"🪙 <b>{ev_dict['symbol']}</b>{side_tag} | <code>{tf_badge}</code>\n"
        f"💵 <b>Giriş:</b> <code>{fmt_price(ev_dict['entry_price'])}</code> → <b>Çıkış:</b> <code>{fmt_price(ev_dict['exit_price'])}</code>\n"
        f"📊 <b>Net Kâr/Zarar:</b> <b>${pnl_usd:+.2f} ({pnl_pct:+.2f}%)</b>"
        f"{paper_block}\n"
        f"🧾 <b>Emir ID:</b> <code>{ev_dict['order_id']}</code>"
        f"{cooldown_line}"
    )


def format_dual_4h_message(
    res: Coin4hResult,
    side: str,
    trade_exec: TradeExecution | None = None,
    trade_note: str | None = None,
    stop_loss_pct: float = 1.5,
    trailing_trigger_pct: float = 2.0,
    trailing_callback_pct: float = 0.3,
    trailing_floor_pct: float = 1.7,
    tf_override: str | None = None,
    ev_override=None,
    long_window_min: int = 0,
    short_window_min: int = 15,
) -> str:
    is_long = side.upper() == "LONG"
    tf_raw = tf_override or getattr(res, "timeframe", "4h")
    ev = ev_override or (res.evaluation if is_long else (res.long_evaluation or res.evaluation))
    tf_label = "1S (1H)" if tf_raw == "1h" else "4S (4H)"
    tf_short = "1S" if tf_raw == "1h" else "4S"
    entry = trade_exec.price if trade_exec else res.price
    sl = (
        entry * (1.0 - stop_loss_pct / 100.0)
        if is_long
        else entry * (1.0 + stop_loss_pct / 100.0)
    )
    bar_dt = datetime.fromtimestamp(ev.bar_open_time / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    trade_block = ""
    if trade_exec:
        if trade_exec.is_paper:
            wname = trade_exec.wallet_name or "KRİPTO"
            trade_block = (
                f"\n🧪 <b>[{wname} SANAL] {side.upper()} EMİR AÇILDI</b>\n"
                f"• Büyüklük: <b>${trade_exec.notional_usd:.2f}</b> | Miktar: <code>{trade_exec.qty:g}</code>\n"
                f"• Giriş Komisyonu (%0.05): <code>-${trade_exec.fee_usd:.3f}</code> | {wname} Kasa: <b>${trade_exec.paper_balance:.2f}</b>\n"
                f"• Emir ID: <code>{trade_exec.order_id}</code>\n"
            )
        else:
            trade_block = (
                f"\n⚡ <b>OTOMATİK {side.upper()} EMİR AÇILDI</b>\n"
                f"• Büyüklük: <b>${trade_exec.notional_usd:.2f}</b> | Miktar: <code>{trade_exec.qty:g}</code>\n"
                f"• Emir ID: <code>{trade_exec.order_id}</code>\n"
            )
    elif trade_note:
        trade_block = f"\nℹ️ <i>{html.escape(str(trade_note))}</i>\n"

    if is_long:
        tp1 = entry * 1.015
        sl_close = entry * (1.0 - 0.025)
        sl_hard = entry * (1.0 - 0.025)
        timing_tag = f"Son {long_window_min}dk Mum Kapanışı" if long_window_min > 0 else "Anlık Giriş"
        return (
            f"🟠 <b>[{tf_short} FUTURES v3] KESİN LONG (5/5)</b>\n"
            f"🪙 <b>{res.symbol}</b> | <code>{tf_label}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>Giriş:</b> <code>{fmt_price(entry)}</code> (Anlık: <code>{fmt_price(res.price)}</code>)\n"
            f"🎯 <b>TP1 (%50 Kâr Al):</b> <code>{fmt_price(tp1)}</code> (<b>+1.50%</b>)\n"
            f"🚀 <b>Kalan %50:</b> +%0.15 Garanti Kilit + Zirveden <b>%1.10</b> Trailing\n"
            f"🛑 <b>Stop:</b> <code>{fmt_price(sl_close)}</code> (-%2.50 Kapanış) | Acil Fitil: <code>{fmt_price(sl_hard)}</code> (-%2.50)\n"
            f"📈 <b>{tf_short} Backtest:</b> <b>%{ev.win_rate:.1f} Başarı</b> ({ev.win_trades}/{ev.total_trades}) | <b>Getiri: %{ev.total_profit_pct:+.1f}</b>\n"
            f"{trade_block}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💧 <b>24s Hacim:</b> {fmt_vol(res.volume_24h_usdt)} | 24s: {res.change_24h:+.2f}%\n"
            f"🕒 <i>Mum: {bar_dt} ({timing_tag})</i>"
        )

    timing_tag = f"Son {short_window_min}dk Mum Kapanışı" if short_window_min > 0 else "Anlık Giriş"
    return (
        f"🔴 <b>[{tf_short} ÇİFT YÖN] SHORT 5/5 + EMA200</b>\n"
        f"🪙 <b>{res.symbol}</b> | <code>{tf_label}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Giriş:</b> <code>{fmt_price(entry)}</code> (Anlık: <code>{fmt_price(res.price)}</code>)\n"
        f"🛑 <b>Stop (-%{stop_loss_pct:g}):</b> <code>{fmt_price(sl)}</code>\n"
        f"🔒 <b>Trailing Stop:</b> +%{trailing_trigger_pct:g} görünce başlar (%{trailing_callback_pct:g} takip, +%{trailing_floor_pct:g} taban)\n"
        f"📈 <b>Trend:</b> Fiyat &lt; {tf_short} EMA200 (Düşüş — Son {short_window_min}dk Kapanış Onayı)\n"
        f"{trade_block}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💧 <b>24s Hacim:</b> {fmt_vol(res.volume_24h_usdt)} | 24s: {res.change_24h:+.2f}%\n"
        f"🕒 <i>Mum: {bar_dt} ({timing_tag})</i>"
    )


def format_short_15m_message(
    res: Coin4hResult,
    trade_exec: TradeExecution | None = None,
    trade_note: str | None = None,
    stop_loss_pct: float = 1.5,
    top_n: int = 20,
) -> str:
    sev = res.short_15m_evaluation
    assert sev is not None
    header = (
        "🟣 <b>[1S→15dk] KESİN SHORT (5/5)</b>"
        if sev.full_fired
        else "🔴 <b>[1S→15dk] ERKEN SHORT (İlk Dönüş)</b>"
    )
    entry = trade_exec.price if trade_exec else res.price
    tp = entry * 0.955
    default_sl = entry * (1.0 + stop_loss_pct / 100.0)
    raw_sl = sev.sl_level or default_sl
    sl = raw_sl if (entry * 1.005 < raw_sl <= default_sl) else default_sl
    sl_pct = ((sl - entry) / entry * 100.0) if entry > 0 else stop_loss_pct

    bar_dt = datetime.fromtimestamp(sev.bar_open_time / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    trade_block = ""
    if trade_exec:
        trade_block = (
            f"\n⚡ <b>OTOMATİK SHORT EMİR AÇILDI</b>\n"
            f"• Büyüklük: <b>${trade_exec.notional_usd:.2f}</b> | Miktar: <code>{trade_exec.qty:g}</code>\n"
            f"• Emir ID: <code>{trade_exec.order_id}</code>\n"
        )
    elif trade_note:
        trade_block = f"\nℹ️ <i>{html.escape(str(trade_note))}</i>\n"

    htf_info = html.escape(sev.htf_reason or "1S Direnç / EMA200 Bölgesi")
    ltf_reason = "15dk EMA200 Reddi" if sev.ema200_reject else "15dk Tepe Şişkinliği Dönüşü"
    return (
        f"{header}\n"
        f"🪙 <b>{res.symbol}</b> | <code>1S → 15dk</code> | 🔥 <b>Top {top_n} Gainer ({res.change_24h:+.2f}%)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧭 <b>1S Bölge:</b> {htf_info}\n"
        f"⚡ <b>15dk Tetik:</b> {ltf_reason} (Anlık Giriş)\n"
        f"💵 <b>Giriş:</b> <code>{fmt_price(entry)}</code> (Anlık: <code>{fmt_price(res.price)}</code>)\n"
        f"🎯 <b>Kâr Al (-%4.5):</b> <code>{fmt_price(tp)}</code>\n"
        f"🛑 <b>Stop (+%{sl_pct:.2f}):</b> <code>{fmt_price(sl)}</code> (Max -%{stop_loss_pct:g})\n"
        f"🔒 <b>Çıkış Kuralı:</b> -%4.5 TP | +%1.5'te +%0.40 Kâr Kilidi | 15dk Dönüşte KAPAT\n"
        f"{trade_block}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💧 <b>24s Hacim:</b> {fmt_vol(res.volume_24h_usdt)} | 24s: {res.change_24h:+.2f}%\n"
        f"🕒 <i>15dk Mum: {bar_dt}</i>"
    )


def format_4h_alert_only_message(
    res: Coin4hResult,
    kind: str,
    trade_exec: TradeExecution | None = None,
    trade_note: str | None = None,
) -> str:
    ev = res.evaluation
    if kind == "ORANGE":
        header = "🟠 <b>[4S SİSTEM] TURUNCU KESİN AL (5/5)</b>"
    else:
        header = "🟢 <b>[4S SİSTEM] YEŞİL AL (İlk 3/5)</b>"

    entry = trade_exec.price if trade_exec else (ev.entry_price or res.price)
    tp = ev.tp_level or ev.next_res
    raw_sl = ev.sl_level or ev.prev_sup
    # Show the real clamped stop-loss (max -1.50% safety floor used on Binance)
    if entry > 0 and (raw_sl < entry * 0.985 or raw_sl >= entry * 0.995):
        sl = entry * 0.985
    else:
        sl = raw_sl
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

    def mark(ok: bool) -> str:
        return "✅" if ok else "❌"

    return (
        f"{header}\n"
        f"🪙 <b>{res.symbol}</b> | <code>4S (4H)</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Giriş:</b> <code>{fmt_price(entry)}</code> (Anlık: <code>{fmt_price(res.price)}</code>)\n"
        f"🎯 <b>Kâr Al (Direnç):</b> <code>{fmt_price(tp)}</code> (<b>+{tp_pct:.2f}%</b>)\n"
        f"🛑 <b>Stop (Max -%1.50):</b> <code>{fmt_price(sl)}</code> (<b>{sl_pct:.2f}%</b>)\n"
        f"🔒 <b>Trailing Stop:</b> +%2.50 görünce başlar (%1.80 takip, +%0.50 taban)\n"
        f"🛡️ <b>Sinyal Koruması:</b> 3/5 bozulursa stop -$1.50'ye çekilir\n"
        f"{trade_block}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>5'li İndikatör Durumu:</b>\n"
        f"• StochRSI: {mark(ev.cond1_buy)} | RSI+MA: {mark(ev.cond2_buy)} | Fisher: {mark(ev.cond3_buy)}\n"
        f"• WaveTrend: {mark(ev.cond4_buy)} | SMI>EMA9: {mark(ev.cond5_buy)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 <b>4S Backtest (~5.5 Ay):</b> <b>%{ev.win_rate:.1f} Başarı</b> ({ev.win_trades}/{ev.total_trades} işlem) | <b>Getiri: %{ev.total_profit_pct:+.1f}</b>\n"
        f"💧 <b>24s Hacim:</b> {fmt_vol(res.volume_24h_usdt)} | 24s: {res.change_24h:+.2f}%\n"
        f"🕒 <i>Mum: {bar_dt}</i>"
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
        self.chat_ids: list[str] = [c.strip() for c in str(chat_id).split(",") if c.strip()]
        self.chat_id = self.chat_ids[0] if self.chat_ids else ""
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
                    data.setdefault("paused", False)
                    data.setdefault("dashboard_msg_id", 0)
                    return data
            except Exception:
                pass
        return {"sent_alerts": {}, "tracked_positions": {}, "paused": False, "dashboard_msg_id": 0}

    def is_paused(self, trader: BinanceTrader | None = None) -> bool:
        if trader is not None and trader.is_paused():
            return True
        return bool(self._state.get("paused", False))

    def set_paused(self, paused: bool, trader: BinanceTrader | None = None) -> None:
        self._state["paused"] = bool(paused)
        self._save_state()
        if trader is not None:
            trader.set_paused(paused)

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
        if not self.bot_token or not self.chat_ids:
            raise RuntimeError("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik.")
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        any_ok = False
        last_err: Exception | None = None
        for cid in self.chat_ids:
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": cid,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=self.timeout,
                )
                if resp.status_code == 400:
                    resp = requests.post(
                        url,
                        json={
                            "chat_id": cid,
                            "text": text,
                            "disable_web_page_preview": True,
                        },
                        timeout=self.timeout,
                    )
                resp.raise_for_status()
                if resp.json().get("ok"):
                    any_ok = True
            except Exception as exc:
                last_err = exc
        if not any_ok and last_err is not None:
            raise last_err
        return any_ok

    def poll_commands(
        self,
        trader: BinanceTrader | None,
        extra_traders: list[BinanceTrader] | None = None,
    ) -> None:
        """Handle Telegram commands (/durdur, /baslat, /devam, /durum, /sifirla) from authorized chats."""
        if not self.bot_token or not self.chat_ids:
            return
        all_traders: list[BinanceTrader] = []
        if trader is not None:
            all_traders.append(trader)
        if extra_traders:
            for t in extra_traders:
                if t is not None and t not in all_traders:
                    all_traders.append(t)
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
            if chat_id not in self.chat_ids:
                continue
            raw_text = str(msg.get("text") or "").strip().lower()
            cmd = raw_text.split()[0].split("@")[0] if raw_text else ""
            if cmd in ("/durdur", "/stop", "/kapat", "/duraklat", "/pause"):
                self.set_paused(True, trader)
                for t in all_traders:
                    t.set_paused(True)
                self.send_text(
                    "⏸️ <b>Bot DURDURULDU!</b>\n"
                    "• Yeni pozisyon ve sinyal açılmayacak.\n"
                    "• Açık pozisyon varsa Kâr Al / Kâr Kilidi / Stop takibi çalışmaya devam eder.\n"
                    "▶️ Tekrar açmak için: <code>/baslat</code>"
                )
            elif cmd in ("/baslat", "/başlat", "/ac", "/aç", "/aktif", "/calistir", "/devam", "/reset", "/mola_iptal"):
                self.set_paused(False, trader)
                for t in all_traders:
                    t.set_paused(False)
                    t.reset_cooldown()
                self.send_text(
                    "▶️ <b>Bot AKTİF edildi!</b>\n"
                    "• Yeni sinyallerde otomatik işlem ve bildirim devam ediyor.\n"
                    "⏸️ Durdurmak için: <code>/durdur</code>"
                )
            elif cmd in ("/sifirla", "/sıfırla", "/paper_reset"):
                lines = ["🔄 <b>Sanal Kasalar Sıfırlandı!</b>"]
                for t in all_traders:
                    if t.paper_mode:
                        ps = t.reset_paper_account()
                        lines.append(f"• <b>{t.wallet_name} Sanal Kasa:</b> <b>${ps['balance']:.2f}</b>")
                if len(lines) > 1:
                    self.send_text("\n".join(lines))
                    self.sync_cloud_dashboard(all_traders)
            elif cmd in ("/bugun", "/bugün", "/rapor", "/gunluk"):
                tz_tr = timezone(timedelta(hours=3))
                today_str = datetime.now(tz_tr).strftime("%Y-%m-%d")
                blocks = [f"📅 <b>GÜNLÜK RAPOR ({today_str})</b>"]
                for t in all_traders:
                    rep = t.get_daily_report(today_str)
                    ps = rep["summary"]
                    blocks.append(
                        f"\n🏦 <b>{t.wallet_name} SANAL KASA:</b> <b>${ps['balance']:.2f}</b>\n"
                        f"• Bugün İşlem: <b>{rep['today_trades']}</b> "
                        f"(✅ Başarılı: <b>{rep['today_wins']}</b> | 🛑 Stop/Zarar: <b>{rep['today_stops']}</b>)\n"
                        f"• Bugün Kazanç: <b>+${rep['today_profit_usd']:.2f}</b> | Zarar: <b>${rep['today_loss_usd']:.2f}</b>\n"
                        f"• Bugün Komisyon: <code>-${rep['today_fees_usd']:.3f}</code> | <b>Bugün Net: ${rep['today_net_usd']:+.2f}</b>"
                    )
                self.send_text("\n".join(blocks))
            elif cmd in ("/durum", "/status", "/komutlar", "/yardim", "/help"):
                paused = self.is_paused(trader)
                state_badge = "⏸️ DURDURULDU" if paused else "🟢 AKTİF"
                blocks = [f"📊 <b>Bot Durumu:</b> <b>{state_badge}</b>"]
                for t in all_traders:
                    open_syms = list(t._state.get("open_positions", {}).keys())
                    if t.paper_mode:
                        rep = t.get_daily_report()
                        ps = rep["summary"]
                        blocks.append(
                            f"\n🏦 <b>{t.wallet_name} SANAL KASA:</b> <b>${ps['balance']:.2f}</b> "
                            f"(Toplam Net: <b>${ps['net_usd']:+.2f} / {ps['net_pct']:+.2f}%</b>)\n"
                            f"• Bugün: ✅ <b>{rep['today_wins']}</b> Başarılı | 🛑 <b>{rep['today_stops']}</b> Stop | "
                            f"Kazanç: <b>+${rep['today_profit_usd']:.2f}</b> | Zarar: <b>${rep['today_loss_usd']:.2f}</b> | Net: <b>${rep['today_net_usd']:+.2f}</b>\n"
                            f"• Toplam Komisyon: <code>-${ps['total_fees']:.3f}</code> | Toplam İşlem: <b>{ps['win_trades']}/{ps['total_trades']}</b>\n"
                            f"• Açık ({len(open_syms)}/{t.max_positions}): <code>{', '.join(open_syms) if open_syms else 'YOK'}</code>"
                        )
                    else:
                        blocks.append(
                            f"\n💼 <b>{t.wallet_name} (GERÇEK):</b> Açık: <code>{', '.join(open_syms) if open_syms else 'YOK'}</code>"
                        )
                blocks.append("\n• Komutlar: <code>/durum</code> | <code>/bugun</code> | <code>/durdur</code> | <code>/baslat</code> | <code>/sifirla</code>")
                self.send_text("\n".join(blocks))

    def _pack_cloud_payload(self, traders: list[BinanceTrader]) -> str:
        raw_obj = {
            "v": 1,
            "ts": int(time.time()),
            "wallets": {t.wallet_name: t.export_cloud_snapshot() for t in traders if t is not None},
        }
        # Trim closed_history progressively if needed so compressed URL always stays < 1900 chars
        for max_hist in (20, 10, 5, 0):
            for w in raw_obj["wallets"].values():
                w["closed_history"] = w.get("closed_history", [])[-max_hist:] if max_hist > 0 else []
            comp = zlib.compress(json.dumps(raw_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 9)
            b64 = base64.urlsafe_b64encode(comp).decode("ascii")
            if len(b64) <= 1850:
                return b64
        return ""

    @staticmethod
    def unpack_cloud_payload(pinned_msg: dict) -> dict | None:
        if not isinstance(pinned_msg, dict):
            return None
        for ent in pinned_msg.get("entities") or []:
            url = str(ent.get("url") or "")
            if "?start=st_" in url:
                b64 = url.split("?start=st_", 1)[1]
                try:
                    raw = zlib.decompress(base64.urlsafe_b64decode(b64.encode("ascii")))
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return None
        return None

    def restore_from_cloud_dashboard(self, traders: list[BinanceTrader]) -> bool:
        """Restore paper wallet states from pinned Telegram dashboard if local container restarted."""
        if not self.bot_token or not self.chat_id or not traders:
            return False
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.bot_token}/getChat",
                params={"chat_id": self.chat_id},
                timeout=self.timeout,
            )
            data = resp.json().get("result") or {}
            pinned = data.get("pinned_message")
            if not pinned:
                return False
            if int(pinned.get("message_id") or 0) > 0:
                self._state["dashboard_msg_id"] = int(pinned["message_id"])
                self._save_state()
            payload = self.unpack_cloud_payload(pinned)
            if not payload or not isinstance(payload.get("wallets"), dict):
                return False
            restored = False
            for t in traders:
                if t is not None and t.paper_mode and not t.state_file.exists():
                    snap = payload["wallets"].get(t.wallet_name)
                    if snap:
                        t.import_cloud_snapshot(snap)
                        restored = True
            return restored
        except Exception:
            return False

    def sync_cloud_dashboard(self, traders: list[BinanceTrader]) -> None:
        """Update silent pinned Telegram dashboard + embedded cloud state so local CLI & user can inspect anytime."""
        if not self.bot_token or not self.chat_id or not traders:
            return
        tz_tr = timezone(timedelta(hours=3))
        now_tr = datetime.now(tz_tr)
        today_str = now_tr.strftime("%Y-%m-%d")
        lines = [
            f"📌 <b>CANLI SANAL KASA PANELİ</b> (<i>{now_tr.strftime('%d.%m %H:%M')} TSİ</i>)",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        recent_all: list[dict] = []
        for t in traders:
            if t is None or not t.paper_mode:
                continue
            rep = t.get_daily_report(today_str)
            ps = rep["summary"]
            open_pos = rep["open_positions"]
            open_str = (
                ", ".join(f"{s}(${float(p.get('notional_usd', 0)):.0f})" for s, p in open_pos.items())
                if open_pos
                else "YOK"
            )
            lines.append(
                f"🏦 <b>{t.wallet_name}: ${ps['balance']:.2f}</b> ({ps['net_usd']:+.2f}$ / {ps['net_pct']:+.2f}%)\n"
                f"• Bugün: ✅<b>{rep['today_wins']}</b> Kâr | 🛑<b>{rep['today_stops']}</b> Stop | "
                f"+${rep['today_profit_usd']:.2f} / ${rep['today_loss_usd']:.2f} (Net: <b>${rep['today_net_usd']:+.2f}</b>)\n"
                f"• Toplam: {ps['win_trades']}/{ps['total_trades']} işlem | Komisyon: -${ps['total_fees']:.2f}\n"
                f"• Açık ({len(open_pos)}/{t.max_positions}): <code>{open_str}</code>"
            )
            recent_all.extend(rep["recent_trades"])

        if recent_all:
            recent_all.sort(key=lambda r: int(r.get("ts", 0)), reverse=True)
            lines.append("━━━━━━━━━━━━━━━━━━━━\n🧾 <b>Son Kapanan İşlemler:</b>")
            for r in recent_all[:8]:
                ic = "✅" if float(r.get("pnl_usd", 0)) > 0 else "🛑"
                lines.append(
                    f"{ic} <code>{r.get('time_tr','')}</code> <b>{r.get('symbol')}</b> [{r.get('wallet','')}] "
                    f"<b>${float(r.get('pnl_usd',0)):+.2f}</b> ({html.escape(str(r.get('reason','')))})"
                )

        b64 = self._pack_cloud_payload(traders)
        hidden_link = f'<a href="https://t.me/estrabim_bot?start=st_{b64}">&#8203;</a>' if b64 else ""
        full_html = "\n".join(lines) + hidden_link

        msg_id = int(self._state.get("dashboard_msg_id") or 0)
        base = f"https://api.telegram.org/bot{self.bot_token}"
        try:
            if msg_id == 0:
                chat_r = requests.get(f"{base}/getChat", params={"chat_id": self.chat_id}, timeout=self.timeout).json()
                pinned = (chat_r.get("result") or {}).get("pinned_message") or {}
                if "CANLI SANAL KASA PANELİ" in str(pinned.get("text") or ""):
                    msg_id = int(pinned.get("message_id") or 0)

            if msg_id > 0:
                ed = requests.post(
                    f"{base}/editMessageText",
                    json={
                        "chat_id": self.chat_id,
                        "message_id": msg_id,
                        "text": full_html,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=self.timeout,
                )
                if ed.status_code == 200 or "message is not modified" in ed.text:
                    self._state["dashboard_msg_id"] = msg_id
                    self._save_state()
                    return

            sent_r = requests.post(
                f"{base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": full_html,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": True,
                },
                timeout=self.timeout,
            ).json()
            new_id = int((sent_r.get("result") or {}).get("message_id") or 0)
            if new_id > 0:
                self._state["dashboard_msg_id"] = new_id
                self._save_state()
                requests.post(
                    f"{base}/pinChatMessage",
                    json={
                        "chat_id": self.chat_id,
                        "message_id": new_id,
                        "disable_notification": True,
                    },
                    timeout=self.timeout,
                )
        except Exception:
            pass

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
        min_win_rate: float = 0.0,
        min_profit_pct: float = -1e9,
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

            # Sinyal İptal Koruması:
            # - 3/5 sinyali kaybolursa hemen $0'da kapatma; stopu -$1.50'ye çek (Binance'te de güncelle).
            # - Zaten -$1.50 veya daha fazla zarardaysa anında kapat.
            for res in results:
                pos = trader._state.get("open_positions", {}).get(res.symbol)
                if not pos:
                    continue
                if res.evaluation.all_five_buy and not pos.get("upgraded_5_5"):
                    pos["upgraded_5_5"] = True
                if not pos.get("trailing_active") and not res.evaluation.first_three_buy:
                    try:
                        out = trader.apply_signal_cancel_stop(res.symbol, res.price)
                        if out and out.get("action") == "closed":
                            stats["orders_closed"] += 1
                            self.send_text(format_managed_close_message(out["event"]))
                        elif out and out.get("action") == "tightened":
                            self.send_text(
                                f"🛡️ <b>SİNYAL BOZULDU — STOP -${out['stop_usd']:g}'YE ÇEKİLDİ</b>\n"
                                f"🪙 <b>{res.symbol}</b>\n"
                                f"💵 <b>Giriş:</b> <code>{fmt_price(out['entry_price'])}</code> | "
                                f"<b>Anlık:</b> <code>{fmt_price(out['current_price'])}</code>\n"
                                f"🛑 <b>Yeni Stop:</b> <code>{fmt_price(out['sl_price'])}</code> (-${out['stop_usd']:g})"
                            )
                    except Exception as exc:
                        stats["errors"].append(f"{res.symbol} SIGNAL_CANCEL: {exc}")

        for res in results:
            ev = res.evaluation
            track_key = f"{res.market}:{res.symbol}"
            quality_ok = (
                ev.win_rate >= min_win_rate and ev.total_profit_pct >= min_profit_pct
            )

            # 1. TURUNCU KESİN AL (5/5) -> $200-$300 (only if coin passes WR & Profit filter)
            if ev.orange_trigger and quality_ok:
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

            # 2. YEŞİL AL (İlk 3/5) -> Ön uyarı (only if coin passes WR & Profit filter)
            elif ev.green_trigger and quality_ok:
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

    def process_4h_alert_only(
        self,
        results: list[Coin4hResult],
        min_win_rate: float = 67.0,
        min_profit_pct: float = 250.0,
        dry_run: bool = False,
        trader: BinanceTrader | None = None,
        dual_mode: bool = True,
        entry_window_minutes: int = 0,
        long_entry_window_minutes: int | None = None,
        short_entry_window_minutes: int | None = None,
        short_top_gainers: int = 20,
        long_min_win_rate: float = 67.0,
        long_min_profit_pct: float = 250.0,
        enable_short: bool = False,
    ) -> dict:
        """Process 4H signals: LONG on 4H Pine 5/5 (WR>=65%, Profit>=300%, instant), SHORT on 4H 5/5 + EMA200 (last 15m)."""
        sent_alerts: dict[str, int] = self._state["sent_alerts"]
        stats = {
            "green": 0,
            "orange": 0,
            "long": 0,
            "short": 0,
            "orders_opened": 0,
            "orders_closed": 0,
            "errors": [],
        }

        if trader and trader.is_configured and not dry_run:
            price_map = {r.symbol: r.price for r in results}
            try:
                closed_events = trader.manage_open_positions(price_map)
                for ev_dict in closed_events:
                    if not ev_dict.get("is_partial"):
                        stats["orders_closed"] += 1
                    try:
                        self.send_text(format_managed_close_message(ev_dict))
                    except Exception as exc:
                        stats["errors"].append(f"{ev_dict['symbol']} CLOSE MSG: {exc}")
            except Exception as exc:
                stats["errors"].append(f"MANAGE_POS: {exc}")

            # Pine Script 'SAT' / 'KAPAT (Dönüş)' exit checks for open positions (runs even when paused)
            for res in results:
                pos = trader._state.get("open_positions", {}).get(res.symbol)
                if not pos:
                    continue
                pos_side = str(pos.get("side") or "LONG").upper()
                entry_bar_ts = int(pos.get("entry_bar_open_time") or 0)
                if pos_side == "LONG" and pos.get("v3_mode"):
                    ev_curr = res.evaluation
                    tp1_hit = bool(pos.get("tp1_hit", False))
                    close_sl_px = float(pos.get("close_sl_price") or 0.0)
                    # Confirmed bar checks (matching Pine barstate.isconfirmed on 1H or 4H!)
                    if ev_curr.prev_bar_open_time > entry_bar_ts:
                        is_1h_tf = getattr(res, "timeframe", "1h") == "1h"
                        tf_s = "1S" if is_1h_tf else "4S"
                        tf_lbl = "1S (1H)" if is_1h_tf else "4S (4H)"
                        v3_reason: str | None = None
                        v3_is_stop = False
                        if not tp1_hit and close_sl_px > 0 and ev_curr.prev_bar_close < close_sl_px:
                            v3_reason = f"STOP ({tf_s} Kapanış -%2.50)"
                            v3_is_stop = True
                        elif ev_curr.prev_bar_real_sell:
                            v3_reason = "TREND BİTTİ (KAPAT)"
                            v3_is_stop = False
                        if v3_reason:
                            try:
                                closed = trader.close_position(
                                    res.symbol,
                                    fallback_price=res.price,
                                    is_stop_loss=v3_is_stop,
                                    close_reason=v3_reason,
                                )
                                if closed:
                                    last_h = (
                                        trader._state.get("closed_history", [])[-1]
                                        if trader._state.get("closed_history")
                                        else {}
                                    )
                                    stats["orders_closed"] += 1
                                    ev_close = {
                                        "symbol": res.symbol,
                                        "side": "LONG",
                                        "tf_label": tf_lbl,
                                        "reason": v3_reason,
                                        "entry_price": float(pos.get("entry_price") or closed.price),
                                        "exit_price": closed.price,
                                        "pnl_usd": float(last_h.get("pnl_usd", 0.0)),
                                        "pnl_pct": float(last_h.get("pnl_pct", 0.0)),
                                        "fee_usd": float(last_h.get("fee_usd", closed.fee_usd)),
                                        "is_paper": closed.is_paper,
                                        "wallet_name": trader.wallet_name,
                                        "paper_balance": closed.paper_balance,
                                        "paper_initial": float(trader._state.get("paper_initial_balance", trader.paper_balance_usd)),
                                        "paper_trades": int(trader._state.get("paper_total_trades", 0)),
                                        "paper_wins": int(trader._state.get("paper_win_trades", 0)),
                                        "order_id": closed.order_id,
                                    }
                                    self.send_text(format_managed_close_message(ev_close))
                            except Exception as exc:
                                stats["errors"].append(f"{res.symbol} V3_CLOSE: {exc}")
                elif pos_side == "LONG" and not pos.get("use_trailing", True):
                    ev_curr = res.evaluation
                    if (
                        ev_curr.real_sell_cond
                        and ev_curr.bar_open_time > entry_bar_ts
                        and not ev_curr.orange_trigger
                        and not ev_curr.green_trigger
                    ):
                        try:
                            is_1h = getattr(res, "timeframe", "4h") == "1h"
                            sat_reason = "SAT (1S İndikatör Çıkışı)" if is_1h else "SAT (4S İndikatör Çıkışı)"
                            closed = trader.close_position(
                                res.symbol,
                                fallback_price=res.price,
                                is_stop_loss=False,
                                close_reason=sat_reason,
                            )
                            if closed:
                                entry_px = float(pos.get("entry_price") or closed.price)
                                gross_usd = (closed.price - entry_px) * closed.qty
                                gross_pct = (
                                    ((closed.price - entry_px) / entry_px) * 100.0
                                    if entry_px > 0
                                    else 0.0
                                )
                                net_usd = gross_usd - closed.fee_usd if closed.is_paper else gross_usd
                                net_pct = (
                                    gross_pct - (2.0 * trader.taker_fee_pct)
                                    if closed.is_paper
                                    else gross_pct
                                )
                                stats["orders_closed"] += 1
                                is_1h = getattr(res, "timeframe", "4h") == "1h"
                                ev_close = {
                                    "symbol": res.symbol,
                                    "side": "LONG",
                                    "tf_label": "1S (1H)" if is_1h else "4S (4H)",
                                    "reason": "SAT (1S İndikatör Çıkışı)" if is_1h else "SAT (4S İndikatör Çıkışı)",
                                    "entry_price": entry_px,
                                    "exit_price": closed.price,
                                    "pnl_usd": round(net_usd, 2),
                                    "pnl_pct": round(net_pct, 2),
                                    "fee_usd": round(closed.fee_usd, 3),
                                    "is_paper": closed.is_paper,
                                    "wallet_name": trader.wallet_name,
                                    "paper_balance": closed.paper_balance,
                                    "paper_initial": float(trader._state.get("paper_initial_balance", trader.paper_balance_usd)),
                                    "paper_trades": int(trader._state.get("paper_total_trades", 0)),
                                    "paper_wins": int(trader._state.get("paper_win_trades", 0)),
                                    "order_id": closed.order_id,
                                }
                                self.send_text(format_managed_close_message(ev_close))
                        except Exception as exc:
                            stats["errors"].append(f"{res.symbol} REAL_SELL_CLOSE: {exc}")
                elif (
                    pos_side == "SHORT"
                    and not pos.get("use_trailing", True)
                    and res.short_15m_evaluation is not None
                ):
                    sev = res.short_15m_evaluation
                    if sev.cover_confirmed_prev_bar and sev.prev_bar_open_time > entry_bar_ts:
                        try:
                            closed = trader.close_position(
                                res.symbol,
                                fallback_price=res.price,
                                is_stop_loss=False,
                                close_reason="KAPAT (15dk Dönüş Sinyali)",
                            )
                            if closed:
                                entry_px = float(pos.get("entry_price") or closed.price)
                                pnl_usd = (entry_px - closed.price) * closed.qty
                                pnl_pct = (
                                    ((entry_px - closed.price) / entry_px) * 100.0
                                    if entry_px > 0
                                    else 0.0
                                )
                                stats["orders_closed"] += 1
                                ev_close = {
                                    "symbol": res.symbol,
                                    "side": "SHORT",
                                    "tf_label": "15dk (15M)",
                                    "reason": "KAPAT (15dk Dönüş Sinyali)",
                                    "entry_price": entry_px,
                                    "exit_price": closed.price,
                                    "pnl_usd": round(pnl_usd, 2),
                                    "pnl_pct": round(pnl_pct, 2),
                                    "order_id": closed.order_id,
                                }
                                self.send_text(format_managed_close_message(ev_close))
                        except Exception as exc:
                            stats["errors"].append(f"{res.symbol} SHORT_COVER_CLOSE: {exc}")

        if self.is_paused(trader):
            return stats

        if dual_mode:
            now_ms = int(time.time() * 1000)
            eff_long_min = (
                long_entry_window_minutes
                if long_entry_window_minutes is not None
                else entry_window_minutes
            )
            long_win_ms = max(0, eff_long_min) * 60 * 1000
            eff_short_min = (
                short_entry_window_minutes
                if short_entry_window_minutes is not None
                else 15
            )
            short_win_ms = max(0, eff_short_min) * 60 * 1000
            top_gainers: set[str] | None = None
            if short_top_gainers > 0:
                top_gainers = {
                    r.symbol
                    for r in sorted(results, key=lambda x: x.change_24h, reverse=True)[:short_top_gainers]
                    if r.change_24h > 0
                }
            for res in results:
                long_ev = res.evaluation
                short_ev = res.long_evaluation or res.evaluation
                sev_15m = res.short_15m_evaluation
                side: str | None = None
                use_short_15m_mode = False
                ev = short_ev
                tf_used = res.timeframe
                if (
                    long_ev.orange_trigger
                    and long_ev.win_rate >= long_min_win_rate
                    and long_ev.total_profit_pct >= long_min_profit_pct
                ):
                    if long_win_ms > 0 and long_ev.bar_close_time > 0:
                        rem_ms = long_ev.bar_close_time - now_ms
                        if -60_000 <= rem_ms <= long_win_ms:
                            side = "LONG"
                            ev = long_ev
                            tf_used = res.timeframe
                    else:
                        side = "LONG"
                        ev = long_ev
                        tf_used = res.timeframe
                if enable_short and side is None and sev_15m is not None:
                    if sev_15m.in_1h_zone and sev_15m.early_fired and (
                        top_gainers is None or res.symbol in top_gainers
                    ):
                        side = "SHORT"
                        use_short_15m_mode = True
                        tf_used = "15m"
                elif enable_short and side is None and short_ev.short_5_trigger and (top_gainers is None or res.symbol in top_gainers):
                    if short_win_ms > 0 and short_ev.bar_close_time > 0:
                        rem_ms = short_ev.bar_close_time - now_ms
                        if -60_000 <= rem_ms <= short_win_ms:
                            side = "SHORT"
                            ev = short_ev
                            tf_used = "4h" if res.long_evaluation is not None else res.timeframe
                    else:
                        side = "SHORT"
                        ev = short_ev
                        tf_used = "4h" if res.long_evaluation is not None else res.timeframe
                if not side:
                    continue

                sig_bar_ts = sev_15m.bar_open_time if (use_short_15m_mode and sev_15m is not None) else ev.bar_open_time
                alert_key = f"4h_dual:{tf_used}:{res.symbol}:{side}:{sig_bar_ts}"
                if alert_key in sent_alerts:
                    continue

                trade_exec: TradeExecution | None = None
                trade_note: str | None = None
                if trader and trader.is_configured and not dry_run:
                    try:
                        if side == "LONG":
                            trade_exec = trader.open_position(
                                res.symbol,
                                res.price,
                                trader.max_usd,
                                tp_price=res.price * 1.015,
                                sl_price=res.price * 0.975,
                                side="LONG",
                                use_trailing=False,
                                be_trigger_pct=0.0,
                                entry_bar_open_time=ev.bar_open_time,
                                allow_wide_sl=True,
                                v3_mode=True,
                                tp1_pct=1.50,
                                fee_buffer_pct=0.15,
                                trail_dist_pct=1.10,
                                close_stop_pct=2.50,
                                hard_stop_pct=2.50,
                            )
                        elif use_short_15m_mode and sev_15m is not None:
                            trade_exec = trader.open_position(
                                res.symbol,
                                res.price,
                                trader.max_usd,
                                tp_price=sev_15m.tp_level,
                                sl_price=sev_15m.sl_level,
                                side="SHORT",
                                use_trailing=False,
                                be_trigger_pct=1.5,
                                entry_bar_open_time=sev_15m.bar_open_time,
                            )
                        else:
                            trade_exec = trader.open_position(
                                res.symbol,
                                res.price,
                                trader.max_usd,
                                tp_price=None,
                                sl_price=None,
                                side="SHORT",
                                use_trailing=True,
                            )
                        if trade_exec:
                            stats["orders_opened"] += 1
                        else:
                            reason_txt = getattr(trader, "last_reject_reason", "") or (
                                f"Max {trader.max_positions} pozisyon, coin bekleme veya yetersiz marj."
                            )
                            trade_note = f"Emir açılmadı ({reason_txt})"
                    except Exception as exc:
                        trade_note = f"Emir hatası: {exc}"
                try:
                    if dry_run:
                        print(f"[DRY-RUN DUAL {tf_used.upper()} {side}] {res.symbol} @ {res.price}")
                    elif use_short_15m_mode:
                        self.send_text(
                            format_short_15m_message(
                                res,
                                trade_exec=trade_exec,
                                trade_note=trade_note,
                                stop_loss_pct=trader.stop_loss_pct if trader else 1.5,
                                top_n=short_top_gainers or 20,
                            )
                        )
                    else:
                        self.send_text(
                            format_dual_4h_message(
                                res,
                                side=side,
                                trade_exec=trade_exec,
                                trade_note=trade_note,
                                stop_loss_pct=trader.stop_loss_pct if trader else 1.5,
                                trailing_trigger_pct=trader.trailing_trigger_pct if trader else 2.0,
                                trailing_callback_pct=trader.trailing_callback_pct if trader else 0.3,
                                trailing_floor_pct=trader.trailing_floor_pct if trader else 1.7,
                                tf_override=tf_used,
                                ev_override=ev,
                                long_window_min=eff_long_min,
                                short_window_min=eff_short_min,
                            )
                        )
                    sent_alerts[alert_key] = int(time.time())
                    stats["long" if side == "LONG" else "short"] += 1
                    stats["orange"] += 1
                except Exception as exc:
                    stats["errors"].append(f"4H DUAL {res.symbol} {side}: {exc}")

            if not dry_run:
                self._save_state()
            return stats

        if trader and trader.is_configured and not dry_run:
            # Legacy LONG-only 3/5 signal cancel stop
            for res in results:
                pos = trader._state.get("open_positions", {}).get(res.symbol)
                if not pos:
                    continue
                if res.evaluation.all_five_buy and not pos.get("upgraded_5_5"):
                    pos["upgraded_5_5"] = True
                if not pos.get("trailing_active") and not res.evaluation.first_three_buy:
                    try:
                        out = trader.apply_signal_cancel_stop(res.symbol, res.price)
                        if out and out.get("action") == "closed":
                            stats["orders_closed"] += 1
                            self.send_text(format_managed_close_message(out["event"]))
                        elif out and out.get("action") == "tightened":
                            self.send_text(
                                f"🛡️ <b>SİNYAL BOZULDU — STOP -${out['stop_usd']:g}'YE ÇEKİLDİ</b>\n"
                                f"🪙 <b>{res.symbol}</b>\n"
                                f"💵 <b>Giriş:</b> <code>{fmt_price(out['entry_price'])}</code> | "
                                f"<b>Anlık:</b> <code>{fmt_price(out['current_price'])}</code>\n"
                                f"🛑 <b>Yeni Stop:</b> <code>{fmt_price(out['sl_price'])}</code> (-${out['stop_usd']:g})"
                            )
                    except Exception as exc:
                        stats["errors"].append(f"{res.symbol} SIGNAL_CANCEL: {exc}")

        for res in results:
            ev = res.evaluation
            if ev.win_rate < min_win_rate or ev.total_profit_pct < min_profit_pct:
                continue

            # 1) Fresh 3/5 (green_trigger) OR direct 5/5 (green_trigger & orange_trigger on same bar):
            # Always target max_usd ($250, auto-clamped down to min_usd $150 if margin is tighter).
            if ev.green_trigger:
                kind = "ORANGE" if ev.orange_trigger else "GREEN"
                alert_key = f"4h_bist:{res.symbol}:{kind}:{ev.bar_open_time}"
                if alert_key not in sent_alerts:
                    trade_exec: TradeExecution | None = None
                    trade_note: str | None = None
                    if trader and trader.is_configured and not dry_run:
                        try:
                            trade_exec = trader.open_position(
                                res.symbol,
                                res.price,
                                trader.max_usd,
                                tp_price=None,
                                sl_price=ev.sl_level or ev.prev_sup,
                            )
                            if trade_exec:
                                stats["orders_opened"] += 1
                            else:
                                trade_note = f"Emir açılmadı (Max {trader.max_positions} pozisyon, coin bekleme veya yetersiz marj)."
                        except Exception as exc:
                            trade_note = f"Emir hatası: {exc}"
                    try:
                        if dry_run:
                            print(
                                f"[DRY-RUN 4H {kind}] {res.symbol} WR={ev.win_rate}% Profit={ev.total_profit_pct}%"
                            )
                        else:
                            self.send_text(
                                format_4h_alert_only_message(
                                    res, kind, trade_exec=trade_exec, trade_note=trade_note
                                )
                            )
                        sent_alerts[alert_key] = int(time.time())
                        stats["orange" if kind == "ORANGE" else "green"] += 1
                    except Exception as exc:
                        stats["errors"].append(f"4H {res.symbol} {kind}: {exc}")
            elif ev.orange_trigger:
                alert_key = f"4h_bist:{res.symbol}:ORANGE_LATE:{ev.bar_open_time}"
                if alert_key not in sent_alerts:
                    trade_note = "5/5 onayı geldi (Geç giriş yapılmadı — emir sadece ilk 3/5 girişinde açılır)."
                    try:
                        if dry_run:
                            print(
                                f"[DRY-RUN 4H ORANGE INFO] {res.symbol} WR={ev.win_rate}% Profit={ev.total_profit_pct}%"
                            )
                        else:
                            self.send_text(
                                format_4h_alert_only_message(
                                    res, "ORANGE", trade_exec=None, trade_note=trade_note
                                )
                            )
                        sent_alerts[alert_key] = int(time.time())
                        stats["orange"] += 1
                    except Exception as exc:
                        stats["errors"].append(f"4H {res.symbol} ORANGE: {exc}")

        if not dry_run:
            self._save_state()

        return stats
