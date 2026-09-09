import requests
import pandas as pd
import numpy as np
import time

FAPI_BASE = "https://fapi.binance.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json"
}

def get_klines(sym, interval="15m", limit=500):
    for host in [FAPI_BASE, "https://fapi1.binance.com", "https://fapi2.binance.com"]:
        try:
            r = requests.get(f"{host}/fapi/v1/klines", params={"symbol": sym, "interval": interval, "limit": limit}, headers=HEADERS, timeout=6)
            if r.status_code == 200:
                raw = r.json()
                df = pd.DataFrame(raw, columns=["ot","o","h","l","c","v","ct","qv","tr","tb","tq","x"])
                for col in ["o","h","l","c","v"]:
                    df[col] = pd.to_numeric(df[col])
                return df
        except Exception:
            continue
    return None

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window=period, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi

pairs = [
    "SOLUSDT", "DOGEUSDT", "AVAXUSDT", "NEARUSDT", "SUIUSDT", 
    "APTUSDT", "ARBUSDT", "OPUSDT", "LINKUSDT", "PEPEUSDT", 
    "WIFUSDT", "INJUSDT", "TIAUSDT", "RENDERUSDT", "FETUSDT"
]

print(f"📊 Binance Gerçek Vadeli Mum Verileri ile Test Başlatılıyor ({len(pairs)} Parite)...")

trades = []

for idx, sym in enumerate(pairs):
    print(f"[{idx+1}/{len(pairs)}] {sym} test ediliyor...", flush=True)
    df15m = get_klines(sym, "15m", 500)
    df1h = get_klines(sym, "1h", 200)
    if df15m is None or df1h is None or len(df15m) < 100 or len(df1h) < 50:
        continue
        
    df1h["ema20"] = df1h["c"].ewm(span=20, adjust=False).mean()
    df1h["ema50"] = df1h["c"].ewm(span=50, adjust=False).mean()
    
    df15m["rsi"] = calc_rsi(df15m["c"], 14)
    df15m["sma20"] = df15m["c"].rolling(20).mean()
    df15m["std20"] = df15m["c"].rolling(20).std()
    df15m["lower_bb"] = df15m["sma20"] - (2.0 * df15m["std20"])
    df15m["upper_bb"] = df15m["sma20"] + (2.0 * df15m["std20"])
    df15m["vol_avg"] = df15m["v"].rolling(20).mean()
    
    i = 50
    while i < len(df15m) - 20:
        row = df15m.iloc[i]
        c, o, h, l, v = row["c"], row["o"], row["h"], row["l"], row["v"]
        rsi = row["rsi"]
        
        ot_15 = row["ot"]
        df1h_sub = df1h[df1h["ot"] <= ot_15]
        if len(df1h_sub) < 10:
            i += 1; continue
        c1h = df1h_sub["c"].iloc[-1]
        ema20_1h = df1h_sub["ema20"].iloc[-1]
        ema50_1h = df1h_sub["ema50"].iloc[-1]
        
        is_uptrend = (c1h > ema50_1h and ema20_1h >= ema50_1h)
        is_downtrend = (c1h < ema50_1h and ema20_1h <= ema50_1h)
        
        sig_side = None
        sig_mode = None
        
        if is_uptrend and rsi <= 38.0 and l <= row["lower_bb"] and (c >= o or (c - l) > (h - c)):
            sig_side = "LONG"
            sig_mode = "PULLBACK_LONG"
        elif is_uptrend and 52.0 <= rsi <= 72.0 and row["vol_avg"] > 0 and (v / row["vol_avg"]) >= 2.5:
            max24 = df15m["h"].iloc[max(0, i-96):i].max()
            if max24 > 0 and 0.1 <= (c - max24) / max24 * 100 <= 2.2:
                sig_side = "LONG"
                sig_mode = "BREAKOUT_LONG"
        elif is_downtrend and rsi >= 62.0 and h >= row["upper_bb"] and (c <= o or (h - c) > (c - l)):
            sig_side = "SHORT"
            sig_mode = "REJECTION_SHORT"
        elif is_downtrend and 30.0 <= rsi <= 48.0 and row["vol_avg"] > 0 and (v / row["vol_avg"]) >= 2.5:
            min24 = df15m["l"].iloc[max(0, i-96):i].min()
            if min24 > 0 and 0.1 <= (min24 - c) / min24 * 100 <= 2.2:
                sig_side = "SHORT"
                sig_mode = "BREAKDOWN_SHORT"
                
        if sig_side:
            entry_price = c
            notional = 250.0
            qty = notional / entry_price
            
            sl_usd = 1.50
            be_trigger_usd = 1.00
            tp_trigger_usd = 2.00
            trailing_drop_usd = 1.00
            
            be_hit = False
            trailing_active = False
            highest_profit = 0.0
            exit_price = None
            exit_reason = None
            
            for j in range(i + 1, min(i + 48, len(df15m))):
                b = df15m.iloc[j]
                
                if sig_side == "LONG":
                    max_pnl = (b["h"] - entry_price) * qty
                    min_pnl = (b["l"] - entry_price) * qty
                    curr_pnl = (b["c"] - entry_price) * qty
                else:
                    max_pnl = (entry_price - b["l"]) * qty
                    min_pnl = (entry_price - b["h"]) * qty
                    curr_pnl = (entry_price - b["c"]) * qty
                    
                if max_pnl > highest_profit:
                    highest_profit = max_pnl
                    
                if highest_profit >= tp_trigger_usd:
                    trailing_active = True
                    
                if highest_profit >= be_trigger_usd:
                    be_hit = True
                    
                if trailing_active and (highest_profit - curr_pnl) >= trailing_drop_usd:
                    exit_reason = "TRAILING_TP"
                    exit_pnl = highest_profit - trailing_drop_usd
                    exit_price = entry_price + (exit_pnl / qty) if sig_side == "LONG" else entry_price - (exit_pnl / qty)
                    break
                if not be_hit and min_pnl <= -sl_usd:
                    exit_reason = "STOP_LOSS"
                    exit_pnl = -sl_usd
                    exit_price = entry_price - (sl_usd / qty) if sig_side == "LONG" else entry_price + (sl_usd / qty)
                    break
                if be_hit and not trailing_active and min_pnl <= 0.20:
                    exit_reason = "BREAKEVEN"
                    exit_pnl = 0.20
                    exit_price = entry_price + (0.20 / qty) if sig_side == "LONG" else entry_price - (0.20 / qty)
                    break
                    
            if not exit_reason:
                last_b = df15m.iloc[min(i + 48, len(df15m)-1)]
                exit_price = last_b["c"]
                exit_pnl = (exit_price - entry_price) * qty if sig_side == "LONG" else (entry_price - exit_price) * qty
                exit_reason = "TIMEOUT"
                
            net_pnl = exit_pnl - 0.25
            trades.append({
                "sym": sym, "side": sig_side, "mode": sig_mode,
                "entry": entry_price, "exit": exit_price, "pnl": net_pnl,
                "reason": exit_reason, "highest_profit": highest_profit
            })
            i = j + 1
        else:
            i += 1

tdf = pd.DataFrame(trades)
if len(tdf) > 0:
    wins = tdf[tdf["pnl"] > 0]
    losses = tdf[tdf["pnl"] < 0]
    
    total_trades = len(tdf)
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / total_trades) * 100
    
    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0
    net_pnl = tdf["pnl"].sum()
    avg_win = wins["pnl"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl"].mean() if len(losses) > 0 else 0
    
    print("\n" + "="*60)
    print("🎯 BİNANCE GEÇMİŞ PİYASA VERİSİ BACKTEST RAPORU")
    print("="*60)
    print(f"Toplam Test Edilen İşlem   : {total_trades}")
    print(f"✅ Başarılı İşlem (Win)    : {win_count}  (Başarı Oranı: %{win_rate:.1f})")
    print(f"❌ Stop / Kayıp (Loss)     : {loss_count}  (%{100 - win_rate:.1f})")
    print(f"------------------------------------------------------------")
    print(f"📈 Kâr Faktörü (Profit Factor): {profit_factor:.2f}")
    print(f"💸 Ortalama Kâr (İşlem Başı)  : +${avg_win:.2f}")
    print(f"🛑 Ortalama Zarar (Stop)      : ${avg_loss:.2f}")
    print(f"------------------------------------------------------------")
    print(f"💰 TOPLAM KASAYA GİREN NET KÂR: ${net_pnl:+.2f} USDT")
    print("="*60)
    
    print("\n📋 Strateji Bazında Ayrıntılı Karne:")
    for mode, grp in tdf.groupby("mode"):
        m_win = len(grp[grp["pnl"] > 0])
        m_tot = len(grp)
        m_rate = (m_win / m_tot) * 100
        m_net = grp["pnl"].sum()
        print(f"  • {mode:<26}: {m_win:>2}/{m_tot:<2} İşlem (%{m_rate:.1f} Win) -> Net: ${m_net:+.2f} USDT")
