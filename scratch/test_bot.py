import pandas as pd
import numpy as np
import sys
from bot import build_candidate_setups, add_indicators, MarketTicker

def test_signal_generation():
    print("Testing Signal Generation...")
    
    # Mock data
    dates = pd.date_range(start="2024-01-01", periods=300, freq="15min")
    price = 100.0 + np.cumsum(np.random.randn(300) * 0.1)
    
    df = pd.DataFrame({
        "open_time": dates,
        "open": price,
        "high": price + 0.5,
        "low": price - 0.5,
        "close": price,
        "volume": np.random.rand(300) * 1000,
        "close_time": dates,
        "quote_asset_volume": np.random.rand(300) * 100000,
        "number_of_trades": 100,
        "taker_buy_base_asset_volume": 50,
        "taker_buy_quote_asset_volume": 5000,
        "ignore": 0
    })
    
    df = add_indicators(df)
    
    market = MarketTicker(symbol="TESTUSDT", price_change_pct=5.0, last_price=101.0, quote_volume=20000000)
    
    frames = {
        "15m": df,
        "1h": df.iloc[::4], # Mocking
        "4h": df.iloc[::16] # Mocking
    }
    
    try:
        setups = build_candidate_setups(market, frames, funding_rate=0.0001)
        print(f"Setups generated: {len(setups)}")
        for s in setups:
            print(f" - {s.symbol}: {s.decision} ({s.setup_type}) Confidence: {s.confidence}")
        print("Test passed!")
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_signal_generation()
