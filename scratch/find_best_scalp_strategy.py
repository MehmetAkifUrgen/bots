import os, time, math
import pandas as pd
import numpy as np
import trader_bot

PAIRS = ["SOLUSDT", "DOGEUSDT", "BTCUSDT", "ETHUSDT", "NEARUSDT", "SUIUSDT", "PEPEUSDT", "AVAXUSDT", "ENAUSDT", "LINKUSDT"]

def fetch_data(sym, interval="3m", limit=600):
    try:
        return trader_bot.klines(sym, interval, limit)
    except Exception as e:
        print(f"Error {sym}: {e}")
        return None

POSITION_NOTIONAL = 220.0
SL_USD = 1.50          # Max -$1.50
TRAILING_START = 2.00  # Start trailing at +$2.00
TRAILING_DIST = 0.70   # Trail $0.70 behind peak
COMMISSION = 0.0015    # 0.15% roundtrip

def run_backtest_strategy(strategy_func, all_dfs):
    total_trades = 0
    wins = 0
    losses = 0
    total_pnl = 0.0
    durations = []
    
    for sym, df in all_dfs.items():
        if df is None or len(df) < 150: continue
        
        in_pos = False
        entry_p = 0
        highest_p = 0
        pos_open_bar = 0
        
        # Prepare strategy indicators
        ind_df = strategy_func(df)
        
        for i in range(80, len(df) - 1):
            curr_c = df['c'].iloc[i]
            curr_h = df['h'].iloc[i]
            curr_l = df['l'].iloc[i]
            
            if in_pos:
                dur = i - pos_open_bar
                if curr_h > highest_p:
                    highest_p = curr_h
                
                pnl_high = (highest_p - entry_p) / entry_p * POSITION_NOTIONAL
                pnl_low = (curr_l - entry_p) / entry_p * POSITION_NOTIONAL
                
                sl_price = entry_p * (1.0 - (SL_USD / POSITION_NOTIONAL))
                
                # Check trailing stop if reached trigger
                if pnl_high >= TRAILING_START:
                    trail_sl = highest_p - (TRAILING_DIST / POSITION_NOTIONAL * entry_p)
                    if curr_l <= trail_sl:
                        exit_p = trail_sl
                        fee = POSITION_NOTIONAL * COMMISSION
                        pnl = ((exit_p - entry_p) / entry_p * POSITION_NOTIONAL) - fee
                        total_trades += 1
                        total_pnl += pnl
                        if pnl > 0: wins += 1
                        else: losses += 1
                        durations.append(dur)
                        in_pos = False
                        continue
                
                # Check hard SL
                if curr_l <= sl_price:
                    exit_p = sl_price
                    fee = POSITION_NOTIONAL * COMMISSION
                    pnl = ((exit_p - entry_p) / entry_p * POSITION_NOTIONAL) - fee
                    total_trades += 1
                    total_pnl += pnl
                    losses += 1
                    durations.append(dur)
                    in_pos = False
                    continue
                    
                # Time stop: 40 bars (120 mins)
                if dur >= 40:
                    exit_p = curr_c
                    fee = POSITION_NOTIONAL * COMMISSION
                    pnl = ((exit_p - entry_p) / entry_p * POSITION_NOTIONAL) - fee
                    total_trades += 1
                    total_pnl += pnl
                    if pnl > 0: wins += 1
                    else: losses += 1
                    durations.append(dur)
                    in_pos = False
                    continue
            else:
                # Check if signal triggered
                sig = ind_df['signal'].iloc[i]
                if sig == 1:
                    in_pos = True
                    entry_p = curr_c
                    highest_p = curr_c
                    pos_open_bar = i
                    
    wr = (wins / total_trades * 100) if total_trades > 0 else 0
    avg_dur = (sum(durations) / len(durations) * 3) if durations else 0
    return {
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "pnl": total_pnl,
        "avg_duration_min": avg_dur
    }

# STRATEGY 1: EMA Ribbon + StochRSI Pullback Rebound
def strat_ema_pullback(df):
    d = df.copy()
    c = d['c']
    d['ema9'] = c.ewm(span=9, adjust=False).mean()
    d['ema21'] = c.ewm(span=21, adjust=False).mean()
    d['ema50'] = c.ewm(span=50, adjust=False).mean()
    d['vol_ma'] = d['v'].rolling(20).mean()
    d['taker_ratio'] = d['tb'] / d['v'].replace(0, 1e-9)
    
    # Stoch RSI
    period = 14
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    ll = rsi.rolling(period).min()
    hh = rsi.rolling(period).max()
    stoch_k = ((rsi - ll) / (hh - ll).replace(0, 1e-9)) * 100
    d['stoch_k'] = stoch_k
    d['stoch_d'] = stoch_k.rolling(3).mean()
    
    # Conditions:
    # 1. Uptrend: EMA9 > EMA21 and EMA21 > EMA50
    trend = (d['ema9'] > d['ema21']) & (d['ema21'] > d['ema50'])
    # 2. Pullback: Low touched or came within 0.15% of EMA21 or EMA9
    pullback = (d['l'] <= d['ema9'] * 1.001) & (d['c'] >= d['ema21'] * 0.998)
    # 3. StochRSI oversold hook (< 40 and crossing up)
    stoch_hook = (d['stoch_k'] < 45) & (d['stoch_k'] > d['stoch_d']) & (d['stoch_k'].shift(1) <= d['stoch_d'].shift(1))
    # 4. Volume / Taker
    vol_ok = (d['v'] >= d['vol_ma'] * 0.9) & (d['taker_ratio'] > 0.48)
    # 5. Bullish candle
    green = (d['c'] > d['o']) | ((d['c'] - d['l']) > (d['h'] - d['c']))
    
    d['signal'] = np.where(trend & pullback & stoch_hook & vol_ok & green, 1, 0)
    return d

# STRATEGY 2: Lorentzian Machine Learning Classifier
def strat_lorentzian(df):
    d = df.copy()
    c = d['c']
    d['ema21'] = c.ewm(span=21, adjust=False).mean()
    d['ema50'] = c.ewm(span=50, adjust=False).mean()
    d['vol_ma'] = d['v'].rolling(20).mean()
    d['taker_ratio'] = d['tb'] / d['v'].replace(0, 1e-9)
    
    # Features
    # f1: RSI
    period = 14
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    f1 = (rsi - 50.0) / 25.0
    
    # f2: WT Diff
    ap = (d['h'] + d['l'] + c) / 3.0
    esa = ap.ewm(span=10, adjust=False).mean()
    diff = (ap - esa).abs().ewm(span=10, adjust=False).mean()
    ci = (ap - esa) / (0.015 * diff.replace(0, 1e-9))
    wt1 = ci.ewm(span=11, adjust=False).mean()
    wt2 = wt1.rolling(4).mean()
    f2 = (wt1 - wt2) / 20.0
    
    # f3: CCI
    tp = (d['h'] + d['l'] + c) / 3.0
    sma = tp.rolling(20).mean()
    mad = (tp - sma).abs().rolling(20).mean()
    f3 = ((tp - sma) / (0.015 * mad.replace(0, 1e-9))) / 100.0
    
    # f4: ATR normalized price deviation
    tr = pd.concat([d['h'] - d['l'], (d['h'] - c.shift(1)).abs(), (d['l'] - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().replace(0, 1e-9)
    f4 = (c - d['ema21']) / atr
    
    feats = np.column_stack([f1.values, f2.values, f3.values, f4.values])
    
    sig = np.zeros(len(d))
    n = len(d)
    
    for i in range(80, n):
        # Baseline trend & volume
        if c.iloc[i] < d['ema50'].iloc[i] or d['taker_ratio'].iloc[i] < 0.50 or d['v'].iloc[i] < d['vol_ma'].iloc[i] * 0.9:
            continue
        
        curr = feats[i]
        if np.isnan(curr).any(): continue
        
        # Historical window
        hist_feats = feats[max(0, i-75):i-3]
        hist_c = c.iloc[max(0, i-75):i-3].values
        hist_fut = c.iloc[max(3, i-72):i].values
        
        dists = []
        for k in range(len(hist_feats)):
            if np.isnan(hist_feats[k]).any(): continue
            ld = np.sum(np.log(1.0 + np.abs(curr - hist_feats[k])))
            target = 1 if ((hist_fut[k] - hist_c[k]) / hist_c[k]) >= 0.007 else -1
            dists.append((ld, target))
            
        if len(dists) < 15: continue
        dists.sort(key=lambda x: x[0])
        votes = sum(1 for _, t in dists[:8] if t == 1)
        if votes >= 6: # 75% consensus
            sig[i] = 1
            
    d['signal'] = sig
    return d

# STRATEGY 3: AI Ensemble (Lorentzian + Dynamic Pullback + Volatility Squeeze)
def strat_ai_ensemble(df):
    d = df.copy()
    c = d['c']
    d['ema9'] = c.ewm(span=9, adjust=False).mean()
    d['ema21'] = c.ewm(span=21, adjust=False).mean()
    d['ema50'] = c.ewm(span=50, adjust=False).mean()
    d['vol_ma'] = d['v'].rolling(20).mean()
    d['taker_ratio'] = d['tb'] / d['v'].replace(0, 1e-9)
    
    # ATR %
    tr = pd.concat([d['h'] - d['l'], (d['h'] - c.shift(1)).abs(), (d['l'] - c.shift(1)).abs()], axis=1).max(axis=1)
    d['atr'] = tr.rolling(14).mean()
    d['atr_pct'] = d['atr'] / c * 100.0
    
    # RSI & Stoch RSI
    period = 14
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    d['rsi'] = rsi
    ll = rsi.rolling(period).min()
    hh = rsi.rolling(period).max()
    d['stoch_k'] = ((rsi - ll) / (hh - ll).replace(0, 1e-9)) * 100
    d['stoch_d'] = d['stoch_k'].rolling(3).mean()
    
    # WaveTrend
    ap = (d['h'] + d['l'] + c) / 3.0
    esa = ap.ewm(span=10, adjust=False).mean()
    diff = (ap - esa).abs().ewm(span=10, adjust=False).mean()
    ci = (ap - esa) / (0.015 * diff.replace(0, 1e-9))
    wt1 = ci.ewm(span=11, adjust=False).mean()
    wt2 = wt1.rolling(4).mean()
    d['wt_diff'] = wt1 - wt2
    
    sig = np.zeros(len(d))
    n = len(d)
    
    for i in range(80, n):
        # 1. Macro Trend: Price > EMA50 and EMA9 >= EMA21 * 0.999
        if c.iloc[i] < d['ema50'].iloc[i] or d['ema9'].iloc[i] < d['ema21'].iloc[i] * 0.999:
            continue
            
        # 2. Volatility: ATR >= 0.25% (enough movement to hit 0.91% TP before 0.68% SL)
        if d['atr_pct'].iloc[i] < 0.25:
            continue
            
        # 3. Volume & Taker: volume >= 85% avg, taker ratio >= 0.50
        if d['v'].iloc[i] < d['vol_ma'].iloc[i] * 0.85 or d['taker_ratio'].iloc[i] < 0.50:
            continue
            
        # 4. Pullback rejection candle (wick >= 30% of range OR green candle bouncing off EMA9/21)
        candle_rng = d['h'].iloc[i] - d['l'].iloc[i]
        lower_wick = min(d['o'].iloc[i], c.iloc[i]) - d['l'].iloc[i]
        wick_rebound = (candle_rng > 0) and (lower_wick >= candle_rng * 0.25)
        is_green = c.iloc[i] > d['o'].iloc[i]
        if not (wick_rebound or is_green):
            continue
            
        # 5. Momentum hook: WaveTrend diff expanding OR Stoch K crossing D in oversold
        wt_hook = d['wt_diff'].iloc[i] > d['wt_diff'].iloc[i-1]
        stoch_hook = (d['stoch_k'].iloc[i] < 50) and (d['stoch_k'].iloc[i] >= d['stoch_d'].iloc[i])
        rsi_range = 38.0 <= d['rsi'].iloc[i] <= 68.0
        
        if (wt_hook or stoch_hook) and rsi_range:
            sig[i] = 1
            
    d['signal'] = sig
    return d

print("Fetching historical 3m data for 10 pairs...")
all_dfs = {}
for sym in PAIRS:
    df = fetch_data(sym, "3m", 600)
    if df is not None:
        all_dfs[sym] = df
        print(f"  ✓ {sym}: {len(df)} bars")
    time.sleep(0.1)

print("\nRunning Backtest on Strategy 1: EMA Ribbon + StochRSI Pullback...")
res1 = run_backtest_strategy(strat_ema_pullback, all_dfs)
print(f"Results 1: Trades={res1['trades']}, WinRate=%{res1['win_rate']:.1f}, PnL=${res1['pnl']:.2f}, AvgDur={res1['avg_duration_min']:.1f}m")

print("\nRunning Backtest on Strategy 2: Lorentzian ML Classifier...")
res2 = run_backtest_strategy(strat_lorentzian, all_dfs)
print(f"Results 2: Trades={res2['trades']}, WinRate=%{res2['win_rate']:.1f}, PnL=${res2['pnl']:.2f}, AvgDur={res2['avg_duration_min']:.1f}m")

print("\nRunning Backtest on Strategy 3: AI Ensemble (Lorentzian + Dynamic Pullback + Volatility)...")
res3 = run_backtest_strategy(strat_ai_ensemble, all_dfs)
print(f"Results 3: Trades={res3['trades']}, WinRate=%{res3['win_rate']:.1f}, PnL=${res3['pnl']:.2f}, AvgDur={res3['avg_duration_min']:.1f}m")
