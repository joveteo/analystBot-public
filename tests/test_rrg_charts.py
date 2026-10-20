from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from analystbot.core.config import Settings
from analystbot.db.date_utils import date_str_to_unix
from analystbot.services.rrg_charts import (
    RrgChartConfig,
    RrgChartService,
    WeeklyDecision,
    classify_quadrant,
    compute_rrg_series,
    evaluate_weekly_decision,
    latest_sector_statuses,
    most_recent_friday,
    read_rrg_state,
    resolve_run_timezone,
    resolve_test_week_ending,
    run,
    run_forced,
    sector_label,
    write_rrg_state,
)


def _settings(temp_dir: str) -> Settings:
    settings = Settings(_loaded=True)
    root = Path(temp_dir)
    settings.project_root = root
    settings.data_dir = root / "data"
    settings.logs_dir = root / "logs"
    settings.db_path = settings.data_dir / "live_stocks.db"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _weekly_frame(week_ending: str, *, spy: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {"SPY": [spy]},
        index=pd.DatetimeIndex([pd.Timestamp(week_ending)]),
    )


class QuadrantAndLabelTests(unittest.TestCase):
    def test_quadrants_use_inclusive_100_boundary(self) -> None:
        self.assertEqual(classify_quadrant(100.0, 100.0), "Leading")
        self.assertEqual(classify_quadrant(101.0, 99.0), "Weakening")
        self.assertEqual(classify_quadrant(99.0, 101.0), "Improving")
        self.assertEqual(classify_quadrant(99.0, 99.0), "Lagging")

    def test_sector_label_falls_back_to_symbol(self) -> None:
        self.assertEqual(sector_label("XLK"), "Technology")
        self.assertEqual(sector_label("ZZZ"), "ZZZ")

    def test_latest_sector_statuses_skips_empty_tails(self) -> None:
        dates = pd.date_range("2026-01-02", periods=2, freq="W-FRI")
        tails = {
            "XLK": pd.DataFrame(
                {"rs_ratio": [101.0, 102.0], "rs_momentum": [99.0, 101.0]},
                index=dates,
            ),
            "XLF": pd.DataFrame(columns=["rs_ratio", "rs_momentum"]),
        }
        self.assertEqual(latest_sector_statuses(tails), {"XLK": "Leading"})


class FridayAndTimezoneTests(unittest.TestCase):
    def test_most_recent_friday_on_and_after_friday(self) -> None:
        self.assertEqual(most_recent_friday(date(2026, 10, 2)), date(2026, 10, 2))
        self.assertEqual(most_recent_friday(date(2026, 10, 5)), date(2026, 10, 2))
        self.assertEqual(most_recent_friday(date(2026, 10, 3)), date(2026, 10, 2))

    def test_resolve_run_timezone_falls_back_to_utc(self) -> None:
        settings = Settings(_loaded=True)
        settings.rrg_run_timezone = None
        self.assertEqual(resolve_run_timezone(settings), UTC)
        settings.rrg_run_timezone = "America/New_York"
        self.assertEqual(str(resolve_run_timezone(settings)), "America/New_York")
        settings.rrg_run_timezone = "Not/AZone"
        self.assertEqual(resolve_run_timezone(settings), UTC)


class RrgStateTests(unittest.TestCase):
    def test_write_and_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertIsNone(read_rrg_state(settings))
            write_rrg_state(settings, date(2026, 10, 2))
            self.assertEqual(read_rrg_state(settings), "2026-10-02")

    def test_invalid_or_malformed_state_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            path = settings.data_dir / "chart_graphs" / "rrg_state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not-json", encoding="utf-8")
            self.assertIsNone(read_rrg_state(settings))
            path.write_text(json.dumps(["oops"]), encoding="utf-8")
            self.assertIsNone(read_rrg_state(settings))
            path.write_text(json.dumps({"other": True}), encoding="utf-8")
            self.assertIsNone(read_rrg_state(settings))


class ComputeRrgSeriesTests(unittest.TestCase):
    def test_aligned_weekly_series_drops_warmup_nans(self) -> None:
        dates = pd.date_range("2024-01-05", periods=40, freq="W-FRI")
        spy = pd.Series([100.0 + i * 0.2 for i in range(40)], index=dates)
        xlk = pd.Series([110.0 + i * 0.5 for i in range(40)], index=dates)
        frame = compute_rrg_series(xlk, spy, window=4, roc_period=6)
        self.assertGreater(len(frame), 0)
        self.assertEqual(list(frame.columns), ["rs_ratio", "rs_momentum"])
        self.assertFalse(frame.isna().any().any())


class WeeklyFrameTests(unittest.TestCase):
    def test_build_weekly_frame_resamples_friday_closes(self) -> None:
        settings = Settings(_loaded=True)
        config = RrgChartConfig(sectors=("XLK",), fetch_rows=20)
        service = RrgChartService(settings, config)
        repo = MagicMock()
        repo.get_stock_data.side_effect = lambda symbol, days=20: [
            {
                "date": date_str_to_unix("2026-09-28"),
                "close_price": 10.0 if symbol == "SPY" else 20.0,
            },
            {
                "date": date_str_to_unix("2026-10-02"),
                "close_price": 11.0 if symbol == "SPY" else 21.0,
            },
            {
                "date": date_str_to_unix("2026-10-02"),
                "close_price": None,
            },
        ]
        frame = service.build_weekly_frame(repo)
        self.assertEqual(list(frame.columns), ["SPY", "XLK"])
        self.assertEqual(service.latest_week_ending(frame), date(2026, 10, 2))

    def test_latest_week_ending_requires_benchmark(self) -> None:
        settings = Settings(_loaded=True)
        service = RrgChartService(settings)
        self.assertIsNone(service.latest_week_ending(pd.DataFrame({"XLK": [1.0]})))
        self.assertIsNone(service.latest_week_ending(pd.DataFrame({"SPY": []})))

    def test_latest_week_ending_from_repo_empty(self) -> None:
        settings = Settings(_loaded=True)
        service = RrgChartService(settings)
        repo = MagicMock()
        repo.get_stock_data.return_value = [{"date": 1, "close_price": None}]
        self.assertIsNone(service.latest_week_ending_from_repo(repo))
        self.assertIsNone(resolve_test_week_ending(settings, repo))


class WeeklyDecisionTests(unittest.TestCase):
    def test_weekday_is_not_saturday(self) -> None:
        settings = Settings(_loaded=True)
        now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        self.assertEqual(
            evaluate_weekly_decision(settings, _weekly_frame("2026-10-02"), now).action,
            "not_saturday",
        )

    def test_missing_benchmark_is_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
            decision = evaluate_weekly_decision(settings, pd.DataFrame(), now)
            self.assertEqual(decision.action, "no_data")

    def test_stale_friday_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
            decision = evaluate_weekly_decision(
                settings, _weekly_frame("2026-09-25"), now
            )
            self.assertEqual(decision, WeeklyDecision("not_ready", date(2026, 9, 25)))

    def test_matching_state_is_already_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            write_rrg_state(settings, date(2026, 10, 2))
            now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
            decision = evaluate_weekly_decision(
                settings, _weekly_frame("2026-10-02"), now
            )
            self.assertEqual(
                decision, WeeklyDecision("already_sent", date(2026, 10, 2))
            )

    def test_saturday_with_fresh_friday_is_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
            decision = evaluate_weekly_decision(
                settings, _weekly_frame("2026-10-02"), now
            )
            self.assertEqual(decision, WeeklyDecision("due", date(2026, 10, 2)))


class RrgChartSmokeTests(unittest.TestCase):
    def test_generate_chart_writes_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            config = RrgChartConfig(
                sectors=("XLK",),
                smoothing_window=3,
                momentum_roc_period=2,
                tail_length=3,
            )
            service = RrgChartService(settings, config)
            dates = pd.date_range("2025-01-03", periods=20, freq="W-FRI")
            frame = pd.DataFrame(
                {
                    "SPY": [100.0 + i * 0.3 for i in range(20)],
                    "XLK": [110.0 + i * 0.8 for i in range(20)],
                },
                index=dates,
            )
            week_ending = date(dates[-1].year, dates[-1].month, dates[-1].day)
            self.assertTrue(service.generate_chart(frame, week_ending))
            self.assertTrue(service.output_path.is_file())
            self.assertGreater(service.output_path.stat().st_size, 1000)

    def test_generate_chart_fails_without_benchmark(self) -> None:
        settings = Settings(_loaded=True)
        service = RrgChartService(settings, RrgChartConfig(sectors=("XLK",)))
        self.assertFalse(
            service.generate_chart(pd.DataFrame({"XLK": [1.0]}), date(2026, 10, 2))
        )


class RrgRunTests(unittest.TestCase):
    def test_run_without_database_is_a_clean_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertTrue(run(settings))

    def test_run_forced_without_database_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            self.assertFalse(run_forced(settings))

    def test_run_skips_when_gate_is_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.db_path.write_bytes(b"")
            with (
                patch("analystbot.services.rrg_charts.connect") as connect,
                patch.object(
                    RrgChartService, "build_weekly_frame", return_value=pd.DataFrame()
                ),
                patch(
                    "analystbot.services.rrg_charts.evaluate_weekly_decision",
                    return_value=WeeklyDecision("not_saturday"),
                ),
            ):
                conn = MagicMock()
                connect.return_value = conn
                self.assertTrue(run(settings))
            conn.close.assert_called_once()

    def test_run_forced_fails_without_weekly_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings(temp_dir)
            settings.db_path.write_bytes(b"")
            with (
                patch("analystbot.services.rrg_charts.connect") as connect,
                patch.object(
                    RrgChartService, "build_weekly_frame", return_value=pd.DataFrame()
                ),
            ):
                connect.return_value = MagicMock()
                self.assertFalse(run_forced(settings))


if __name__ == "__main__":
    unittest.main()
