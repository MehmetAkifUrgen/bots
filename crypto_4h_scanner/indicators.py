"""Pine Script v5 'Gün İçi Anlık Sistem (Yüksek Başarı Oranı %70+)' 4H implementation."""

from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SystemEvaluation:
    bar_open_time: int
    price: float
    cond1_buy: bool  # 1. Stoch RSI (k > d)
    cond2_buy: bool  # 2. RSI (14) > RSI MA (14)
    cond3_buy: bool  # 3. Fisher (9) > Fisher[1]
    cond4_buy: bool  # 4. WaveTrend (wt1 > wt2)
    cond5_buy: bool  # 5. SMI > SMI Signal & EMA9
    trend_ok: bool   # close > EMA(200)
    first_three_buy: bool
    all_five_buy: bool
    green_trigger: bool
    orange_trigger: bool
    exit_trigger: bool
    exit_reason: str | None
    exit_price: float | None
    exit_trade_pct: float | None
    in_green_pos: bool
    in_orange_pos: bool
    be_locked: bool
    entry_price: float | None
    tp_level: float | None
    sl_level: float | None
    next_res: float
    prev_sup: float
    total_trades: int
    win_trades: int
    win_rate: float
    total_profit_pct: float
    bar_close_time: int = 0
    ema200_bull: bool = False
    ema200_bear: bool = False
    all_five_sell: bool = False
    long_5_trigger: bool = False
    short_5_trigger: bool = False
    first_three_sell: bool = False
    short_3_trigger: bool = False
    short_any2_trigger: bool = False
    short_any4_trigger: bool = False
    long_3_trigger: bool = False
    short_rev_trigger: bool = False


def wilder_rsi(closes: pd.Series, length: int = 14) -> pd.Series:
    """Compute RSI using Wilder's Smoothing (RMA), matching TradingView ta.rsi."""
    if len(closes) < length + 1:
        return pd.Series(np.nan, index=closes.index, dtype=float)

    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    ag_vals = np.full(len(closes), np.nan, dtype=float)
    al_vals = np.full(len(closes), np.nan, dtype=float)

    ag_vals[length] = gain.iloc[1 : length + 1].mean()
    al_vals[length] = loss.iloc[1 : length + 1].mean()

    alpha = 1.0 / length
    gain_vals = gain.to_numpy(dtype=float)
    loss_vals = loss.to_numpy(dtype=float)

    for i in range(length + 1, len(closes)):
        ag_vals[i] = alpha * gain_vals[i] + (1.0 - alpha) * ag_vals[i - 1]
        al_vals[i] = alpha * loss_vals[i] + (1.0 - alpha) * al_vals[i - 1]

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag_vals / al_vals
        rsi = np.where(al_vals == 0.0, 100.0, 100.0 - (100.0 / (1.0 + rs)))
        rsi = np.where((ag_vals == 0.0) & (al_vals == 0.0), 50.0, rsi)
        rsi[:length] = np.nan

    return pd.Series(rsi, index=closes.index, dtype=float)


def pine_ema(series: pd.Series, length: int) -> pd.Series:
    """EMA matching TradingView ta.ema (alpha = 2 / (length + 1), NaN-aware)."""
    vals = series.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan, dtype=float)
    alpha = 2.0 / (length + 1.0)

    # Find first valid index to seed EMA with SMA of first `length` valid points if possible,
    # or first valid value when fewer remain.
    valid_indices = np.where(~np.isnan(vals))[0]
    if len(valid_indices) == 0:
        return pd.Series(out, index=series.index, dtype=float)

    first = valid_indices[0]
    if first + length <= len(vals) and not np.isnan(vals[first : first + length]).any():
        seed_idx = first + length - 1
        out[seed_idx] = float(np.mean(vals[first : first + length]))
        start = seed_idx + 1
    else:
        out[first] = float(vals[first])
        start = first + 1

    for i in range(start, len(vals)):
        if np.isnan(vals[i]):
            out[i] = out[i - 1]
        elif np.isnan(out[i - 1]):
            out[i] = vals[i]
        else:
            out[i] = alpha * vals[i] + (1.0 - alpha) * out[i - 1]

    return pd.Series(out, index=series.index, dtype=float)


def stochastic_rsi(
    closes: pd.Series,
    smooth_k: int = 3,
    smooth_d: int = 3,
    rsi_length: int = 14,
    stoch_length: int = 14,
) -> tuple[pd.Series, pd.Series]:
    """Stoch RSI (3, 3, 14, 14) matching TradingView."""
    rsi = wilder_rsi(closes, length=rsi_length)
    lowest_rsi = rsi.rolling(window=stoch_length, min_periods=stoch_length).min()
    highest_rsi = rsi.rolling(window=stoch_length, min_periods=stoch_length).max()

    denom = highest_rsi - lowest_rsi
    with np.errstate(divide="ignore", invalid="ignore"):
        raw_stoch = np.where(denom == 0.0, 0.0, 100.0 * (rsi - lowest_rsi) / denom)
    raw_stoch = np.clip(raw_stoch, 0.0, 100.0)
    raw_stoch_s = pd.Series(raw_stoch, index=closes.index, dtype=float)
    raw_stoch_s[rsi.isna() | lowest_rsi.isna()] = np.nan

    k_line = raw_stoch_s.rolling(window=smooth_k, min_periods=smooth_k).mean()
    d_line = k_line.rolling(window=smooth_d, min_periods=smooth_d).mean()
    return k_line, d_line


def fisher_transform(
    highs: pd.Series, lows: pd.Series, length: int = 9
) -> tuple[pd.Series, pd.Series]:
    """Fisher Transform (9) matching Pine Script var val1 / fisher."""
    hl2 = (highs + lows) / 2.0
    high_ = hl2.rolling(window=length, min_periods=1).max()
    low_ = hl2.rolling(window=length, min_periods=1).min()

    hl2_arr = hl2.to_numpy(dtype=float)
    hi_arr = high_.to_numpy(dtype=float)
    lo_arr = low_.to_numpy(dtype=float)

    n = len(hl2_arr)
    fisher_arr = np.zeros(n, dtype=float)
    trigger_arr = np.zeros(n, dtype=float)

    val1_prev = 0.0
    fisher_prev = 0.0

    for i in range(n):
        span = max(hi_arr[i] - lo_arr[i], 1e-10)
        val1 = 0.66 * ((hl2_arr[i] - lo_arr[i]) / span - 0.5) + 0.67 * val1_prev
        if val1 > 0.99:
            val1 = 0.999
        elif val1 < -0.99:
            val1 = -0.999

        fisher = 0.5 * math.log((1.0 + val1) / max(1.0 - val1, 1e-10)) + 0.5 * fisher_prev
        trigger_arr[i] = fisher_prev
        fisher_arr[i] = fisher

        val1_prev = val1
        fisher_prev = fisher

    return (
        pd.Series(fisher_arr, index=highs.index, dtype=float),
        pd.Series(trigger_arr, index=highs.index, dtype=float),
    )


def wavetrend(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    n1: int = 10,
    n2: int = 21,
) -> tuple[pd.Series, pd.Series]:
    """WT_CROSS_LB (10, 21) matching Pine Script."""
    ap = (highs + lows + closes) / 3.0
    esa = pine_ema(ap, n1)
    d_wt = pine_ema((ap - esa).abs(), n1)
    denom = 0.015 * d_wt.clip(lower=1e-10)
    ci = (ap - esa) / denom
    wt1 = pine_ema(ci, n2)
    wt2 = wt1.rolling(window=4, min_periods=4).mean()
    return wt1, wt2


def stochastic_momentum_index(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    k_len: int = 10,
    d_len: int = 3,
    ema_len: int = 3,
    ema9_len: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Stoch MI (10, 3, 3) + EMA 9 matching Pine Script."""
    ll = lows.rolling(window=k_len, min_periods=k_len).min()
    hh = highs.rolling(window=k_len, min_periods=k_len).max()
    diff = hh - ll
    rdiff = closes - (hh + ll) / 2.0

    avgrel = pine_ema(pine_ema(rdiff, d_len), ema_len)
    avgdiff = pine_ema(pine_ema(diff, d_len), ema_len)

    with np.errstate(divide="ignore", invalid="ignore"):
        smi_vals = np.where(
            (avgdiff != 0.0) & (~np.isnan(avgdiff)),
            avgrel / (avgdiff / 2.0) * 100.0,
            0.0,
        )
    smi = pd.Series(smi_vals, index=closes.index, dtype=float)
    smi[avgdiff.isna()] = np.nan
    smi_signal = pine_ema(smi, d_len)
    smi_ema9 = pine_ema(smi, ema9_len)
    return smi, smi_signal, smi_ema9


def compute_pivots(
    highs: pd.Series, lows: pd.Series, left: int = 7, right: int = 7
) -> tuple[np.ndarray, np.ndarray]:
    """
    Compute confirmed pivot highs and lows at confirmation bar index (p + right),
    matching Pine Script ta.pivothigh(high, left, right) and ta.pivotlow(low, left, right).
    """
    h = highs.to_numpy(dtype=float)
    l = lows.to_numpy(dtype=float)
    n = len(h)
    piv_h = np.full(n, np.nan, dtype=float)
    piv_l = np.full(n, np.nan, dtype=float)

    for i in range(left + right, n):
        p = i - right
        win_h = h[p - left : p + right + 1]
        if h[p] >= np.max(h[p - left : p]) and h[p] > np.max(h[p + 1 : p + right + 1]):
            piv_h[i] = h[p]
        if l[p] <= np.min(l[p - left : p]) and l[p] < np.min(l[p + 1 : p + right + 1]):
            piv_l[i] = l[p]
        _ = win_h  # keep clear

    return piv_h, piv_l


def _monthly_fib_for_bar(
    bar_open_time: int, monthly_df: pd.DataFrame | None
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Return (fibP, fibR1, fibR2, fibS1, fibS2) from previous completed 1M candle."""
    if monthly_df is None or monthly_df.empty:
        return None, None, None, None, None

    closed = monthly_df[monthly_df["close_time"] < bar_open_time]
    if closed.empty:
        return None, None, None, None, None

    prev = closed.iloc[-1]
    h = float(prev["high"])
    l = float(prev["low"])
    c = float(prev["close"])
    fib_p = (h + l + c) / 3.0
    fib_r1 = fib_p + 0.382 * (h - l)
    fib_r2 = fib_p + 0.618 * (h - l)
    fib_s1 = fib_p - 0.382 * (h - l)
    fib_s2 = fib_p - 0.618 * (h - l)
    return fib_p, fib_r1, fib_r2, fib_s1, fib_s2


def _get_next_res(
    price: float,
    last_res: float | None,
    fib_p: float | None,
    fib_r1: float | None,
    fib_r2: float | None,
    h20: float,
) -> float:
    thresh = price * 1.015
    if last_res is not None and last_res > thresh:
        return last_res
    if fib_p is not None and fib_p > thresh:
        return fib_p
    if fib_r1 is not None and fib_r1 > thresh:
        return fib_r1
    if fib_r2 is not None and fib_r2 > thresh:
        return fib_r2
    return h20 if h20 > thresh else price * 1.05


def _get_prev_sup(
    price: float,
    last_sup: float | None,
    fib_p: float | None,
    fib_s1: float | None,
    fib_s2: float | None,
    l15: float,
) -> float:
    thresh = price * 0.985
    if last_sup is not None and last_sup < thresh:
        return last_sup
    if fib_p is not None and fib_p < thresh:
        return fib_p
    if fib_s1 is not None and fib_s1 < thresh:
        return fib_s1
    if fib_s2 is not None and fib_s2 < thresh:
        return fib_s2
    return l15 if l15 < thresh else price * 0.95


def evaluate_4h_system(
    df_4h: pd.DataFrame,
    df_1m: pd.DataFrame | None = None,
    pivot_len: int = 7,
    be_trigger_pct: float = 2.5,
    ema200_filter: bool = False,
) -> SystemEvaluation | None:
    """
    Run the Pine Script 'Gün İçi Anlık Sistem' state machine on 4H OHLCV DataFrame.
    df_4h must have columns: open_time, open, high, low, close.
    df_1m (optional) must have columns: open_time, high, low, close, close_time.
    """
    if len(df_4h) < 60:
        return None

    opens = df_4h["open"].astype(float).reset_index(drop=True)
    highs = df_4h["high"].astype(float).reset_index(drop=True)
    lows = df_4h["low"].astype(float).reset_index(drop=True)
    closes = df_4h["close"].astype(float).reset_index(drop=True)
    open_times = df_4h["open_time"].astype("int64").reset_index(drop=True)

    # 1. Stoch RSI (3, 3, 14, 14)
    k, d = stochastic_rsi(closes, smooth_k=3, smooth_d=3, rsi_length=14, stoch_length=14)
    # 2. RSI (14) + MA (14)
    rsi_main = wilder_rsi(closes, length=14)
    rsi_ma = rsi_main.rolling(window=14, min_periods=14).mean()
    # 3. Fisher (9)
    fisher, fisher_trigger = fisher_transform(highs, lows, length=9)
    # 4. WaveTrend (10, 21)
    wt1, wt2 = wavetrend(highs, lows, closes, n1=10, n2=21)
    # 5. Stoch MI (10, 3, 3) + EMA 9
    smi, smi_sig, smi_ema9 = stochastic_momentum_index(
        highs, lows, closes, k_len=10, d_len=3, ema_len=3, ema9_len=9
    )
    # 6. Crypto Trend Filter (EMA 200)
    ema200 = pine_ema(closes, 200)

    piv_h, piv_l = compute_pivots(highs, lows, left=pivot_len, right=pivot_len)
    h20_s = highs.rolling(window=20, min_periods=1).max()
    l15_s = lows.rolling(window=15, min_periods=1).min()

    in_green_pos = False
    in_orange_pos = False
    be_locked = False
    entry_price: float | None = None
    tp_level: float | None = None
    sl_level: float | None = None
    total_trades = 0
    win_trades = 0
    total_profit_pct = 0.0

    last_res: float | None = None
    last_sup: float | None = None

    n = len(df_4h)
    last_green_trig = False
    last_orange_trig = False
    last_exit_trig = False
    last_exit_reason: str | None = None
    last_exit_price: float | None = None
    last_exit_pct: float | None = None

    last_c1 = last_c2 = last_c3 = last_c4 = last_c5 = False
    last_trend_ok = True
    last_first3 = last_all5 = False
    last_all5_sell = False
    last_ema200_bull = False
    last_ema200_bear = False
    last_long_5_trig = False
    last_long_3_trig = False
    last_short_5_trig = False
    last_first3_sell = False
    last_short_3_trig = False
    last_short_any2_trig = False
    last_short_any4_trig = False
    last_short_rev_trig = False
    prev_all5_up_strict = False
    prev_first3_up = False
    prev_all5_dn_strict = False
    prev_first3_dn = False
    prev_any2_dn = False
    prev_any4_dn = False
    prev_rev_dn = False
    bull4_hist: list[bool] = []
    last_fib_p = last_fib_r1 = last_fib_r2 = last_fib_s1 = last_fib_s2 = None

    for i in range(n):
        if not np.isnan(piv_h[i]):
            last_res = float(piv_h[i])
        if not np.isnan(piv_l[i]):
            last_sup = float(piv_l[i])

        # Skip until all indicators have valid values
        if any(
            np.isnan(x)
            for x in (
                k.iloc[i],
                d.iloc[i],
                rsi_main.iloc[i],
                rsi_ma.iloc[i],
                wt1.iloc[i],
                wt2.iloc[i],
                smi.iloc[i],
                smi_sig.iloc[i],
                smi_ema9.iloc[i],
            )
        ):
            continue

        c1 = bool(k.iloc[i] > d.iloc[i])
        c2 = bool(rsi_main.iloc[i] > rsi_ma.iloc[i])
        c3 = bool(fisher.iloc[i] > fisher_trigger.iloc[i])
        c4 = bool(wt1.iloc[i] > wt2.iloc[i])
        c5 = bool(
            (smi.iloc[i] > smi_sig.iloc[i])
            and (smi.iloc[i] > smi_ema9.iloc[i])
            and (smi_sig.iloc[i] > smi_ema9.iloc[i])
        )

        first3 = c1 and c2 and c3
        all5 = first3 and c4 and c5
        # Dual-direction 5/5 strict conditions (matches 25-coin 4H backtest)
        all5_up_strict = bool(
            c1 and c2 and c3 and c4
            and (smi.iloc[i] > smi_sig.iloc[i])
            and (smi.iloc[i] > smi_ema9.iloc[i])
        )
        c1_dn = bool(k.iloc[i] < d.iloc[i])
        c2_dn = bool(rsi_main.iloc[i] < rsi_ma.iloc[i])
        c3_dn = bool(fisher.iloc[i] < fisher_trigger.iloc[i])
        c4_dn = bool(wt1.iloc[i] < wt2.iloc[i])
        c5_dn = bool((smi.iloc[i] < smi_sig.iloc[i]) and (smi.iloc[i] < smi_ema9.iloc[i]))
        first3_dn = c1_dn and c2_dn and c3_dn
        dn_count = int(c1_dn) + int(c2_dn) + int(c3_dn) + int(c4_dn) + int(c5_dn)
        any2_dn = dn_count >= 2
        any4_dn = dn_count >= 4
        all5_dn_strict = bool(first3_dn and c4_dn and c5_dn)
        up_count = int(c1) + int(c2) + int(c3) + int(c4) + int((smi.iloc[i] > smi_sig.iloc[i]) and (smi.iloc[i] > smi_ema9.iloc[i]))
        recent_bull4 = any(bull4_hist[-3:]) if bull4_hist else False
        rev_dn = recent_bull4 and (dn_count >= 3)
        real_sell = bool(
            (smi.iloc[i] < smi_ema9.iloc[i])
            or ((wt1.iloc[i] < wt2.iloc[i]) and (fisher.iloc[i] < fisher_trigger.iloc[i]))
        )

        bar_o = float(opens.iloc[i])
        bar_h = float(highs.iloc[i])
        bar_c = float(closes.iloc[i])
        bar_ts = int(open_times.iloc[i])
        ema200_val = float(ema200.iloc[i])
        trend_ok = True if (not ema200_filter or np.isnan(ema200_val)) else (bar_c > ema200_val)

        # Last bar acts as realtime (close), historical bars use avg(open, high)
        exec_price = bar_c if i == n - 1 else (bar_o + bar_h) / 2.0

        fib_p, fib_r1, fib_r2, fib_s1, fib_s2 = _monthly_fib_for_bar(bar_ts, df_1m)
        h20 = float(h20_s.iloc[i])
        l15 = float(l15_s.iloc[i])

        green_trig = False
        orange_trig = False
        exit_trig = False
        exit_reason: str | None = None
        exit_price: float | None = None
        exit_pct: float | None = None

        # 1. YEŞİL AL
        if first3 and trend_ok and not in_green_pos:
            green_trig = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _get_next_res(entry_price, last_res, fib_p, fib_r1, fib_r2, h20)
                sl_level = _get_prev_sup(entry_price, last_sup, fib_p, fib_s1, fib_s2, l15)

        # 2. TURUNCU KESİN AL
        if all5 and trend_ok and not in_orange_pos:
            orange_trig = True
            in_orange_pos = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _get_next_res(entry_price, last_res, fib_p, fib_r1, fib_r2, h20)
                sl_level = _get_prev_sup(entry_price, last_sup, fib_p, fib_s1, fib_s2, l15)

        # 3. ÇIKIŞ KONTROLÜ
        any_pos = in_green_pos or in_orange_pos
        if any_pos and not green_trig and not orange_trig:
            assert entry_price is not None and tp_level is not None and sl_level is not None
            if not be_locked and bar_h >= entry_price * (1.0 + be_trigger_pct / 100.0):
                be_locked = True
                sl_level = entry_price * 1.003

            hit_stop = bar_c < sl_level
            hit_tp = bar_h >= tp_level

            if hit_tp:
                exit_trig = True
                exec_tp = max(bar_o, tp_level)
                trade_pct = ((exec_tp - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "KÂR AL (Direnç)"
                exit_price = exec_tp
                exit_pct = trade_pct
                in_green_pos = False
                in_orange_pos = False
                be_locked = False
                entry_price = None
                tp_level = None
                sl_level = None
            elif hit_stop:
                exit_trig = True
                exec_sl = sl_level if be_locked else bar_c
                trade_pct = ((exec_sl - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT (Kâr Kilidi)" if be_locked else "STOP (Destek)"
                exit_price = exec_sl
                exit_pct = trade_pct
                in_green_pos = False
                in_orange_pos = False
                be_locked = False
                entry_price = None
                tp_level = None
                sl_level = None
            elif real_sell:
                exit_trig = True
                trade_pct = ((bar_c - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT"
                exit_price = bar_c
                exit_pct = trade_pct
                in_green_pos = False
                in_orange_pos = False
                be_locked = False
                entry_price = None
                tp_level = None
                sl_level = None

        if i == n - 1:
            last_green_trig = green_trig
            last_orange_trig = orange_trig
            last_exit_trig = exit_trig
            last_exit_reason = exit_reason
            last_exit_price = exit_price
            last_exit_pct = exit_pct
            last_c1, last_c2, last_c3, last_c4, last_c5 = c1, c2, c3, c4, c5
            last_trend_ok = trend_ok
            last_first3, last_all5 = first3, all5
            last_all5_sell = all5_dn_strict
            last_first3_sell = first3_dn
            last_ema200_bull = bool(not np.isnan(ema200_val) and bar_c > ema200_val)
            last_ema200_bear = bool(not np.isnan(ema200_val) and bar_c < ema200_val)
            last_long_5_trig = all5_up_strict and (not prev_all5_up_strict) and last_ema200_bull
            last_long_3_trig = first3 and (not prev_first3_up) and last_ema200_bull
            last_short_5_trig = all5_dn_strict and (not prev_all5_dn_strict) and last_ema200_bear
            last_short_3_trig = first3_dn and (not prev_first3_dn) and last_ema200_bear
            last_short_any2_trig = any2_dn and (not prev_any2_dn) and last_ema200_bear
            last_short_any4_trig = any4_dn and (not prev_any4_dn) and last_ema200_bear
            last_short_rev_trig = rev_dn and (not prev_rev_dn)
            last_fib_p, last_fib_r1, last_fib_r2, last_fib_s1, last_fib_s2 = (
                fib_p,
                fib_r1,
                fib_r2,
                fib_s1,
                fib_s2,
            )

        prev_all5_up_strict = all5_up_strict
        prev_first3_up = first3
        prev_all5_dn_strict = all5_dn_strict
        prev_first3_dn = first3_dn
        prev_any2_dn = any2_dn
        prev_any4_dn = any4_dn
        prev_rev_dn = rev_dn
        bull4_hist.append(up_count == 5)

    last_close = float(closes.iloc[-1])
    next_res = (
        tp_level
        if tp_level is not None
        else _get_next_res(
            last_close,
            last_res,
            last_fib_p,
            last_fib_r1,
            last_fib_r2,
            float(h20_s.iloc[-1]),
        )
    )
    prev_sup = (
        sl_level
        if sl_level is not None
        else _get_prev_sup(
            last_close,
            last_sup,
            last_fib_p,
            last_fib_s1,
            last_fib_s2,
            float(l15_s.iloc[-1]),
        )
    )
    win_rate = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0

    bar_close_ts = (
        int(df_4h["close_time"].iloc[-1])
        if "close_time" in df_4h.columns
        else int(open_times.iloc[-1]) + 4 * 3600 * 1000 - 1
    )

    return SystemEvaluation(
        bar_open_time=int(open_times.iloc[-1]),
        price=last_close,
        cond1_buy=last_c1,
        cond2_buy=last_c2,
        cond3_buy=last_c3,
        cond4_buy=last_c4,
        cond5_buy=last_c5,
        trend_ok=last_trend_ok,
        first_three_buy=last_first3,
        all_five_buy=last_all5,
        green_trigger=last_green_trig,
        orange_trigger=last_orange_trig,
        exit_trigger=last_exit_trig,
        exit_reason=last_exit_reason,
        exit_price=last_exit_price,
        exit_trade_pct=last_exit_pct,
        in_green_pos=in_green_pos,
        in_orange_pos=in_orange_pos,
        be_locked=be_locked,
        entry_price=entry_price,
        tp_level=tp_level,
        sl_level=sl_level,
        next_res=next_res,
        prev_sup=prev_sup,
        total_trades=total_trades,
        win_trades=win_trades,
        win_rate=round(win_rate, 2),
        total_profit_pct=round(total_profit_pct, 2),
        bar_close_time=bar_close_ts,
        ema200_bull=last_ema200_bull,
        ema200_bear=last_ema200_bear,
        all_five_sell=last_all5_sell,
        long_5_trigger=last_long_5_trig,
        short_5_trigger=last_short_5_trig,
        first_three_sell=last_first3_sell,
        short_3_trigger=last_short_3_trig,
        short_any2_trigger=last_short_any2_trig,
        short_any4_trigger=last_short_any4_trig,
        long_3_trigger=last_long_3_trig,
        short_rev_trigger=last_short_rev_trig,
    )


def _target_res_1h_15m(
    price: float,
    last_res: float | None,
    fib_p: float | None,
    fib_r1: float | None,
    fib_r2: float | None,
    h20: float,
    min_tp_pct: float,
) -> float:
    min_tp = price * (1.0 + min_tp_pct / 100.0)
    if last_res is not None and last_res >= min_tp:
        return last_res
    if fib_p is not None and fib_p >= min_tp:
        return fib_p
    if fib_r1 is not None and fib_r1 >= min_tp:
        return fib_r1
    if fib_r2 is not None and fib_r2 >= min_tp:
        return fib_r2
    if h20 >= min_tp:
        return h20
    return price * (1.0 + (min_tp_pct * 1.5) / 100.0)


def _dynamic_stop_1h_15m(
    price: float,
    last_sup: float | None,
    fib_s1: float | None,
    active_stop_pct: float,
) -> float:
    max_stop_floor = price * (1.0 - active_stop_pct / 100.0)
    min_safe_zone = price * (1.0 - (active_stop_pct * 0.55) / 100.0)
    if last_sup is not None and max_stop_floor <= last_sup <= min_safe_zone:
        return last_sup
    if fib_s1 is not None and max_stop_floor <= fib_s1 <= min_safe_zone:
        return fib_s1
    return max_stop_floor


def evaluate_1h_15m_system(
    df_ltf: pd.DataFrame,
    df_trend: pd.DataFrame,
    df_pivot: pd.DataFrame,
    timeframe: str = "1h",
    pivot_len: int = 6,
    use_trend_filter: bool = True,
    fee_buffer_pct: float = 0.20,
) -> SystemEvaluation | None:
    """
    Pine Script v5 'Kripto 1S & 15dk Optimize - 5'li Kademeli Sistem'.
    - 1h: stop=2.20%, be=2.00%, min_tp=2.40%, trend=4h EMA50, pivot=1d Fib
    - 15m: stop=1.50%, be=1.20%, min_tp=1.40%, trend=1h EMA50, pivot=4h Fib
    """
    if len(df_ltf) < 60 or len(df_trend) < 55:
        return None

    is15m = timeframe == "15m"
    is1h = timeframe == "1h"
    active_stop_pct = 1.50 if is15m else (2.20 if is1h else 2.50)
    active_be_pct = 1.20 if is15m else (2.00 if is1h else 2.20)
    min_tp_pct = 1.40 if is15m else (2.40 if is1h else 2.80)
    ltf_ms = (15 if is15m else (45 if timeframe == "45m" else 60)) * 60 * 1000

    opens = df_ltf["open"].astype(float).reset_index(drop=True)
    highs = df_ltf["high"].astype(float).reset_index(drop=True)
    lows = df_ltf["low"].astype(float).reset_index(drop=True)
    closes = df_ltf["close"].astype(float).reset_index(drop=True)
    open_times = df_ltf["open_time"].astype("int64").reset_index(drop=True)

    # HTF Trend EMA(50)
    dft = df_trend.sort_values("open_time").reset_index(drop=True)
    ema50_htf = pine_ema(dft["close"].astype(float), 50)
    tr_open_times = dft["open_time"].astype("int64").to_numpy()
    tr_ema_arr = ema50_htf.to_numpy(dtype=float)

    # HTF Fib Pivots (previous closed HTF bar)
    dfp = df_pivot.sort_values("open_time").reset_index(drop=True) if not df_pivot.empty else df_pivot
    pv_close_times = dfp["close_time"].astype("int64").to_numpy() if not dfp.empty else np.array([], dtype="int64")
    pv_h_arr = dfp["high"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)
    pv_l_arr = dfp["low"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)
    pv_c_arr = dfp["close"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)

    # 1. Stoch RSI (3, 3, 14, 14)
    k, d = stochastic_rsi(closes, smooth_k=3, smooth_d=3, rsi_length=14, stoch_length=14)
    # 2. RSI (14) + MA (14)
    rsi_main = wilder_rsi(closes, length=14)
    rsi_ma = rsi_main.rolling(window=14, min_periods=14).mean()
    # 3. Fisher (9)
    fisher, fisher_trigger = fisher_transform(highs, lows, length=9)
    # 4. WaveTrend (10, 21)
    wt1, wt2 = wavetrend(highs, lows, closes, n1=10, n2=21)
    # 5. Stoch MI (10, 3, 3) + EMA 9
    smi, smi_sig, smi_ema9 = stochastic_momentum_index(
        highs, lows, closes, k_len=10, d_len=3, ema_len=3, ema9_len=9
    )

    piv_h, piv_l = compute_pivots(highs, lows, left=pivot_len, right=pivot_len)
    h20_s = highs.rolling(window=20, min_periods=1).max()

    in_green_pos = False
    in_orange_pos = False
    be_locked = False
    entry_price: float | None = None
    tp_level: float | None = None
    sl_level: float | None = None
    total_trades = 0
    win_trades = 0
    total_profit_pct = 0.0

    last_res: float | None = None
    last_sup: float | None = None

    n = len(df_ltf)
    last_green_trig = False
    last_orange_trig = False
    last_exit_trig = False
    last_exit_reason: str | None = None
    last_exit_price: float | None = None
    last_exit_pct: float | None = None
    last_c1 = last_c2 = last_c3 = last_c4 = last_c5 = False
    last_trend_ok = True
    last_first3 = last_all5 = False
    last_fib_p = last_fib_r1 = last_fib_r2 = last_fib_s1 = None

    for i in range(n):
        if not np.isnan(piv_h[i]):
            last_res = float(piv_h[i])
        if not np.isnan(piv_l[i]):
            last_sup = float(piv_l[i])

        if any(
            np.isnan(x)
            for x in (
                k.iloc[i],
                d.iloc[i],
                rsi_main.iloc[i],
                rsi_ma.iloc[i],
                wt1.iloc[i],
                wt2.iloc[i],
                smi.iloc[i],
                smi_sig.iloc[i],
                smi_ema9.iloc[i],
            )
        ):
            continue

        bar_o = float(opens.iloc[i])
        bar_h = float(highs.iloc[i])
        bar_c = float(closes.iloc[i])
        bar_ts = int(open_times.iloc[i])

        # HTF Trend EMA(50) with lookahead_off (latest HTF bar <= bar close)
        bar_end_ts = bar_ts + ltf_ms - 1
        idx_tr = int(np.searchsorted(tr_open_times, bar_end_ts, side="right")) - 1
        ema50_val = float(tr_ema_arr[idx_tr]) if idx_tr >= 0 else np.nan
        cond_trend = (
            True
            if (not use_trend_filter or np.isnan(ema50_val))
            else (bar_c >= ema50_val)
        )

        # Previous closed HTF Pivot bar
        closed_idx = int(np.searchsorted(pv_close_times, bar_ts, side="left")) - 1
        if closed_idx >= 0:
            ph = pv_h_arr[closed_idx]
            pl = pv_l_arr[closed_idx]
            pc = pv_c_arr[closed_idx]
            fib_p = (ph + pl + pc) / 3.0
            fib_r1 = fib_p + 0.382 * (ph - pl)
            fib_r2 = fib_p + 0.618 * (ph - pl)
            fib_s1 = fib_p - 0.382 * (ph - pl)
        else:
            fib_p = fib_r1 = fib_r2 = fib_s1 = None

        c1 = bool(k.iloc[i] > d.iloc[i])
        c2 = bool(rsi_main.iloc[i] > rsi_ma.iloc[i])
        c3 = bool(fisher.iloc[i] > fisher_trigger.iloc[i])
        c4 = bool(wt1.iloc[i] > wt2.iloc[i])
        c5 = bool(
            (smi.iloc[i] > smi_sig.iloc[i])
            and (smi.iloc[i] > smi_ema9.iloc[i])
            and (smi_sig.iloc[i] > smi_ema9.iloc[i])
        )

        first3 = c1 and c2 and c3 and cond_trend
        all5 = first3 and c4 and c5
        real_sell = bool(
            (smi.iloc[i] < smi_ema9.iloc[i])
            or ((wt1.iloc[i] < wt2.iloc[i]) and (fisher.iloc[i] < fisher_trigger.iloc[i]))
        )

        exec_price = bar_c if i == n - 1 else (bar_o + bar_h) / 2.0
        h20 = float(h20_s.iloc[i])

        green_trig = False
        orange_trig = False
        exit_trig = False
        exit_reason: str | None = None
        exit_price: float | None = None
        exit_pct: float | None = None

        if first3 and not in_green_pos:
            green_trig = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _target_res_1h_15m(
                    entry_price, last_res, fib_p, fib_r1, fib_r2, h20, min_tp_pct
                )
                sl_level = _dynamic_stop_1h_15m(
                    entry_price, last_sup, fib_s1, active_stop_pct
                )

        if all5 and not in_orange_pos:
            orange_trig = True
            in_orange_pos = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _target_res_1h_15m(
                    entry_price, last_res, fib_p, fib_r1, fib_r2, h20, min_tp_pct
                )
                sl_level = _dynamic_stop_1h_15m(
                    entry_price, last_sup, fib_s1, active_stop_pct
                )

        any_pos = in_green_pos or in_orange_pos
        if any_pos and not green_trig and not orange_trig:
            assert entry_price is not None and tp_level is not None and sl_level is not None
            if not be_locked and bar_h >= entry_price * (1.0 + active_be_pct / 100.0):
                be_locked = True
                sl_level = entry_price * (1.0 + fee_buffer_pct / 100.0)

            hit_stop = bar_c < sl_level
            hit_tp = bar_h >= tp_level

            if hit_tp:
                exit_trig = True
                exec_tp = max(bar_o, tp_level)
                trade_pct = ((exec_tp - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "KÂR AL (Direnç)"
                exit_price = exec_tp
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif hit_stop:
                exit_trig = True
                exec_sl = sl_level if be_locked else bar_c
                trade_pct = ((exec_sl - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT (Kâr Kilidi)" if be_locked else "STOP"
                exit_price = exec_sl
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif real_sell:
                exit_trig = True
                trade_pct = ((bar_c - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT"
                exit_price = bar_c
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None

        if i == n - 1:
            last_green_trig = green_trig
            last_orange_trig = orange_trig
            last_exit_trig = exit_trig
            last_exit_reason = exit_reason
            last_exit_price = exit_price
            last_exit_pct = exit_pct
            last_c1, last_c2, last_c3, last_c4, last_c5 = c1, c2, c3, c4, c5
            last_trend_ok = cond_trend
            last_first3, last_all5 = first3, all5
            last_fib_p, last_fib_r1, last_fib_r2, last_fib_s1 = (
                fib_p,
                fib_r1,
                fib_r2,
                fib_s1,
            )

    last_close = float(closes.iloc[-1])
    next_res = (
        tp_level
        if tp_level is not None
        else _target_res_1h_15m(
            last_close,
            last_res,
            last_fib_p,
            last_fib_r1,
            last_fib_r2,
            float(h20_s.iloc[-1]),
            min_tp_pct,
        )
    )
    prev_sup = (
        sl_level
        if sl_level is not None
        else _dynamic_stop_1h_15m(last_close, last_sup, last_fib_s1, active_stop_pct)
    )
    win_rate = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0

    return SystemEvaluation(
        bar_open_time=int(open_times.iloc[-1]),
        price=last_close,
        cond1_buy=last_c1,
        cond2_buy=last_c2,
        cond3_buy=last_c3,
        cond4_buy=last_c4,
        cond5_buy=last_c5,
        trend_ok=last_trend_ok,
        first_three_buy=last_first3,
        all_five_buy=last_all5,
        green_trigger=last_green_trig,
        orange_trigger=last_orange_trig,
        exit_trigger=last_exit_trig,
        exit_reason=last_exit_reason,
        exit_price=last_exit_price,
        exit_trade_pct=last_exit_pct,
        in_green_pos=in_green_pos,
        in_orange_pos=in_orange_pos,
        be_locked=be_locked,
        entry_price=entry_price,
        tp_level=tp_level,
        sl_level=sl_level,
        next_res=next_res,
        prev_sup=prev_sup,
        total_trades=total_trades,
        win_trades=win_trades,
        win_rate=round(win_rate, 2),
        total_profit_pct=round(total_profit_pct, 2),
    )


def _target_res_1h_15m(
    price: float,
    last_res: float | None,
    fib_p: float | None,
    fib_r1: float | None,
    fib_r2: float | None,
    h20: float,
    min_tp_pct: float,
) -> float:
    min_tp = price * (1.0 + min_tp_pct / 100.0)
    if last_res is not None and last_res >= min_tp:
        return last_res
    if fib_p is not None and fib_p >= min_tp:
        return fib_p
    if fib_r1 is not None and fib_r1 >= min_tp:
        return fib_r1
    if fib_r2 is not None and fib_r2 >= min_tp:
        return fib_r2
    if h20 >= min_tp:
        return h20
    return price * (1.0 + (min_tp_pct * 1.5) / 100.0)


def _dynamic_stop_1h_15m(
    price: float,
    last_sup: float | None,
    fib_s1: float | None,
    active_stop_pct: float,
) -> float:
    max_stop_floor = price * (1.0 - active_stop_pct / 100.0)
    min_safe_zone = price * (1.0 - (active_stop_pct * 0.55) / 100.0)
    if last_sup is not None and max_stop_floor <= last_sup <= min_safe_zone:
        return last_sup
    if fib_s1 is not None and max_stop_floor <= fib_s1 <= min_safe_zone:
        return fib_s1
    return max_stop_floor


def evaluate_1h_15m_system(
    df_ltf: pd.DataFrame,
    df_trend: pd.DataFrame,
    df_pivot: pd.DataFrame,
    timeframe: str = "1h",
    pivot_len: int = 6,
    use_trend_filter: bool = True,
    fee_buffer_pct: float = 0.20,
) -> SystemEvaluation | None:
    """
    Pine Script v5 'Kripto 1S & 15dk Optimize - 5'li Kademeli Sistem'.
    - 1h: stop=2.20%, be=2.00%, min_tp=2.40%, trend=4h EMA50, pivot=1d Fib
    - 15m: stop=1.50%, be=1.20%, min_tp=1.40%, trend=1h EMA50, pivot=4h Fib
    """
    if len(df_ltf) < 60 or len(df_trend) < 55:
        return None

    is15m = timeframe == "15m"
    is1h = timeframe == "1h"
    active_stop_pct = 1.50 if is15m else (2.20 if is1h else 2.50)
    active_be_pct = 1.20 if is15m else (2.00 if is1h else 2.20)
    min_tp_pct = 1.40 if is15m else (2.40 if is1h else 2.80)
    ltf_ms = (15 if is15m else (45 if timeframe == "45m" else 60)) * 60 * 1000

    opens = df_ltf["open"].astype(float).reset_index(drop=True)
    highs = df_ltf["high"].astype(float).reset_index(drop=True)
    lows = df_ltf["low"].astype(float).reset_index(drop=True)
    closes = df_ltf["close"].astype(float).reset_index(drop=True)
    open_times = df_ltf["open_time"].astype("int64").reset_index(drop=True)

    # HTF Trend EMA(50)
    dft = df_trend.sort_values("open_time").reset_index(drop=True)
    ema50_htf = pine_ema(dft["close"].astype(float), 50)
    tr_open_times = dft["open_time"].astype("int64").to_numpy()
    tr_ema_arr = ema50_htf.to_numpy(dtype=float)

    # HTF Fib Pivots (previous closed HTF bar)
    dfp = df_pivot.sort_values("open_time").reset_index(drop=True) if not df_pivot.empty else df_pivot
    pv_close_times = dfp["close_time"].astype("int64").to_numpy() if not dfp.empty else np.array([], dtype="int64")
    pv_h_arr = dfp["high"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)
    pv_l_arr = dfp["low"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)
    pv_c_arr = dfp["close"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)

    # 1. Stoch RSI (3, 3, 14, 14)
    k, d = stochastic_rsi(closes, smooth_k=3, smooth_d=3, rsi_length=14, stoch_length=14)
    # 2. RSI (14) + MA (14)
    rsi_main = wilder_rsi(closes, length=14)
    rsi_ma = rsi_main.rolling(window=14, min_periods=14).mean()
    # 3. Fisher (9)
    fisher, fisher_trigger = fisher_transform(highs, lows, length=9)
    # 4. WaveTrend (10, 21)
    wt1, wt2 = wavetrend(highs, lows, closes, n1=10, n2=21)
    # 5. Stoch MI (10, 3, 3) + EMA 9
    smi, smi_sig, smi_ema9 = stochastic_momentum_index(
        highs, lows, closes, k_len=10, d_len=3, ema_len=3, ema9_len=9
    )

    piv_h, piv_l = compute_pivots(highs, lows, left=pivot_len, right=pivot_len)
    h20_s = highs.rolling(window=20, min_periods=1).max()

    in_green_pos = False
    in_orange_pos = False
    be_locked = False
    entry_price: float | None = None
    tp_level: float | None = None
    sl_level: float | None = None
    total_trades = 0
    win_trades = 0
    total_profit_pct = 0.0

    last_res: float | None = None
    last_sup: float | None = None

    n = len(df_ltf)
    last_green_trig = False
    last_orange_trig = False
    last_exit_trig = False
    last_exit_reason: str | None = None
    last_exit_price: float | None = None
    last_exit_pct: float | None = None
    last_c1 = last_c2 = last_c3 = last_c4 = last_c5 = False
    last_trend_ok = True
    last_first3 = last_all5 = False
    last_fib_p = last_fib_r1 = last_fib_r2 = last_fib_s1 = None

    for i in range(n):
        if not np.isnan(piv_h[i]):
            last_res = float(piv_h[i])
        if not np.isnan(piv_l[i]):
            last_sup = float(piv_l[i])

        if any(
            np.isnan(x)
            for x in (
                k.iloc[i],
                d.iloc[i],
                rsi_main.iloc[i],
                rsi_ma.iloc[i],
                wt1.iloc[i],
                wt2.iloc[i],
                smi.iloc[i],
                smi_sig.iloc[i],
                smi_ema9.iloc[i],
            )
        ):
            continue

        bar_o = float(opens.iloc[i])
        bar_h = float(highs.iloc[i])
        bar_c = float(closes.iloc[i])
        bar_ts = int(open_times.iloc[i])

        # HTF Trend EMA(50) with lookahead_off (latest HTF bar <= bar close)
        bar_end_ts = bar_ts + ltf_ms - 1
        idx_tr = int(np.searchsorted(tr_open_times, bar_end_ts, side="right")) - 1
        ema50_val = float(tr_ema_arr[idx_tr]) if idx_tr >= 0 else np.nan
        cond_trend = (
            True
            if (not use_trend_filter or np.isnan(ema50_val))
            else (bar_c >= ema50_val)
        )

        # Previous closed HTF Pivot bar
        closed_idx = int(np.searchsorted(pv_close_times, bar_ts, side="left")) - 1
        if closed_idx >= 0:
            ph = pv_h_arr[closed_idx]
            pl = pv_l_arr[closed_idx]
            pc = pv_c_arr[closed_idx]
            fib_p = (ph + pl + pc) / 3.0
            fib_r1 = fib_p + 0.382 * (ph - pl)
            fib_r2 = fib_p + 0.618 * (ph - pl)
            fib_s1 = fib_p - 0.382 * (ph - pl)
        else:
            fib_p = fib_r1 = fib_r2 = fib_s1 = None

        c1 = bool(k.iloc[i] > d.iloc[i])
        c2 = bool(rsi_main.iloc[i] > rsi_ma.iloc[i])
        c3 = bool(fisher.iloc[i] > fisher_trigger.iloc[i])
        c4 = bool(wt1.iloc[i] > wt2.iloc[i])
        c5 = bool(
            (smi.iloc[i] > smi_sig.iloc[i])
            and (smi.iloc[i] > smi_ema9.iloc[i])
            and (smi_sig.iloc[i] > smi_ema9.iloc[i])
        )

        first3 = c1 and c2 and c3 and cond_trend
        all5 = first3 and c4 and c5
        real_sell = bool(
            (smi.iloc[i] < smi_ema9.iloc[i])
            or ((wt1.iloc[i] < wt2.iloc[i]) and (fisher.iloc[i] < fisher_trigger.iloc[i]))
        )

        exec_price = bar_c if i == n - 1 else (bar_o + bar_h) / 2.0
        h20 = float(h20_s.iloc[i])

        green_trig = False
        orange_trig = False
        exit_trig = False
        exit_reason: str | None = None
        exit_price: float | None = None
        exit_pct: float | None = None

        if first3 and not in_green_pos:
            green_trig = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _target_res_1h_15m(
                    entry_price, last_res, fib_p, fib_r1, fib_r2, h20, min_tp_pct
                )
                sl_level = _dynamic_stop_1h_15m(
                    entry_price, last_sup, fib_s1, active_stop_pct
                )

        if all5 and not in_orange_pos:
            orange_trig = True
            in_orange_pos = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _target_res_1h_15m(
                    entry_price, last_res, fib_p, fib_r1, fib_r2, h20, min_tp_pct
                )
                sl_level = _dynamic_stop_1h_15m(
                    entry_price, last_sup, fib_s1, active_stop_pct
                )

        any_pos = in_green_pos or in_orange_pos
        if any_pos and not green_trig and not orange_trig:
            assert entry_price is not None and tp_level is not None and sl_level is not None
            if not be_locked and bar_h >= entry_price * (1.0 + active_be_pct / 100.0):
                be_locked = True
                sl_level = entry_price * (1.0 + fee_buffer_pct / 100.0)

            hit_stop = bar_c < sl_level
            hit_tp = bar_h >= tp_level

            if hit_tp:
                exit_trig = True
                exec_tp = max(bar_o, tp_level)
                trade_pct = ((exec_tp - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "KÂR AL (Direnç)"
                exit_price = exec_tp
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif hit_stop:
                exit_trig = True
                exec_sl = sl_level if be_locked else bar_c
                trade_pct = ((exec_sl - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT (Kâr Kilidi)" if be_locked else "STOP"
                exit_price = exec_sl
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif real_sell:
                exit_trig = True
                trade_pct = ((bar_c - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT"
                exit_price = bar_c
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None

        if i == n - 1:
            last_green_trig = green_trig
            last_orange_trig = orange_trig
            last_exit_trig = exit_trig
            last_exit_reason = exit_reason
            last_exit_price = exit_price
            last_exit_pct = exit_pct
            last_c1, last_c2, last_c3, last_c4, last_c5 = c1, c2, c3, c4, c5
            last_trend_ok = cond_trend
            last_first3, last_all5 = first3, all5
            last_fib_p, last_fib_r1, last_fib_r2, last_fib_s1 = (
                fib_p,
                fib_r1,
                fib_r2,
                fib_s1,
            )

    last_close = float(closes.iloc[-1])
    next_res = (
        tp_level
        if tp_level is not None
        else _target_res_1h_15m(
            last_close,
            last_res,
            last_fib_p,
            last_fib_r1,
            last_fib_r2,
            float(h20_s.iloc[-1]),
            min_tp_pct,
        )
    )
    prev_sup = (
        sl_level
        if sl_level is not None
        else _dynamic_stop_1h_15m(last_close, last_sup, last_fib_s1, active_stop_pct)
    )
    win_rate = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0

    return SystemEvaluation(
        bar_open_time=int(open_times.iloc[-1]),
        price=last_close,
        cond1_buy=last_c1,
        cond2_buy=last_c2,
        cond3_buy=last_c3,
        cond4_buy=last_c4,
        cond5_buy=last_c5,
        trend_ok=last_trend_ok,
        first_three_buy=last_first3,
        all_five_buy=last_all5,
        green_trigger=last_green_trig,
        orange_trigger=last_orange_trig,
        exit_trigger=last_exit_trig,
        exit_reason=last_exit_reason,
        exit_price=last_exit_price,
        exit_trade_pct=last_exit_pct,
        in_green_pos=in_green_pos,
        in_orange_pos=in_orange_pos,
        be_locked=be_locked,
        entry_price=entry_price,
        tp_level=tp_level,
        sl_level=sl_level,
        next_res=next_res,
        prev_sup=prev_sup,
        total_trades=total_trades,
        win_trades=win_trades,
        win_rate=round(win_rate, 2),
        total_profit_pct=round(total_profit_pct, 2),
    )


_45M_MS = 45 * 60 * 1000


def resample_15m_to_45m(df_15m: pd.DataFrame) -> pd.DataFrame:
    """Resample 15m OHLCV candles into 45m candles aligned to 45m UTC boundaries."""
    if df_15m.empty:
        return df_15m.copy()

    df = df_15m.sort_values("open_time").reset_index(drop=True).copy()
    df["_bucket"] = (df["open_time"].astype("int64") // _45M_MS) * _45M_MS
    grouped = (
        df.groupby("_bucket", sort=True, as_index=False)
        .agg(
            open_time=("open_time", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            close_time=("close_time", "last"),
        )
        .drop(columns=["_bucket"])
    )
    return grouped


_4H_MS = 4 * 3600 * 1000


def resample_1h_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h OHLCV candles into 4h candles aligned to 4h UTC boundaries."""
    if df_1h.empty:
        return df_1h.copy()

    df = df_1h.sort_values("open_time").reset_index(drop=True).copy()
    df["_bucket"] = (df["open_time"].astype("int64") // _4H_MS) * _4H_MS
    grouped = (
        df.groupby("_bucket", sort=True, as_index=False)
        .agg(
            open_time=("open_time", "first"),
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            close_time=("close_time", "last"),
        )
        .drop(columns=["_bucket"])
    )
    return grouped


def _scalp_res_45m(
    price: float,
    last_res: float | None,
    fib_p: float | None,
    fib_r1: float | None,
    h12: float,
    min_tp_pct: float = 0.45,
) -> float:
    min_tp = price * (1.0 + min_tp_pct / 100.0)
    max_tp = price * 1.025
    if last_res is not None and min_tp <= last_res <= max_tp:
        return last_res
    if fib_p is not None and min_tp <= fib_p <= max_tp:
        return fib_p
    if fib_r1 is not None and min_tp <= fib_r1 <= max_tp:
        return fib_r1
    if min_tp <= h12 <= max_tp:
        return h12
    return price * (1.0 + min_tp_pct * 1.6 / 100.0)


def _tight_sup_45m(
    price: float,
    last_sup: float | None,
    fib_s1: float | None,
    max_stop_pct: float = 0.40,
) -> float:
    hard_floor = price * (1.0 - max_stop_pct / 100.0)
    min_buf = price * 0.9985
    if last_sup is not None and hard_floor < last_sup < min_buf:
        return last_sup
    if fib_s1 is not None and hard_floor < fib_s1 < min_buf:
        return fib_s1
    return hard_floor


def evaluate_45m_scalp_system(
    df_45m: pd.DataFrame,
    df_15m: pd.DataFrame,
    pivot_len: int = 4,
    be_trigger_pct: float = 0.35,
    fee_buffer_pct: float = 0.12,
    max_stop_pct: float = 0.40,
    min_tp_pct: float = 0.45,
    use_15m_trend_filter: bool = True,
) -> SystemEvaluation | None:
    """
    Pine Script v5 'Kripto 5dk Ultra Scalp - Dar Stop & 5'li Kademeli Sistem'
    applied to 45m candles with 15m EMA(50) trend filter & 15m Fib pivots.
    """
    if len(df_45m) < 60 or len(df_15m) < 55:
        return None

    opens = df_45m["open"].astype(float).reset_index(drop=True)
    highs = df_45m["high"].astype(float).reset_index(drop=True)
    lows = df_45m["low"].astype(float).reset_index(drop=True)
    closes = df_45m["close"].astype(float).reset_index(drop=True)
    open_times = df_45m["open_time"].astype("int64").reset_index(drop=True)

    # 15m EMA(50) & 15m Fib Pivots aligned to each 45m bar
    df15 = df_15m.sort_values("open_time").reset_index(drop=True)
    ema50_15m = pine_ema(df15["close"].astype(float), 50)
    m15_open_times = df15["open_time"].astype("int64").to_numpy()
    m15_close_times = df15["close_time"].astype("int64").to_numpy()
    m15_ema_arr = ema50_15m.to_numpy(dtype=float)
    m15_h_arr = df15["high"].astype(float).to_numpy()
    m15_l_arr = df15["low"].astype(float).to_numpy()
    m15_c_arr = df15["close"].astype(float).to_numpy()

    # 1. Stoch RSI (3, 3, 14, 14)
    k, d = stochastic_rsi(closes, smooth_k=3, smooth_d=3, rsi_length=14, stoch_length=14)
    # 2. RSI (14) + MA (14)
    rsi_main = wilder_rsi(closes, length=14)
    rsi_ma = rsi_main.rolling(window=14, min_periods=14).mean()
    # 3. Fisher (9)
    fisher, fisher_trigger = fisher_transform(highs, lows, length=9)
    # 4. WaveTrend (10, 21)
    wt1, wt2 = wavetrend(highs, lows, closes, n1=10, n2=21)
    # 5. Stoch MI (10, 3, 3) + EMA 9
    smi, smi_sig, smi_ema9 = stochastic_momentum_index(
        highs, lows, closes, k_len=10, d_len=3, ema_len=3, ema9_len=9
    )

    piv_h, piv_l = compute_pivots(highs, lows, left=pivot_len, right=pivot_len)
    h12_s = highs.rolling(window=12, min_periods=1).max()

    in_green_pos = False
    in_orange_pos = False
    be_locked = False
    entry_price: float | None = None
    tp_level: float | None = None
    sl_level: float | None = None
    total_trades = 0
    win_trades = 0
    total_profit_pct = 0.0

    last_res: float | None = None
    last_sup: float | None = None

    n = len(df_45m)
    last_green_trig = False
    last_orange_trig = False
    last_exit_trig = False
    last_exit_reason: str | None = None
    last_exit_price: float | None = None
    last_exit_pct: float | None = None
    last_c1 = last_c2 = last_c3 = last_c4 = last_c5 = False
    last_trend_ok = True
    last_first3 = last_all5 = False
    last_fib_p = last_fib_r1 = last_fib_s1 = None

    for i in range(n):
        if not np.isnan(piv_h[i]):
            last_res = float(piv_h[i])
        if not np.isnan(piv_l[i]):
            last_sup = float(piv_l[i])

        if any(
            np.isnan(x)
            for x in (
                k.iloc[i],
                d.iloc[i],
                rsi_main.iloc[i],
                rsi_ma.iloc[i],
                wt1.iloc[i],
                wt2.iloc[i],
                smi.iloc[i],
                smi_sig.iloc[i],
                smi_ema9.iloc[i],
            )
        ):
            continue

        bar_o = float(opens.iloc[i])
        bar_h = float(highs.iloc[i])
        bar_l = float(lows.iloc[i])
        bar_c = float(closes.iloc[i])
        bar_ts = int(open_times.iloc[i])

        # Latest 15m EMA50 at or before this 45m bar's close
        bar_end_ts = bar_ts + _45M_MS - 1
        idx_15 = int(np.searchsorted(m15_open_times, bar_end_ts, side="right")) - 1
        ema50_val = float(m15_ema_arr[idx_15]) if idx_15 >= 0 else np.nan
        cond_15m_trend = (
            True
            if (not use_15m_trend_filter or np.isnan(ema50_val))
            else (bar_c >= ema50_val)
        )

        # Previous closed 15m bar for Fib Pivots
        closed_idx = int(np.searchsorted(m15_close_times, bar_ts, side="left")) - 1
        if closed_idx >= 0:
            mh = m15_h_arr[closed_idx]
            ml = m15_l_arr[closed_idx]
            mc = m15_c_arr[closed_idx]
            fib_p = (mh + ml + mc) / 3.0
            fib_r1 = fib_p + 0.382 * (mh - ml)
            fib_s1 = fib_p - 0.382 * (mh - ml)
        else:
            fib_p = fib_r1 = fib_s1 = None

        c1 = bool(k.iloc[i] > d.iloc[i])
        c2 = bool(rsi_main.iloc[i] > rsi_ma.iloc[i])
        c3 = bool(fisher.iloc[i] > fisher_trigger.iloc[i])
        c4 = bool(wt1.iloc[i] > wt2.iloc[i])
        c5 = bool(
            (smi.iloc[i] > smi_sig.iloc[i])
            and (smi.iloc[i] > smi_ema9.iloc[i])
            and (smi_sig.iloc[i] > smi_ema9.iloc[i])
        )

        first3 = c1 and c2 and c3 and cond_15m_trend
        all5 = first3 and c4 and c5
        scalp_sell = bool(
            (smi.iloc[i] < smi_ema9.iloc[i])
            or ((wt1.iloc[i] < wt2.iloc[i]) and (fisher.iloc[i] < fisher_trigger.iloc[i]))
        )

        exec_price = bar_c if i == n - 1 else (bar_o + bar_h) / 2.0
        h12 = float(h12_s.iloc[i])

        green_trig = False
        orange_trig = False
        exit_trig = False
        exit_reason: str | None = None
        exit_price: float | None = None
        exit_pct: float | None = None

        if first3 and not in_green_pos:
            green_trig = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _scalp_res_45m(
                    entry_price, last_res, fib_p, fib_r1, h12, min_tp_pct
                )
                sl_level = _tight_sup_45m(entry_price, last_sup, fib_s1, max_stop_pct)

        if all5 and not in_orange_pos:
            orange_trig = True
            in_orange_pos = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _scalp_res_45m(
                    entry_price, last_res, fib_p, fib_r1, h12, min_tp_pct
                )
                sl_level = _tight_sup_45m(entry_price, last_sup, fib_s1, max_stop_pct)

        any_pos = in_green_pos or in_orange_pos
        if any_pos and not green_trig and not orange_trig:
            assert entry_price is not None and tp_level is not None and sl_level is not None
            if not be_locked and bar_h >= entry_price * (1.0 + be_trigger_pct / 100.0):
                be_locked = True
                sl_level = entry_price * (1.0 + fee_buffer_pct / 100.0)

            hit_stop = bar_l <= sl_level
            hit_tp = bar_h >= tp_level

            if hit_stop:
                exit_trig = True
                exec_sl = bar_c if i == n - 1 else sl_level
                trade_pct = ((exec_sl - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT (Kâr Kilidi)" if be_locked else "STOP"
                exit_price = exec_sl
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif hit_tp:
                exit_trig = True
                exec_tp = max(bar_o, tp_level)
                trade_pct = ((exec_tp - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "KÂR AL (Direnç)"
                exit_price = exec_tp
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif scalp_sell:
                exit_trig = True
                trade_pct = ((bar_c - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT"
                exit_price = bar_c
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None

        if i == n - 1:
            last_green_trig = green_trig
            last_orange_trig = orange_trig
            last_exit_trig = exit_trig
            last_exit_reason = exit_reason
            last_exit_price = exit_price
            last_exit_pct = exit_pct
            last_c1, last_c2, last_c3, last_c4, last_c5 = c1, c2, c3, c4, c5
            last_trend_ok = cond_15m_trend
            last_first3, last_all5 = first3, all5
            last_fib_p, last_fib_r1, last_fib_s1 = fib_p, fib_r1, fib_s1

    last_close = float(closes.iloc[-1])
    next_res = (
        tp_level
        if tp_level is not None
        else _scalp_res_45m(
            last_close, last_res, last_fib_p, last_fib_r1, float(h12_s.iloc[-1]), min_tp_pct
        )
    )
    prev_sup = (
        sl_level
        if sl_level is not None
        else _tight_sup_45m(last_close, last_sup, last_fib_s1, max_stop_pct)
    )
    win_rate = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0

    return SystemEvaluation(
        bar_open_time=int(open_times.iloc[-1]),
        price=last_close,
        cond1_buy=last_c1,
        cond2_buy=last_c2,
        cond3_buy=last_c3,
        cond4_buy=last_c4,
        cond5_buy=last_c5,
        trend_ok=last_trend_ok,
        first_three_buy=last_first3,
        all_five_buy=last_all5,
        green_trigger=last_green_trig,
        orange_trigger=last_orange_trig,
        exit_trigger=last_exit_trig,
        exit_reason=last_exit_reason,
        exit_price=last_exit_price,
        exit_trade_pct=last_exit_pct,
        in_green_pos=in_green_pos,
        in_orange_pos=in_orange_pos,
        be_locked=be_locked,
        entry_price=entry_price,
        tp_level=tp_level,
        sl_level=sl_level,
        next_res=next_res,
        prev_sup=prev_sup,
        total_trades=total_trades,
        win_trades=win_trades,
        win_rate=round(win_rate, 2),
        total_profit_pct=round(total_profit_pct, 2),
    )


def _target_res_1h_15m(
    price: float,
    last_res: float | None,
    fib_p: float | None,
    fib_r1: float | None,
    fib_r2: float | None,
    h20: float,
    min_tp_pct: float,
) -> float:
    min_tp = price * (1.0 + min_tp_pct / 100.0)
    if last_res is not None and last_res >= min_tp:
        return last_res
    if fib_p is not None and fib_p >= min_tp:
        return fib_p
    if fib_r1 is not None and fib_r1 >= min_tp:
        return fib_r1
    if fib_r2 is not None and fib_r2 >= min_tp:
        return fib_r2
    if h20 >= min_tp:
        return h20
    return price * (1.0 + (min_tp_pct * 1.5) / 100.0)


def _dynamic_stop_1h_15m(
    price: float,
    last_sup: float | None,
    fib_s1: float | None,
    active_stop_pct: float,
) -> float:
    max_stop_floor = price * (1.0 - active_stop_pct / 100.0)
    min_safe_zone = price * (1.0 - (active_stop_pct * 0.55) / 100.0)
    if last_sup is not None and max_stop_floor <= last_sup <= min_safe_zone:
        return last_sup
    if fib_s1 is not None and max_stop_floor <= fib_s1 <= min_safe_zone:
        return fib_s1
    return max_stop_floor


def evaluate_1h_15m_system(
    df_ltf: pd.DataFrame,
    df_trend: pd.DataFrame,
    df_pivot: pd.DataFrame,
    timeframe: str = "1h",
    pivot_len: int = 6,
    use_trend_filter: bool = True,
    fee_buffer_pct: float = 0.20,
) -> SystemEvaluation | None:
    """
    Pine Script v5 'Kripto 1S & 15dk Optimize - 5'li Kademeli Sistem'.
    - 1h: stop=2.20%, be=2.00%, min_tp=2.40%, trend=4h EMA50, pivot=1d Fib
    - 15m: stop=1.50%, be=1.20%, min_tp=1.40%, trend=1h EMA50, pivot=4h Fib
    """
    if len(df_ltf) < 60 or len(df_trend) < 55:
        return None

    is15m = timeframe == "15m"
    is1h = timeframe == "1h"
    active_stop_pct = 1.50 if is15m else (2.20 if is1h else 2.50)
    active_be_pct = 1.20 if is15m else (2.00 if is1h else 2.20)
    min_tp_pct = 1.40 if is15m else (2.40 if is1h else 2.80)
    ltf_ms = (15 if is15m else (45 if timeframe == "45m" else 60)) * 60 * 1000

    opens = df_ltf["open"].astype(float).reset_index(drop=True)
    highs = df_ltf["high"].astype(float).reset_index(drop=True)
    lows = df_ltf["low"].astype(float).reset_index(drop=True)
    closes = df_ltf["close"].astype(float).reset_index(drop=True)
    open_times = df_ltf["open_time"].astype("int64").reset_index(drop=True)

    # HTF Trend EMA(50)
    dft = df_trend.sort_values("open_time").reset_index(drop=True)
    ema50_htf = pine_ema(dft["close"].astype(float), 50)
    tr_open_times = dft["open_time"].astype("int64").to_numpy()
    tr_ema_arr = ema50_htf.to_numpy(dtype=float)

    # HTF Fib Pivots (previous closed HTF bar)
    dfp = df_pivot.sort_values("open_time").reset_index(drop=True) if not df_pivot.empty else df_pivot
    pv_close_times = dfp["close_time"].astype("int64").to_numpy() if not dfp.empty else np.array([], dtype="int64")
    pv_h_arr = dfp["high"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)
    pv_l_arr = dfp["low"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)
    pv_c_arr = dfp["close"].astype(float).to_numpy() if not dfp.empty else np.array([], dtype=float)

    # 1. Stoch RSI (3, 3, 14, 14)
    k, d = stochastic_rsi(closes, smooth_k=3, smooth_d=3, rsi_length=14, stoch_length=14)
    # 2. RSI (14) + MA (14)
    rsi_main = wilder_rsi(closes, length=14)
    rsi_ma = rsi_main.rolling(window=14, min_periods=14).mean()
    # 3. Fisher (9)
    fisher, fisher_trigger = fisher_transform(highs, lows, length=9)
    # 4. WaveTrend (10, 21)
    wt1, wt2 = wavetrend(highs, lows, closes, n1=10, n2=21)
    # 5. Stoch MI (10, 3, 3) + EMA 9
    smi, smi_sig, smi_ema9 = stochastic_momentum_index(
        highs, lows, closes, k_len=10, d_len=3, ema_len=3, ema9_len=9
    )

    piv_h, piv_l = compute_pivots(highs, lows, left=pivot_len, right=pivot_len)
    h20_s = highs.rolling(window=20, min_periods=1).max()

    in_green_pos = False
    in_orange_pos = False
    be_locked = False
    entry_price: float | None = None
    tp_level: float | None = None
    sl_level: float | None = None
    total_trades = 0
    win_trades = 0
    total_profit_pct = 0.0

    last_res: float | None = None
    last_sup: float | None = None

    n = len(df_ltf)
    last_green_trig = False
    last_orange_trig = False
    last_exit_trig = False
    last_exit_reason: str | None = None
    last_exit_price: float | None = None
    last_exit_pct: float | None = None
    last_c1 = last_c2 = last_c3 = last_c4 = last_c5 = False
    last_trend_ok = True
    last_first3 = last_all5 = False
    last_fib_p = last_fib_r1 = last_fib_r2 = last_fib_s1 = None

    for i in range(n):
        if not np.isnan(piv_h[i]):
            last_res = float(piv_h[i])
        if not np.isnan(piv_l[i]):
            last_sup = float(piv_l[i])

        if any(
            np.isnan(x)
            for x in (
                k.iloc[i],
                d.iloc[i],
                rsi_main.iloc[i],
                rsi_ma.iloc[i],
                wt1.iloc[i],
                wt2.iloc[i],
                smi.iloc[i],
                smi_sig.iloc[i],
                smi_ema9.iloc[i],
            )
        ):
            continue

        bar_o = float(opens.iloc[i])
        bar_h = float(highs.iloc[i])
        bar_c = float(closes.iloc[i])
        bar_ts = int(open_times.iloc[i])

        # HTF Trend EMA(50) with lookahead_off (latest HTF bar <= bar close)
        bar_end_ts = bar_ts + ltf_ms - 1
        idx_tr = int(np.searchsorted(tr_open_times, bar_end_ts, side="right")) - 1
        ema50_val = float(tr_ema_arr[idx_tr]) if idx_tr >= 0 else np.nan
        cond_trend = (
            True
            if (not use_trend_filter or np.isnan(ema50_val))
            else (bar_c >= ema50_val)
        )

        # Previous closed HTF Pivot bar
        closed_idx = int(np.searchsorted(pv_close_times, bar_ts, side="left")) - 1
        if closed_idx >= 0:
            ph = pv_h_arr[closed_idx]
            pl = pv_l_arr[closed_idx]
            pc = pv_c_arr[closed_idx]
            fib_p = (ph + pl + pc) / 3.0
            fib_r1 = fib_p + 0.382 * (ph - pl)
            fib_r2 = fib_p + 0.618 * (ph - pl)
            fib_s1 = fib_p - 0.382 * (ph - pl)
        else:
            fib_p = fib_r1 = fib_r2 = fib_s1 = None

        c1 = bool(k.iloc[i] > d.iloc[i])
        c2 = bool(rsi_main.iloc[i] > rsi_ma.iloc[i])
        c3 = bool(fisher.iloc[i] > fisher_trigger.iloc[i])
        c4 = bool(wt1.iloc[i] > wt2.iloc[i])
        c5 = bool(
            (smi.iloc[i] > smi_sig.iloc[i])
            and (smi.iloc[i] > smi_ema9.iloc[i])
            and (smi_sig.iloc[i] > smi_ema9.iloc[i])
        )

        first3 = c1 and c2 and c3 and cond_trend
        all5 = first3 and c4 and c5
        real_sell = bool(
            (smi.iloc[i] < smi_ema9.iloc[i])
            or ((wt1.iloc[i] < wt2.iloc[i]) and (fisher.iloc[i] < fisher_trigger.iloc[i]))
        )

        exec_price = bar_c if i == n - 1 else (bar_o + bar_h) / 2.0
        h20 = float(h20_s.iloc[i])

        green_trig = False
        orange_trig = False
        exit_trig = False
        exit_reason: str | None = None
        exit_price: float | None = None
        exit_pct: float | None = None

        if first3 and not in_green_pos:
            green_trig = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _target_res_1h_15m(
                    entry_price, last_res, fib_p, fib_r1, fib_r2, h20, min_tp_pct
                )
                sl_level = _dynamic_stop_1h_15m(
                    entry_price, last_sup, fib_s1, active_stop_pct
                )

        if all5 and not in_orange_pos:
            orange_trig = True
            in_orange_pos = True
            in_green_pos = True
            be_locked = False
            if entry_price is None:
                entry_price = exec_price
                tp_level = _target_res_1h_15m(
                    entry_price, last_res, fib_p, fib_r1, fib_r2, h20, min_tp_pct
                )
                sl_level = _dynamic_stop_1h_15m(
                    entry_price, last_sup, fib_s1, active_stop_pct
                )

        any_pos = in_green_pos or in_orange_pos
        if any_pos and not green_trig and not orange_trig:
            assert entry_price is not None and tp_level is not None and sl_level is not None
            if not be_locked and bar_h >= entry_price * (1.0 + active_be_pct / 100.0):
                be_locked = True
                sl_level = entry_price * (1.0 + fee_buffer_pct / 100.0)

            hit_stop = bar_c < sl_level
            hit_tp = bar_h >= tp_level

            if hit_tp:
                exit_trig = True
                exec_tp = max(bar_o, tp_level)
                trade_pct = ((exec_tp - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "KÂR AL (Direnç)"
                exit_price = exec_tp
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif hit_stop:
                exit_trig = True
                exec_sl = sl_level if be_locked else bar_c
                trade_pct = ((exec_sl - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT (Kâr Kilidi)" if be_locked else "STOP"
                exit_price = exec_sl
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None
            elif real_sell:
                exit_trig = True
                trade_pct = ((bar_c - entry_price) / entry_price) * 100.0
                total_trades += 1
                total_profit_pct += trade_pct
                if trade_pct > 0:
                    win_trades += 1
                exit_reason = "SAT"
                exit_price = bar_c
                exit_pct = trade_pct
                in_green_pos = in_orange_pos = be_locked = False
                entry_price = tp_level = sl_level = None

        if i == n - 1:
            last_green_trig = green_trig
            last_orange_trig = orange_trig
            last_exit_trig = exit_trig
            last_exit_reason = exit_reason
            last_exit_price = exit_price
            last_exit_pct = exit_pct
            last_c1, last_c2, last_c3, last_c4, last_c5 = c1, c2, c3, c4, c5
            last_trend_ok = cond_trend
            last_first3, last_all5 = first3, all5
            last_fib_p, last_fib_r1, last_fib_r2, last_fib_s1 = (
                fib_p,
                fib_r1,
                fib_r2,
                fib_s1,
            )

    last_close = float(closes.iloc[-1])
    next_res = (
        tp_level
        if tp_level is not None
        else _target_res_1h_15m(
            last_close,
            last_res,
            last_fib_p,
            last_fib_r1,
            last_fib_r2,
            float(h20_s.iloc[-1]),
            min_tp_pct,
        )
    )
    prev_sup = (
        sl_level
        if sl_level is not None
        else _dynamic_stop_1h_15m(last_close, last_sup, last_fib_s1, active_stop_pct)
    )
    win_rate = (win_trades / total_trades) * 100.0 if total_trades > 0 else 0.0

    return SystemEvaluation(
        bar_open_time=int(open_times.iloc[-1]),
        price=last_close,
        cond1_buy=last_c1,
        cond2_buy=last_c2,
        cond3_buy=last_c3,
        cond4_buy=last_c4,
        cond5_buy=last_c5,
        trend_ok=last_trend_ok,
        first_three_buy=last_first3,
        all_five_buy=last_all5,
        green_trigger=last_green_trig,
        orange_trigger=last_orange_trig,
        exit_trigger=last_exit_trig,
        exit_reason=last_exit_reason,
        exit_price=last_exit_price,
        exit_trade_pct=last_exit_pct,
        in_green_pos=in_green_pos,
        in_orange_pos=in_orange_pos,
        be_locked=be_locked,
        entry_price=entry_price,
        tp_level=tp_level,
        sl_level=sl_level,
        next_res=next_res,
        prev_sup=prev_sup,
        total_trades=total_trades,
        win_trades=win_trades,
        win_rate=round(win_rate, 2),
        total_profit_pct=round(total_profit_pct, 2),
    )
