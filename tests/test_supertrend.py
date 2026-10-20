from __future__ import annotations

import unittest

from analystbot.core.indicators import compute_supertrend
from analystbot.core.indicators.supertrend import (
    _calculate_rma,
    _calculate_true_range,
)


class ComputeSupertrendTests(unittest.TestCase):
    def test_rejects_mismatched_series_lengths(self) -> None:
        with self.assertRaises(ValueError):
            compute_supertrend([1.0, 2.0], [1.0], [1.0, 2.0])

    def test_rejects_invalid_period_and_factor(self) -> None:
        with self.assertRaises(ValueError):
            compute_supertrend([1.0], [1.0], [1.0], atr_period=0)
        with self.assertRaises(ValueError):
            compute_supertrend([1.0], [1.0], [1.0], factor=0)

    def test_empty_series_returns_empty_lists(self) -> None:
        self.assertEqual(compute_supertrend([], [], []), ([], []))

    def test_warmup_bars_are_none_before_atr_seed(self) -> None:
        high = [10.0, 11.0, 12.0]
        low = [9.0, 10.0, 11.0]
        close = [9.5, 10.5, 11.5]
        supertrend, direction = compute_supertrend(high, low, close, atr_period=3)

        self.assertEqual(supertrend[:2], [None, None])
        self.assertEqual(direction[:2], [None, None])
        self.assertIsNotNone(supertrend[2])
        self.assertEqual(direction[2], 1)

    def test_first_seeded_bar_starts_in_downtrend_on_upper_band(self) -> None:
        high = [12.0, 12.5]
        low = [10.0, 10.5]
        close = [11.0, 11.2]
        supertrend, direction = compute_supertrend(
            high, low, close, atr_period=1, factor=1.0
        )

        self.assertEqual(direction[0], 1)
        self.assertEqual(supertrend[0], 13.0)

    def test_close_above_upper_band_flips_to_uptrend(self) -> None:
        high = [12.0, 16.0, 16.5]
        low = [10.0, 14.0, 15.0]
        close = [11.0, 15.5, 16.0]
        _supertrend, direction = compute_supertrend(
            high, low, close, atr_period=1, factor=1.0
        )

        self.assertEqual(direction[0], 1)
        self.assertEqual(direction[1], -1)
        self.assertEqual(direction[2], -1)

    def test_close_below_lower_band_flips_back_to_downtrend(self) -> None:
        high = [12.0, 16.0, 16.5, 10.0]
        low = [10.0, 14.0, 15.0, 7.0]
        close = [11.0, 15.5, 16.0, 8.0]
        supertrend, direction = compute_supertrend(
            high, low, close, atr_period=1, factor=1.0
        )

        self.assertEqual(direction[1], -1)
        self.assertEqual(direction[-1], 1)
        self.assertIsNotNone(supertrend[-1])

    def test_upper_band_only_tightens_until_close_pierces(self) -> None:
        high = [12.0, 12.2, 12.1]
        low = [10.0, 10.1, 10.05]
        close = [11.0, 11.0, 11.0]
        supertrend, direction = compute_supertrend(
            high, low, close, atr_period=1, factor=1.0
        )

        self.assertTrue(all(value == 1 for value in direction))
        self.assertLessEqual(supertrend[2], supertrend[1])


class TrueRangeAndRmaTests(unittest.TestCase):
    def test_first_true_range_is_high_minus_low(self) -> None:
        self.assertEqual(
            _calculate_true_range([12.0, 13.0], [10.0, 9.0], [11.0, 12.0]),
            [2.0, 4.0],
        )

    def test_rma_is_none_until_period_is_filled(self) -> None:
        self.assertEqual(_calculate_rma([1.0, 2.0], 3), [None, None])

    def test_rma_seeds_with_sma_then_wilder_smooths(self) -> None:
        result = _calculate_rma([2.0, 4.0, 6.0, 8.0], 3)
        self.assertEqual(result[0], None)
        self.assertEqual(result[1], None)
        self.assertEqual(result[2], 4.0)
        self.assertAlmostEqual(result[3] or 0.0, (4.0 * 2 + 8.0) / 3)


if __name__ == "__main__":
    unittest.main()
