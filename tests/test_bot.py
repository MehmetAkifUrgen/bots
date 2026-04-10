import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot import ActivePosition, Setup, StrategyStats, evaluate_position_exit, load_config, select_best_ready_setup


class BotLogicTests(unittest.TestCase):
    def test_load_config_enforces_minimum_scan_interval(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SCAN_EVERY_SECONDS": "60",
                "MIN_SCAN_INTERVAL_SECONDS": "900",
            },
            clear=False,
        ):
            cfg = load_config()
        self.assertEqual(cfg.scan_every_seconds, 900)
        self.assertEqual(cfg.top_gainers_limit, 0)
        self.assertEqual(cfg.max_position_hold_hours, 0)

    def test_select_best_ready_setup_prefers_better_historical_strategy(self) -> None:
        long_setup = Setup(
            symbol="AAAUSDT",
            decision="LONG",
            setup_type="trend continuation",
            strategy_key="long::trend continuation",
            confidence=80,
            price_change_pct=9.0,
            funding_rate_pct=0.01,
            entry_low=10.0,
            entry_high=10.2,
            stop_loss=9.6,
            target_1=10.8,
            target_2=11.2,
            ready=True,
            invalidation="x",
            summary="x",
        )
        short_setup = Setup(
            symbol="BBBUSDT",
            decision="SHORT",
            setup_type="exhaustion fade",
            strategy_key="short::exhaustion fade",
            confidence=78,
            price_change_pct=18.0,
            funding_rate_pct=0.04,
            entry_low=5.0,
            entry_high=5.1,
            stop_loss=5.4,
            target_1=4.6,
            target_2=4.3,
            ready=True,
            invalidation="x",
            summary="x",
        )
        strategy_stats = {
            "short::exhaustion fade": StrategyStats(
                strategy_key="short::exhaustion fade",
                closed_trades=8,
                win_rate=0.75,
                avg_pnl_net=4.5,
                score_bonus=9.0,
            )
        }

        best_setup = select_best_ready_setup([long_setup, short_setup], 75, strategy_stats)
        self.assertIsNotNone(best_setup)
        self.assertEqual(best_setup.symbol, "BBBUSDT")

    def test_evaluate_position_exit_ignores_time_when_disabled(self) -> None:
        opened_at = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
        position = ActivePosition(
            symbol="AAAUSDT",
            side="LONG",
            setup_type="trend continuation",
            strategy_key="long::trend continuation",
            confidence=80,
            entry_price=10.0,
            stop_loss=9.0,
            take_profit=11.0,
            quantity=10.0,
            opened_at=opened_at.isoformat(),
            max_hold_until=None,
            price_change_pct=12.0,
        )

        exit_signal = evaluate_position_exit(
            position,
            current_price=10.1,
            now=opened_at,
        )
        self.assertIsNone(exit_signal)


if __name__ == "__main__":
    unittest.main()