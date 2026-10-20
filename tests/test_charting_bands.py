from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from analystbot.core.config import Settings
from analystbot.services.charting import (
    ChartConfig,
    ChartingService,
    _threshold_y_limits,
    resolve_threshold_band,
)
from analystbot.services.fred_chart_catalog import FRED_PLOT_CONFIGS, VIX_PLOT_CONFIG


class ThresholdBandTests(unittest.TestCase):
    def test_boundaries_are_half_open(self) -> None:
        bands = VIX_PLOT_CONFIG.highlight_bands
        self.assertEqual(resolve_threshold_band(0.0, bands)[0], "#2dc937")
        self.assertEqual(resolve_threshold_band(19.99, bands)[0], "#2dc937")
        self.assertEqual(resolve_threshold_band(20.0, bands)[0], "#e7b416")
        self.assertEqual(resolve_threshold_band(29.99, bands)[0], "#e7b416")
        self.assertEqual(resolve_threshold_band(30.0, bands)[0], "#cc3232")
        self.assertEqual(resolve_threshold_band(80.0, bands)[0], "#cc3232")

    def test_y_limits_keep_red_zone_visible(self) -> None:
        y_min, y_max = _threshold_y_limits(
            [12.0, 18.0], VIX_PLOT_CONFIG.highlight_bands
        )
        self.assertEqual(y_min, 0.0)
        self.assertGreater(y_max, 30.0)

    def test_stress_index_splits_background_at_zero(self) -> None:
        stress = next(c for c in FRED_PLOT_CONFIGS if c.series_id == "STLFSI4")
        bands = stress.highlight_bands
        self.assertEqual(
            resolve_threshold_band(-0.01, bands)[0],
            ChartConfig.NEGATIVE_FILL_GOOD_COLOR,
        )
        self.assertEqual(
            resolve_threshold_band(0.0, bands)[0], ChartConfig.ZERO_LINE_COLOR
        )

    def test_stress_limits_keep_both_sides_of_zero(self) -> None:
        stress = next(c for c in FRED_PLOT_CONFIGS if c.series_id == "STLFSI4")
        y_min, y_max = _threshold_y_limits([0.4, 1.1], stress.highlight_bands)
        self.assertLess(y_min, 0.0)
        self.assertGreater(y_max, 1.1)


class ChartDrawOrderTests(unittest.TestCase):
    def test_bands_then_recessions_then_line(self) -> None:
        order: list[str] = []
        recessions = [(datetime(2020, 2, 1), datetime(2020, 4, 1))]
        data = [("2020-01-01", 15.0), ("2020-03-01", 40.0), ("2020-06-01", 25.0)]

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            service = ChartingService(settings)

            def capture_bands(*_args, **_kwargs) -> None:
                order.append("bands")

            def capture_recessions(*_args, **_kwargs) -> None:
                order.append("recessions")

            with (
                patch(
                    "analystbot.services.charting._apply_threshold_background",
                    side_effect=capture_bands,
                ),
                patch.object(
                    ChartingService,
                    "_apply_recession_shading",
                    side_effect=capture_recessions,
                ),
            ):
                self.assertTrue(
                    service.generate_chart(data, VIX_PLOT_CONFIG, recessions)
                )
            self.assertTrue((Path(temp_dir) / "fred_graphs" / "vix.png").exists())

        self.assertEqual(order, ["bands", "recessions"])
