from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from analystbot.api.fred import FredSeriesConfig
from analystbot.core.config import Settings
from analystbot.services.fred_data import (
    FredDataService,
    read_fred_release_ids,
    read_fred_update_state,
    refresh_fred_release_calendar,
    run,
    write_fred_release_ids,
    write_fred_update_state,
)


def _settings(temp_dir: str) -> Settings:
    settings = Settings(_loaded=True)
    root = Path(temp_dir)
    settings.project_root = root
    settings.data_dir = root / "data"
    settings.db_path = settings.data_dir / "live_stocks.db"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


class FredUpdateStateTests(unittest.TestCase):
    def test_write_and_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertIsNone(read_fred_update_state(settings))
            write_fred_update_state(settings, ["CPIAUCSL"], ["CPIAUCSL", "UNRATE"])
            state = read_fred_update_state(settings)
            assert state is not None
            self.assertEqual(state["updated_series_ids"], ["CPIAUCSL"])
            self.assertEqual(state["processed_series_ids"], ["CPIAUCSL", "UNRATE"])

    def test_invalid_state_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            path = settings.data_dir / "fred_update_state.json"
            path.write_text("{nope", encoding="utf-8")
            self.assertIsNone(read_fred_update_state(settings))
            path.write_text(json.dumps(["list"]), encoding="utf-8")
            self.assertIsNone(read_fred_update_state(settings))
            path.write_text(json.dumps({"updated_series_ids": ["X"]}), encoding="utf-8")
            self.assertIsNone(read_fred_update_state(settings))


class FredReleaseIdCacheTests(unittest.TestCase):
    def test_nested_and_invalid_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertEqual(read_fred_release_ids(settings), {})
            write_fred_release_ids(settings, {"UNRATE": 50})
            self.assertEqual(read_fred_release_ids(settings), {"UNRATE": 50})
            path = settings.data_dir / "fred_release_ids.json"
            path.write_text(
                json.dumps({"series_release_ids": {"UNRATE": 50, "BAD": "x"}}),
                encoding="utf-8",
            )
            self.assertEqual(read_fred_release_ids(settings), {"UNRATE": 50})
            path.write_text("{nope", encoding="utf-8")
            self.assertEqual(read_fred_release_ids(settings), {})
            path.write_text(json.dumps(["oops"]), encoding="utf-8")
            self.assertEqual(read_fred_release_ids(settings), {})


class RefreshCalendarTests(unittest.TestCase):
    def test_fetch_failures_leave_calendar_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            client = MagicMock()
            client.fetch_series_release_id.side_effect = RuntimeError("api")
            refresh_fred_release_calendar(settings, client, ["UNRATE", "SPAM"])
            self.assertFalse(
                (settings.data_dir / "fred_release_calendar.json").exists()
            )

    def test_new_release_ids_are_cached_and_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            client = MagicMock()
            client.fetch_series_release_id.return_value = 50
            client.fetch_next_release_date.return_value = "2099-01-15"
            refresh_fred_release_calendar(settings, client, ["UNRATE"])
            self.assertEqual(read_fred_release_ids(settings), {"UNRATE": 50})
            calendar_path = settings.data_dir / "fred_release_calendar.json"
            self.assertTrue(calendar_path.is_file())


class UpdateSeriesTests(unittest.TestCase):
    def test_fetch_none_or_empty_returns_zeros(self) -> None:
        service = FredDataService(Settings(_loaded=True))
        repo = MagicMock()
        repo.get_latest_observation.return_value = None
        client = MagicMock()
        config = FredSeriesConfig("UNRATE", "m", "Percent", "Unemployment")
        client.fetch_series_observations.return_value = None
        self.assertEqual(service.update_series(config, client, repo), (0, 0, 0, 0))
        client.fetch_series_observations.return_value = []
        self.assertEqual(service.update_series(config, client, repo), (0, 0, 0, 0))

    def test_monthly_series_uses_changed_upsert(self) -> None:
        service = FredDataService(Settings(_loaded=True))
        repo = MagicMock()
        repo.get_latest_observation.return_value = ("2026-01-01", 4.0)
        repo.get_observations_since.return_value = {"2026-01-01": 4.0}
        repo.upsert_only_changed.return_value = (1, 2, 0)
        client = MagicMock()
        client.fetch_series_observations.return_value = [
            {"date": "2026-02-01", "value": 4.1}
        ]
        config = FredSeriesConfig("UNRATE", "m", "Percent", "Unemployment")
        self.assertEqual(service.update_series(config, client, repo), (1, 1, 2, 0))
        repo.upsert_only_changed.assert_called_once()

    def test_daily_series_also_uses_changed_upsert(self) -> None:
        service = FredDataService(Settings(_loaded=True))
        repo = MagicMock()
        repo.get_latest_observation.return_value = ("2026-01-01", 20.0)
        repo.get_observations_since.return_value = {"2026-01-01": 20.0}
        repo.upsert_only_changed.return_value = (0, 1, 0)
        client = MagicMock()
        client.fetch_series_observations.return_value = [
            {"date": "2026-01-01", "value": 20.0}
        ]
        config = FredSeriesConfig("VIXCLS", "d", "Index", "VIX")
        self.assertEqual(service.update_series(config, client, repo), (1, 0, 1, 0))


class FredDataRunTests(unittest.TestCase):
    def test_missing_api_key_is_non_critical_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.fred_api_key = None
            self.assertTrue(run(settings))
            state = read_fred_update_state(settings)
            assert state is not None
            self.assertEqual(state["updated_series_ids"], [])

    def test_force_full_deletes_then_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.fred_api_key = "key"
            series = [
                FredSeriesConfig("UNRATE", "m", "Percent", "Unemployment"),
                FredSeriesConfig("PAYEMS", "m", "Thousands", "Payrolls"),
            ]
            repo = MagicMock()
            repo.delete_series.return_value = 3
            client = MagicMock()
            with (
                patch("analystbot.services.fred_data.SessionManager.get_session"),
                patch("analystbot.services.fred_data.FredClient", return_value=client),
                patch("analystbot.services.fred_data.connect") as connect,
                patch(
                    "analystbot.services.fred_data.FredRepository", return_value=repo
                ),
                patch.object(
                    FredDataService,
                    "update_series",
                    side_effect=[(5, 2, 0, 0), (0, 0, 0, 0)],
                ),
                patch("analystbot.services.fred_data.refresh_fred_release_calendar"),
            ):
                connect.return_value = MagicMock()
                self.assertFalse(run(settings, series=series, force_full=True))
            self.assertEqual(repo.delete_series.call_count, 2)
            state = read_fred_update_state(settings)
            assert state is not None
            self.assertEqual(state["updated_series_ids"], ["UNRATE"])

    def test_run_contains_unexpected_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.fred_api_key = "key"
            with patch(
                "analystbot.services.fred_data.SessionManager.get_session",
                side_effect=RuntimeError("session"),
            ):
                self.assertFalse(run(settings, series=[]))


if __name__ == "__main__":
    unittest.main()
