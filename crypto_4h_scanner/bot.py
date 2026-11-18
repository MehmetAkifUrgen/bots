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
from .telegram_notifier import TelegramNotifier

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
        default=int(os.getenv("LEVERAGE", "10")),
        help="Futures kaldıraç oranı (varsayılan: 10)",
    )
    p.add_argument(
        "--stop-loss-usd",
        type=float,
        default=float(os.getenv("STOP_LOSS_USD", "3.0")),
        help="Sabit zarar kes USD (varsayılan: 3.0)",
    )
    p.add_argument(
        "--trailing-trigger-pct",
        type=float,
        default=float(os.getenv("TRAILING_TRIGGER_PCT", "1.0")),
        help="Trailing stop başlangıç kâr yüzdesi (varsayılan: 1.0)",
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
        default=int(os.getenv("COOLDOWN_MINUTES", "240")),
        help="Art arda 3 stop sonrası mola süresi dakika (varsayılan: 240)",
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
            stop_loss_usd=args.stop_loss_usd,
            trailing_trigger_pct=args.trailing_trigger_pct,
            trailing_callback_pct=args.trailing_callback_pct,
            cooldown_minutes=args.cooldown_minutes,
            state_file=DEFAULT_TRADE_STATE_FILE,
            proxy_url=proxy_url,
        )

    scanner = Binance4hScanner(market=args.market, max_workers=args.workers)

    if args.port > 0 and not args.once:
        _start_health_server(args.port)

    trade_mode_str = (
        f"AÇIK (${args.min_usd:.0f}-${args.max_usd:.0f}, Max {args.max_positions} poz, -${args.stop_loss_usd:g} SL, +{args.trailing_trigger_pct:g}% Trail)"
        if trader
        else "KAPALI (Sadece Bildirim)"
    )
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] 45dk Ultra Scalp Tarayıcı başlatıldı | "
        f"Piyasa={args.market.upper()} | OtoAlım={trade_mode_str} | "
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
            _HEALTH_STATE.update({"status": "error", "last_error": str(exc)})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Tarama hatası: {exc}")

        if args.once:
            break
        time.sleep(max(10, args.poll_seconds))


if __name__ == "__main__":
    main()
