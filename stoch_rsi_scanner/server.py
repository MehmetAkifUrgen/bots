"""Web server for Binance Futures Stoch RSI (3,3,14,14,close) scanner."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .binance_scanner import BinanceFuturesScanner
from .paper_trader import PaperTrader

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_FILE = Path(__file__).resolve().parent / "paper_positions.json"
SCANNER = BinanceFuturesScanner(max_workers=12, timeout=10.0)
PAPER_TRADER = PaperTrader(state_file=STATE_FILE, default_notional=250.0)


class ScannerHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_bytes: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            index_path = STATIC_DIR / "index.html"
            if not index_path.exists():
                self.send_error(HTTPStatus.NOT_FOUND, "index.html not found")
                return
            self._send_html(index_path.read_bytes())
            return

        if parsed.path == "/health":
            self._send_json({"status": "ok"})
            return

        if parsed.path == "/api/positions":
            self._send_json(PAPER_TRADER.snapshot())
            return

        if parsed.path == "/api/scan":
            qs = parse_qs(parsed.query)
            interval = qs.get("interval", ["1d"])[0]
            if interval not in ("1d", "15m"):
                interval = "1d"
            mode = qs.get("mode", ["live"])[0]
            if mode not in ("live", "closed"):
                mode = "live"
            try:
                min_volume = float(qs.get("min_volume", ["5000000"])[0])
            except ValueError:
                min_volume = 5_000_000.0
            try:
                limit = int(qs.get("limit", ["150"])[0])
            except ValueError:
                limit = 150

            try:
                data = SCANNER.scan_market(
                    interval=interval,  # type: ignore[arg-type]
                    mode=mode,  # type: ignore[arg-type]
                    min_volume_usdt=min_volume,
                    limit=limit,
                )
                data["positions"] = PAPER_TRADER.process_scan_results(
                    data["results"], interval=interval
                )
                self._send_json(data)
            except Exception as exc:
                self._send_json(
                    {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR
                )
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body = self._read_json_body()

        if parsed.path == "/api/positions/config":
            snap = PAPER_TRADER.configure(
                auto_trade=body.get("auto_trade"),
                notional_usdt=body.get("notional_usdt"),
                stop_loss_pct=body.get("stop_loss_pct"),
            )
            self._send_json(snap)
            return

        if parsed.path == "/api/positions/close":
            pos_id = str(body.get("id", ""))
            self._send_json(PAPER_TRADER.close_position(pos_id))
            return

        if parsed.path == "/api/positions/close-all":
            self._send_json(PAPER_TRADER.close_all())
            return

        if parsed.path == "/api/positions/clear-history":
            self._send_json(PAPER_TRADER.clear_history())
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Stoch RSI Futures Scanner Web App")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), ScannerHandler)
    print(f"Stoch RSI Scanner running at http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
