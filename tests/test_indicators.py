from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from analystbot.core.config import Settings
from analystbot.services.indicators import IndicatorService, VixFixCalculator, run


class VixFixCalculatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calculator = VixFixCalculator()

    @staticmethod
    def _prices(count: int) -> pd.DataFrame:
        closes = [float(value) for value in range(1, count + 1)]
        return pd.DataFrame(
            {
                "close_price": closes,
                "high_price": [value + 1 for value in closes],
                "low_price": [value - 1 for value in closes],
            }
        )

    def test_sma_200_uses_latest_200_closes(self) -> None:
        data = self._prices(220)

        result = self.calculator.calculate_all_periods(data)

        self.assertEqual(result["sma_200"], 120.5)

    def test_sma_200_is_none_with_insufficient_closes(self) -> None:
        data = self._prices(199)

        self.assertIsNone(self.calculator.calculate_sma(data, 200))

    def test_btd_str_matches_known_22_day_window(self) -> None:
        prior = pd.DataFrame(
            {
                "close_price": [100.0] * 22,
                "high_price": [101.0] * 22,
                "low_price": [99.0] * 22,
            }
        )
        current = pd.DataFrame(
            [{"close_price": 1.0, "high_price": 110.0, "low_price": 90.0}]
        )

        btd, str_value = self.calculator.calculate_btd_str(
            pd.concat([prior, current], ignore_index=True),
            period=22,
        )

        self.assertEqual(btd, 10.0)
        self.assertEqual(str_value, -10.0)

    def test_btd_str_requires_one_current_row_after_lookback(self) -> None:
        btd, str_value = self.calculator.calculate_btd_str(
            self._prices(22),
            period=22,
        )

        self.assertIsNone(btd)
        self.assertIsNone(str_value)

    def test_btd_str_does_not_divide_by_non_positive_closes(self) -> None:
        prior = pd.DataFrame(
            {
                "close_price": [0.0] * 22,
                "high_price": [1.0] * 22,
                "low_price": [0.0] * 22,
            }
        )
        current = pd.DataFrame(
            [{"close_price": 5.0, "high_price": 6.0, "low_price": 4.0}]
        )

        result = self.calculator.calculate_btd_str(
            pd.concat([prior, current], ignore_index=True),
            period=22,
        )

        self.assertEqual(result, (None, None))

    def test_all_periods_returns_signal_and_sma_values(self) -> None:
        result = self.calculator.calculate_all_periods(self._prices(220))

        self.assertEqual(
            result,
            {
                "btd_22": 11.62,
                "str_22": 0.0,
                "sma_200": 120.5,
            },
        )


class IndicatorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = IndicatorService(Settings(_loaded=True))
        self.repo = MagicMock()

    def test_process_symbol_persists_calculated_signals(self) -> None:
        rows = VixFixCalculatorTests._prices(23).to_dict("records")
        self.repo.get_stock_data.return_value = rows
        self.repo.update_indicators.return_value = True

        self.assertTrue(self.service.process_symbol("AAPL", self.repo))

        self.repo.get_stock_data.assert_called_once_with(
            "AAPL",
            days=self.service.calculator.MIN_DATA_DAYS,
        )
        self.repo.update_indicators.assert_called_once_with(
            "AAPL",
            {"btd_22": 2300.0, "str_22": 0.0, "sma_200": None},
        )

    def test_process_symbol_rejects_missing_or_insufficient_history(self) -> None:
        for rows in ([], VixFixCalculatorTests._prices(22).to_dict("records")):
            with self.subTest(row_count=len(rows)):
                self.repo.reset_mock()
                self.repo.get_stock_data.return_value = rows

                self.assertFalse(self.service.process_symbol("AAPL", self.repo))
                self.repo.update_indicators.assert_not_called()

    def test_process_symbol_contains_repository_errors(self) -> None:
        self.repo.get_stock_data.side_effect = RuntimeError("database unavailable")

        self.assertFalse(self.service.process_symbol("AAPL", self.repo))
        self.repo.update_indicators.assert_not_called()

    def test_process_symbol_returns_false_when_persist_fails(self) -> None:
        self.repo.get_stock_data.return_value = VixFixCalculatorTests._prices(
            23
        ).to_dict("records")
        self.repo.update_indicators.return_value = False
        self.assertFalse(self.service.process_symbol("AAPL", self.repo))


class IndicatorRunTests(unittest.TestCase):
    def test_run_fails_when_database_has_no_symbols(self) -> None:
        settings = Settings(_loaded=True)
        repo = MagicMock()
        repo.get_all_symbols.return_value = []
        conn = MagicMock()
        with (
            patch("analystbot.services.indicators.connect", return_value=conn),
            patch("analystbot.services.indicators.StockRepository", return_value=repo),
        ):
            self.assertFalse(run(settings))
        conn.close.assert_called_once()

    def test_run_specific_symbols_requires_eighty_percent_success(self) -> None:
        settings = Settings(_loaded=True)
        conn = MagicMock()
        with (
            patch("analystbot.services.indicators.connect", return_value=conn),
            patch("analystbot.services.indicators.StockRepository"),
            patch.object(
                IndicatorService,
                "process_symbol",
                side_effect=[True, False, True, True, True],
            ),
        ):
            self.assertTrue(
                run(settings, symbols=["aapl", "msft", "nvda", "spy", "qqq"])
            )

        with (
            patch("analystbot.services.indicators.connect", return_value=conn),
            patch("analystbot.services.indicators.StockRepository"),
            patch.object(
                IndicatorService,
                "process_symbol",
                side_effect=[True, False],
            ),
        ):
            self.assertFalse(run(settings, symbols=["aapl", "msft"]))

    def test_sma_rejects_non_finite_or_non_positive_means(self) -> None:
        calculator = VixFixCalculator()
        inf_df = pd.DataFrame({"close_price": [float("inf")] * 200})
        self.assertIsNone(calculator.calculate_sma(inf_df, 200))
        negative_df = pd.DataFrame({"close_price": [-1.0] * 200})
        self.assertIsNone(calculator.calculate_sma(negative_df, 200))


if __name__ == "__main__":
    unittest.main()
