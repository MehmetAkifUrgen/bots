"""Unit tests for Stoch RSI (3, 3, 14, 14, close) indicator and signals."""

import numpy as np
import pandas as pd

from stoch_rsi_scanner.indicators import evaluate_signal, stochastic_rsi, wilder_rsi


def test_wilder_rsi_bounds_and_monotonic():
    up = pd.Series(np.linspace(100.0, 200.0, 50))
    rsi_up = wilder_rsi(up, 14)
    assert rsi_up.iloc[:14].isna().all()
    assert rsi_up.iloc[14:].notna().all()
    assert np.isclose(rsi_up.iloc[-1], 100.0)

    down = pd.Series(np.linspace(200.0, 100.0, 50))
    rsi_down = wilder_rsi(down, 14)
    assert np.isclose(rsi_down.iloc[-1], 0.0)


def test_stoch_rsi_range_and_crossover():
    # Create a wave pattern that forces a clear bottoming reversal (AL) and topping reversal (SAT)
    x = np.linspace(0, 6 * np.pi, 120)
    closes = pd.Series(100.0 + 10.0 * np.sin(x))
    k, d = stochastic_rsi(closes, smooth_k=3, smooth_d=3, rsi_length=14, stoch_length=14)

    valid_k = k.dropna()
    valid_d = d.dropna()
    assert ((valid_k >= 0.0) & (valid_k <= 100.0)).all()
    assert ((valid_d >= 0.0) & (valid_d <= 100.0)).all()

    # Check that evaluate_signal detects both AL and SAT across the sine wave
    signals = set()
    for end in range(60, len(closes) + 1):
        sig = evaluate_signal(closes.iloc[:end], mode="live")
        assert sig is not None
        signals.add(sig.signal)
    assert "AL" in signals
    assert "SAT" in signals
