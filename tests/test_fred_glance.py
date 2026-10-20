from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from analystbot.__main__ import _parse_args
from analystbot.api.fred import (
    DEFAULT_FRED_SERIES,
    FredClient,
    FredConfig,
    FredSeriesConfig,
    get_date_years_ago,
)
from analystbot.core.config import Settings
from analystbot.pipeline.stages import build_forced_chart_stages
from analystbot.services.charting import (
    ChartConfig,
    ChartingService,
    _clip_to_fred_history,
    _plot_xy,
    load_recessions_for_settings,
)
from analystbot.services.fred_chart_catalog import (
    FRED_COMBINED_PLOT_CONFIGS,
    FRED_FORCE_FLAG_SPECS,
    FRED_PLOT_CONFIGS,
    JOBS_COMBINED_ID,
    PCE_INFLATION_COMBINED_ID,
    RATES_COMBINED_ID,
    VIX_PLOT_CONFIG,
    combined_configs_for_series,
    plot_config_for,
    select_fred_chart_work,
)
from analystbot.services.fred_data import (
    FredDataService,
    align_observation_start,
    read_fred_release_calendar,
    refresh_fred_release_calendar,
    upcoming_release_date,
)
from analystbot.services.notifications.delivery import (
    NotificationService,
    _caption_next_releases,
)
from analystbot.services.notifications.formatters import format_next_release_date


class FakeObservationResponse:
    ok = True

    def json(self) -> dict[str, object]:
        return {"observations": [{"date": "2020-01-01", "value": "1.2"}]}


class FredUnitsAndAlignmentTests(unittest.TestCase):
    def test_default_series_use_glance_transforms(self) -> None:
        by_id = {series.series_id: series for series in DEFAULT_FRED_SERIES}
        self.assertEqual(by_id["GDPC1"].fred_units, "pch")
        self.assertEqual(by_id["PCECC96"].fred_units, "pch")
        self.assertEqual(by_id["CPIAUCSL"].fred_units, "pc1")
        self.assertEqual(by_id["PCEPI"].fred_units, "pc1")
        self.assertEqual(by_id["PAYEMS"].fred_units, "lin")
        self.assertEqual(by_id["GDPC1"].frequency, "q")
        self.assertEqual(by_id["STLFSI4"].frequency, "w")

    def test_fetch_includes_configured_units(self) -> None:
        client = FredClient("test-api-key")
        fake_http = MagicMock()
        fake_http.get.return_value = FakeObservationResponse()
        client._http = fake_http

        rows = client.fetch_series_observations(
            "GDPC1", observation_start="2006-01-01", units="pch"
        )

        params = fake_http.get.call_args.kwargs["params"]
        self.assertEqual(params["units"], "pch")
        self.assertEqual(params["series_id"], "GDPC1")
        assert rows is not None
        self.assertEqual(rows[0]["value"], 1.2)

    def test_fetch_defaults_to_lin_units(self) -> None:
        client = FredClient("test-api-key")
        fake_http = MagicMock()
        fake_http.get.return_value = FakeObservationResponse()
        client._http = fake_http

        client.fetch_series_observations("UNRATE", observation_start="2006-01-01")
        params = fake_http.get.call_args.kwargs["params"]
        self.assertEqual(params["units"], "lin")

    def test_fetch_defaults_to_ten_year_start(self) -> None:
        client = FredClient("test-api-key")
        fake_http = MagicMock()
        fake_http.get.return_value = FakeObservationResponse()
        client._http = fake_http

        client.fetch_series_observations("UNRATE")
        params = fake_http.get.call_args.kwargs["params"]
        self.assertEqual(
            params["observation_start"],
            get_date_years_ago(FredConfig.HISTORY_YEARS),
        )

    def test_quarterly_start_aligns_to_quarter(self) -> None:
        self.assertEqual(align_observation_start("2020-05-17", "q"), "2020-04-01")
        self.assertEqual(align_observation_start("2020-02-15", "q"), "2020-01-01")
        self.assertEqual(align_observation_start("2020-11-02", "q"), "2020-10-01")

    def test_monthly_and_daily_alignment(self) -> None:
        self.assertEqual(align_observation_start("2020-05-17", "m"), "2020-05-01")
        self.assertEqual(align_observation_start("2020-05-17", "d"), "2020-05-17")
        self.assertEqual(align_observation_start("2020-05-17", "w"), "2020-05-17")


class FredChartSelectionTests(unittest.TestCase):
    def test_dgs10_update_sends_rates_combined_only(self) -> None:
        selection = select_fred_chart_work({"DGS10"})
        self.assertEqual(
            {config.series_id for config in selection.generate_individuals},
            {"DGS10"},
        )
        self.assertEqual(
            {config.combined_id for config in selection.generate_combined},
            {RATES_COMBINED_ID},
        )
        self.assertEqual(selection.send_individuals, ())
        self.assertEqual(
            {config.combined_id for config in selection.send_combined},
            {RATES_COMBINED_ID},
        )

    def test_both_rates_updates_send_combined_only(self) -> None:
        selection = select_fred_chart_work({"DGS10", "T10Y2Y"})
        generated = [config.series_id for config in selection.generate_individuals]
        self.assertEqual(generated, ["DGS10", "T10Y2Y"])
        self.assertEqual(selection.send_individuals, ())
        self.assertEqual(
            {config.combined_id for config in selection.send_combined},
            {RATES_COMBINED_ID},
        )

    def test_unrate_update_sends_jobs_combined_only(self) -> None:
        selection = select_fred_chart_work({"UNRATE"})
        generated = {config.series_id for config in selection.generate_individuals}
        self.assertEqual(generated, {"UNRATE"})
        self.assertEqual(
            {config.combined_id for config in selection.send_combined},
            {JOBS_COMBINED_ID},
        )
        self.assertEqual(selection.send_individuals, ())

    def test_gdpc1_update_sends_individual(self) -> None:
        selection = select_fred_chart_work({"GDPC1"})
        self.assertEqual(
            {config.series_id for config in selection.send_individuals},
            {"GDPC1"},
        )
        self.assertEqual(selection.send_combined, ())

    def test_pce_update_sends_inflation_combined_only(self) -> None:
        selection = select_fred_chart_work({"PCECC96"})
        self.assertEqual(
            {config.series_id for config in selection.generate_individuals},
            {"PCECC96"},
        )
        self.assertEqual(
            {config.combined_id for config in selection.send_combined},
            {PCE_INFLATION_COMBINED_ID},
        )
        self.assertEqual(selection.send_individuals, ())

    def test_force_helpers_map_series_to_related_combined(self) -> None:
        jobs = combined_configs_for_series(["UNRATE"])
        self.assertEqual([config.combined_id for config in jobs], [JOBS_COMBINED_ID])
        self.assertEqual(
            [config.combined_id for config in combined_configs_for_series(["T10Y2Y"])],
            [RATES_COMBINED_ID],
        )
        self.assertEqual(combined_configs_for_series(["VIXCLS"]), [])


class FredCombinedChartTests(unittest.TestCase):
    def test_rates_stacked_chart_uses_panel_titles(self) -> None:
        import matplotlib.pyplot as plt

        rates = next(
            config
            for config in FRED_COMBINED_PLOT_CONFIGS
            if config.combined_id == RATES_COMBINED_ID
        )
        series_data = {
            "DGS10": [("2020-01-02", 1.8), ("2020-06-01", 0.7)],
            "T10Y2Y": [("2020-01-02", 0.2), ("2020-06-01", -0.5)],
        }
        parsed = {}
        for spec in rates.series:
            dates = [
                datetime.strptime(row[0], "%Y-%m-%d")
                for row in series_data[spec.series_id]
            ]
            values = [row[1] for row in series_data[spec.series_id]]
            parsed[spec.series_id] = (dates, values)

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            service = ChartingService(settings)
            fig = service._draw_stacked_combined(parsed, rates, None, (12, 10))
            try:
                self.assertIsNone(fig._suptitle)
                self.assertEqual(len(fig.axes), 2)
                self.assertEqual(
                    fig.axes[0].get_title(),
                    "10-Year Treasury Rate, Daily, 2020-06-01",
                )
                self.assertEqual(
                    fig.axes[1].get_title(),
                    "10Y-2Y Spread, Daily, 2020-06-01",
                )
                self.assertIsNone(fig.axes[0].get_legend())
                self.assertIsNone(fig.axes[1].get_legend())
                dotted_zero = [
                    line
                    for line in fig.axes[1].lines
                    if line.get_linestyle() in {"--", "dotted", ":"}
                    and list(line.get_ydata()) == [0, 0]
                ]
                self.assertEqual(dotted_zero, [])
                self.assertEqual(
                    [t.get_text() for t in fig.axes[0].texts],
                    ["0.70%"],
                )
                self.assertEqual(
                    [t.get_text() for t in fig.axes[1].texts],
                    ["-0.50%"],
                )
                dotted_level = [
                    line
                    for line in fig.axes[0].lines
                    if line.get_linestyle() in {":", "dotted"}
                    and list(line.get_ydata()) == [0.7, 0.7]
                ]
                self.assertEqual(len(dotted_level), 1)
                spread_level = [
                    line
                    for line in fig.axes[1].lines
                    if line.get_linestyle() in {":", "dotted"}
                    and list(line.get_ydata()) == [-0.5, -0.5]
                ]
                self.assertEqual(len(spread_level), 1)
                dotted_drop = [
                    line
                    for line in fig.axes[0].lines
                    if line.get_linestyle() in {":", "dotted"}
                    and len(list(line.get_ydata())) == 2
                    and list(line.get_ydata())[0] != list(line.get_ydata())[-1]
                ]
                self.assertEqual(dotted_drop, [])
            finally:
                plt.close(fig)
            self.assertTrue(service.generate_combined_chart(series_data, rates))
            self.assertTrue(
                (Path(temp_dir) / "fred_graphs" / "dgs10_t10y2y.png").exists()
            )


def _latest(series_id: str) -> tuple[str, float] | None:
    values = {
        "UNRATE": ("2026-08-01", 4.1),
        "PAYEMS": ("2026-08-01", 159000.0),
        "GDPC1": ("2026-04-01", 0.52),
        "DGS10": ("2026-09-04", 4.2),
        "T10Y2Y": ("2026-09-04", 0.31),
        "HOUST": ("2026-08-01", 1300.0),
        "STLFSI4": ("2026-09-04", -0.4),
        "PCECC96": ("2026-04-01", 0.4),
        "CPIAUCSL": ("2026-08-01", 2.9),
        "PCEPI": ("2026-08-01", 2.6),
    }
    return values.get(series_id)


class FredTelegramGlanceTests(unittest.TestCase):
    def _settings(self, temp_dir: str) -> Settings:
        settings = Settings(
            _loaded=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_chat_fred_id="fred-topic",
        )
        settings.data_dir = Path(temp_dir)
        graphs = settings.data_dir / "fred_graphs"
        graphs.mkdir()
        return settings

    def _send(
        self,
        settings: Settings,
        *,
        updated: list[str],
        forced_series: list[str] | None = None,
    ):
        client = MagicMock()
        client.send_photo.return_value = True
        client.send_media_group.return_value = True
        fred_repo = MagicMock()
        fred_repo.get_latest_observation.side_effect = _latest
        with (
            patch(
                "analystbot.services.notifications.delivery.TelegramClient",
                return_value=client,
            ),
            patch(
                "analystbot.services.notifications.delivery.connect",
                return_value=MagicMock(),
            ),
            patch(
                "analystbot.services.notifications.delivery.FredRepository",
                return_value=fred_repo,
            ),
            patch(
                "analystbot.services.notifications.delivery.StockRepository",
            ),
            patch(
                "analystbot.services.notifications.delivery.read_fred_update_state",
                return_value={"updated_series_ids": updated},
            ),
        ):
            service = NotificationService(settings)
            if forced_series is None:
                self.assertTrue(service.send_fred_charts())
            else:
                self.assertTrue(service.send_fred_charts_forced(forced_series))
        return client.send_photo.call_args_list

    def test_unrate_update_sends_jobs_combined_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            for name in ("unrate.png", "payems.png", "unrate_payems.png"):
                (graphs / name).write_bytes(b"png")
            calls = self._send(settings, updated=["UNRATE"])

        self.assertEqual(len(calls), 1)
        kwargs = calls[0].kwargs
        self.assertEqual(kwargs["topic_id"], "fred-topic")
        self.assertTrue(kwargs["disable_notification"])
        self.assertEqual(Path(calls[0].args[0]).name, "unrate_payems.png")
        caption = kwargs["caption"]
        self.assertIn("Unemployment Rate", caption)
        self.assertIn("Nonfarm Payrolls", caption)
        self.assertIn("159,000", caption)
        self.assertNotIn("Share of labor force without work", caption)
        self.assertNotIn("Jobs on nonfarm payrolls", caption)
        self.assertNotIn("Unemployment rate with nonfarm payroll employment", caption)
        self.assertNotIn("Next release date:", caption)

    def test_gdpc1_update_sends_individual_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            (settings.data_dir / "fred_graphs" / "gdpc1.png").write_bytes(b"png")
            calls = self._send(settings, updated=["GDPC1"])

        self.assertEqual(len(calls), 1)
        kwargs = calls[0].kwargs
        self.assertEqual(kwargs["topic_id"], "fred-topic")
        self.assertTrue(kwargs["disable_notification"])
        self.assertEqual(Path(calls[0].args[0]).name, "gdpc1.png")
        caption = kwargs["caption"]
        self.assertIn("Real GDP Percent Change, Quarterly, 2026-04-01", caption)
        self.assertNotIn("Date:", caption)
        self.assertNotIn("Inflation-adjusted output. Up = expanding economy.", caption)
        self.assertNotIn("Real Gross Domestic Product, percent change", caption)
        self.assertNotIn("Next release date:", caption)

    def test_gdpc1_caption_includes_next_release_days(self) -> None:
        gdp_date = (datetime.now(UTC).date() + timedelta(days=13)).isoformat()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            (settings.data_dir / "fred_graphs" / "gdpc1.png").write_bytes(b"png")
            calendar_path = settings.data_dir / "fred_release_calendar.json"
            calendar_path.write_text(
                json.dumps({"next_release_dates": {"GDPC1": gdp_date}})
            )
            calls = self._send(settings, updated=["GDPC1"])

        caption = calls[0].kwargs["caption"]
        dated = format_next_release_date(gdp_date)
        self.assertIn(f"Next release date: {dated}", caption)
        self.assertNotIn("Inflation-adjusted output", caption)

    def test_dgs10_update_sends_rates_combined_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "dgs10_t10y2y.png").write_bytes(b"png")
            calls = self._send(settings, updated=["DGS10"])

        self.assertEqual(len(calls), 1)
        kwargs = calls[0].kwargs
        self.assertEqual(kwargs["topic_id"], "fred-topic")
        self.assertTrue(kwargs["disable_notification"])
        self.assertEqual(Path(calls[0].args[0]).name, "dgs10_t10y2y.png")
        caption = kwargs["caption"]
        self.assertIn("10-Year Treasury Rate", caption)
        self.assertIn("10Y-2Y Spread", caption)
        self.assertNotIn("inverted curve", caption)
        self.assertNotIn("Benchmark 10-year yield with the 10y–2y spread.", caption)

    def test_both_rates_updates_send_one_combined_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            for name in ("dgs10.png", "t10y2y.png", "dgs10_t10y2y.png"):
                (graphs / name).write_bytes(b"png")
            calls = self._send(settings, updated=["DGS10", "T10Y2Y"])

        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0].args[0]).name, "dgs10_t10y2y.png")
        self.assertTrue(calls[0].kwargs["disable_notification"])

    def test_jobs_and_rates_updates_send_one_album(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "unrate_payems.png").write_bytes(b"png")
            (graphs / "dgs10_t10y2y.png").write_bytes(b"png")
            client = MagicMock()
            client.send_photo.return_value = True
            client.send_media_group.return_value = True
            fred_repo = MagicMock()
            fred_repo.get_latest_observation.side_effect = _latest
            with (
                patch(
                    "analystbot.services.notifications.delivery.TelegramClient",
                    return_value=client,
                ),
                patch(
                    "analystbot.services.notifications.delivery.connect",
                    return_value=MagicMock(),
                ),
                patch(
                    "analystbot.services.notifications.delivery.FredRepository",
                    return_value=fred_repo,
                ),
                patch(
                    "analystbot.services.notifications.delivery.StockRepository",
                ),
                patch(
                    "analystbot.services.notifications.delivery.read_fred_update_state",
                    return_value={"updated_series_ids": ["UNRATE", "DGS10"]},
                ),
            ):
                self.assertTrue(NotificationService(settings).send_fred_charts())

        client.send_photo.assert_not_called()
        items = client.send_media_group.call_args.args[0]
        self.assertEqual(
            [path.name for path, _caption in items],
            ["unrate_payems.png", "dgs10_t10y2y.png"],
        )
        kwargs = client.send_media_group.call_args.kwargs
        self.assertEqual(kwargs["topic_id"], "fred-topic")
        self.assertTrue(kwargs["disable_notification"])
        self.assertIn("Unemployment Rate", items[0][1])
        self.assertIn("10-Year Treasury Rate", items[1][1])

    def test_forced_unemployment_sends_jobs_combined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "unrate.png").write_bytes(b"png")
            (graphs / "payems.png").write_bytes(b"png")
            (graphs / "unrate_payems.png").write_bytes(b"png")
            calls = self._send(
                settings,
                updated=[],
                forced_series=["UNRATE"],
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0].args[0]).name, "unrate_payems.png")
        self.assertTrue(calls[0].kwargs["disable_notification"])

    def test_forced_nonfarm_sends_jobs_combined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "unrate_payems.png").write_bytes(b"png")
            calls = self._send(
                settings,
                updated=[],
                forced_series=["PAYEMS"],
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0].args[0]).name, "unrate_payems.png")

    def test_forced_yield_spread_sends_rates_combined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "dgs10_t10y2y.png").write_bytes(b"png")
            calls = self._send(
                settings,
                updated=[],
                forced_series=["T10Y2Y"],
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0].args[0]).name, "dgs10_t10y2y.png")
        caption = calls[0].kwargs["caption"]
        self.assertIn("10-Year Treasury Rate", caption)
        self.assertIn("10Y-2Y Spread", caption)
        self.assertNotIn("inverted curve", caption)

    def test_forced_treasury_sends_rates_combined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "dgs10_t10y2y.png").write_bytes(b"png")
            calls = self._send(
                settings,
                updated=[],
                forced_series=["DGS10"],
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0].args[0]).name, "dgs10_t10y2y.png")
        self.assertIn("10-Year Treasury Rate", calls[0].kwargs["caption"])

    def test_jobs_caption_includes_shared_next_release_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "unrate_payems.png").write_bytes(b"png")
            calendar_path = settings.data_dir / "fred_release_calendar.json"
            calendar_path.write_text(
                json.dumps(
                    {
                        "next_release_dates": {
                            "UNRATE": "2026-11-06",
                            "PAYEMS": "2026-11-06",
                        }
                    }
                )
            )
            calls = self._send(settings, updated=["UNRATE"])

        caption = calls[0].kwargs["caption"]
        lines = caption.split("\n")
        self.assertEqual(
            lines[0],
            "Unemployment Rate and Nonfarm Payrolls, Monthly, 2026-08-01",
        )
        self.assertEqual(
            lines[1],
            f"Next release date: {format_next_release_date('2026-11-06')}",
        )
        self.assertIn("Unemployment Rate:", lines[2])
        self.assertIn("Nonfarm Payrolls:", lines[3])
        self.assertNotIn("Date:", caption)
        self.assertNotIn("Unemployment rate with nonfarm payroll employment", caption)
        self.assertNotIn("weaker labor market", caption)
        self.assertNotIn("hiring", caption)

    def test_inflation_caption_lists_distinct_next_release_dates(self) -> None:
        today = datetime.now(UTC).date()
        pce_date = (today + timedelta(days=13)).isoformat()
        cpi_date = (today + timedelta(days=5)).isoformat()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = self._settings(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            (graphs / "pcecc96_cpi_pcepi.png").write_bytes(b"png")
            calendar_path = settings.data_dir / "fred_release_calendar.json"
            calendar_path.write_text(
                json.dumps(
                    {
                        "next_release_dates": {
                            "PCECC96": pce_date,
                            "CPIAUCSL": cpi_date,
                            "PCEPI": pce_date,
                        }
                    }
                )
            )
            calls = self._send(settings, updated=["PCEPI"])

        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0].args[0]).name, "pcecc96_cpi_pcepi.png")
        caption = calls[0].kwargs["caption"]
        real_pce = format_next_release_date(pce_date)
        cpi = format_next_release_date(cpi_date)
        self.assertIn(f"Next release date (Real PCE): {real_pce}", caption)
        self.assertIn(f"Next release date (CPI): {cpi}", caption)
        self.assertIn(f"Next release date (PCE Price Index): {real_pce}", caption)
        self.assertNotIn(f"Next release date: {cpi_date}", caption)
        self.assertNotIn(f"Next release date: {pce_date}", caption)
        self.assertNotIn("Real PCE percent change with CPI and PCE inflation", caption)
        self.assertNotIn("Headline consumer inflation vs a year ago.", caption)
        self.assertNotIn("Fed's preferred inflation gauge vs a year ago.", caption)


class FredForceFlagTests(unittest.TestCase):
    def test_help_lists_new_fred_force_flags(self) -> None:
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(buf):
            _parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        help_text = buf.getvalue()
        for _dest, flag, _series, _help in FRED_FORCE_FLAG_SPECS:
            self.assertIn(flag, help_text)

    def test_parse_new_force_flags(self) -> None:
        args = _parse_args(["--gdp", "--nonfarm", "--stress"])
        self.assertTrue(args.gdp)
        self.assertTrue(args.nonfarm)
        self.assertTrue(args.stress)
        self.assertFalse(args.unemployment)

    def test_forced_unemployment_generates_both_jobs_individuals(self) -> None:
        stages = {
            stage.name: stage
            for stage in build_forced_chart_stages(fred_flags={"unemployment": True})
        }
        plot_configs = stages["FRED Graphs"].run_fn.keywords["plot_configs"]
        self.assertEqual(
            {config.series_id for config in plot_configs},
            {"UNRATE", "PAYEMS"},
        )
        self.assertEqual(
            set(stages["FRED Graphs"].worker_kwargs["series_ids"]),
            {"UNRATE", "PAYEMS"},
        )
        self.assertEqual(
            stages["FRED Telegram"].worker_kwargs["series_ids"],
            ["UNRATE"],
        )

    def test_forced_treasury_generates_both_rates_individuals(self) -> None:
        stages = {
            stage.name: stage
            for stage in build_forced_chart_stages(fred_flags={"treasury_10y": True})
        }
        plot_configs = stages["FRED Graphs"].run_fn.keywords["plot_configs"]
        self.assertEqual(
            {config.series_id for config in plot_configs},
            {"DGS10", "T10Y2Y"},
        )
        self.assertEqual(
            set(stages["FRED Graphs"].worker_kwargs["series_ids"]),
            {"DGS10", "T10Y2Y"},
        )
        self.assertEqual(
            stages["FRED Telegram"].worker_kwargs["series_ids"],
            ["DGS10"],
        )

    def test_forced_yield_spread_generates_both_rates_individuals(self) -> None:
        stages = {
            stage.name: stage
            for stage in build_forced_chart_stages(fred_flags={"yield_spread": True})
        }
        plot_configs = stages["FRED Graphs"].run_fn.keywords["plot_configs"]
        self.assertEqual(
            {config.series_id for config in plot_configs},
            {"DGS10", "T10Y2Y"},
        )
        self.assertEqual(
            stages["FRED Telegram"].worker_kwargs["series_ids"],
            ["T10Y2Y"],
        )


class FredChartStyleTests(unittest.TestCase):
    def test_gdp_uses_bars_payrolls_use_line(self) -> None:
        by_id = {config.series_id: config for config in FRED_PLOT_CONFIGS}
        self.assertEqual(by_id["GDPC1"].kind, "bar")
        self.assertFalse(by_id["GDPC1"].show_zero_line)
        self.assertEqual(by_id["PAYEMS"].kind, "line")
        self.assertEqual(by_id["UNRATE"].kind, "line")
        jobs = next(
            config
            for config in FRED_COMBINED_PLOT_CONFIGS
            if config.combined_id == JOBS_COMBINED_ID
        )
        self.assertEqual(plot_config_for(jobs.series[0].series_id).kind, "line")
        self.assertEqual(plot_config_for(jobs.series[1].series_id).kind, "line")
        rates = next(
            config
            for config in FRED_COMBINED_PLOT_CONFIGS
            if config.combined_id == RATES_COMBINED_ID
        )
        self.assertEqual([spec.panel for spec in rates.series], [0, 1])
        self.assertEqual(rates.series[0].series_id, "DGS10")
        self.assertEqual(rates.series[1].series_id, "T10Y2Y")
        self.assertEqual(rates.series[0].panel_title, "10-Year Treasury Rate")
        self.assertEqual(rates.series[1].panel_title, "10Y-2Y Spread")
        spread_plot = plot_config_for("T10Y2Y")
        self.assertFalse(spread_plot.show_zero_line)
        self.assertEqual(rates.series[1].color, ChartConfig.LINE_COLOR)
        treasury = by_id["DGS10"]
        spread = by_id["T10Y2Y"]
        self.assertTrue(plot_config_for(rates.series[0].series_id).mark_latest)
        self.assertTrue(plot_config_for(rates.series[1].series_id).mark_latest)
        self.assertTrue(treasury.mark_latest)
        self.assertTrue(spread.mark_latest)
        self.assertTrue(by_id["GDPC1"].mark_latest)
        self.assertTrue(VIX_PLOT_CONFIG.mark_latest)
        self.assertEqual(VIX_PLOT_CONFIG.frequency, "d")
        for config in FRED_PLOT_CONFIGS:
            self.assertTrue(config.mark_latest, config.series_id)
        for combined in FRED_COMBINED_PLOT_CONFIGS:
            for spec in combined.series:
                self.assertTrue(
                    plot_config_for(spec.series_id).mark_latest, spec.series_id
                )
        self.assertEqual(treasury.title, "10-Year Treasury Rate")
        self.assertEqual(spread.title, "10Y-2Y Spread")
        self.assertFalse(spread.show_zero_line)
        self.assertEqual(
            plot_config_for("UNRATE").caption_note,
            "Share of labor force without work. Up = weaker labor market.",
        )

    def test_t10y2y_omits_dotted_zero_line(self) -> None:
        import matplotlib.pyplot as plt

        spread = next(c for c in FRED_PLOT_CONFIGS if c.series_id == "T10Y2Y")
        fig, ax = plt.subplots()
        try:
            _plot_xy(
                ax,
                [datetime(2020, 1, 2), datetime(2020, 6, 1)],
                [0.2, -0.5],
                color=ChartConfig.LINE_COLOR,
                kind="line",
                frequency="d",
                zero_color=ChartConfig.ZERO_LINE_COLOR,
                show_zero_line=spread.show_zero_line,
                fill_negative=False,
            )
            dotted_zero = [
                line
                for line in ax.lines
                if line.get_linestyle() in {"--", "dotted", ":"}
                and list(line.get_ydata()) == [0, 0]
            ]
            self.assertEqual(dotted_zero, [])
        finally:
            plt.close(fig)

    def test_latest_marker_is_horizontal_with_label(self) -> None:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots()
        try:
            _plot_xy(
                ax,
                [datetime(2020, 1, 2), datetime(2020, 6, 1)],
                [1.8, 0.7],
                color=ChartConfig.LINE_COLOR,
                kind="line",
                frequency="d",
                zero_color=ChartConfig.ZERO_LINE_COLOR,
                mark_latest=True,
                value_suffix="%",
            )
            self.assertEqual([t.get_text() for t in ax.texts], ["0.70%"])
            dotted = [
                line for line in ax.lines if line.get_linestyle() in {":", "dotted"}
            ]
            self.assertEqual(len(dotted), 1)
            self.assertEqual(list(dotted[0].get_ydata()), [0.7, 0.7])
        finally:
            plt.close(fig)

    def test_stress_index_background_green_below_zero_red_above(self) -> None:
        stress = next(c for c in FRED_PLOT_CONFIGS if c.series_id == "STLFSI4")
        self.assertFalse(stress.show_zero_line)
        self.assertEqual(
            stress.highlight_bands,
            (
                (float("-inf"), 0.0, ChartConfig.NEGATIVE_FILL_GOOD_COLOR, 0.25),
                (0.0, None, ChartConfig.ZERO_LINE_COLOR, 0.25),
            ),
        )

    def test_fred_history_window_is_ten_years(self) -> None:
        self.assertEqual(FredConfig.HISTORY_YEARS, 10)

    def test_clip_to_fred_history_drops_older_points(self) -> None:
        with patch(
            "analystbot.services.charting.get_date_years_ago",
            return_value="2016-09-09",
        ):
            clipped = _clip_to_fred_history(
                [
                    ("2010-01-01", 1.0),
                    ("2016-09-08", 2.0),
                    ("2016-09-09", 3.0),
                    ("2024-01-01", 4.0),
                ]
            )
        self.assertEqual(
            clipped,
            [("2016-09-09", 3.0), ("2024-01-01", 4.0)],
        )

    def test_jobs_stacked_shades_both_panels(self) -> None:
        jobs = next(
            config
            for config in FRED_COMBINED_PLOT_CONFIGS
            if config.combined_id == JOBS_COMBINED_ID
        )
        series_data = {
            "UNRATE": [("2019-01-01", 4.0), ("2020-04-01", 14.8), ("2021-01-01", 6.3)],
            "PAYEMS": [
                ("2019-01-01", 150000.0),
                ("2020-04-01", 130000.0),
                ("2021-01-01", 142000.0),
            ],
        }
        recessions = [(datetime(2020, 2, 1), datetime(2020, 4, 1))]
        parsed = {}
        for spec in jobs.series:
            dates = [
                datetime.strptime(row[0], "%Y-%m-%d")
                for row in series_data[spec.series_id]
            ]
            values = [row[1] for row in series_data[spec.series_id]]
            parsed[spec.series_id] = (dates, values)

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            service = ChartingService(settings)
            with patch.object(
                service,
                "_apply_recession_shading",
                wraps=service._apply_recession_shading,
            ) as shaded:
                fig = service._draw_stacked_combined(parsed, jobs, recessions, (12, 10))
            try:
                self.assertEqual(len(fig.axes), 2)
                self.assertIsNone(fig._suptitle)
                self.assertEqual(
                    fig.axes[0].get_title(),
                    "Unemployment Rate, Monthly, 2021-01-01",
                )
                self.assertEqual(
                    fig.axes[1].get_title(),
                    "Nonfarm Payrolls, Monthly, 2021-01-01",
                )
                self.assertEqual(shaded.call_count, 2)
                shaded_axes = [call.args[0] for call in shaded.call_args_list]
                self.assertEqual(set(shaded_axes), set(fig.axes))
            finally:
                import matplotlib.pyplot as plt

                plt.close(fig)
            self.assertTrue(
                service.generate_combined_chart(series_data, jobs, recessions)
            )
            self.assertTrue(
                (Path(temp_dir) / "fred_graphs" / "unrate_payems.png").exists()
            )

    def test_gdpc1_bar_chart_writes_png(self) -> None:
        config = next(c for c in FRED_PLOT_CONFIGS if c.series_id == "GDPC1")
        data = [
            ("2019-01-01", 0.6),
            ("2019-04-01", -0.2),
            ("2020-04-01", -8.5),
        ]
        recessions = [(datetime(2020, 2, 1), datetime(2020, 4, 1))]
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            service = ChartingService(settings)
            self.assertTrue(service.generate_chart(data, config, recessions))
            self.assertTrue((Path(temp_dir) / "fred_graphs" / "gdpc1.png").exists())


class PackagedRecessionTests(unittest.TestCase):
    def test_packaged_recessions_load_without_data_dir_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            recessions = load_recessions_for_settings(settings)
        self.assertGreaterEqual(len(recessions), 12)
        self.assertEqual(recessions[-1][0], datetime(2020, 2, 1))
        self.assertEqual(recessions[-1][1], datetime(2020, 4, 1))

    def test_data_dir_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            (Path(temp_dir) / "recessions.json").write_text(
                json.dumps(
                    {"recessions": [{"peak": "2007-12-01", "trough": "2009-06-01"}]}
                )
            )
            recessions = load_recessions_for_settings(settings)
        self.assertEqual(len(recessions), 1)
        self.assertEqual(recessions[0][0], datetime(2007, 12, 1))


class FredReleaseCalendarTests(unittest.TestCase):
    def test_fetch_series_release_id(self) -> None:
        client = FredClient("test-api-key")
        fake_http = MagicMock()
        fake_http.get.return_value = FakeReleaseResponse()
        client._http = fake_http

        release_id = client.fetch_series_release_id("UNRATE")
        self.assertEqual(release_id, 50)
        self.assertIn("series/release", fake_http.get.call_args.args[0])

    def test_fetch_next_release_date_picks_upcoming(self) -> None:
        client = FredClient("test-api-key")
        fake_http = MagicMock()
        fake_http.get.return_value = FakeDatesResponse()
        client._http = fake_http

        next_date = client.fetch_next_release_date(50, as_of=date(2026, 9, 9))
        self.assertEqual(next_date, "2026-10-03")
        params = fake_http.get.call_args.kwargs["params"]
        self.assertEqual(params["include_release_dates_with_no_data"], "true")

    def test_refresh_persists_shared_employment_date(self) -> None:
        client = MagicMock()
        client.fetch_series_release_id.return_value = 50
        client.fetch_next_release_date.return_value = "2026-11-06"
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            refresh_fred_release_calendar(
                settings, client, ["UNRATE", "PAYEMS", "VIXCLS"]
            )
            calendar = read_fred_release_calendar(settings)
        self.assertEqual(calendar["UNRATE"], "2026-11-06")
        self.assertEqual(calendar["PAYEMS"], "2026-11-06")
        self.assertNotIn("VIXCLS", calendar)
        self.assertEqual(client.fetch_next_release_date.call_count, 1)

    def test_release_ids_are_cached_across_refreshes(self) -> None:
        client = MagicMock()
        client.fetch_series_release_id.return_value = 50
        client.fetch_next_release_date.return_value = "2026-11-06"
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            refresh_fred_release_calendar(settings, client, ["UNRATE"])
            client.fetch_series_release_id.reset_mock()
            refresh_fred_release_calendar(settings, client, ["UNRATE"])
        client.fetch_series_release_id.assert_not_called()

    def test_daily_series_fetches_from_latest_stored_date(self) -> None:
        repo = MagicMock()
        repo.get_latest_observation.return_value = ("2026-09-30", 20.0)
        repo.get_observations_since.return_value = {}
        repo.upsert_only_changed.return_value = (1, 0, 0)
        client = MagicMock()
        client.fetch_series_observations.return_value = [
            {"date": "2026-09-30", "value": 20.1}
        ]
        service = FredDataService(Settings(_loaded=True))
        series = FredSeriesConfig("VIXCLS", "d", "index", "VIX")
        service.update_series(series, client, repo)
        self.assertEqual(
            client.fetch_series_observations.call_args.kwargs["observation_start"],
            "2026-09-30",
        )

    def test_upcoming_release_date_rejects_past_and_malformed(self) -> None:
        as_of = date(2026, 9, 9)
        self.assertEqual(upcoming_release_date("2026-10-03", as_of=as_of), "2026-10-03")
        self.assertEqual(upcoming_release_date("2026-09-09", as_of=as_of), "2026-09-09")
        self.assertIsNone(upcoming_release_date("2026-01-01", as_of=as_of))
        self.assertIsNone(upcoming_release_date("2026-13-40", as_of=as_of))
        self.assertIsNone(upcoming_release_date("2026-10-03\nspoofed", as_of=as_of))

    def test_read_calendar_omits_past_and_malformed_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            path = settings.data_dir / "fred_release_calendar.json"
            path.write_text(
                json.dumps(
                    {
                        "next_release_dates": {
                            "UNRATE": "2026-10-03",
                            "PAYEMS": "2026-01-01",
                            "GDPC1": "2026-13-40",
                            "HOUST": "2026-10-03\nspoofed",
                        }
                    }
                )
            )
            calendar = read_fred_release_calendar(settings, as_of=date(2026, 9, 9))
            on_disk = json.loads(path.read_text())
        self.assertEqual(calendar, {"UNRATE": "2026-10-03"})
        self.assertEqual(on_disk["next_release_dates"]["PAYEMS"], "2026-01-01")

    def test_caption_omits_past_next_release_date(self) -> None:
        as_of = date(2026, 9, 9)
        self.assertEqual(
            _caption_next_releases(["UNRATE"], {"UNRATE": "2026-01-01"}, as_of=as_of),
            [],
        )
        self.assertEqual(
            _caption_next_releases(["UNRATE"], {"UNRATE": "2026-10-03"}, as_of=as_of),
            [(None, "2026-10-03")],
        )


class FakeReleaseResponse:
    ok = True

    def json(self) -> dict[str, object]:
        return {"releases": [{"id": 50, "name": "Employment Situation"}]}


class FakeDatesResponse:
    ok = True

    def json(self) -> dict[str, object]:
        return {
            "release_dates": [
                {"release_id": 50, "date": "2026-10-03"},
                {"release_id": 50, "date": "2026-09-05"},
                {"release_id": 50, "date": "2026-08-01"},
            ]
        }


if __name__ == "__main__":
    unittest.main()
