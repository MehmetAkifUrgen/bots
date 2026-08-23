#!/usr/bin/env python3
"""
backtest.py — Çoklu Strateji Destekli Geçmiş Veri Test Motoru (Backtester)
Binance USDT-M Vadeli İşlemler geçmiş verilerini çekerek hem 'Dip Avcısı' (Mean Reversion)
hem de 'Kırılım' (Breakout) stratejilerinin performansını ve kârlılığını test eder.
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
    """Binance API'den geçmiş mum verilerini sayfalı olarak indirir."""
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
            time.sleep(0.06)
        except Exception:
            break
            
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
    for col in ["o","h","l","c","v","qv"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df['open_time'] = pd.to_datetime(df['ot'], unit='ms', utc=True)
    return df.drop_duplicates(subset=['ot']).sort_values('ot').reset_index(drop=True)

# ── 1. STRATEJİ: DİP AVCISI & MEAN REVERSION (YÜKSEK WIN-RATE) ───────────────

def run_sniper_backtest(symbol, df, verbose=False):
    if len(df) < 100: return []
    trades = []
    in_pos = False
    pos = {}
    
    TP1_PCT = 0.012       # %1.2 kârda %50 pozisyon nakde çevrilir
    BE_SL_PCT = 0.002     # Stop maliyet + %0.2'ye taşınır
    TS_ACTIVATION = 0.025 # %2.5 kârda izleyen stop
    TS_DROP = 0.010       # Zirveden %1.0 çekilirse sat
    HARD_SL_PCT = 0.03    # %3 Stop Loss
    MAX_HOLD_BARS = 48    # 12 Saat
    
    for i in range(30, len(df)):
        c_candle = df.iloc[i]
        if in_pos:
            high_price = c_candle["h"]
            low_price = c_candle["l"]
            entry = pos["entry_price"]
            dur_bars = i - pos["entry_bar"]
            
            if high_price > pos["highest_price"]:
                pos["highest_price"] = high_price
            highest = pos["highest_price"]
            
            # TP1 Kademeli Kâr Alımı (%1.2)
            if not pos["tp1_hit"] and high_price >= entry * (1 + TP1_PCT):
                pos["tp1_hit"] = True
                pos["realized_pnl"] = (POSITION_USD * 0.5) * TP1_PCT
                pos["remaining_size"] = POSITION_USD * 0.5
                pos["sl"] = entry * (1 + BE_SL_PCT)
                pos["be_hit"] = True
                
            exit_price = None
            reason = None
            
            if low_price <= pos["sl"]:
                exit_price = pos["sl"]
                reason = "BE" if pos["be_hit"] else "SL"
            elif highest >= entry * (1 + TS_ACTIVATION):
                ts_price = highest * (1 - TS_DROP)
                if low_price <= ts_price:
                    exit_price = max(ts_price, low_price)
                    reason = "TRAILING_STOP"
            elif dur_bars >= MAX_HOLD_BARS:
                exit_price = c_candle["c"]
                reason = "TIMEOUT"
                
            if reason:
                rem_size = pos.get("remaining_size", POSITION_USD)
                rem_pnl = rem_size * ((exit_price - entry) / entry)
                total_pnl = pos.get("realized_pnl", 0.0) + rem_pnl
                pct = (total_pnl / POSITION_USD) * 100
                
                trade = {
                    "symbol": symbol, "entry_time": pos["entry_time"].strftime("%Y-%m-%d %H:%M"),
                    "exit_time": c_candle["open_time"].strftime("%Y-%m-%d %H:%M"),
                    "entry_price": entry, "exit_price": exit_price,
                    "highest_price": highest, "pct": pct, "pnl": total_pnl,
                    "reason": reason, "tp1_hit": pos.get("tp1_hit", False),
                    "dur_minutes": dur_bars * 15
                }
                trades.append(trade)
                if verbose:
                    icon = "🟢" if total_pnl > 0 else "🔴"
                    print(f"  {icon} [{reason}] {symbol} | Giriş: {entry:.4f} → Çıkış: {exit_price:.4f} "
                          f"| Net PnL: ${total_pnl:+.2f} (%{pct:+.2f}) | TP1:{'✅' if pos.get('tp1_hit') else '❌'}")
                in_pos = False
                pos = {}
            continue
            
        # Sinyal Tespiti: RSI < 25 + Bollinger Alt Bandı Dışı
        rsi_val = calc_rsi(df["c"].iloc[i-20:i+1], 14)
        if rsi_val > 25.0: continue
        
        sma20 = df["c"].iloc[i-20:i].mean()
        std20 = df["c"].iloc[i-20:i].std()
        lower_bb = sma20 - (2.0 * std20)
        
        c = c_candle["c"]
        o = c_candle["o"]
        l = c_candle["l"]
        
        if l > lower_bb and c > lower_bb: continue
        if c < o and ((c - l) / (c_candle["h"] - l) if (c_candle["h"] - l) > 0 else 0) < 0.4: continue
        
        entry_price = c
        in_pos = True
        pos = {
            "symbol": symbol, "entry_time": c_candle["open_time"],
            "entry_price": entry_price, "entry_bar": i,
            "sl": entry_price * (1 - HARD_SL_PCT),
            "highest_price": entry_price,
            "be_hit": False, "tp1_hit": False,
            "realized_pnl": 0.0, "remaining_size": POSITION_USD
        }
    return trades

def print_performance_report(all_trades, days, strategy_name="Dip Avcısı (Mean Reversion)"):
    print("\n" + "="*70)
    print(f"📊 BACKTEST PERFORMANS RAPORU — {strategy_name} (Son {days} Gün)")
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
        
    reason_counts = {}
    for t in all_trades:
        r = t["reason"]
        reason_counts[r] = reason_counts.get(r, 0) + 1
        
    print(f"📌 Toplam İşlem Sayısı       : {total}")
    print(f"🎯 Kazanma Oranı (Win Rate)  : %{win_rate:.1f} ({len(wins)} Kazanç / {len(losses)} Kayıp)")
    print(f"💰 Toplam Net Kâr/Zarar      : ${total_pnl:+.2f} (Pozisyon Başı: ${POSITION_USD})")
    print(f"⚖️ Kâr Faktörü (Profit Factor): {profit_factor:.2f}")
    print(f"📉 Maksimum Drawdown ($)     : -${max_dd:.2f}")
    print("-" * 70)
    print(f"🟢 Ortalama Kazanan İşlem    : +${avg_win:.2f} (+%{avg_win_pct:.2f})")
    print(f"🔴 Ortalama Kaybeden İşlem   : -${avg_loss:.2f} (-%{avg_loss_pct:.2f})")
    print(f"🎲 Risk/Ödül Oranı (R:R)     : {(avg_win / avg_loss):.2f}" if avg_loss > 0 else "🎲 Risk/Ödül: N/A")
    print("-" * 70)
    print("📋 Çıkış Türleri Dağılımı:")
    for reason, count in reason_counts.items():
        pct_share = (count / total) * 100
        print(f"   • {reason:<15}: {count} adet (%{pct_share:.1f})")
    print("="*70)

def main():
    parser = argparse.ArgumentParser(description="Multi-Strategy Crypto Backtester")
    parser.add_argument("--symbols", type=str, default="", help="Virgülle ayrılmış semboller")
    parser.add_argument("--days", type=int, default=30, help="Geriye dönük gün sayısı")
    parser.add_argument("--top", type=int, default=10, help="Taranacak sembol adedi")
    parser.add_argument("--verbose", action="store_true", help="Detaylı log")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = ["DOGEUSDT", "1000PEPEUSDT", "NEARUSDT", "ARBUSDT", "SUIUSDT", "AVAXUSDT", "FETUSDT", "RENDERUSDT", "SOLUSDT", "INJUSDT"][:args.top]

    print(f"🚀 Toplam {len(symbols)} adet sembol üzerinde {args.days} günlük Dip Avcısı testi başlatılıyor...")
    all_trades = []
    for sym in symbols:
        df = fetch_klines_historical(sym, interval="15m", days=args.days)
        if not df.empty:
            trades = run_sniper_backtest(sym, df, verbose=args.verbose)
            all_trades.extend(trades)

    all_trades.sort(key=lambda x: x["entry_time"])
    print_performance_report(all_trades, args.days, strategy_name="Dip Avcısı & Mean Reversion")

if __name__ == "__main__":
    main()
