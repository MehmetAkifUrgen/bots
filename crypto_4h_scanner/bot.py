"""CLI & background scanner for Binance 4H 5-indicator Telegram alerts."""

from __future__ import annotations

import argparse
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time

from dotenv import load_dotenv

from .binance_scanner import Binance4hScanner
from .telegram_notifier import TelegramNotifier

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "alert_state.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Binance 4H 'Gün İçi Anlık Sistem' Telegram Tarayıcı"
    )
    p.add_argument(
        "--market",
        choices=["futures", "spot"],
        default="futures",
        help="Taranacak Binance piyasası (varsayılan: futures)",
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
        default=60,
        help="Taramalar arası bekleme süresi saniye (varsayılan: 60)",
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
        default=12,
        help="Paralel tarama iş parçacığı sayısı (varsayılan: 12)",
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

    scanner = Binance4hScanner(market=args.market, max_workers=args.workers)

    if args.port > 0 and not args.once:
        _start_health_server(args.port)

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] 4S Tarayıcı başlatıldı | "
        f"Piyasa={args.market.upper()} | MinHacim=${args.min_volume:,.0f} | "
        f"Limit={args.limit or 'TÜMÜ'} | Periyot={args.poll_seconds}sn"
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
                    "last_scan_at": datetime.utcnow().isoformat() + "Z",
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
            _HEALTH_STATE.update({"status": "error", "last_error": str(exc)})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama hatası: {exc}")

        if args.once:
            break
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    main()
