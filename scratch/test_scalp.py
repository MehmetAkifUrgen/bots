import requests, numpy as np, pandas as pd

def get_klines(sym, interval='3m', limit=500):
    url = 'https://api.binance.com/api/v3/klines'
    try:
        r = requests.get(url, params={'symbol': sym, 'interval': interval, 'limit': limit}, timeout=10)
        data = r.json()
        df = pd.DataFrame(data, columns=['ot','o','h','l','c','v','ct','qv','tr','tb','tq','x'])
        for col in ['o','h','l','c','v','tb','qv']: df[col] = pd.to_numeric(df[col])
        return df
    except Exception as e:
        print(f"Error {sym}: {e}")
        return None

def calc_rsi(s, period=14):
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))

def calc_wt(df, n1=10, n2=11):
    ap = (df['h'] + df['l'] + df['c']) / 3.0
    esa = ap.ewm(span=n1, adjust=False).mean()
    d = (ap - esa).abs().ewm(span=n1, adjust=False).mean()
    ci = (ap - esa) / (0.015 * d.replace(0, 1e-9))
    wt1 = ci.ewm(span=n2, adjust=False).mean()
    wt2 = wt1.rolling(4).mean()
    return wt1, wt2

def calc_cci(df, period=20):
    tp = (df['h'] + df['l'] + df['c']) / 3.0
    sma = tp.rolling(period).mean()
    mad = (tp - sma).abs().rolling(period).mean()
    return (tp - sma) / (0.015 * mad.replace(0, 1e-9))

pairs = ['SOLUSDT', 'DOGEUSDT', 'PEPEUSDT', 'NEARUSDT', 'SUIUSDT', 'AVAXUSDT', 'BTCUSDT', 'ETHUSDT', 'ENAUSDT', 'WLDUSDT']

total_trades = 0
total_pnl = 0.0
wins = 0
losses = 0

POSITION_NOTIONAL = 220.0
SL_USD = 1.50 # -$1.50 max loss
TRAILING_START = 2.00 # +$2.00 start trailing
TRAILING_DIST = 0.60 # trails 0.60$ behind peak

for sym in pairs:
    df = get_klines(sym, interval='3m', limit=500)
    if df is None or len(df) < 200: continue
    
    df['rsi'] = calc_rsi(df['c'], 14)
    wt1, wt2 = calc_wt(df)
    df['wt_diff'] = wt1 - wt2
    df['cci'] = calc_cci(df, 20)
    df['ema9'] = df['c'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['c'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
    df['vol_ma'] = df['v'].rolling(20).mean()
    df['taker_ratio'] = df['tb'] / df['v'].replace(0, 1e-9)

    # Normalize features for Lorentzian classifier
    f1 = (df['rsi'] - 50.0) / 25.0
    f2 = df['wt_diff'] / 20.0
    f3 = df['cci'] / 100.0
    f4 = (df['c'] - df['ema21']) / df['ema21'] * 100.0

    features = np.column_stack([f1.values, f2.values, f3.values, f4.values])

    in_pos = False
    entry_p = 0
    highest_p = 0
    pos_open_bar = 0

    for i in range(120, len(df) - 1):
        c_price = df['c'].iloc[i]
        
        if in_pos:
            dur = i - pos_open_bar
            curr_h = df['h'].iloc[i]
            curr_l = df['l'].iloc[i]
            
            if curr_h > highest_p: highest_p = curr_h
            
            pnl_high = (highest_p - entry_p) / entry_p * POSITION_NOTIONAL
            pnl_low = (curr_l - entry_p) / entry_p * POSITION_NOTIONAL
            
            # Stop loss price:
            sl_price = entry_p * (1.0 - (SL_USD / POSITION_NOTIONAL))
            
            # Trailing stop check:
            if pnl_high >= TRAILING_START:
                trail_stop_price = highest_p - (TRAILING_DIST / POSITION_NOTIONAL * entry_p)
                if curr_l <= trail_stop_price:
                    exit_p = trail_stop_price
                    trade_pnl = (exit_p - entry_p) / entry_p * POSITION_NOTIONAL - (POSITION_NOTIONAL * 0.0015)
                    total_trades += 1
                    total_pnl += trade_pnl
                    if trade_pnl > 0: wins += 1
                    else: losses += 1
                    in_pos = False
                    continue
            
            if curr_l <= sl_price:
                exit_p = sl_price
                trade_pnl = (exit_p - entry_p) / entry_p * POSITION_NOTIONAL - (POSITION_NOTIONAL * 0.0015)
                total_trades += 1
                total_pnl += trade_pnl
                losses += 1
                in_pos = False
                continue
                
            # Time exit if in trade for 40 bars (120 mins)
            if dur >= 40:
                exit_p = c_price
                trade_pnl = (exit_p - entry_p) / entry_p * POSITION_NOTIONAL - (POSITION_NOTIONAL * 0.0015)
                total_trades += 1
                total_pnl += trade_pnl
                if trade_pnl > 0: wins += 1
                else: losses += 1
                in_pos = False
                continue
                
        else:
            # 1. Trend filter: EMA9 > EMA21 and close > EMA50
            if not (df['ema9'].iloc[i] > df['ema21'].iloc[i] and c_price > df['ema50'].iloc[i]):
                continue
            # 2. Volume & Taker confirmation
            if not (df['v'].iloc[i] > df['vol_ma'].iloc[i] * 1.1 and df['taker_ratio'].iloc[i] > 0.50):
                continue
            # 3. WaveTrend hook or RSI momentum
            if not (df['wt_diff'].iloc[i] > df['wt_diff'].iloc[i-1] and 42.0 < df['rsi'].iloc[i] < 72.0):
                continue
                
            # 4. Lorentzian AI classification
            curr_feat = features[i]
            if np.isnan(curr_feat).any(): continue
            
            # Lookback past 80 bars
            past_feats = features[max(0, i-80):i-4]
            past_c = df['c'].iloc[max(0, i-80):i-4].values
            past_fut_c = df['c'].iloc[max(4, i-76):i].values
            
            dists = []
            for k in range(len(past_feats)):
                if np.isnan(past_feats[k]).any(): continue
                d = np.sum(np.log(1.0 + np.abs(curr_feat - past_feats[k])))
                # Target: did price rise by >= +0.8% in 4 bars?
                target = 1 if (past_fut_c[k] - past_c[k]) / past_c[k] >= 0.007 else -1
                dists.append((d, target))
                
            if len(dists) < 15: continue
            dists.sort(key=lambda x: x[0])
            k_nearest = dists[:8]
            pos_votes = sum(1 for d, t in k_nearest if t == 1)
            ai_score = pos_votes / 8.0
            
            if ai_score >= 0.625: # at least 5 out of 8 neighbors were winning
                in_pos = True
                entry_p = c_price
                highest_p = c_price
                pos_open_bar = i

wr = (wins / total_trades * 100) if total_trades > 0 else 0
print(f"Total Trades: {total_trades}")
print(f"Wins: {wins} | Losses: {losses} | Win Rate: %{wr:.1f}")
print(f"Total Net PnL: ${total_pnl:.2f}")
