"""Unit tests for the 4H 5-indicator Pine Script scanner and Telegram notifier."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from crypto_4h_scanner.binance_scanner import Coin4hResult
from crypto_4h_scanner.binance_trader import BinanceTrader
from crypto_4h_scanner.indicators import (
    SystemEvaluation,
    compute_pivots,
    evaluate_45m_scalp_system,
    evaluate_4h_system,
    fisher_transform,
    resample_15m_to_45m,
    stochastic_momentum_index,
    stochastic_rsi,
    wavetrend,
    wilder_rsi,
)
from crypto_4h_scanner.telegram_notifier import TelegramNotifier, format_entry_message


def _make_ohlcv(closes: np.ndarray, start_ts: int = 1_700_000_000_000) -> pd.DataFrame:
    step_ms = 4 * 3600 * 1000
    n = len(closes)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = np.maximum(opens, closes) * 1.008
    lows = np.minimum(opens, closes) * 0.992
    open_times = [start_ts + i * step_ms for i in range(n)]
    close_times = [t + step_ms - 1 for t in open_times]
    return pd.DataFrame(
        {
            "open_time": open_times,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.full(n, 1000.0),
            "close_time": close_times,
        }
    )


class TestCrypto4hIndicators(unittest.TestCase):
    def test_five_indicators_and_state_machine_on_wave(self) -> None:
        x = np.linspace(0, 8 * np.pi, 220)
        closes = 100.0 + 15.0 * np.sin(x)
        df = _make_ohlcv(closes)

        rsi = wilder_rsi(df["close"], 14)
        self.assertTrue((rsi.dropna() >= 0.0).all() and (rsi.dropna() <= 100.0).all())

        k, d = stochastic_rsi(df["close"])
        self.assertTrue((k.dropna() >= 0.0).all() and (k.dropna() <= 100.0).all())
        self.assertTrue((d.dropna() >= 0.0).all() and (d.dropna() <= 100.0).all())

        fish, trig = fisher_transform(df["high"], df["low"], 9)
        self.assertEqual(len(fish), len(df))
        self.assertEqual(len(trig), len(df))

        wt1, wt2 = wavetrend(df["high"], df["low"], df["close"], 10, 21)
        self.assertTrue(wt1.iloc[40:].notna().all())
        self.assertTrue(wt2.iloc[45:].notna().all())

        smi, smi_sig, smi_ema9 = stochastic_momentum_index(
            df["high"], df["low"], df["close"]
        )
        self.assertTrue(smi.iloc[30:].notna().all())
        self.assertTrue(smi_sig.iloc[30:].notna().all())
        self.assertTrue(smi_ema9.iloc[30:].notna().all())

        # Monthly candle before the 4h series
        df_1m = pd.DataFrame(
            {
                "open_time": [1_695_000_000_000],
                "high": [120.0],
                "low": [80.0],
                "close": [105.0],
                "close_time": [1_699_999_999_999],
            }
        )

        seen_green = False
        seen_orange = False
        seen_exit = False
        for end in range(80, len(df) + 1):
            ev = evaluate_4h_system(df.iloc[:end].copy(), df_1m=df_1m, ema200_filter=False)
            self.assertIsNotNone(ev)
            assert ev is not None
            seen_green = seen_green or ev.green_trigger
            seen_orange = seen_orange or ev.orange_trigger
            seen_exit = seen_exit or ev.exit_trigger

        self.assertTrue(seen_green, "Expected at least one green trigger")
        self.assertTrue(seen_orange, "Expected at least one orange trigger")
        self.assertTrue(seen_exit, "Expected at least one exit trigger")

    def test_pivots_confirm_after_right_bars(self) -> None:
        highs = pd.Series([10.0] * 7 + [20.0] + [10.0] * 7)
        lows = pd.Series([5.0] * 15)
        piv_h, _ = compute_pivots(highs, lows, left=7, right=7)
        self.assertTrue(np.isnan(piv_h[:14]).all())
        self.assertEqual(piv_h[14], 20.0)

    def test_45m_resample_and_scalp_system(self) -> None:
        step_ms = 15 * 60 * 1000
        start_ts = (1_700_000_000_000 // (45 * 60 * 1000)) * (45 * 60 * 1000)
        n = 300
        x = np.linspace(0, 10 * np.pi, n)
        closes = 100.0 + 5.0 * np.sin(x) + np.linspace(0, 4.0, n)
        opens = np.roll(closes, 1)
        opens[0] = closes[0]
        df_15m = pd.DataFrame(
            {
                "open_time": [start_ts + i * step_ms for i in range(n)],
                "open": opens,
                "high": np.maximum(opens, closes) * 1.003,
                "low": np.minimum(opens, closes) * 0.997,
                "close": closes,
                "volume": np.full(n, 100.0),
                "close_time": [start_ts + (i + 1) * step_ms - 1 for i in range(n)],
            }
        )
        df_45m = resample_15m_to_45m(df_15m)
        self.assertEqual(len(df_45m), 100)
        ev = evaluate_45m_scalp_system(df_45m, df_15m)
        self.assertIsNotNone(ev)


class TestTelegramNotifier(unittest.TestCase):
    def test_deduplication_and_tracked_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "alert_state.json"
            notifier = TelegramNotifier("dummy_token", "dummy_chat", state_file)
            sent_messages: list[str] = []
            notifier.send_text = lambda text: sent_messages.append(text) or True  # type: ignore[method-assign]

            ev_entry = SystemEvaluation(
                bar_open_time=1_700_000_000_000,
                price=100.0,
                cond1_buy=True,
                cond2_buy=True,
                cond3_buy=True,
                cond4_buy=True,
                cond5_buy=True,
                trend_ok=True,
                first_three_buy=True,
                all_five_buy=True,
                green_trigger=True,
                orange_trigger=True,
                exit_trigger=False,
                exit_reason=None,
                exit_price=None,
                exit_trade_pct=None,
                in_green_pos=True,
                in_orange_pos=True,
                be_locked=False,
                entry_price=100.0,
                tp_level=105.0,
                sl_level=97.0,
                next_res=105.0,
                prev_sup=97.0,
                total_trades=10,
                win_trades=7,
                win_rate=70.0,
                total_profit_pct=18.5,
            )
            res = Coin4hResult(
                symbol="BTCUSDT",
                market="futures",
                price=100.0,
                change_24h=2.5,
                volume_24h_usdt=1_500_000_000.0,
                evaluation=ev_entry,
            )

            # First call sends ORANGE alert
            stats1 = notifier.process_results([res])
            self.assertEqual(stats1["orange"], 1)
            self.assertEqual(len(sent_messages), 1)
            self.assertIn("TURUNCU KESİN AL", format_entry_message(res, "ORANGE"))

            # Second call on same bar_open_time is deduplicated
            stats2 = notifier.process_results([res])
            self.assertEqual(stats2["orange"], 0)
            self.assertEqual(len(sent_messages), 1)

            # Exit on next bar for tracked symbol sends exit alert
            ev_exit = SystemEvaluation(
                **{
                    **ev_entry.__dict__,
                    "bar_open_time": 1_700_014_400_000,
                    "green_trigger": False,
                    "orange_trigger": False,
                    "exit_trigger": True,
                    "exit_reason": "KÂR AL (Direnç)",
                    "exit_price": 105.0,
                    "exit_trade_pct": 5.0,
                    "in_green_pos": False,
                    "in_orange_pos": False,
                }
            )
            res_exit = Coin4hResult(
                symbol="BTCUSDT",
                market="futures",
                price=105.0,
                change_24h=5.0,
                volume_24h_usdt=1_500_000_000.0,
                evaluation=ev_exit,
            )
            stats3 = notifier.process_results([res_exit])
            self.assertEqual(stats3["exit"], 1)
            self.assertEqual(len(sent_messages), 2)


class TestBinanceTrader(unittest.TestCase):
    def test_min_max_notional_and_max_three_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trader = BinanceTrader(
                api_key="k",
                api_secret="s",
                market="futures",
                min_usd=200.0,
                max_usd=300.0,
                max_positions=3,
                leverage=10,
                stop_loss_pct=1.0,
                trailing_trigger_pct=1.0,
                trailing_callback_pct=0.4,
                trailing_floor_pct=0.3,
                max_consecutive_stops=3,
                cooldown_minutes=240,
                state_file=Path(tmp) / "trade_state.json",
            )
            trader._filters_cache = {
                "BTCUSDT": {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0},
                "ETHUSDT": {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0},
                "SOLUSDT": {"stepSize": 0.1, "minQty": 0.1, "minNotional": 5.0},
                "XRPUSDT": {"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0},
            }
            trader.get_free_usdt = lambda: 2000.0  # type: ignore[method-assign]
            trader.get_live_open_positions = lambda: {}  # type: ignore[method-assign]

            def fake_signed(method: str, path: str, params=None):
                if path == "/fapi/v1/order":
                    return {
                        "orderId": 12345,
                        "avgPrice": "100.0",
                        "executedQty": params["quantity"],
                    }
                return {}

            trader._signed_request = fake_signed  # type: ignore[method-assign]

            # 1) Entry ($250)
            t1 = trader.open_position("BTCUSDT", price=100.0, target_usd=250.0)
            self.assertIsNotNone(t1)
            assert t1 is not None
            self.assertGreaterEqual(t1.notional_usd, 200.0)

            # 2) Entry ($300)
            t2 = trader.open_position("ETHUSDT", price=100.0, target_usd=300.0)
            self.assertIsNotNone(t2)
            assert t2 is not None
            self.assertLessEqual(t2.notional_usd, 300.0)

            # 3) Third position allowed
            t3 = trader.open_position("SOLUSDT", price=100.0, target_usd=300.0)
            self.assertIsNotNone(t3)

            # 4) Fourth position blocked by max_positions=3
            t4 = trader.open_position("XRPUSDT", price=100.0, target_usd=300.0)
            self.assertIsNone(t4)

            # 5) Closing one frees a slot
            closed = trader.close_position("BTCUSDT", fallback_price=105.0)
            self.assertIsNotNone(closed)
            t4_retry = trader.open_position("XRPUSDT", price=100.0, target_usd=250.0)
            self.assertIsNotNone(t4_retry)

    def test_three_dollar_sl_trailing_and_three_stop_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trader = BinanceTrader(
                api_key="k",
                api_secret="s",
                market="papi",
                min_usd=200.0,
                max_usd=300.0,
                max_positions=3,
                leverage=10,
                stop_loss_pct=1.0,
                trailing_trigger_pct=1.0,
                trailing_callback_pct=0.4,
                trailing_floor_pct=0.3,
                max_consecutive_stops=3,
                cooldown_minutes=240,
                state_file=Path(tmp) / "trade_state.json",
            )
            trader._filters_cache = {
                "AUSDT": {"stepSize": 0.1, "minQty": 0.1, "minNotional": 5.0},
                "BUSDT": {"stepSize": 0.1, "minQty": 0.1, "minNotional": 5.0},
                "CUSDT": {"stepSize": 0.1, "minQty": 0.1, "minNotional": 5.0},
                "DUSDT": {"stepSize": 0.1, "minQty": 0.1, "minNotional": 5.0},
            }
            trader.get_free_usdt = lambda: 500.0  # type: ignore[method-assign]
            trader.get_live_open_positions = lambda: {}  # type: ignore[method-assign]

            last_price = {"p": 100.0}

            def fake_signed(method: str, path: str, params=None):
                if path == "/papi/v1/um/order":
                    return {
                        "orderId": 999,
                        "avgPrice": str(last_price["p"]),
                        "executedQty": params["quantity"],
                    }
                return {}

            trader._signed_request = fake_signed  # type: ignore[method-assign]

            # Trailing stop test: open at 100, peak at 102 (+2%), pullback to 101.5 (< 102*0.996=101.592) -> closes in profit
            last_price["p"] = 100.0
            trader.open_position("AUSDT", price=100.0, target_usd=300.0)
            self.assertEqual(len(trader.manage_open_positions({"AUSDT": 102.0})), 0)
            self.assertTrue(trader._state["open_positions"]["AUSDT"]["trailing_active"])
            last_price["p"] = 101.5
            evs = trader.manage_open_positions({"AUSDT": 101.5})
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["reason"], "TRAILING STOP")
            self.assertGreater(evs[0]["pnl_usd"], 0)
            self.assertEqual(trader._state["consecutive_stops"], 0)

            # 3 consecutive -1% stops trigger cooldown (even with $22 balance via auto-leverage)
            trader.get_free_usdt = lambda: 22.0  # type: ignore[method-assign]
            for idx, sym in enumerate(("AUSDT", "BUSDT", "CUSDT"), start=1):
                last_price["p"] = 100.0
                opened = trader.open_position(sym, price=100.0, target_usd=300.0)
                self.assertIsNotNone(opened)
                last_price["p"] = 98.9  # -1.1% <= -1.0% SL
                stop_evs = trader.manage_open_positions({sym: 98.9})
                self.assertEqual(len(stop_evs), 1)
                self.assertIn("STOP", stop_evs[0]["reason"])
                self.assertEqual(trader._state["consecutive_stops"], idx)

            self.assertTrue(trader.is_in_cooldown())
            self.assertIsNone(trader.open_position("DUSDT", price=100.0, target_usd=250.0))

    def test_sync_live_positions_protects_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trader = BinanceTrader(
                api_key="k",
                api_secret="s",
                market="papi",
                stop_loss_pct=1.0,
                state_file=Path(tmp) / "trade_state.json",
            )
            # Simulate Railway restart: empty local state, but Binance has an open LONG
            trader.get_live_open_positions = lambda: {  # type: ignore[method-assign]
                "HUSDT": {"symbol": "HUSDT", "qty": 1000.0, "entry_price": 0.02, "side": "LONG"}
            }
            synced = trader.sync_live_positions()
            self.assertIn("HUSDT", synced)
            self.assertAlmostEqual(synced["HUSDT"]["sl_price"], 0.02 * 0.99)


if __name__ == "__main__":
    unittest.main()
