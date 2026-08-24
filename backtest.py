#!/usr/bin/env python3
"""
backtest.py — Çift Motorlu Hibrit Geçmiş Veri Test Motoru
Binance USDT-M Vadeli İşlemler geçmiş verilerini çekerek hem 'Dip Avcısı' hem de 'Pump Sniper'
stratejilerini tek tek veya hibrit olarak test eder.
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
import pandas as pd
import requests

BASE = "https://fapi.binance.com"
POSITION_USD = 300.0

def calc_rsi(series, period=14):
    if len(series) < period + 1:
        return 50.0
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if not math.isnan(val) else 50.0

def fetch_klines_historical(symbol, interval="15m", days=30):
    limit = 1000
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)
    all_data = []
    curr_start = start_ms
    
    print(f"📥 [{symbol}] Son {days} günlük {interval} verisi indiriliyor...")
    while curr_start < now_ms:
        url = f"{BASE}/fapi/v1/klines"
        params = {"symbol": symbol, "interval": interval, "startTime": curr_start, "limit": limit}
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code != 200: break
            data = r.json()
            if not data or not isinstance(data, list): break
            all_data.extend(data)
            last_open_time = data[-1][0]
            if len(data) < limit or last_open_time <= curr_start: break
            curr_start = last_open_time + 1
            time.sleep(0.05)
        except Exception:
            break
            
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v","qv"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df['open_time'] = pd.to_datetime(df['ot'], unit='ms', utc=True)
    return df.drop_duplicates(subset=['ot']).sort_values('ot').reset_index(drop=True)

def run_dual_engine_backtest(symbol, df, mode="dual", verbose=False):
    if len(df) < 150: return []
    trades = []
    in_pos = False
    pos = {}
    
    TP1_PCT = 0.012
    BE_SL_PCT = 0.002
    TS_ACTIVATION = 0.025
    TS_DROP = 0.010
    MAX_HOLD_BARS = 48
    
    for i in range(96, len(df)):
        c = df.iloc[i]
        if in_pos:
            high_p = c["h"]
            low_p = c["l"]
            entry = pos["entry"]
            dur = i - pos["bar"]
            if high_p > pos["highest_p"]: pos["highest_p"] = high_p
            highest = pos["highest_p"]
            
            # TP1 (%1.2 kârda %50 satış + Breakeven)
            if not pos["tp1"] and high_p >= entry * (1 + TP1_PCT):
                pos["tp1"] = True
                pos["realized"] = (POSITION_USD * 0.5) * TP1_PCT
                pos["sl"] = entry * (1 + BE_SL_PCT)
                pos["be_hit"] = True
                
            exit_p = None
            reason = None
            if low_p <= pos["sl"]:
                exit_p = pos["sl"]
                reason = "BE" if pos["be_hit"] else "SL"
            elif highest >= entry * (1 + TS_ACTIVATION):
                ts_price = highest * (1 - TS_DROP)
                if low_p <= ts_price:
                    exit_p = max(ts_price, low_p)
                    reason = "TRAILING_STOP"
            elif dur >= MAX_HOLD_BARS:
                exit_p = c["c"]
                reason = "TIMEOUT"
                
            if reason:
                rem_pnl = (150.0 if pos["tp1"] else POSITION_USD) * ((exit_p - entry) / entry)
                total_pnl = pos.get("realized", 0.0) + rem_pnl
                pct = (total_pnl / POSITION_USD) * 100
                trades.append({
                    "symbol": symbol, "entry_time": pos["entry_time"].strftime("%Y-%m-%d %H:%M"),
                    "exit_time": c["open_time"].strftime("%Y-%m-%d %H:%M"),
                    "entry_price": entry, "exit_price": exit_p, "pnl": total_pnl, "pct": pct,
                    "reason": reason, "mode": pos["mode"]
                })
                if verbose:
                    icon = "🟢" if total_pnl > 0 else "🔴"
                    print(f"  {icon} [{pos['mode']}] {symbol} | Giriş: {entry:.4f} → Çıkış: {exit_p:.4f} "
                          f"| PnL: ${total_pnl:+.2f} (%{pct:+.2f}) | {reason}")
                in_pos = False
                pos = {}
            continue
            
        c_c, o_c, h_c, l_c = c["c"], c["o"], c["h"], c["l"]
        delta = df["c"].iloc[i-20:i+1].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])
        
        # 1. MOTOR: DİP AVCISI
        dip_sig = False
        if mode in ["dual", "dip"]:
            if rsi <= 25.0:
                sma20 = df["c"].iloc[i-20:i].mean()
                std20 = df["c"].iloc[i-20:i].std()
                lower_bb = sma20 - (2.0 * std20)
                if (l_c <= lower_bb or c_c <= lower_bb):
                    if (c_c >= o_c or ((c_c - l_c) / (h_c - l_c) if (h_c - l_c) > 0 else 0) > 0.4):
                        dip_sig = True
                        
        # 2. MOTOR: PUMP SNIPER
        pump_sig = False
        if mode in ["dual", "pump"] and not dip_sig:
            if 52.0 <= rsi <= 75.0:
                window_24h = df.iloc[i-96:i]
                max_h24 = window_24h["h"].max()
                min_l24 = window_24h["l"].min()
                if ((max_h24 - min_l24) / min_l24 * 100) <= 7.0:
                    dist = (c_c - max_h24) / max_h24 * 100
                    if 0.1 <= dist <= 2.5:
                        rng = h_c - l_c
                        if rng > 0 and (c_c - o_c) / rng >= 0.45:
                            vol_avg = df["v"].iloc[i-20:i-2].mean()
                            vol_now = c["v"] + df["v"].iloc[i-1]
                            if vol_avg > 0 and (vol_now / vol_avg) >= 3.5:
                                pump_sig = True
                                
        if dip_sig:
            in_pos = True
            pos = {"entry": c_c, "entry_time": c["open_time"], "bar": i, "sl": c_c * 0.97, "highest_p": c_c, "tp1": False, "be_hit": False, "realized": 0.0, "mode": "DİP_AVCISI"}
        elif pump_sig:
            in_pos = True
            pos = {"entry": c_c, "entry_time": c["open_time"], "bar": i, "sl": c_c * 0.975, "highest_p": c_c, "tp1": False, "be_hit": False, "realized": 0.0, "mode": "PUMP_SNIPER"}
            
    return trades

def print_performance_report(all_trades, days, title="Çift Motorlu Hibrit"):
    print("\n" + "="*70)
    print(f"📊 BACKTEST PERFORMANS RAPORU — {title} (Son {days} Gün)")
    print("="*70)
    total = len(all_trades)
    if total == 0:
        print("❌ Kriterlere uyan işlem bulunamadı.")
        print("="*70)
        return
        
    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] < 0]
    win_rate = (len(wins) / total) * 100
    total_pnl = sum(t["pnl"] for t in all_trades)
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0)
    
    avg_win = (gross_profit / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0
    avg_win_pct = (sum(t["pct"] for t in wins) / len(wins)) if wins else 0
    avg_loss_pct = (sum(t["pct"] for t in losses) / len(losses)) if losses else 0
    
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in all_trades:
        cum_pnl += t["pnl"]
        if cum_pnl > peak: peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd: max_dd = dd
        
    dip_trades = [t for t in all_trades if t.get("mode") == "DİP_AVCISI"]
    pump_trades = [t for t in all_trades if t.get("mode") == "PUMP_SNIPER"]
    
    print(f"📌 Toplam İşlem Sayısı       : {total}")
    print(f"   • Dip Avcısı İşlemleri    : {len(dip_trades)} adet")
    print(f"   • Pump Sniper İşlemleri   : {len(pump_trades)} adet")
    print(f"🎯 Kazanma Oranı (Win Rate)  : %{win_rate:.1f} ({len(wins)} Kazanç / {len(losses)} Kayıp)")
    print(f"💰 Toplam Net Kâr/Zarar      : ${total_pnl:+.2f} (Pozisyon Başı: ${POSITION_USD})")
    print(f"⚖️ Kâr Faktörü (Profit Factor): {profit_factor:.2f}")
    print(f"📉 Maksimum Drawdown ($)     : -${max_dd:.2f}")
    print("-" * 70)
    print(f"🟢 Ortalama Kazanan İşlem    : +${avg_win:.2f} (+%{avg_win_pct:.2f})")
    print(f"🔴 Ortalama Kaybeden İşlem   : -${avg_loss:.2f} (-%{avg_loss_pct:.2f})")
    print("="*70)

def main():
    parser = argparse.ArgumentParser(description="Dual-Engine Crypto Backtester")
    parser.add_argument("--strategy", type=str, default="dual", choices=["dual", "dip", "pump"], help="dual, dip veya pump")
    parser.add_argument("--days", type=int, default=30, help="Geriye dönük gün sayısı")
    parser.add_argument("--top", type=int, default=10, help="Sembol adedi")
    parser.add_argument("--verbose", action="store_true", help="Detaylı log")
    args = parser.parse_args()

    symbols = ["DOGEUSDT", "1000PEPEUSDT", "NEARUSDT", "ARBUSDT", "SUIUSDT", "AVAXUSDT", "FETUSDT", "RENDERUSDT", "SOLUSDT", "INJUSDT"][:args.top]
    print(f"🚀 Toplam {len(symbols)} adet sembol üzerinde {args.days} günlük '{args.strategy.upper()}' testi başlatılıyor...")
    
    all_trades = []
    for sym in symbols:
        df = fetch_klines_historical(sym, interval="15m", days=args.days)
        if not df.empty:
            trades = run_dual_engine_backtest(sym, df, mode=args.strategy, verbose=args.verbose)
            all_trades.extend(trades)

    all_trades.sort(key=lambda x: x["entry_time"])
    print_performance_report(all_trades, args.days, title=f"Çift Motorlu ({args.strategy.upper()})")

if __name__ == "__main__":
    main()
