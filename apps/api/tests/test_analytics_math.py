"""Tests for the analytics maths.

These are pure-function tests over known P/L sequences. The point is to pin down
that the derived statistics behave correctly, including on the edge cases that
would otherwise produce a flattering-but-wrong number: a flat P/L series, a
strategy with no losses, and a curve that only ever loses.
"""

from __future__ import annotations

import math

from app.services.analytics import max_drawdown, sharpe_from_daily_pnl, streaks


class TestStreaks:
    def test_counts_longest_consecutive_runs(self):
        # W, W, L, L, L, W  ->  longest win run 2, longest loss run 3
        assert streaks([1.0, 2.0, -1.0, -2.0, -3.0, 5.0]) == (2, 3)

    def test_breakeven_breaks_both_runs(self):
        # A flat trade is neither a win nor a loss, so it must not extend a run
        # and must not join two runs into one longer run.
        assert streaks([1.0, 0.0, 1.0]) == (1, 0)
        assert streaks([-1.0, 0.0, -1.0]) == (0, 1)

    def test_empty_series(self):
        assert streaks([]) == (0, 0)

    def test_all_wins(self):
        assert streaks([1.0, 2.0, 3.0]) == (3, 0)


class TestMaxDrawdown:
    def test_drawdown_from_peak_to_trough(self):
        # Equity 0 -> 100 (peak) -> 60 (trough) -> 90.
        # Largest decline is 100 - 60 = 40, which is 40% of the 100 peak.
        absolute, percent = max_drawdown([0.0, 100.0, 60.0, 90.0])
        assert absolute == 40.0
        assert percent == 40.0

    def test_monotonic_rise_has_no_drawdown(self):
        assert max_drawdown([0.0, 10.0, 20.0, 30.0]) == (0.0, 0.0)

    def test_first_value_is_its_own_peak(self):
        # A series that opens negative has a negative peak, so the absolute
        # decline is still meaningful (30 units) but cannot be expressed as a
        # percentage of a positive peak, and must not report a negative one.
        absolute, percent = max_drawdown([-50.0, -80.0])
        assert absolute == 30.0
        assert percent == 0.0

    def test_drawdown_recovered_then_falls_again(self):
        # Peak 100, trough 70 (30% decline), recover to 120, fall to 90 (25%).
        # The worst decline is the 30% one, not the later 25% one.
        absolute, percent = max_drawdown([100.0, 70.0, 120.0, 90.0])
        assert absolute == 30.0
        assert percent == 30.0

    def test_empty_series(self):
        assert max_drawdown([]) == (0.0, 0.0)

    def test_monotonic_loss_still_measures_from_its_peak(self):
        # The series opens at 100 and only ever falls, but it still has a
        # positive peak to measure against, so the decline is fully defined.
        absolute, percent = max_drawdown([100.0, 50.0, 10.0])
        assert absolute == 90.0
        assert percent == 90.0

    def test_curve_that_never_rises_reports_no_percentage(self):
        # Every value is at or below zero, so "percent of peak" would divide by a
        # non-positive peak. Report 0.0 rather than an infinite or negative
        # percentage.
        absolute, percent = max_drawdown([-10.0, -40.0, -90.0])
        assert absolute == 80.0
        assert percent == 0.0


class TestSharpe:
    def test_positive_consistent_returns(self):
        # Constant positive daily P/L has zero variance, so the ratio is
        # undefined rather than infinite.
        assert sharpe_from_daily_pnl([10.0, 10.0, 10.0]) is None

    def test_single_observation_is_not_enough(self):
        assert sharpe_from_daily_pnl([10.0]) is None

    def test_empty_series(self):
        assert sharpe_from_daily_pnl([]) is None

    def test_flat_series_is_undefined(self):
        assert sharpe_from_daily_pnl([0.0, 0.0, 0.0, 0.0]) is None

    def test_mixed_series_is_positive(self):
        # More good days than bad, and a clear positive mean, must score above
        # zero. Exact value is checked against the definition below.
        daily = [10.0, -5.0, 12.0, -4.0, 8.0, 11.0, -3.0, 9.0]
        result = sharpe_from_daily_pnl(daily)
        assert result is not None and result > 0

    def test_matches_the_stated_definition(self):
        # Pins the formula: (mean / sample stdev) * sqrt(252).
        daily = [10.0, -5.0, 12.0, -4.0, 8.0, 11.0, -3.0, 9.0]
        n = len(daily)
        mean = sum(daily) / n
        stdev = math.sqrt(sum((x - mean) ** 2 for x in daily) / (n - 1))
        expected = round(mean / stdev * math.sqrt(252.0), 3)
        assert sharpe_from_daily_pnl(daily) == expected

    def test_mirrored_series_has_negated_sharpe(self):
        daily = [10.0, -5.0, 12.0, -4.0, 8.0, 11.0, -3.0, 9.0]
        mirrored = [-x for x in daily]
        assert sharpe_from_daily_pnl(mirrored) == -sharpe_from_daily_pnl(daily)

    def test_mostly_losing_series_is_negative(self):
        assert sharpe_from_daily_pnl([-10.0, 1.0, -12.0, 2.0, -8.0, -11.0, 1.0, -9.0]) < 0
