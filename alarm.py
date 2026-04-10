"""
alarm.py — Terminalde bağımsız çalışan alarm botu.
Sadece iki koşulu tarar:
  • 4h trend tam net degil
  • fiyat 15m EMA20'den cok uzak
Koşul tetiklenirse terminale yazar; TELEGRAM_BOT_TOKEN varsa Telegram'a da gönderir.
"""

import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

FUTURES_BASE = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com").strip()
SCAN_EVERY = int(os.getenv("ALARM_SCAN_EVERY_SECONDS", "120"))
TOP_LIMIT = int(os.getenv("TOP_GAINERS_LIMIT", "30"))
MIN_VOLUME = float(os.getenv("MIN_QUOTE_VOLUME_USD", "15000000"))
MAX_VOLUME = float(os.getenv("MAX_QUOTE_VOLUME_USD", "5000000000"))
LOOKBACK = int(os.getenv("LOOKBACK_BARS", "260"))
EMA20_DIST_THRESHOLD = float(os.getenv("EMA20_DIST_THRESHOLD", "1.0"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STABLECOIN_BASES = {
    "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "USDD",
    "SUSD", "FRAX", "GUSD", "LUSD", "MIM", "USDN", "USDJ",
    "HUSD", "USDK", "VAI", "USTC", "UST", "CUSD", "EURC",
}


def fetch_json(url: str, *, params: dict | None = None, timeout: int = 20) -> dict | list:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def safe_float(v) -> float:
    try:
        result = float(v)
        return 0.0 if (math.isnan(result) or math.isinf(result)) else result
    except Exception:
        return 0.0


def symbol_ok(symbol: str) -> bool:
    if not symbol.endswith("USDT") or not symbol.isascii():
        return False
    base = symbol[:-4]
    return base.isalnum() and base.upper() not in STABLECOIN_BASES


def fetch_active_symbols() -> set[str]:
    data = fetch_json(f"{FUTURES_BASE}/fapi/v1/exchangeInfo", timeout=30)
    return {
        str(row["symbol"]).upper()
        for row in data.get("symbols", [])
        if row.get("status") == "TRADING"
        and row.get("contractType") == "PERPETUAL"
        and row.get("quoteAsset") == "USDT"
        and symbol_ok(str(row.get("symbol", "")))
    }


def fetch_top_gainers(active: set[str]) -> list[str]:
    tickers = fetch_json(f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=30)
    candidates: list[tuple[str, float, float]] = []
    for row in tickers:
        sym = str(row.get("symbol", "")).upper()
        if sym not in active:
            continue
        try:
            pct = float(row["priceChangePercent"])
            price = float(row["lastPrice"])
            vol = float(row["quoteVolume"])
        except (TypeError, ValueError):
            continue
        if pct <= 0 or price <= 0:
            continue
        if vol < MIN_VOLUME or vol > MAX_VOLUME:
            continue
        candidates.append((sym, pct, vol))
    candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [sym for sym, _, _ in candidates[:TOP_LIMIT]]


def fetch_klines(symbol: str, interval: str) -> pd.DataFrame:
    raw = fetch_json(
        f"{FUTURES_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": LOOKBACK},
        timeout=30,
    )
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean()
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - out["close"].shift(1)).abs(),
        (out["low"] - out["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    out["atr14"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    return out


def trend_up(row: pd.Series) -> bool:
    c = safe_float(row["close"])
    e20 = safe_float(row["ema20"])
    e50 = safe_float(row["ema50"])
    e200 = safe_float(row["ema200"])
    return c > e20 > e50 > e200


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except Exception as exc:
        print(f"  [Telegram gönderme hatası] {exc}")


def check_once() -> list[str]:
    active = fetch_active_symbols()
    symbols = fetch_top_gainers(active)
    print(f"  Taranan coinler ({len(symbols)}): {', '.join(symbols)}")

    alerts: list[str] = []
    for sym in symbols:
        try:
            df_15 = add_indicators(fetch_klines(sym, "15m"))
            df_4h = add_indicators(fetch_klines(sym, "4h"))
            row_15 = df_15.iloc[-2]
            row_4h = df_4h.iloc[-2]

            close = safe_float(row_15["close"])
            ema20 = safe_float(row_15["ema20"])
            atr = max(safe_float(row_15["atr14"]), close * 0.003)
            dist = (close - ema20) / atr if atr > 0 else 0.0

            reasons: list[str] = []
            if not trend_up(row_4h):
                reasons.append("4h trend tam net degil")
            if dist > EMA20_DIST_THRESHOLD:
                reasons.append("fiyat 15m EMA20'den cok uzak")

            if reasons:
                alerts.append(f"*{sym}* | {'; '.join(reasons)}")
        except Exception as exc:
            print(f"  [{sym}] hata: {exc}")

    return alerts


def main() -> None:
    print(
        f"Alarm botu basladi | "
        f"Top: {TOP_LIMIT} | "
        f"Scan: {SCAN_EVERY}s | "
        f"EMA20 uzaklik esigi: {EMA20_DIST_THRESHOLD} ATR"
    )

    while True:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n[{now}] Tarama basliyor...")
        try:
            alerts = check_once()
            if alerts:
                body = "\n".join(alerts)
                msg = f"⚠️ *ALARM* | {now}\n\n{body}"
                print(msg)
                send_telegram(msg)
            else:
                print("  Alarm yok.")
        except Exception as exc:
            print(f"[HATA] {now} | {exc}")

        time.sleep(SCAN_EVERY)


if __name__ == "__main__":
    main()
