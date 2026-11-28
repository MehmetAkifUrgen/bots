"""Railway / CLI 4-Hour Auto-Trading & Telegram Bot (WR >= 60%, Profit >= 200%)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

from dotenv import load_dotenv

from .binance_scanner import Binance4hScanner
from .binance_trader import BinanceTrader
from .telegram_notifier import TelegramNotifier, format_managed_close_message

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "alert_state.json"
DEFAULT_TRADE_STATE_FILE = Path(__file__).resolve().parent / "trade_state.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Binance 4S (4H) Otomatik Al-Sat & Telegram Botu (WR >= %60, Getiri >= %200)"
    )
    p.add_argument(
        "--market",
        choices=["futures", "papi", "spot"],
        default=os.getenv("BINANCE_MARKET", "papi"),
        help="Taranacak ve işlem açılacak Binance piyasası (varsayılan: papi)",
    )
    p.add_argument(
        "--min-usd",
        type=float,
        default=float(os.getenv("MIN_POSITION_USD", "100")),
        help="Minimum pozisyon büyüklüğü USD (varsayılan: 100)",
    )
    p.add_argument(
        "--max-usd",
        type=float,
        default=float(os.getenv("MAX_POSITION_USD", "150")),
        help="Maksimum pozisyon büyüklüğü USD (varsayılan: 150)",
    )
    p.add_argument(
        "--max-positions",
        type=int,
        default=int(os.getenv("MAX_OPEN_POSITIONS", "3")),
        help="Aynı anda açık olabilecek maksimum pozisyon sayısı (varsayılan: 3)",
    )
    p.add_argument(
        "--leverage",
        type=int,
        default=int(os.getenv("LEVERAGE", "50")),
        help="Varsayılan kaldıraç (varsayılan: 50)",
    )
    p.add_argument(
        "--max-leverage",
        type=int,
        default=int(os.getenv("MAX_LEVERAGE", "75")),
        help="Maksimum kaldıraç tavanı (varsayılan: 75)",
    )
    p.add_argument(
        "--min-volume",
        type=float,
        default=float(os.getenv("MIN_VOLUME_USDT", "15000000")),
        help="Minimum 24s USDT hacim filtresi (varsayılan: 15000000)",
    )
    p.add_argument(
        "--min-win-rate",
        type=float,
        default=float(os.getenv("MIN_WIN_RATE", "65.0")),
        help="Minimum coin başarı oranı yüzdesi (varsayılan: 65.0)",
    )
    p.add_argument(
        "--min-profit-pct",
        type=float,
        default=float(os.getenv("MIN_PROFIT_PCT", "300.0")),
        help="Minimum coin toplam getiri yüzdesi (varsayılan: 300.0)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="En yüksek hacimli ilk N coin (0 = tümü, varsayılan: 0)",
    )
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("POLL_SECONDS", "60")),
        help="Taramalar arası bekleme süresi saniye (varsayılan: 60)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("SCAN_WORKERS", "2")),
        help="Paralel tarama iş parçacığı sayısı (varsayılan: 2)",
    )
    p.add_argument(
        "--no-auto-trade",
        action="store_true",
        help="Binance API bağlı olsa bile otomatik emir açma (sadece Telegram bildirimi)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Tek sefer tarama yap ve çık",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Telegram mesajı göndermeden konsola yazdır",
    )
    p.add_argument(
        "--test-telegram",
        action="store_true",
        help="Telegram bağlantısını test etmek için örnek mesaj gönder",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "0")),
        help="Railway / cloud web servisi için HTTP health portu",
    )
    return p


_HEALTH_STATE: dict = {
    "status": "starting",
    "mode": "4h_auto_trade",
    "last_scan_at": None,
    "total_scanned": 0,
    "qualifying_coins": 0,
    "last_error": None,
}


def _start_health_server(port: int) -> None:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(_HEALTH_STATE, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Health HTTP sunucusu :{port} portunda aktif")


def _start_position_monitor(
    traders: list[BinanceTrader],
    notifier: TelegramNotifier,
    silent_traders: list[BinanceTrader] | None = None,
    dry_run: bool = False,
    interval_sec: int = 2,
) -> None:
    """Fast 2-second monitor for TP1, Trailing, and Stop-Loss (visible paper + silent real Binance)."""
    primary = traders[0] if traders else None
    extras = traders[1:] if len(traders) > 1 else []
    hidden = silent_traders or []

    def _loop() -> None:
        last_sync = 0.0
        while True:
            try:
                notifier.poll_commands(primary, extra_traders=extras)
                is_paused = notifier.is_paused(primary)
                for ht in hidden:
                    if ht.is_paused() != is_paused:
                        ht.set_paused(is_paused)
                now = time.time()
                if now - last_sync >= 15.0:
                    for t in traders:
                        synced = t.sync_live_positions()
                        if synced:
                            print(
                                f"[{datetime.now().strftime('%H:%M:%S')}] "
                                f"[{t.wallet_name}] Açık pozisyon: {list(synced.keys())}"
                            )
                    for ht in hidden:
                        synced_h = ht.sync_live_positions()
                        if synced_h:
                            print(
                                f"[{datetime.now().strftime('%H:%M:%S')}] "
                                f"[SESSİZ GERÇEK] Açık pozisyon: {list(synced_h.keys())}"
                            )
                    last_sync = now
                if not dry_run:
                    any_closed = False
                    for t in traders:
                        if t.has_open_positions() or t.has_pending_close_events():
                            closed_events = t.manage_open_positions()
                            for ev in closed_events:
                                any_closed = True
                                print(
                                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                                    f"[{t.wallet_name}] KAPANIŞ: {ev['symbol']} {ev['reason']} "
                                    f"({ev['pnl_usd']:+.2f}$ / {ev['pnl_pct']:+.2f}%)"
                                )
                                try:
                                    notifier.send_text(format_managed_close_message(ev))
                                except Exception as exc:
                                    print(f"  Telegram kapanış bildirim hatası: {exc}")
                    if any_closed:
                        notifier.sync_cloud_dashboard(traders)
                    # Silent real Binance management (NEVER sends anything to Telegram!)
                    for ht in hidden:
                        if ht.has_open_positions() or ht.has_pending_close_events():
                            h_events = ht.manage_open_positions()
                            for hev in h_events:
                                print(
                                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                                    f"[SESSİZ GERÇEK] {hev['symbol']} {hev['reason']} "
                                    f"({hev['pnl_usd']:+.2f}$ / {hev['pnl_pct']:+.2f}%)"
                                )
            except Exception as exc:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Pozisyon takip hatası: {exc}")
            time.sleep(interval_sec)

    t_thread = threading.Thread(target=_loop, daemon=True)
    t_thread.start()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Hızlı TP / Trailing / Stop takipçisi aktif (her {interval_sec}sn)")


def _process_silent_real_v3(
    real_trader: BinanceTrader,
    results: list,
    long_min_wr: float,
    long_min_profit: float,
    long_window_min: int,
    is_paused: bool,
) -> tuple[int, int]:
    """Execute Futures v3 entries & confirmed-bar exits on real Binance silently (zero Telegram output)."""
    opened = 0
    closed_cnt = 0
    price_map = {r.symbol: r.price for r in results}
    try:
        evs = real_trader.manage_open_positions(price_map)
        closed_cnt += sum(1 for e in evs if not e.get("is_partial"))
    except Exception as exc:
        print(f"  [SESSİZ GERÇEK] manage hatası: {exc}")

    # 1) Confirmed 4H bar exits (-2.20% close stop & TREND BİTTİ) even when paused
    for res in results:
        pos = real_trader._state.get("open_positions", {}).get(res.symbol)
        if not pos or not pos.get("v3_mode"):
            continue
        ev_curr = res.evaluation
        entry_bar_ts = int(pos.get("entry_bar_open_time") or 0)
        tp1_hit = bool(pos.get("tp1_hit", False))
        close_sl_px = float(pos.get("close_sl_price") or 0.0)
        if ev_curr.prev_bar_open_time > entry_bar_ts:
            v3_reason: str | None = None
            v3_stop = False
            if not tp1_hit and close_sl_px > 0 and ev_curr.prev_bar_close < close_sl_px:
                v3_reason = "STOP (4S Kapanış -%2.20)"
                v3_stop = True
            elif ev_curr.prev_bar_real_sell:
                v3_reason = "TREND BİTTİ (KAPAT)"
                v3_stop = False
            if v3_reason:
                try:
                    c_res = real_trader.close_position(
                        res.symbol, fallback_price=res.price, is_stop_loss=v3_stop, close_reason=v3_reason
                    )
                    if c_res:
                        closed_cnt += 1
                        print(f"  [SESSİZ GERÇEK KAPANIŞ] {res.symbol} -> {v3_reason} @ {c_res.price}")
                except Exception as exc:
                    print(f"  [SESSİZ GERÇEK] kapanış hatası {res.symbol}: {exc}")

    if is_paused:
        return opened, closed_cnt

    # 2) New 4H Futures v3 LONG entries ($100-$150, Max 3, high leverage)
    now_ms = int(time.time() * 1000)
    win_ms = max(0, long_window_min) * 60 * 1000
    seen_sigs: dict = real_trader._state.setdefault("opened_signals", {})
    if len(seen_sigs) > 1000:
        recent_items = sorted(seen_sigs.items(), key=lambda kv: kv[1], reverse=True)[:500]
        real_trader._state["opened_signals"] = dict(recent_items)
        seen_sigs = real_trader._state["opened_signals"]

    for res in results:
        ev = res.evaluation
        if not (ev.orange_trigger and ev.win_rate >= long_min_wr and ev.total_profit_pct >= long_min_profit):
            continue
        if win_ms > 0 and ev.bar_close_time > 0:
            rem_ms = ev.bar_close_time - now_ms
            if not (-60_000 <= rem_ms <= win_ms):
                continue
        sig_key = f"{res.symbol}:{ev.bar_open_time}"
        if sig_key in seen_sigs:
            continue
        try:
            tex = real_trader.open_position(
                res.symbol,
                res.price,
                real_trader.max_usd,
                tp_price=res.price * 1.01,
                sl_price=res.price * 0.97,
                side="LONG",
                use_trailing=False,
                be_trigger_pct=0.0,
                entry_bar_open_time=ev.bar_open_time,
                allow_wide_sl=True,
                v3_mode=True,
                tp1_pct=1.00,
                fee_buffer_pct=0.15,
                trail_dist_pct=1.10,
                close_stop_pct=2.20,
                hard_stop_pct=3.00,
            )
            if tex:
                seen_sigs[sig_key] = int(time.time())
                real_trader._save_state()
                opened += 1
                print(
                    f"  [SESSİZ GERÇEK AÇILIŞ] {res.symbol} ${tex.notional_usd:.2f} @ {tex.price} (ID:{tex.order_id})"
                )
        except Exception as exc:
            print(f"  [SESSİZ GERÇEK] açılış hatası {res.symbol}: {exc}")

    return opened, closed_cnt


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")
    args = build_parser().parse_args()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    notifier = TelegramNotifier(
        bot_token=bot_token,
        chat_id=chat_id,
        state_file=DEFAULT_STATE_FILE,
    )

    if args.test_telegram:
        ok = notifier.send_text(
            "✅ <b>4S (4H) Otomatik Al-Sat Botu</b> aktif! (WR>=%60 & Getiri>=%200)"
        )
        print("Telegram test:", "OK" if ok else "FAILED")
        return

    paper_mode = os.getenv("PAPER_TRADE", "true").strip().lower() not in ("0", "false", "no")
    silent_real_env = os.getenv("SILENT_REAL_TRADE", "true").strip().lower() not in ("0", "false", "no")
    paper_balance = float(os.getenv("PAPER_BALANCE_USD", "100.0"))
    paper_min_usd = float(os.getenv("PAPER_MIN_USD", "100.0"))
    paper_max_usd = float(os.getenv("PAPER_MAX_USD", "150.0"))
    paper_max_pos = int(os.getenv("PAPER_MAX_POSITIONS", "3"))
    real_min_usd = float(os.getenv("REAL_MIN_USD", "100.0"))
    real_max_usd = float(os.getenv("REAL_MAX_USD", "150.0"))
    real_max_pos = int(os.getenv("REAL_MAX_POSITIONS", "3"))
    real_max_lev = int(os.getenv("REAL_MAX_LEVERAGE", "75"))
    taker_fee_pct = float(os.getenv("TAKER_FEE_PCT", "0.05"))
    auto_trade_env = os.getenv("AUTO_TRADE", "true").strip().lower() not in ("0", "false", "no")
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    proxy_url = (
        os.getenv("BINANCE_PROXY_URL")
        or os.getenv("FIXIE_URL")
        or os.getenv("HTTPS_PROXY")
        or ""
    )
    trader: BinanceTrader | None = None
    tradifi_trader: BinanceTrader | None = None
    real_trader: BinanceTrader | None = None
    if auto_trade_env and not args.no_auto_trade and (paper_mode or (api_key and api_secret)):
        trade_state_path = (
            Path(__file__).resolve().parent / "paper_trade_state.json"
            if paper_mode
            else DEFAULT_TRADE_STATE_FILE
        )
        eff_max_pos = paper_max_pos if paper_mode else args.max_positions
        eff_min_usd = paper_min_usd if paper_mode else args.min_usd
        eff_max_usd = paper_max_usd if paper_mode else args.max_usd
        trader = BinanceTrader(
            api_key=api_key,
            api_secret=api_secret,
            market=args.market,
            min_usd=eff_min_usd,
            max_usd=eff_max_usd,
            max_positions=eff_max_pos,
            leverage=args.leverage,
            max_leverage=args.max_leverage,
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "1.5")),
            trailing_trigger_pct=float(os.getenv("TRAILING_TRIGGER_PCT", "2.0")),
            trailing_callback_pct=float(os.getenv("TRAILING_CALLBACK_PCT", "0.3")),
            trailing_floor_pct=float(os.getenv("TRAILING_FLOOR_PCT", "1.7")),
            cooldown_minutes=0,
            symbol_stop_cooldown_minutes=240,
            signal_cancel_stop_usd=float(os.getenv("SIGNAL_CANCEL_STOP_USD", "1.5")),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "7.5")),
            state_file=trade_state_path,
            proxy_url=proxy_url,
            paper_mode=paper_mode,
            paper_balance_usd=paper_balance,
            taker_fee_pct=taker_fee_pct,
            wallet_name="KRİPTO",
        )
        # Second independent $100 virtual wallet for Binance US Stock Futures (TRADIFI_PERPETUAL)
        tradifi_trader = BinanceTrader(
            api_key="",
            api_secret="",
            market=args.market,
            min_usd=paper_min_usd,
            max_usd=paper_max_usd,
            max_positions=paper_max_pos,
            leverage=args.leverage,
            max_leverage=args.max_leverage,
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "1.5")),
            cooldown_minutes=0,
            symbol_stop_cooldown_minutes=240,
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "7.5")),
            state_file=Path(__file__).resolve().parent / "paper_tradifi_state.json",
            proxy_url=proxy_url,
            paper_mode=True,
            paper_balance_usd=paper_balance,
            taker_fee_pct=taker_fee_pct,
            wallet_name="ABD BORSA",
        )
        # Silent real Binance executor (NEVER exposed in Telegram!)
        if paper_mode and silent_real_env and api_key and api_secret:
            real_trader = BinanceTrader(
                api_key=api_key,
                api_secret=api_secret,
                market=args.market,
                min_usd=real_min_usd,
                max_usd=real_max_usd,
                max_positions=real_max_pos,
                leverage=real_max_lev,
                max_leverage=real_max_lev,
                stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "1.5")),
                cooldown_minutes=0,
                symbol_stop_cooldown_minutes=240,
                risk_per_trade_pct=0.0,
                state_file=DEFAULT_TRADE_STATE_FILE,
                proxy_url=proxy_url,
                paper_mode=False,
                wallet_name="GERÇEK",
                high_lev_fixed_usd=True,
            )

    scanner = Binance4hScanner(market=args.market, max_workers=args.workers, contract_type="PERPETUAL")
    tradifi_scanner = Binance4hScanner(market=args.market, max_workers=args.workers, contract_type="TRADIFI_PERPETUAL")

    if args.port > 0 and not args.once:
        _start_health_server(args.port)

    active_traders = [t for t in (trader, tradifi_trader) if t is not None]
    silent_traders = [real_trader] if real_trader is not None else []
    if (active_traders or silent_traders) and not args.once:
        restored = notifier.restore_from_cloud_dashboard(active_traders)
        if restored:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Sanal kasa verileri bulut panelinden geri yüklendi!")
        for t in active_traders:
            try:
                synced = t.sync_live_positions()
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"[{t.wallet_name}] Pozisyon senkronu: {list(synced.keys()) or 'YOK'}"
                )
            except Exception as exc:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [{t.wallet_name}] İlk senkron hatası: {exc}")
        for ht in silent_traders:
            try:
                synced_h = ht.sync_live_positions()
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"[SESSİZ GERÇEK] Canlı senkron: {list(synced_h.keys()) or 'YOK'}"
                )
            except Exception as exc:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [SESSİZ GERÇEK] İlk senkron hatası: {exc}")
        notifier.sync_cloud_dashboard(active_traders)
        _start_position_monitor(
            active_traders,
            notifier,
            silent_traders=silent_traders,
            dry_run=args.dry_run,
            interval_sec=2,
        )

    long_window_min = int(os.getenv("LONG_ENTRY_WINDOW_MINUTES", "0"))
    short_window_min = int(os.getenv("SHORT_ENTRY_WINDOW_MINUTES", "15"))
    long_min_wr = float(os.getenv("LONG_MIN_WIN_RATE", "70.0"))
    long_min_profit = float(os.getenv("LONG_MIN_PROFIT_PCT", "300.0"))
    effective_poll = min(args.poll_seconds, 60)

    enable_short = os.getenv("ENABLE_SHORT", "false").strip().lower() in ("1", "true", "yes")
    short_top_n = int(os.getenv("SHORT_TOP_GAINERS", "20")) or 20
    mode_badge = f"SANAL TEST (${paper_balance:g}, Komisyon %{taker_fee_pct:g}x2)" if paper_mode else "GERÇEK BİNANCE"
    trade_mode_str = (
        f"{mode_badge} (Max {paper_max_pos if paper_mode else args.max_positions} poz, 4S Futures v3: %50 TP1 + %50 Trailing)"
        if trader
        else "KAPALI (Sadece Bildirim)"
    )
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] 4S (4H) Futures v3 Botu başlatıldı | "
        f"Piyasa={args.market.upper()} | OtoAlım={trade_mode_str} | "
        f"LONG=4S 5/5 (WR>=%{long_min_wr:g}, Getiri>=%{long_min_profit:g}, %50 TP1 + %50 Trailing) | "
        f"SHORT={'AÇIK' if enable_short else 'KAPALI'} | Periyot={effective_poll}sn"
    )

    scan_interval = os.getenv("SCAN_INTERVAL", "4h").strip().lower()
    if scan_interval not in ("1h", "4h"):
        scan_interval = "4h"

    while True:
        try:
            if trader is None:
                notifier.poll_commands(None)
            open_short_syms: set[str] = set()
            if trader is not None:
                open_short_syms = {
                    s
                    for s, p in trader._state.get("open_positions", {}).items()
                    if str(p.get("side") or "").upper() == "SHORT"
                }
            results, errors, duration = scanner.scan_4h_bist_alerts(
                min_volume_usdt=args.min_volume,
                limit=args.limit,
                interval=scan_interval,
                short_top_gainers=short_top_n,
                extra_15m_symbols=open_short_syms,
                enable_short=enable_short,
            )
            entry_window_min = int(os.getenv("ENTRY_WINDOW_MINUTES", "0"))
            stats = notifier.process_4h_alert_only(
                results,
                min_win_rate=long_min_wr,
                min_profit_pct=long_min_profit,
                dry_run=args.dry_run,
                trader=trader,
                dual_mode=True,
                entry_window_minutes=entry_window_min,
                long_entry_window_minutes=long_window_min,
                short_entry_window_minutes=short_window_min,
                short_top_gainers=short_top_n,
                long_min_win_rate=long_min_wr,
                long_min_profit_pct=long_min_profit,
                enable_short=enable_short,
            )
            if real_trader is not None and not args.dry_run:
                _process_silent_real_v3(
                    real_trader,
                    results,
                    long_min_wr=long_min_wr,
                    long_min_profit=long_min_profit,
                    long_window_min=long_window_min,
                    is_paused=notifier.is_paused(trader),
                )

            # 2) Binance US Stock Futures (TRADIFI_PERPETUAL) on its own $100 virtual wallet
            tradifi_results, tradifi_errors, tradifi_dur = tradifi_scanner.scan_4h_bist_alerts(
                min_volume_usdt=float(os.getenv("TRADIFI_MIN_VOLUME_USDT", "500000")),
                limit=args.limit,
                interval=scan_interval,
                enable_short=False,
            )
            tradifi_stats = notifier.process_4h_alert_only(
                tradifi_results,
                min_win_rate=long_min_wr,
                min_profit_pct=long_min_profit,
                dry_run=args.dry_run,
                trader=tradifi_trader,
                dual_mode=True,
                entry_window_minutes=entry_window_min,
                long_entry_window_minutes=long_window_min,
                short_entry_window_minutes=short_window_min,
                short_top_gainers=short_top_n,
                long_min_win_rate=long_min_wr,
                long_min_profit_pct=long_min_profit,
                enable_short=False,
            )

            long_now = sum(
                1
                for r in results
                if r.evaluation.orange_trigger
                and r.evaluation.win_rate >= long_min_wr
                and r.evaluation.total_profit_pct >= long_min_profit
            )
            tradifi_long_now = sum(
                1
                for r in tradifi_results
                if r.evaluation.orange_trigger
                and r.evaluation.win_rate >= long_min_wr
                and r.evaluation.total_profit_pct >= long_min_profit
            )

            _HEALTH_STATE.update(
                {
                    "status": "paused" if notifier.is_paused(trader) else "ok",
                    "last_scan_at": datetime.now(timezone.utc).isoformat(),
                    "total_scanned": len(results) + len(tradifi_results),
                    "qualifying_coins": long_now + tradifi_long_now,
                    "last_error": None,
                }
            )
            if (
                stats["orders_opened"]
                + stats["orders_closed"]
                + tradifi_stats["orders_opened"]
                + tradifi_stats["orders_closed"]
            ) > 0 and active_traders:
                notifier.sync_cloud_dashboard(active_traders)
            paused_tag = " [DURDURULDU]" if notifier.is_paused(trader) else ""
            c_bal = f"${trader.get_account_equity():.2f}" if trader else "-"
            t_bal = f"${tradifi_trader.get_account_equity():.2f}" if tradifi_trader else "-"
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"[4S SADECE LONG]{paused_tag} Kripto: {len(results)} ({duration}s, LONG:{long_now}, Kasa:{c_bal}) | "
                f"ABD Borsa: {len(tradifi_results)} ({tradifi_dur}s, LONG:{tradifi_long_now}, Kasa:{t_bal}) | "
                f"Açılan: {stats['orders_opened'] + tradifi_stats['orders_opened']}, "
                f"Kapanan: {stats['orders_closed'] + tradifi_stats['orders_closed']}"
            )
            if stats["errors"]:
                print("  Hatalar:", stats["errors"][:5])
        except Exception as exc:
            err_str = str(exc)
            _HEALTH_STATE.update({"status": "error", "last_error": err_str})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama hatası: {err_str}")
            if "418" in err_str or "429" in err_str:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Binance rate limit — 180sn bekleniyor...")
                if not args.once:
                    time.sleep(180)
                    continue

        if args.once:
            break
        time.sleep(max(30, effective_poll))


if __name__ == "__main__":
    main()
