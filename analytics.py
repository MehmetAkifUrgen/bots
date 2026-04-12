"""
analytics.py — Kapsamli ticaret analizi ve drawdown takibi
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Optional


def load_trade_history(path: str) -> pd.DataFrame:
    """Gecmis islemleri yukle."""
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame()
    
    try:
        df = pd.read_csv(path)
        df["opened_at"] = pd.to_datetime(df["opened_at"], errors="coerce")
        df["closed_at"] = pd.to_datetime(df["closed_at"], errors="coerce")
        df["pnl_net"] = pd.to_numeric(df["pnl_net"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


def calculate_max_drawdown(equity_curve: pd.Series) -> tuple[float, float, int]:
    """
    Maksimum drawdown hesapla.
    Returns: (max_dd_pct, dd_duration_bars, peak_index)
    """
    if len(equity_curve) < 2:
        return (0.0, 0, 0)
    
    peak = equity_curve.expanding(min_periods=1).max()
    drawdown = (equity_curve - peak) / peak * 100
    max_dd = drawdown.min()
    max_dd_idx = drawdown.idxmin()
    
    # Drawdown suresini hesapla
    peak_idx = equity_curve[:max_dd_idx].idxmax() if max_dd_idx > 0 else 0
    duration = max_dd_idx - peak_idx
    
    return (max_dd, duration, peak_idx)


def calculate_sharpe_ratio(pnl_series: pd.Series, risk_free_rate: float = 0.0, periods_per_year: int = 252) -> float:
    """Sharpe ratio hesapla (annualized)."""
    if len(pnl_series) < 2 or pnl_series.std() == 0:
        return 0.0
    
    excess_returns = pnl_series - risk_free_rate
    sharpe = excess_returns.mean() / excess_returns.std() * np.sqrt(periods_per_year)
    return float(sharpe)


def calculate_sortino_ratio(pnl_series: pd.Series, risk_free_rate: float = 0.0, periods_per_year: int = 252) -> float:
    """Sortino ratio hesapla (sadece downside volatilite)."""
    if len(pnl_series) < 2:
        return 0.0
    
    excess_returns = pnl_series - risk_free_rate
    downside_returns = excess_returns[excess_returns < 0]
    
    if len(downside_returns) == 0 or downside_returns.std() == 0:
        return float('inf')
    
    sortino = excess_returns.mean() / downside_returns.std() * np.sqrt(periods_per_year)
    return float(sortino)


def calculate_calmar_ratio(total_return: float, max_drawdown: float) -> float:
    """Calmar ratio hesapla (return / max_drawdown)."""
    if max_drawdown == 0:
        return float('inf') if total_return > 0 else 0.0
    return total_return / abs(max_drawdown)


def analyze_strategy_performance(trade_history: pd.DataFrame) -> dict:
    """Strateji bazinda performans analizi."""
    if trade_history.empty or "strategy_key" not in trade_history.columns:
        return {}
    
    strategy_stats = {}
    
    for strategy_key, group in trade_history.groupby("strategy_key"):
        closed = group.dropna(subset=["closed_at", "pnl_net"])
        if closed.empty:
            continue
        
        pnl = closed["pnl_net"]
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        
        # Calculate various metrics
        win_rate = len(wins) / len(closed) if len(closed) > 0 else 0
        avg_win = wins.mean() if len(wins) > 0 else 0
        avg_loss = losses.mean() if len(losses) > 0 else 0
        
        gross_profit = wins.sum() if len(wins) > 0 else 0
        gross_loss = abs(losses.sum()) if len(losses) > 0 else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Consecutive wins/losses
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        current_wins = 0
        current_losses = 0
        
        for _, trade in closed.sort_values("closed_at").iterrows():
            if trade["pnl_net"] > 0:
                current_wins += 1
                current_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
        
        strategy_stats[str(strategy_key)] = {
            "total_trades": len(closed),
            "win_rate": round(win_rate * 100, 2),
            "avg_pnl": round(pnl.mean(), 2),
            "total_pnl": round(pnl.sum(), 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
            "avg_hold_time_hours": round(closed["closed_at"].sub(closed["opened_at"]).dt.total_seconds().mean() / 3600, 2) if "closed_at" in closed.columns and "opened_at" in closed.columns else 0,
        }
    
    return strategy_stats


def analyze_symbol_performance(trade_history: pd.DataFrame) -> dict:
    """Sembol bazinda performans analizi."""
    if trade_history.empty or "symbol" not in trade_history.columns:
        return {}
    
    symbol_stats = {}
    
    for symbol, group in trade_history.groupby("symbol"):
        closed = group.dropna(subset=["closed_at", "pnl_net"])
        if closed.empty:
            continue
        
        pnl = closed["pnl_net"]
        
        symbol_stats[symbol] = {
            "total_trades": len(closed),
            "win_rate": round((pnl > 0).mean() * 100, 2),
            "avg_pnl": round(pnl.mean(), 2),
            "total_pnl": round(pnl.sum(), 2),
            "best_trade": round(pnl.max(), 2),
            "worst_trade": round(pnl.min(), 2),
        }
    
    return symbol_stats


def analyze_time_performance(trade_history: pd.DataFrame) -> dict:
    """Zaman bazinda performans analizi (saat, gun)."""
    if trade_history.empty or "closed_at" not in trade_history.columns:
        return {}
    
    closed = trade_history.dropna(subset=["closed_at", "pnl_net"])
    if closed.empty:
        return {}
    
    # UTC saat'e gore analiz
    closed_copy = closed.copy()
    closed_copy["hour"] = closed_copy["closed_at"].dt.hour
    closed_copy["day_of_week"] = closed_copy["closed_at"].dt.dayofweek
    
    hourly_performance = {}
    for hour in range(24):
        hour_trades = closed_copy[closed_copy["hour"] == hour]
        if len(hour_trades) > 0:
            hourly_performance[str(hour)] = {
                "trades": len(hour_trades),
                "win_rate": round((hour_trades["pnl_net"] > 0).mean() * 100, 2),
                "avg_pnl": round(hour_trades["pnl_net"].mean(), 2),
            }
    
    daily_performance = {}
    days = ["Pazartesi", "Sali", "Carsamba", "Persembe", "Cuma", "Cumartesi", "Pazar"]
    for day_idx in range(7):
        day_trades = closed_copy[closed_copy["day_of_week"] == day_idx]
        if len(day_trades) > 0:
            daily_performance[days[day_idx]] = {
                "trades": len(day_trades),
                "win_rate": round((day_trades["pnl_net"] > 0).mean() * 100, 2),
                "avg_pnl": round(day_trades["pnl_net"].mean(), 2),
            }
    
    return {
        "hourly": hourly_performance,
        "daily": daily_performance,
    }


def generate_equity_curve(trade_history: pd.DataFrame, starting_balance: float = 1000) -> pd.Series:
    """Equity curve olustur."""
    if trade_history.empty or "closed_at" not in trade_history.columns:
        return pd.Series(dtype=float)
    
    closed = trade_history.dropna(subset=["closed_at", "pnl_net"]).sort_values("closed_at")
    if closed.empty:
        return pd.Series(dtype=float)
    
    equity = [starting_balance]
    for _, trade in closed.iterrows():
        equity.append(equity[-1] + trade["pnl_net"])
    
    return pd.Series(equity)


def generate_comprehensive_report(
    trade_history_path: str,
    starting_balance: float = 1000,
) -> dict:
    """Kapsamli analiz raporu olustur."""
    trade_history = load_trade_history(trade_history_path)
    
    if trade_history.empty:
        return {"error": "No trade history found"}
    
    # Equity curve
    equity_curve = generate_equity_curve(trade_history, starting_balance)
    
    # Max drawdown
    max_dd, dd_duration, _ = calculate_max_drawdown(equity_curve)
    
    # PnL series
    pnl_series = trade_history["pnl_net"].dropna()
    
    # Core metrics
    total_return = equity_curve.iloc[-1] - equity_curve.iloc[0] if len(equity_curve) > 1 else 0
    total_return_pct = (total_return / starting_balance * 100) if starting_balance > 0 else 0
    
    report = {
        "overview": {
            "starting_balance": starting_balance,
            "current_balance": round(equity_curve.iloc[-1], 2) if len(equity_curve) > 0 else starting_balance,
            "total_return_usd": round(total_return, 2),
            "total_return_pct": round(total_return_pct, 2),
            "total_trades": len(trade_history.dropna(subset=["closed_at"])),
            "win_rate": round((pnl_series > 0).mean() * 100, 2) if len(pnl_series) > 0 else 0,
        },
        "risk_metrics": {
            "max_drawdown_pct": round(max_dd, 2),
            "max_drawdown_duration_bars": int(dd_duration),
            "sharpe_ratio": round(calculate_sharpe_ratio(pnl_series), 2),
            "sortino_ratio": round(calculate_sortino_ratio(pnl_series), 2),
            "calmar_ratio": round(calculate_calmar_ratio(total_return, max_dd), 2),
            "avg_trade_pnl": round(pnl_series.mean(), 2) if len(pnl_series) > 0 else 0,
            "std_trade_pnl": round(pnl_series.std(), 2) if len(pnl_series) > 0 else 0,
        },
        "strategy_breakdown": analyze_strategy_performance(trade_history),
        "symbol_performance": analyze_symbol_performance(trade_history),
        "time_performance": analyze_time_performance(trade_history),
    }
    
    return report


def print_analytics_report(report: dict) -> None:
    """Analiz raporunu guzel bir formatta yazdir."""
    if "error" in report:
        print(f"\n❌ {report['error']}")
        return
    
    print("\n" + "=" * 70)
    print("📊 KAPSAMLI TICARET ANALIZI")
    print("=" * 70)
    
    overview = report["overview"]
    print(f"\n💰 Baslangic: {overview['starting_balance']:.2f} USD")
    print(f"💵 Guncel Bakiye: {overview['current_balance']:.2f} USD")
    print(f"📈 Toplam Getiri: {overview['total_return_usd']:+.2f} USD ({overview['total_return_pct']:+.2f}%)")
    print(f"📊 Toplam Islem: {overview['total_trades']}")
    print(f"✅ Win Rate: {overview['win_rate']}%")
    
    risk = report["risk_metrics"]
    print(f"\n⚠️  Risk Metrikleri:")
    print(f"  Max Drawdown: {risk['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe Ratio: {risk['sharpe_ratio']:.2f}")
    print(f"  Sortino Ratio: {risk['sortino_ratio']:.2f}")
    print(f"  Calmar Ratio: {risk['calmar_ratio']:.2f}")
    print(f"  Ort Islem PnL: {risk['avg_trade_pnl']:+.2f} USD")
    print(f"  Std Islem PnL: {risk['std_trade_pnl']:.2f} USD")
    
    if "strategy_breakdown" in report and report["strategy_breakdown"]:
        print(f"\n🎯 Strateji Breakdown:")
        print("-" * 70)
        for strategy_key, stats in report["strategy_breakdown"].items():
            emoji = "🟢" if stats["win_rate"] > 50 else "🔴"
            print(f"\n{emoji} {strategy_key}")
            print(f"  Islem: {stats['total_trades']} | WR: {stats['win_rate']}%")
            print(f"  Toplam PnL: {stats['total_pnl']:+.2f} USD | Profit Factor: {stats['profit_factor']:.2f}")
    
    if "symbol_performance" in report and report["symbol_performance"]:
        print(f"\n💎 En Iyi Semboller:")
        print("-" * 70)
        sorted_symbols = sorted(
            report["symbol_performance"].items(),
            key=lambda x: x[1]["total_pnl"],
            reverse=True,
        )
        for symbol, stats in sorted_symbols[:5]:
            emoji = "✅" if stats["total_pnl"] > 0 else "❌"
            print(f"{emoji} {symbol}: {stats['total_pnl']:+.2f} USD ({stats['win_rate']}% WR, {stats['total_trades']} trades)")
    
    if "time_performance" in report and report["time_performance"]:
        if "daily" in report["time_performance"]:
            print(f"\n📅 Gunluk Performans:")
            print("-" * 70)
            for day, stats in report["time_performance"]["daily"].items():
                emoji = "🟢" if stats["win_rate"] > 50 else "🔴"
                print(f"{emoji} {day}: {stats['win_rate']}% WR | {stats['avg_pnl']:+.2f} USD | {stats['trades']} trades")
    
    print("\n" + "=" * 70)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Ticaret Analiz Modulu")
    parser.add_argument("--file", type=str, default="paper_trades.csv", help="Ticaret dosyasi")
    parser.add_argument("--balance", type=float, default=1000, help="Baslangic bakiyesi")
    parser.add_argument("--output", type=str, help="JSON output dosyasi")
    
    args = parser.parse_args()
    
    report = generate_comprehensive_report(args.file, args.balance)
    print_analytics_report(report)
    
    if args.output:
        import json
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Rapor kaydedildi: {args.output}")
