"""CLI & background scanner for Binance 4H 5-indicator Telegram alerts."""

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
        description="Binance 4H 'Gün İçi Anlık Sistem' Telegram Tarayıcı"
    )
    p.add_argument(
        "--market",
        choices=["futures", "papi", "spot"],
        default=os.getenv("BINANCE_MARKET", "papi"),
        help="Taranacak ve işlem açılacak Binance piyasası (varsayılan: papi)",
    )
    p.add_argument(
        "--timeframe",
        choices=["45m", "1h", "15m"],
        default=os.getenv("SCAN_TIMEFRAME", "45m"),
        help="Tarama zaman dilimi: 45m, 1h veya 15m (varsayılan: 45m)",
    )
    p.add_argument(
        "--min-win-rate",
        type=float,
        default=float(os.getenv("MIN_WIN_RATE", "60.0")),
        help="Minimum coin başarı oranı yüzdesi (varsayılan: 60.0)",
    )
    p.add_argument(
        "--min-profit-pct",
        type=float,
        default=float(os.getenv("MIN_PROFIT_PCT", "200.0")),
        help="Minimum coin toplam getiri yüzdesi (varsayılan: 200.0)",
    )
    p.add_argument(
        "--min-usd",
        type=float,
        default=float(os.getenv("MIN_POSITION_USD", "200")),
        help="Minimum pozisyon büyüklüğü USD (varsayılan: 200)",
    )
    p.add_argument(
        "--max-usd",
        type=float,
        default=float(os.getenv("MAX_POSITION_USD", "300")),
        help="Maksimum pozisyon büyüklüğü USD (varsayılan: 300)",
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
        default=int(os.getenv("LEVERAGE", "20")),
        help="Varsayılan Futures kaldıraç oranı (gerekirse otomatik artırılır, varsayılan: 20)",
    )
    p.add_argument(
        "--stop-loss-pct",
        type=float,
        default=float(os.getenv("STOP_LOSS_PCT", "1.0")),
        help="Zarar kes yüzdesi (varsayılan: 1.0)",
    )
    p.add_argument(
        "--trailing-trigger-pct",
        type=float,
        default=float(os.getenv("TRAILING_TRIGGER_PCT", "2.0")),
        help="Trailing stop başlangıç kâr yüzdesi (varsayılan: 2.0)",
    )
    p.add_argument(
        "--trailing-callback-pct",
        type=float,
        default=float(os.getenv("TRAILING_CALLBACK_PCT", "0.4")),
        help="Trailing stop geri çekilme yüzdesi (varsayılan: 0.4)",
    )
    p.add_argument(
        "--cooldown-minutes",
        type=int,
        default=int(os.getenv("COOLDOWN_MINUTES", "0")),
        help="Art arda 3 stop sonrası mola süresi dakika (0 = kapalı, varsayılan: 0)",
    )
    p.add_argument(
        "--no-auto-trade",
        action="store_true",
        help="Binance API bağlı olsa bile otomatik emir açma (sadece Telegram bildirimi)",
    )
    p.add_argument(
        "--trade-on-green",
        action="store_true",
        default=os.getenv("TRADE_ON_GREEN", "false").strip().lower() in ("1", "true", "yes"),
        help="Yeşil AL (3/5) sinyalinde de emir aç (varsayılan: kapalı, sadece 5/5)",
    )
    p.add_argument(
        "--min-volume",
        type=float,
        default=5_000_000.0,
        help="Minimum 24s USDT hacim filtresi (varsayılan: 5000000)",
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
        default=int(os.getenv("POLL_SECONDS", "120")),
        help="Taramalar arası bekleme süresi saniye (varsayılan: 120)",
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
        "--notify-all-exits",
        action="store_true",
        help="Sadece takip edilenler değil tüm çıkış sinyallerini bildir",
    )
    p.add_argument(
        "--test-telegram",
        action="store_true",
        help="Telegram bağlantısını test etmek için örnek mesaj gönder",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("SCAN_WORKERS", "4")),
        help="Paralel tarama iş parçacığı sayısı (varsayılan: 4)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "0")),
        help="Ücretsiz cloud web servisleri (Render/Koyeb) için HTTP health portu",
    )
    return p


_HEALTH_STATE: dict = {
    "status": "starting",
    "last_scan_at": None,
    "total_scanned": 0,
    "in_positions": 0,
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
    trader: BinanceTrader,
    notifier: TelegramNotifier,
    dry_run: bool = False,
    interval_sec: int = 5,
) -> None:
    """Fast 5-second background monitor for -1% SL and +1% Trailing Stop."""

    def _loop() -> None:
        last_sync = 0.0
        while True:
            try:
                notifier.poll_commands(trader)
                now = time.time()
                if now - last_sync >= 60.0:
                    synced = trader.sync_live_positions()
                    last_sync = now
                    if synced:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S')}] "
                            f"Açık pozisyon senkronu: {list(synced.keys())}"
                        )
                if trader.has_open_positions() and not dry_run:
                    closed_events = trader.manage_open_positions()
                    for ev in closed_events:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S')}] "
                            f"HIZLI KAPANIŞ: {ev['symbol']} {ev['reason']} "
                            f"({ev['pnl_usd']:+.2f}$ / {ev['pnl_pct']:+.2f}%)"
                        )
                        try:
                            notifier.send_text(format_managed_close_message(ev))
                        except Exception as exc:
                            print(f"  Telegram kapanış bildirim hatası: {exc}")
            except Exception as exc:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Pozisyon takip hatası: {exc}")
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Hızlı Stop-Loss / Trailing takipçisi aktif (her {interval_sec}sn)")


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
            "✅ <b>4S (4H) Kripto Tarayıcı</b> Telegram bağlantısı aktif!"
        )
        print("Telegram test:", "OK" if ok else "FAILED")
        return

    auto_trade_env = os.getenv("AUTO_TRADE", "true").strip().lower() not in ("0", "false", "no")
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    proxy_url = (
        os.getenv("BINANCE_PROXY_URL")
        or os.getenv("FIXIE_URL")
        or os.getenv("HTTPS_PROXY")
        or ""
    )
    active_stop_pct = args.stop_loss_pct
    active_be_pct = args.trailing_trigger_pct

    trader: BinanceTrader | None = None
    if auto_trade_env and not args.no_auto_trade and api_key and api_secret:
        trader = BinanceTrader(
            api_key=api_key,
            api_secret=api_secret,
            market=args.market,
            min_usd=args.min_usd,
            max_usd=args.max_usd,
            max_positions=args.max_positions,
            leverage=args.leverage,
            stop_loss_pct=active_stop_pct,
            trailing_trigger_pct=active_be_pct,
            trailing_callback_pct=args.trailing_callback_pct,
            trailing_floor_pct=0.20,
            cooldown_minutes=args.cooldown_minutes,
            state_file=DEFAULT_TRADE_STATE_FILE,
            proxy_url=proxy_url,
        )

    scanner = Binance4hScanner(
        market=args.market,
        timeframe=args.timeframe,
        max_workers=args.workers,
    )

    if args.port > 0 and not args.once:
        _start_health_server(args.port)

    if trader and not args.once:
        try:
            synced = trader.sync_live_positions()
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Binance canlı pozisyon senkronu: {list(synced.keys()) or 'YOK'}"
            )
        except Exception as exc:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] İlk senkron hatası: {exc}")
        _start_position_monitor(trader, notifier, dry_run=args.dry_run, interval_sec=5)

    trade_mode_str = (
        f"AÇIK (${args.min_usd:.0f}-${args.max_usd:.0f}, Max {args.max_positions} poz, -%{active_stop_pct:g} SL, +%{active_be_pct:g} Trail)"
        if trader
        else "KAPALI (Sadece Bildirim)"
    )
    tf_title = "15dk" if args.timeframe == "15m" else ("45dk" if args.timeframe == "45m" else "1S (1h)")
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Kripto {tf_title} 5'li Tarayıcı başlatıldı | "
        f"Piyasa={args.market.upper()} | OtoAlım={trade_mode_str} | "
        f"Filtre=WR>={args.min_win_rate:g}% & Getiri>={args.min_profit_pct:g}% | "
        f"MinHacim=${args.min_volume:,.0f} | Limit={args.limit or 'TÜMÜ'} | Periyot={args.poll_seconds}sn"
    )

    while True:
        try:
            results, errors, duration = scanner.scan_market(
                min_volume_usdt=args.min_volume,
                limit=args.limit,
            )
            stats = notifier.process_results(
                results,
                notify_all_exits=args.notify_all_exits,
                dry_run=args.dry_run,
                trader=trader,
                trade_on_green=args.trade_on_green,
                min_win_rate=args.min_win_rate,
                min_profit_pct=args.min_profit_pct,
            )

            green_now = sum(1 for r in results if r.evaluation.green_trigger)
            orange_now = sum(1 for r in results if r.evaluation.orange_trigger)
            in_pos = sum(
                1
                for r in results
                if r.evaluation.in_green_pos or r.evaluation.in_orange_pos
            )
            _HEALTH_STATE.update(
                {
                    "status": "ok",
                    "last_scan_at": datetime.now(timezone.utc).isoformat(),
                    "total_scanned": len(results),
                    "in_positions": in_pos,
                    "last_error": None,
                }
            )
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Tarandı: {len(results)} coin ({duration}s, hata: {len(errors)}) | "
                f"Açık Pozisyon: {in_pos} | Tetiklenen Yeşil: {green_now}, Turuncu: {orange_now} | "
                f"Gönderilen -> Yeşil: {stats['green']}, Turuncu: {stats['orange']}, Çıkış: {stats['exit']}"
            )
            if stats["errors"]:
                print("  Telegram hataları:", stats["errors"][:5])
        except Exception as exc:
            err_str = str(exc)
            _HEALTH_STATE.update({"status": "error", "last_error": err_str})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama hatası: {err_str}")
            if "418" in err_str or "429" in err_str:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    "Binance rate limit (418/429) — 180sn bekleniyor..."
                )
                if not args.once:
                    time.sleep(180)
                    continue

        if args.once:
            break
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    main()
