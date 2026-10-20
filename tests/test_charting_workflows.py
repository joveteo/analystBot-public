from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analystbot.core.config import Settings
from analystbot.services.charting import (
    ChartingService,
    get_vix_chart_path,
    load_packaged_recessions,
    load_recessions,
    load_recessions_for_settings,
    resolve_threshold_band,
    run,
    run_forced,
)
from analystbot.services.fred_chart_catalog import SeriesPlotConfig
from analystbot.services.fred_data import write_fred_update_state


def _settings(temp_dir: str) -> Settings:
    settings = Settings(_loaded=True)
    root = Path(temp_dir)
    settings.project_root = root
    settings.data_dir = root / "data"
    settings.db_path = settings.data_dir / "live_stocks.db"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


class RecessionLoadTests(unittest.TestCase):
    def test_missing_or_invalid_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.json"
            self.assertEqual(load_recessions(missing), [])
            bad = Path(temp_dir) / "bad.json"
            bad.write_text("{nope", encoding="utf-8")
            self.assertEqual(load_recessions(bad), [])

    def test_payload_skips_junk_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recessions.json"
            path.write_text(
                json.dumps(
                    {
                        "recessions": [
                            {"peak": "2020-02-01", "trough": "2020-04-01"},
                            "skip",
                            {"peak": "nope", "trough": "2020-04-01"},
                            {"peak": "2020-02-01"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            recessions = load_recessions(path)
            self.assertEqual(len(recessions), 1)

    def test_packaged_recessions_are_non_empty(self) -> None:
        self.assertGreater(len(load_packaged_recessions()), 0)

    def test_settings_prefer_override_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            override = settings.data_dir / "recessions.json"
            override.write_text(
                json.dumps(
                    {"recessions": [{"peak": "2020-02-01", "trough": "2020-04-01"}]}
                ),
                encoding="utf-8",
            )
            self.assertEqual(len(load_recessions_for_settings(settings)), 1)


class ThresholdBandTests(unittest.TestCase):
    def test_no_matching_band_returns_none(self) -> None:
        self.assertIsNone(resolve_threshold_band(50.0, ((0.0, 10.0, "red", 0.2),)))
        self.assertEqual(
            resolve_threshold_band(5.0, ((0.0, 10.0, "red", 0.2),)),
            ("red", 0.2),
        )


class ChartingServiceTests(unittest.TestCase):
    def test_empty_or_all_none_data_fails(self) -> None:
        settings = Settings(_loaded=True)
        service = ChartingService(settings)
        config = SeriesPlotConfig(
            series_id="VIXCLS",
            title="VIX",
            ylabel="Index",
            filename="vix.png",
            description="VIX",
        )
        self.assertFalse(service.generate_chart([], config))
        self.assertFalse(service.generate_chart([("2026-01-01", None)], config))

    def test_generate_chart_writes_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            service = ChartingService(settings)
            config = SeriesPlotConfig(
                series_id="UNRATE",
                title="Unemployment",
                ylabel="Percent",
                filename="unrate.png",
                description="Unemployment",
                frequency="m",
            )
            data = [
                ("2024-01-01", 3.7),
                ("2025-01-01", 4.0),
                ("2026-01-01", 4.2),
            ]
            self.assertTrue(service.generate_chart(data, config))
            output = settings.data_dir / "fred_graphs" / "unrate.png"
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 1000)

    def test_vix_chart_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertEqual(
                get_vix_chart_path(settings),
                settings.data_dir / "fred_graphs" / "vix.png",
            )


class ChartingRunTests(unittest.TestCase):
    def test_run_fails_without_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertFalse(run(settings))

    def test_empty_update_state_is_success_with_nothing_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.db_path.write_bytes(b"")
            write_fred_update_state(settings, [], [])
            self.assertTrue(run(settings, plot_configs=[]))

    def test_run_forced_requires_configs_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            config = SeriesPlotConfig(
                series_id="VIXCLS",
                title="VIX",
                ylabel="Index",
                filename="vix.png",
                description="VIX",
            )
            self.assertFalse(run_forced(settings, [config]))
            settings.db_path.write_bytes(b"")
            self.assertFalse(run_forced(settings, []))

    def test_run_forced_generates_when_configs_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.db_path.write_bytes(b"")
            config = SeriesPlotConfig(
                series_id="VIXCLS",
                title="VIX",
                ylabel="Index",
                filename="vix.png",
                description="VIX",
            )
            with (
                patch(
                    "analystbot.services.charting._generate_configs",
                    return_value=(1, 0),
                ),
                patch(
                    "analystbot.services.charting.combined_configs_for_series",
                    return_value=[],
                ),
            ):
                self.assertTrue(run_forced(settings, [config]))


if __name__ == "__main__":
    unittest.main()
