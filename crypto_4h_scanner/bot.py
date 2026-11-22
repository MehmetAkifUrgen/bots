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
        default=float(os.getenv("MIN_POSITION_USD", "25")),
        help="Minimum pozisyon büyüklüğü USD (varsayılan: 25)",
    )
    p.add_argument(
        "--max-usd",
        type=float,
        default=float(os.getenv("MAX_POSITION_USD", "250")),
        help="Maksimum pozisyon büyüklüğü USD (varsayılan: 250)",
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
        default=int(os.getenv("LEVERAGE", "10")),
        help="Varsayılan kaldıraç (max 10x güvenlik tavanı, varsayılan: 10)",
    )
    p.add_argument(
        "--max-leverage",
        type=int,
        default=int(os.getenv("MAX_LEVERAGE", "10")),
        help="Likidasyon koruması için maksimum kaldıraç tavanı (varsayılan: 10)",
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
        default=float(os.getenv("MIN_WIN_RATE", "60.0")),
        help="Minimum coin başarı oranı yüzdesi (varsayılan: 60.0)",
    )
    p.add_argument(
        "--min-profit-pct",
        type=float,
        default=float(os.getenv("MIN_PROFIT_PCT", "250.0")),
        help="Minimum coin toplam getiri yüzdesi (varsayılan: 250.0)",
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
        default=int(os.getenv("POLL_SECONDS", "180")),
        help="Taramalar arası bekleme süresi saniye (varsayılan: 180)",
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
    trader: BinanceTrader,
    notifier: TelegramNotifier,
    dry_run: bool = False,
    interval_sec: int = 2,
) -> None:
    """Fast 2-second monitor for Pivot TP, +2.5% Break-Even Lock (+0.3%), and Stop-Loss."""

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
                            f"KAPANIŞ: {ev['symbol']} {ev['reason']} "
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Hızlı TP / Kâr Kilidi / Stop takipçisi aktif (her {interval_sec}sn)")


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
    if auto_trade_env and not args.no_auto_trade and api_key and api_secret:
        trader = BinanceTrader(
            api_key=api_key,
            api_secret=api_secret,
            market=args.market,
            min_usd=args.min_usd,
            max_usd=args.max_usd,
            max_positions=args.max_positions,
            leverage=args.leverage,
            max_leverage=args.max_leverage,
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "2.0")),            # 4S Çift Yön Stop: -%2.00
            trailing_trigger_pct=float(os.getenv("TRAILING_TRIGGER_PCT", "2.0")),  # +%2.00 görünce trailing başlar
            trailing_callback_pct=float(os.getenv("TRAILING_CALLBACK_PCT", "0.3")),  # %0.30 zirve takibi
            trailing_floor_pct=float(os.getenv("TRAILING_FLOOR_PCT", "1.7")),  # En kötü +%1.70 kâr tabanı
            cooldown_minutes=0,
            symbol_stop_cooldown_minutes=240,
            signal_cancel_stop_usd=float(os.getenv("SIGNAL_CANCEL_STOP_USD", "1.5")),
            risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "8.0")),  # Kasa koruma: işlem başı max %8 risk
            state_file=DEFAULT_TRADE_STATE_FILE,
            proxy_url=proxy_url,
        )

    scanner = Binance4hScanner(market=args.market, max_workers=args.workers)

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
        _start_position_monitor(trader, notifier, dry_run=args.dry_run, interval_sec=2)

    trade_mode_str = (
        f"AÇIK (${args.min_usd:.0f}-${args.max_usd:.0f}, Max {args.max_positions} poz, Pivot TP/SL, +%2.5 Kâr Kilidi)"
        if trader
        else "KAPALI (Sadece Bildirim)"
    )
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] 4S (4H) Otomatik Al-Sat Botu başlatıldı | "
        f"Piyasa={args.market.upper()} | OtoAlım={trade_mode_str} | "
        f"Filtre=WR>={args.min_win_rate:g}% & Getiri>={args.min_profit_pct:g}% | Periyot={args.poll_seconds}sn"
    )

    scan_interval = os.getenv("SCAN_INTERVAL", "4h").strip().lower()
    if scan_interval not in ("1h", "4h"):
        scan_interval = "4h"

    while True:
        try:
            results, errors, duration = scanner.scan_4h_bist_alerts(
                min_volume_usdt=args.min_volume,
                limit=args.limit,
                interval=scan_interval,
            )
            entry_window_min = int(os.getenv("ENTRY_WINDOW_MINUTES", "0"))
            stats = notifier.process_4h_alert_only(
                results,
                min_win_rate=args.min_win_rate,
                min_profit_pct=args.min_profit_pct,
                dry_run=args.dry_run,
                trader=trader,
                dual_mode=True,
                entry_window_minutes=entry_window_min,
            )

            long_now = sum(1 for r in results if (r.long_evaluation or r.evaluation).long_5_trigger)
            short_now = sum(1 for r in results if r.evaluation.short_5_trigger)

            _HEALTH_STATE.update(
                {
                    "status": "ok",
                    "last_scan_at": datetime.now(timezone.utc).isoformat(),
                    "total_scanned": len(results),
                    "qualifying_coins": long_now + short_now,
                    "last_error": None,
                }
            )
            win_lbl = f"Son {entry_window_min}dk" if entry_window_min > 0 else "Anlık Giriş"
            tf_tag = "1S" if scan_interval == "1h" else "4S"
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"[{tf_tag} ÇİFT YÖN] Tarandı: {len(results)} coin ({duration}s, hata: {len(errors)}) | "
                f"LONG(5/5): {long_now}, SHORT(5/5): {short_now} ({win_lbl}) | "
                f"Emir -> Açılan: {stats['orders_opened']}, Kapanan: {stats['orders_closed']}"
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
        time.sleep(max(30, args.poll_seconds))


if __name__ == "__main__":
    main()
