"""Stochastic RSI (3, 3, 14, 14, close) calculation matching TradingView."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import numpy as np
import pandas as pd

SignalType = Literal["AL", "SAT", "NÖTR"]


@dataclass(frozen=True)
class StochRSISignal:
    signal: SignalType
    k_curr: float
    d_curr: float
    k_prev: float
    d_prev: float
    k_series: list[float]
    d_series: list[float]
    timestamps: list[int]


def wilder_rsi(closes: pd.Series, length: int = 14) -> pd.Series:
    """Compute RSI using Wilder's Smoothing (RMA), matching TradingView ta.rsi."""
    if len(closes) < length + 1:
        return pd.Series(np.nan, index=closes.index, dtype=float)

    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = pd.Series(np.nan, index=closes.index, dtype=float)
    avg_loss = pd.Series(np.nan, index=closes.index, dtype=float)

    # First RMA value is the simple moving average over the first `length` diffs
    avg_gain.iloc[length] = gain.iloc[1 : length + 1].mean()
    avg_loss.iloc[length] = loss.iloc[1 : length + 1].mean()

    alpha = 1.0 / length
    gain_vals = gain.to_numpy(dtype=float)
    loss_vals = loss.to_numpy(dtype=float)
    ag_vals = avg_gain.to_numpy(dtype=float)
    al_vals = avg_loss.to_numpy(dtype=float)

    for i in range(length + 1, len(closes)):
        ag_vals[i] = alpha * gain_vals[i] + (1.0 - alpha) * ag_vals[i - 1]
        al_vals[i] = alpha * loss_vals[i] + (1.0 - alpha) * al_vals[i - 1]

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag_vals / al_vals
        rsi = np.where(al_vals == 0.0, 100.0, 100.0 - (100.0 / (1.0 + rs)))
        rsi = np.where((ag_vals == 0.0) & (al_vals == 0.0), 50.0, rsi)
        rsi[:length] = np.nan

    return pd.Series(rsi, index=closes.index, dtype=float)


def stochastic_rsi(
    closes: pd.Series,
    smooth_k: int = 3,
    smooth_d: int = 3,
    rsi_length: int = 14,
    stoch_length: int = 14,
) -> tuple[pd.Series, pd.Series]:
    """Compute TradingView Stochastic RSI (%K = Blue, %D = Red)."""
    rsi = wilder_rsi(closes, length=rsi_length)
    lowest_rsi = rsi.rolling(window=stoch_length, min_periods=stoch_length).min()
    highest_rsi = rsi.rolling(window=stoch_length, min_periods=stoch_length).max()

    denom = highest_rsi - lowest_rsi
    with np.errstate(divide="ignore", invalid="ignore"):
        raw_stoch = np.where(denom == 0.0, 0.0, 100.0 * (rsi - lowest_rsi) / denom)
    raw_stoch_s = pd.Series(raw_stoch, index=closes.index, dtype=float)
    raw_stoch_s[rsi.isna() | lowest_rsi.isna()] = np.nan

    k_line = raw_stoch_s.rolling(window=smooth_k, min_periods=smooth_k).mean()
    d_line = k_line.rolling(window=smooth_d, min_periods=smooth_d).mean()
    return k_line, d_line


def evaluate_signal(
    closes: pd.Series,
    timestamps: pd.Series | None = None,
    mode: Literal["live", "closed"] = "live",
    history_points: int = 30,
) -> StochRSISignal | None:
    """
    Evaluate Stoch RSI (3, 3, 14, 14, close) crossover signal.

    - AL (BUY): Blue (%K) crosses above Red (%D) -> k_prev <= d_prev and k_curr > d_curr
    - SAT (SELL): Blue (%K) crosses below Red (%D) -> k_prev >= d_prev and k_curr < d_curr
    - mode="live": compares previous closed candle (-2) with current forming candle (-1)
    - mode="closed": compares (-3) with last closed candle (-2)
    """
    k_line, d_line = stochastic_rsi(
        closes, smooth_k=3, smooth_d=3, rsi_length=14, stoch_length=14
    )

    curr_idx = -1 if mode == "live" else -2
    prev_idx = curr_idx - 1

    if len(closes) < abs(prev_idx) + 35:
        return None

    k_curr = float(k_line.iloc[curr_idx])
    d_curr = float(d_line.iloc[curr_idx])
    k_prev = float(k_line.iloc[prev_idx])
    d_prev = float(d_line.iloc[prev_idx])

    if any(np.isnan([k_curr, d_curr, k_prev, d_prev])):
        return None

    if k_prev <= d_prev and k_curr > d_curr:
        signal: SignalType = "AL"
    elif k_prev >= d_prev and k_curr < d_curr:
        signal = "SAT"
    else:
        signal = "NÖTR"

    end_slice = len(closes) if mode == "live" else len(closes) - 1
    start_slice = max(0, end_slice - history_points)

    k_hist = [
        None if np.isnan(v) else round(float(v), 2)
        for v in k_line.iloc[start_slice:end_slice]
    ]
    d_hist = [
        None if np.isnan(v) else round(float(v), 2)
        for v in d_line.iloc[start_slice:end_slice]
    ]
    ts_hist = (
        [int(t) for t in timestamps.iloc[start_slice:end_slice]]
        if timestamps is not None
        else list(range(start_slice, end_slice))
    )

    return StochRSISignal(
        signal=signal,
        k_curr=round(k_curr, 2),
        d_curr=round(d_curr, 2),
        k_prev=round(k_prev, 2),
        d_prev=round(d_prev, 2),
        k_series=[v if v is not None else 0.0 for v in k_hist],
        d_series=[v if v is not None else 0.0 for v in d_hist],
        timestamps=ts_hist,
    )
