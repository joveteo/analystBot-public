from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from analystbot.core.config import Settings
from analystbot.db.date_utils import date_str_to_unix
from analystbot.services.market_charts import (
    MARKET_CHART_FILENAME,
    TREND_DOWN,
    TREND_UP,
    MarketChartConfig,
    MarketChartService,
    count_consecutive_trend_days,
    get_output_path,
    run,
)


def _settings(temp_dir: str) -> Settings:
    settings = Settings(_loaded=True)
    root = Path(temp_dir)
    settings.project_root = root
    settings.data_dir = root / "data"
    settings.db_path = settings.data_dir / "live_stocks.db"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _ohlc_rows(start: str = "2026-01-02", count: int = 20, trend: float = 1.0):
    start_ts = date_str_to_unix(start)
    rows = []
    price = 100.0
    for index in range(count):
        rows.append(
            {
                "date": start_ts + index * 86400,
                "high_price": price + 2,
                "low_price": price - 2,
                "close_price": price + 1,
            }
        )
        price += trend
    return rows


class TrendDayCountTests(unittest.TestCase):
    def test_counts_trailing_direction_and_skips_nones(self) -> None:
        self.assertEqual(
            count_consecutive_trend_days([None, 1, 1, -1, -1, -1]),
            (-1, 3),
        )
        self.assertEqual(count_consecutive_trend_days([None, None]), (None, 0))
        self.assertEqual(count_consecutive_trend_days([]), (None, 0))


class OutputPathTests(unittest.TestCase):
    def test_output_path_uses_chart_graphs_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertEqual(
                get_output_path(settings),
                settings.data_dir / "chart_graphs" / MARKET_CHART_FILENAME,
            )


class MarketChartServiceTests(unittest.TestCase):
    def test_empty_symbols_still_write_placeholder_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            service = MarketChartService(settings)
            repo = MagicMock()
            repo.get_latest_timestamp.return_value = None
            self.assertTrue(service.generate_chart(repo))
            self.assertTrue(service.output_path.is_file())

    def test_generate_chart_plots_supertrend_panels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            config = MarketChartConfig(atr_period=3, lookback_days=30)
            service = MarketChartService(settings, config)
            repo = MagicMock()
            rows = _ohlc_rows(count=15)
            repo.get_latest_timestamp.return_value = rows[-1]["date"]
            repo.get_stock_data_range.return_value = rows
            self.assertTrue(service.generate_chart(repo))
            self.assertGreater(service.output_path.stat().st_size, 1000)

    def test_incomplete_ohlc_uses_empty_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            service = MarketChartService(
                settings, MarketChartConfig(atr_period=3, symbols=("SPY", "QQQ", "IWM"))
            )
            repo = MagicMock()
            repo.get_latest_timestamp.return_value = date_str_to_unix("2026-01-05")
            repo.get_stock_data_range.return_value = [
                {
                    "date": date_str_to_unix("2026-01-05"),
                    "high_price": None,
                    "low_price": None,
                    "close_price": None,
                }
            ]
            self.assertTrue(service.generate_chart(repo))

    def test_compute_trend_summaries_omits_short_symbols(self) -> None:
        settings = Settings(_loaded=True)
        service = MarketChartService(settings, MarketChartConfig(atr_period=5))
        repo = MagicMock()

        def latest(symbol: str):
            return None if symbol == "IWM" else date_str_to_unix("2026-01-20")

        def rows(symbol: str, start_ts: int, end_ts: int):
            if symbol == "QQQ":
                return _ohlc_rows(count=3)
            return _ohlc_rows(count=20)

        repo.get_latest_timestamp.side_effect = latest
        repo.get_stock_data_range.side_effect = rows
        summaries = service.compute_trend_summaries(repo)
        self.assertIn("SPY", summaries)
        self.assertNotIn("QQQ", summaries)
        self.assertNotIn("IWM", summaries)
        self.assertIn(summaries["SPY"]["trend"], (TREND_UP, TREND_DOWN))
        self.assertGreaterEqual(summaries["SPY"]["days"], 1)
        self.assertIn("emoji", summaries["SPY"])


class MarketChartRunTests(unittest.TestCase):
    def test_run_fails_without_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertFalse(run(settings))

    def test_run_propagates_generate_chart_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.db_path.write_bytes(b"")
            with (
                patch("analystbot.services.market_charts.connect") as connect,
                patch.object(
                    MarketChartService, "generate_chart", return_value=True
                ) as generate,
            ):
                connect.return_value = MagicMock()
                self.assertTrue(run(settings))
            generate.assert_called_once()

    def test_run_contains_unexpected_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.db_path.write_bytes(b"")
            with patch(
                "analystbot.services.market_charts.connect",
                side_effect=RuntimeError("db"),
            ):
                self.assertFalse(run(settings))


if __name__ == "__main__":
    unittest.main()
