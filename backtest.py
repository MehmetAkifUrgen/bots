"""
backtest.py — Binance Futures stratejilerini gecmis verilerle test etme modulu.

Kullanim:
  python backtest.py --symbol BTCUSDT --days 90 --interval 1h
  python backtest.py --symbol ETHUSDT --days 30 --interval 15m
  python backtest.py --all-symbols --days 60 --interval 1h --top 20
"""

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

# bot.py'deki ayni indikator fonksiyonlarini import edelim
import sys
sys.path.insert(0, os.path.dirname(__file__))
from bot import (
    TIMEFRAMES,
    STABLECOIN_BASES,
    compute_rsi,
    compute_atr,
    compute_adx,
    add_indicators,
    build_candidate_setups,
    safe_float,
    MarketTicker,
    Setup,
)


FUTURES_BASE = os.getenv("BINANCE_API_FUTURES_BASE", "https://fapi.binance.com")


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    setup_type: str
    strategy_key: str
    entry_price: float
    entry_time: str
    exit_price: float
    exit_time: str
    stop_loss: float
    take_profit: float
    exit_reason: str
    pnl_pct: float
    held_bars: int
    max_drawdown_pct: float
    max_profit_pct: float
    confidence: int


@dataclass
class BacktestResult:
    symbol: str
    strategy_key: str
    total_trades: int
    win_rate: float
    avg_pnl_pct: float
    total_pnl_pct: float
    max_drawdown_pct: float
    avg_hold_bars: int
    sharpe_ratio: float
    profit_factor: float
    max_consecutive_losses: int
    trades: list[BacktestTrade]


def fetch_klines_historical(
    symbol: str,
    interval: str,
    days: int,
    limit_per_request: int = 1500,
) -> pd.DataFrame:
    """Gecmis kline verilerini Binance API'den cek."""
    all_klines = []
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)
    current_time = start_time

    while current_time < end_time:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit_per_request,
            "startTime": int(current_time.timestamp() * 1000),
        }

        url = f"{FUTURES_BASE}/fapi/v1/klines"
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        raw = response.json()

        if not raw:
            break

        all_klines.extend(raw)

        # Bir sonraki iterasyon icin zaman araligini ilerlet
        last_open_time = raw[-1][0]
        current_time = datetime.fromtimestamp(last_open_time / 1000, tz=timezone.utc) + timedelta(milliseconds=1)

        # Rate limiting
        time.sleep(0.2)

        # Eger tum veriyi aldiysak dur
        if len(raw) < limit_per_request:
            break

    if not all_klines:
        return pd.DataFrame()

    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore",
    ]
    df = pd.DataFrame(all_klines, columns=columns)

    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)
    return df


def simulate_trades(
    symbol: str,
    df: pd.DataFrame,
    interval: str,
    min_confidence: int = 78,
) -> list[BacktestTrade]:
    """Verilen dataframe uzerinde strateji backtest'i yap."""
    if len(df) < 250:
        return []

    trades = []
    position = None
    entry_idx = None

    # Walk-forward testing
    for i in range(220, len(df) - 1):
        # Mevcut pozisyon varsa kontrol et
        if position is not None:
            current_bar = df.iloc[i]
            current_high = safe_float(current_bar["high"])
            current_low = safe_float(current_bar["low"])
            current_close = safe_float(current_bar["close"])

            exit_price = None
            exit_reason = None

            # Stop loss kontrolu
            if position["side"] == "LONG":
                if current_low <= position["stop_loss"]:
                    exit_price = position["stop_loss"]
                    exit_reason = "stop_loss"
                elif current_high >= position["take_profit"]:
                    exit_price = position["take_profit"]
                    exit_reason = "take_profit"
            else:  # SHORT
                if current_high >= position["stop_loss"]:
                    exit_price = position["stop_loss"]
                    exit_reason = "stop_loss"
                elif current_low <= position["take_profit"]:
                    exit_price = position["take_profit"]
                    exit_reason = "take_profit"

            if exit_price is not None:
                # Pozisyonu kapat
                if position["side"] == "LONG":
                    pnl_pct = ((exit_price - position["entry_price"]) / position["entry_price"]) * 100
                else:
                    pnl_pct = ((position["entry_price"] - exit_price) / position["entry_price"]) * 100

                held_bars = i - entry_idx

                trades.append(BacktestTrade(
                    symbol=symbol,
                    side=position["side"],
                    setup_type=position["setup_type"],
                    strategy_key=position["strategy_key"],
                    entry_price=position["entry_price"],
                    entry_time=position["entry_time"],
                    exit_price=exit_price,
                    exit_time=str(current_bar["open_time"]),
                    stop_loss=position["stop_loss"],
                    take_profit=position["take_profit"],
                    exit_reason=exit_reason,
                    pnl_pct=round(pnl_pct, 2),
                    held_bars=held_bars,
                    max_drawdown_pct=position["max_drawdown_pct"],
                    max_profit_pct=position["max_profit_pct"],
                    confidence=position["confidence"],
                ))
                position = None
                entry_idx = None
                continue

            # Pozisyon icin max profit/drawdown track
            if position["side"] == "LONG":
                current_pnl = ((current_close - position["entry_price"]) / position["entry_price"]) * 100
                position["max_profit_pct"] = max(position["max_profit_pct"], current_pnl)
                position["max_drawdown_pct"] = min(position["max_drawdown_pct"], current_pnl)
            else:
                current_pnl = ((position["entry_price"] - current_close) / position["entry_price"]) * 100
                position["max_profit_pct"] = max(position["max_profit_pct"], current_pnl)
                position["max_drawdown_pct"] = min(position["max_drawdown_pct"], current_pnl)

        # Yeni pozisyon ac
        if position is None:
            # Son 220 bar'i al
            historical_data = df.iloc[max(0, i-220):i]
            frames = {interval: historical_data.copy()}

            # Indicator ekle
            frames[interval] = add_indicators(frames[interval])

            if len(frames[interval]) < 200:
                continue

            # Market ticker simulasyonu
            market = MarketTicker(
                symbol=symbol,
                price_change_pct=0.0,  # Backtest'te onemli degil
                last_price=safe_float(df.iloc[i]["close"]),
                quote_volume=0.0,
            )

            try:
                setups = build_candidate_setups(market, frames, None)

                # En iyi READY setup'i bul
                ready_setups = [s for s in setups if s.ready and s.confidence >= min_confidence]
                if ready_setups:
                    best_setup = max(ready_setups, key=lambda s: s.confidence)

                    entry_price = safe_float(df.iloc[i]["close"])
                    position = {
                        "side": best_setup.decision,
                        "setup_type": best_setup.setup_type,
                        "strategy_key": best_setup.strategy_key,
                        "entry_price": entry_price,
                        "entry_time": str(df.iloc[i]["open_time"]),
                        "stop_loss": best_setup.stop_loss if best_setup.stop_loss else entry_price * 0.98,
                        "take_profit": best_setup.target_1 or entry_price * 1.02,
                        "max_drawdown_pct": 0.0,
                        "max_profit_pct": 0.0,
                        "confidence": best_setup.confidence,
                    }
                    entry_idx = i

            except Exception as e:
                continue

    # Test sonunda hala acik pozisyonu kapat
    if position is not None and len(df) > 220:
        last_bar = df.iloc[-1]
        exit_price = safe_float(last_bar["close"])
        if position["side"] == "LONG":
            pnl_pct = ((exit_price - position["entry_price"]) / position["entry_price"]) * 100
        else:
            pnl_pct = ((position["entry_price"] - exit_price) / position["entry_price"]) * 100

        trades.append(BacktestTrade(
            symbol=symbol,
            side=position["side"],
            setup_type=position["setup_type"],
            strategy_key=position["strategy_key"],
            entry_price=position["entry_price"],
            entry_time=position["entry_time"],
            exit_price=exit_price,
            exit_time=str(last_bar["open_time"]),
            stop_loss=position["stop_loss"],
            take_profit=position["take_profit"],
            exit_reason="test_ended",
            pnl_pct=round(pnl_pct, 2),
            held_bars=len(df) - entry_idx,
            max_drawdown_pct=position["max_drawdown_pct"],
            max_profit_pct=position["max_profit_pct"],
            confidence=position["confidence"],
        ))

    return trades


def compute_backtest_stats(trades: list[BacktestTrade]) -> dict:
    """Backtest sonuclarini analiz et."""
    if not trades:
        return {"error": "No trades generated"}

    results = {}

    # Genel istatistikler
    total_trades = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]

    win_rate = len(wins) / total_trades if total_trades > 0 else 0
    avg_pnl = np.mean([t.pnl_pct for t in trades]) if trades else 0
    total_pnl = np.sum([t.pnl_pct for t in trades]) if trades else 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in sorted(trades, key=lambda x: x.entry_time):
        cumulative += t.pnl_pct
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)

    # Sharpe ratio (basitlestirilmis)
    if len(trades) > 1:
        pnl_series = [t.pnl_pct for t in trades]
        sharpe = np.mean(pnl_series) / np.std(pnl_series) if np.std(pnl_series) > 0 else 0
    else:
        sharpe = 0

    # Profit factor
    gross_profit = sum(t.pnl_pct for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    # Max consecutive losses
    max_cons_losses = 0
    current_cons_losses = 0
    for t in sorted(trades, key=lambda x: x.entry_time):
        if t.pnl_pct <= 0:
            current_cons_losses += 1
            max_cons_losses = max(max_cons_losses, current_cons_losses)
        else:
            current_cons_losses = 0

    # Strategy breakdown
    strategy_stats = {}
    for t in trades:
        if t.strategy_key not in strategy_stats:
            strategy_stats[t.strategy_key] = {
                "trades": [],
                "wins": 0,
                "losses": 0,
            }
        strategy_stats[t.strategy_key]["trades"].append(t)
        if t.pnl_pct > 0:
            strategy_stats[t.strategy_key]["wins"] += 1
        else:
            strategy_stats[t.strategy_key]["losses"] += 1

    results = {
        "symbol": trades[0].symbol if trades else "UNKNOWN",
        "total_trades": total_trades,
        "win_rate": round(win_rate * 100, 2),
        "avg_pnl_pct": round(avg_pnl, 2),
        "total_pnl_pct": round(total_pnl, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": round(sharpe, 2),
        "profit_factor": round(profit_factor, 2),
        "max_consecutive_losses": max_cons_losses,
        "avg_hold_bars": round(np.mean([t.held_bars for t in trades]), 1) if trades else 0,
        "strategies": {},
        "trades": [t.__dict__ for t in trades],
    }

    # Strategy detaylari
    for strategy_key, stats in strategy_stats.items():
        strat_trades = stats["trades"]
        strat_wins = stats["wins"]
        strat_losses = stats["losses"]
        strat_total = strat_wins + strat_losses

        results["strategies"][strategy_key] = {
            "total_trades": strat_total,
            "win_rate": round(strat_wins / strat_total * 100, 2) if strat_total > 0 else 0,
            "avg_pnl_pct": round(np.mean([t.pnl_pct for t in strat_trades]), 2) if strat_trades else 0,
            "total_pnl_pct": round(np.sum([t.pnl_pct for t in strat_trades]), 2) if strat_trades else 0,
        }

    return results


def print_backtest_results(results: dict) -> None:
    """Backtest sonuclarini guzel bir formatta yazdir."""
    if "error" in results:
        print(f"\n❌ {results['error']}")
        return

    print("\n" + "=" * 60)
    print("📊 BACKTEST SONUCLARI")
    print("=" * 60)
    print(f"Symbol: {results['symbol']}")
    print(f"Toplam Islem: {results['total_trades']}")
    print(f"Win Rate: {results['win_rate']}%")
    print(f"Ortalama PnL: {results['avg_pnl_pct']:+.2f}%")
    print(f"Toplam PnL: {results['total_pnl_pct']:+.2f}%")
    print(f"Max Drawdown: {results['max_drawdown_pct']:.2f}%")
    print(f"Sharpe Ratio: {results['sharpe_ratio']:.2f}")
    print(f"Profit Factor: {results['profit_factor']:.2f}")
    print(f"Max Artarda Kayip: {results['max_consecutive_losses']}")
    print(f"Ortalama Tutme Suresi: {results['avg_hold_bars']} bar")

    print("\n" + "-" * 60)
    print("📈 STRATEJI BREAKDOWN")
    print("-" * 60)

    for strategy_key, stats in results["strategies"].items():
        emoji = "🟢" if stats["win_rate"] > 50 else "🔴"
        print(f"\n{emoji} {strategy_key}")
        print(f"  Islem: {stats['total_trades']}")
        print(f"  Win Rate: {stats['win_rate']}%")
        print(f"  Ort PnL: {stats['avg_pnl_pct']:+.2f}%")
        print(f"  Toplam PnL: {stats['total_pnl_pct']:+.2f}%")

    print("\n" + "=" * 60)

    # Ilk 5 trade ornek
    if results.get("trades"):
        print("\n📝 SON 5 ISLEM:")
        print("-" * 60)
        for trade in results["trades"][-5:]:
            pnl_emoji = "✅" if trade["pnl_pct"] > 0 else "❌"
            print(f"{pnl_emoji} {trade['side']} {trade['setup_type']} | {trade['pnl_pct']:+.2f}%")

    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Binance Futures Backtest Modulu")
    parser.add_argument("--symbol", type=str, help="Test edilecek sembol (örn: BTCUSDT)")
    parser.add_argument("--days", type=int, default=90, help="Kac gunluk veri (varsayilan: 90)")
    parser.add_argument("--interval", type=str, default="1h", choices=["15m", "1h", "4h"], help="Zaman dilimi")
    parser.add_argument("--confidence", type=int, default=78, help="Minimum confidence seviyesi")
    parser.add_argument("--output", type=str, help="Sonuclari kaydetmek icin dosya (JSON)")
    parser.add_argument("--all-symbols", action="store_true", help="Top Gainers uzerinde test et")
    parser.add_argument("--top", type=int, default=20, help="Top Gainers limit (sadece --all-symbols ile)")

    args = parser.parse_args()

    if args.all_symbols:
        # Top gainers'i cek
        print("📡 Top gainers sembolleri aliniyor...")
        tickers_url = f"{FUTURES_BASE}/fapi/v1/ticker/24hr"
        tickers = requests.get(tickers_url, timeout=30).json()

        candidates = []
        for row in tickers:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol.endswith("USDT"):
                continue
            if symbol[:-4].upper() in STABLECOIN_BASES:
                continue
            try:
                pct = float(row.get("priceChangePercent", 0))
                vol = float(row.get("quoteVolume", 0))
            except (TypeError, ValueError):
                continue
            if vol > 15_000_000 and pct > 0:
                candidates.append((symbol, pct, vol))

        candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
        symbols = [s[0] for s in candidates[:args.top]]
        print(f"✅ {len(symbols)} sembol secildi: {', '.join(symbols[:10])}...")
    elif args.symbol:
        symbols = [args.symbol]
    else:
        print("❌ --symbol veya --all-symbols belirtmelisiniz!")
        return

    all_results = {}

    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"🔍 Backtest: {symbol} | {args.days} gun | {args.interval}")
        print(f"{'='*60}")

        try:
            # Veri cek
            print("📥 Veri indiriliyor...")
            df = fetch_klines_historical(symbol, args.interval, args.days)

            if df.empty or len(df) < 250:
                print(f"⚠️  Yetersiz veri: {len(df)} bar")
                continue

            print(f"✅ {len(df)} bar indirildi")

            # Backtest yap
            print("🧪 Backtest yapiliyor...")
            trades = simulate_trades(symbol, df, args.interval, args.confidence)

            if not trades:
                print("⚠️  Hiç trade uretilmedi")
                continue

            # Sonuclari hesapla
            results = compute_backtest_stats(trades)
            print_backtest_results(results)

            all_results[symbol] = results

            # Rate limiting
            time.sleep(1)

        except Exception as e:
            print(f"❌ Hata: {e}")
            import traceback
            traceback.print_exc()

    # Tum sonuclari kaydet
    if all_results and args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Sonuclar kaydedildi: {args.output}")

    # Ozet
    if all_results:
        print("\n" + "=" * 60)
        print("📋 GENEL OZET")
        print("=" * 60)

        for symbol, results in all_results.items():
            if "error" not in results:
                emoji = "✅" if results["win_rate"] > 50 else "⚠️"
                print(f"{emoji} {symbol}: {results['win_rate']}% WR | {results['total_pnl_pct']:+.2f}% | {results['total_trades']} trades")


if __name__ == "__main__":
    main()
