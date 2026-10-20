from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from analystbot.services.rrg_charts import (
    RrgChartConfig,
    RrgChartService,
    format_rrg_heading,
)


class RrgTailLengthTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = SimpleNamespace(data_dir=Path("."))
        config = RrgChartConfig(benchmark="SPY", sectors=("XLK",))
        self.service = RrgChartService(settings, config)
        dates = pd.date_range("2026-01-02", periods=8, freq="W-FRI")
        self.frame = pd.DataFrame(
            {"SPY": range(100, 108), "XLK": range(200, 208)},
            index=dates,
        )

    def test_heading_is_title_weekly_friday_and_date(self) -> None:
        self.assertEqual(
            format_rrg_heading("2026-09-04"),
            "S&P 500 Sectors vs SPY RRG, Weekly Friday, 2026-09-04",
        )

    @staticmethod
    def _track(point_count: int) -> pd.DataFrame:
        dates = pd.date_range("2026-01-02", periods=point_count, freq="W-FRI")
        return pd.DataFrame(
            {
                "rs_ratio": range(100, 100 + point_count),
                "rs_momentum": range(99, 99 + point_count),
            },
            index=dates,
        )

    def test_long_track_is_capped_at_five_points(self) -> None:
        with patch(
            "analystbot.services.rrg_charts.compute_rrg_series",
            return_value=self._track(8),
        ):
            tails = self.service._build_sector_tails(self.frame)

        self.assertEqual(self.service.config.tail_length, 5)
        self.assertEqual(len(tails["XLK"]), 5)

    def test_short_track_is_preserved(self) -> None:
        with patch(
            "analystbot.services.rrg_charts.compute_rrg_series",
            return_value=self._track(3),
        ):
            tails = self.service._build_sector_tails(self.frame)

        self.assertEqual(len(tails["XLK"]), 3)


if __name__ == "__main__":
    unittest.main()
