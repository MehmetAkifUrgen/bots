"""Railway / CLI 4-Hour Telegram-Only Alert Scanner (WR >= 60%, Profit >= 200%)."""

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
from .telegram_notifier import TelegramNotifier

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "alert_state.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Binance 4S (4H) Telegram Bildirim Tarayıcısı (Sadece Bildirim — Emir Yok)"
    )
    p.add_argument(
        "--market",
        choices=["futures", "papi", "spot"],
        default=os.getenv("BINANCE_MARKET", "futures"),
        help="Taranacak Binance piyasası (varsayılan: futures)",
    )
    p.add_argument(
        "--min-volume",
        type=float,
        default=float(os.getenv("MIN_VOLUME_USDT", "5000000")),
        help="Minimum 24s USDT hacim filtresi (varsayılan: 5000000)",
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
        "--limit",
        type=int,
        default=0,
        help="En yüksek hacimli ilk N coin (0 = tümü, varsayılan: 0)",
    )
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.getenv("POLL_SECONDS", "300")),
        help="Taramalar arası bekleme süresi saniye (varsayılan: 300)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("SCAN_WORKERS", "4")),
        help="Paralel tarama iş parçacığı sayısı (varsayılan: 4)",
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
    "mode": "4h_telegram_only",
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
            "✅ <b>4S (4H) Telegram Tarayıcı</b> aktif! (Sadece Bildirim — WR>=%60 & Getiri>=%200)"
        )
        print("Telegram test:", "OK" if ok else "FAILED")
        return

    scanner = Binance4hScanner(market=args.market, max_workers=args.workers)

    if args.port > 0 and not args.once:
        _start_health_server(args.port)

    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] 4S (4H) Telegram Tarayıcısı başlatıldı | "
        f"Mod=SADECE BİLDİRİM (Emir Yok) | Filtre=WR>={args.min_win_rate:g}% & Getiri>={args.min_profit_pct:g}% | "
        f"MinHacim=${args.min_volume:,.0f} | Periyot={args.poll_seconds}sn"
    )

    while True:
        try:
            results, errors, duration = scanner.scan_4h_bist_alerts(
                min_volume_usdt=args.min_volume,
                limit=args.limit,
            )
            stats = notifier.process_4h_alert_only(
                results,
                min_win_rate=args.min_win_rate,
                min_profit_pct=args.min_profit_pct,
                dry_run=args.dry_run,
            )

            qual = sum(
                1
                for r in results
                if r.evaluation.win_rate >= args.min_win_rate
                and r.evaluation.total_profit_pct >= args.min_profit_pct
            )
            green_now = sum(1 for r in results if r.evaluation.green_trigger)
            orange_now = sum(1 for r in results if r.evaluation.orange_trigger)

            _HEALTH_STATE.update(
                {
                    "status": "ok",
                    "last_scan_at": datetime.now(timezone.utc).isoformat(),
                    "total_scanned": len(results),
                    "qualifying_coins": qual,
                    "last_error": None,
                }
            )
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"[4S BİLDİRİM] Tarandı: {len(results)} coin ({duration}s, hata: {len(errors)}) | "
                f"Şarta Uyan (WR>=%{args.min_win_rate:g} & Getiri>=%{args.min_profit_pct:g}): {qual} | "
                f"Tetiklenen Yeşil: {green_now}, Turuncu: {orange_now} | "
                f"Gönderilen -> Yeşil: {stats['green']}, Turuncu: {stats['orange']}"
            )
            if stats["errors"]:
                print("  Telegram hataları:", stats["errors"][:5])
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
