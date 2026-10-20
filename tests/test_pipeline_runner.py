from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from analystbot.core.config import Settings
from analystbot.db.fred_repo import FredRepository
from analystbot.db.sqlite import connect
from analystbot.pipeline.runner import (
    PipelineRunner,
    StageConfig,
    StageTimeoutError,
    run_pipeline,
)
from analystbot.pipeline.stage_worker import main as stage_worker_main
from analystbot.pipeline.stages import (
    build_default_stages,
    build_forced_chart_stages,
    build_rebuild_stages,
)
from analystbot.services.fred_chart_catalog import (
    FRED_FORCE_FLAG_SPECS,
    select_fred_chart_work,
)


def _settings() -> Settings:
    return Settings(_loaded=True)


def _runner(stages: list[StageConfig]) -> PipelineRunner:
    temp_dir = tempfile.TemporaryDirectory()
    settings = _settings()
    settings.data_dir = Path(temp_dir.name)
    runner = PipelineRunner(
        settings,
        stages,
        report_path=Path(temp_dir.name) / "pipeline_report.json",
    )
    runner._temp_dir = temp_dir
    return runner


class PipelineDependencyTests(unittest.TestCase):
    def test_skips_telegram_when_chart_stage_fails(self) -> None:
        sent: list[bool] = []
        stages = [
            StageConfig(
                name="Market Heatmap Chart",
                description="generate",
                run_fn=lambda _settings: False,
            ),
            StageConfig(
                name="Market Heatmap Telegram",
                description="send",
                run_fn=lambda _settings: sent.append(True) or True,
                depends_on="Market Heatmap Chart",
            ),
        ]
        runner = _runner(stages)
        self.addCleanup(runner._temp_dir.cleanup)
        self.assertTrue(runner.run())
        self.assertEqual(sent, [])
        self.assertEqual(runner.results[0].status, "failed")
        self.assertEqual(runner.results[1].status, "skipped")

    def test_timeout_retries_then_succeeds(self) -> None:
        stage = StageConfig(
            name="Database Update",
            description="fetch",
            run_fn=lambda _settings: True,
            critical=True,
            timeout_seconds=30,
            max_attempts=3,
            worker_id="market_data",
        )
        runner = _runner([stage])
        self.addCleanup(runner._temp_dir.cleanup)
        proc_timeout = MagicMock()
        proc_timeout.pid = 1
        proc_timeout.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["python"], timeout=30, stderr=""
        )
        proc_ok = MagicMock()
        proc_ok.communicate.return_value = ("", "")
        proc_ok.returncode = 0
        with (
            patch(
                "analystbot.pipeline.runner.subprocess.Popen",
                side_effect=[proc_timeout, proc_ok],
            ),
            patch("analystbot.pipeline.runner._kill_process_group"),
        ):
            self.assertTrue(runner.run())
        self.assertEqual(runner.results[0].status, "success")

    def test_injects_succeeded_stages_into_worker_kwargs(self) -> None:
        stages = [
            StageConfig(
                name="Market Supertrend Charts",
                description="generate",
                run_fn=lambda _settings: True,
            ),
            StageConfig(
                name="Telegram Delivery",
                description="send",
                run_fn=lambda _settings: True,
                timeout_seconds=30,
                worker_id="notifications_delivery",
                inject_succeeded_stages=True,
            ),
        ]
        runner = _runner(stages)
        self.addCleanup(runner._temp_dir.cleanup)
        captured: list[list[str]] = []

        def fake_popen(cmd, **_kwargs):
            captured.append(cmd)
            proc = MagicMock()
            proc.communicate.return_value = ("", "")
            proc.returncode = 0
            return proc

        with patch(
            "analystbot.pipeline.runner.subprocess.Popen", side_effect=fake_popen
        ):
            self.assertTrue(runner.run())
        self.assertEqual(len(captured), 1)
        kwargs = json.loads(captured[0][-1])
        self.assertEqual(kwargs["succeeded_stages"], ["Market Supertrend Charts"])

    def test_parallel_group_retries_timeout_then_succeeds(self) -> None:
        stages = [
            StageConfig(
                name="Database Update",
                description="fetch",
                run_fn=lambda _settings: True,
                critical=True,
                timeout_seconds=30,
                max_attempts=3,
                parallel_group="fetch",
                worker_id="market_data",
            ),
            StageConfig(
                name="FRED Update",
                description="fetch",
                run_fn=lambda _settings: True,
                timeout_seconds=30,
                parallel_group="fetch",
                worker_id="fred_data",
            ),
        ]
        runner = _runner(stages)
        self.addCleanup(runner._temp_dir.cleanup)
        market_attempts = {"n": 0}

        def fake_popen(cmd, **_kwargs):
            proc = MagicMock()
            proc.pid = 1
            worker_id = cmd[3]
            if worker_id == "market_data" and market_attempts["n"] == 0:
                market_attempts["n"] += 1
                proc.communicate.side_effect = subprocess.TimeoutExpired(
                    cmd=cmd, timeout=30, stderr=""
                )
                return proc
            proc.communicate.return_value = ("", "")
            proc.returncode = 0
            return proc

        with (
            patch(
                "analystbot.pipeline.runner.subprocess.Popen", side_effect=fake_popen
            ),
            patch("analystbot.pipeline.runner._kill_process_group"),
        ):
            self.assertTrue(runner.run())
        by_name = {result.name: result.status for result in runner.results}
        self.assertEqual(by_name["Database Update"], "success")
        self.assertEqual(by_name["FRED Update"], "success")
        self.assertEqual(market_attempts["n"], 1)

    def test_runs_telegram_when_chart_stage_succeeds(self) -> None:
        sent: list[bool] = []
        stages = [
            StageConfig(
                name="Market Heatmap Chart",
                description="generate",
                run_fn=lambda _settings: True,
            ),
            StageConfig(
                name="Market Heatmap Telegram",
                description="send",
                run_fn=lambda _settings: sent.append(True) or True,
                depends_on="Market Heatmap Chart",
            ),
        ]
        runner = _runner(stages)
        self.addCleanup(runner._temp_dir.cleanup)
        self.assertTrue(runner.run())
        self.assertEqual(sent, [True])
        self.assertEqual(
            [result.status for result in runner.results], ["success", "success"]
        )


class PipelinePreflightTests(unittest.TestCase):
    def test_missing_telegram_does_not_block_critical_market_data(self) -> None:
        settings = Settings(_loaded=True)
        settings.tastytrade_client_secret = "secret"
        settings.tastytrade_refresh_token = "refresh"
        stages = [
            StageConfig(
                name="Database Update",
                description="fetch",
                run_fn=lambda _settings: True,
                critical=True,
                validation_stage="market_data",
            ),
            StageConfig(
                name="Telegram Messaging",
                description="send",
                run_fn=lambda _settings: True,
                critical=False,
                validation_stage="telegram",
            ),
        ]
        with patch.object(PipelineRunner, "run", return_value=True) as run:
            self.assertTrue(run_pipeline(settings, stages))
        run.assert_called_once()

    def test_missing_chart_topic_aborts_forced_critical_send(self) -> None:
        settings = Settings(_loaded=True)
        settings.telegram_bot_token = "token"
        settings.telegram_chat_id = "chat"
        stages = [
            StageConfig(
                name="Market Supertrend Telegram",
                description="send",
                run_fn=lambda _settings: True,
                critical=True,
                validation_stage="chart_telegram",
            ),
        ]
        with patch.object(PipelineRunner, "run") as run:
            self.assertFalse(run_pipeline(settings, stages))
        run.assert_not_called()


class WorkerTimeoutTests(unittest.TestCase):
    def _timed_stage(self) -> tuple[PipelineRunner, StageConfig]:
        stage = StageConfig(
            name="Database Update",
            description="fetch",
            run_fn=lambda _settings: True,
            timeout_seconds=30,
            worker_id="market_data",
        )
        return PipelineRunner(_settings(), [stage]), stage

    def test_subprocess_uses_new_session(self) -> None:
        runner, stage = self._timed_stage()
        proc = MagicMock()
        proc.communicate.return_value = ("", "")
        proc.returncode = 0
        with patch(
            "analystbot.pipeline.runner.subprocess.Popen", return_value=proc
        ) as popen:
            self.assertTrue(runner._execute_with_timeout(stage))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_timeout_kills_process_group(self) -> None:
        runner, stage = self._timed_stage()
        proc = MagicMock()
        proc.pid = 4242
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["python"], timeout=30, stderr="worker boom"
        )
        with (
            patch("analystbot.pipeline.runner.subprocess.Popen", return_value=proc),
            patch("analystbot.pipeline.runner._kill_process_group") as kill,
            self.assertRaises(StageTimeoutError),
        ):
            runner._execute_with_timeout(stage)
        kill.assert_called_once_with(proc)

    def test_failure_forwards_worker_stderr_at_error(self) -> None:
        runner, stage = self._timed_stage()
        proc = MagicMock()
        proc.communicate.return_value = (
            "",
            "2026-09-15 00:00:00 - api.tastytrade - ERROR - invalid_grant",
        )
        proc.returncode = 1
        with (
            patch("analystbot.pipeline.runner.subprocess.Popen", return_value=proc),
            self.assertLogs("pipeline.runner", level="ERROR") as captured,
            patch("sys.stderr", new=io.StringIO()) as stderr,
        ):
            self.assertFalse(runner._execute_with_timeout(stage))
        text = "\n".join(captured.output) + stderr.getvalue()
        self.assertIn("[worker]", text)
        self.assertIn("invalid_grant", text)


class StageWorkerMainTests(unittest.TestCase):
    def test_unknown_worker_id_exits_2(self) -> None:
        self.assertEqual(stage_worker_main(["not_a_worker"]), 2)

    def test_missing_args_exits_2(self) -> None:
        self.assertEqual(stage_worker_main([]), 2)

    def test_invalid_kwargs_json_exits_2(self) -> None:
        self.assertEqual(stage_worker_main(["market_data", "{"]), 2)

    def test_kwargs_must_be_a_json_object(self) -> None:
        self.assertEqual(stage_worker_main(["market_data", "[1]"]), 2)

    def test_handler_true_exits_0(self) -> None:
        with (
            patch(
                "analystbot.pipeline.stage_worker._load_handler",
                return_value=lambda _settings, **_kwargs: True,
            ),
            patch("analystbot.pipeline.stage_worker.SessionManager.close_session"),
        ):
            self.assertEqual(stage_worker_main(["notifications_delivery"]), 0)

    def test_handler_false_exits_1(self) -> None:
        with (
            patch(
                "analystbot.pipeline.stage_worker._load_handler",
                return_value=lambda _settings, **_kwargs: False,
            ),
            patch("analystbot.pipeline.stage_worker.SessionManager.close_session"),
        ):
            self.assertEqual(stage_worker_main(["notifications_delivery"]), 1)

    def test_handlers_match_timed_stage_ids(self) -> None:
        from analystbot.pipeline.stage_worker import _HANDLERS

        used: set[str] = set()
        fred_flags = {
            dest: True for dest, _flag, _series, _help in FRED_FORCE_FLAG_SPECS
        }
        for stage in (
            *build_default_stages(),
            *build_rebuild_stages(None),
            *build_rebuild_stages(["AAPL"]),
            *build_forced_chart_stages(
                supertrend=True,
                vix=True,
                rrg=True,
                heatmap=True,
                fred_flags=fred_flags,
            ),
        ):
            if stage.worker_id:
                used.add(stage.worker_id)
        self.assertEqual(set(_HANDLERS), used)


class PipelineStageWiringTests(unittest.TestCase):
    def test_default_telegram_is_one_delivery_stage(self) -> None:
        by_name = {stage.name: stage for stage in build_default_stages()}
        self.assertIn("Telegram Delivery", by_name)
        self.assertNotIn("Market Heatmap Telegram", by_name)
        self.assertNotIn("VIX Telegram", by_name)
        self.assertTrue(by_name["Telegram Delivery"].inject_succeeded_stages)
        self.assertEqual(
            by_name["Telegram Delivery"].worker_id, "notifications_delivery"
        )
        self.assertEqual(by_name["Database Update"].timeout_seconds, 210)
        self.assertEqual(by_name["Database Update"].max_attempts, 3)
        self.assertEqual(by_name["Database Update"].parallel_group, "fetch")
        self.assertEqual(by_name["FRED Update"].parallel_group, "fetch")

    def test_forced_vix_chart_is_a_timed_worker(self) -> None:
        vix = {stage.name: stage for stage in build_forced_chart_stages(vix=True)}
        self.assertEqual(vix["VIX Chart"].timeout_seconds, 300)
        self.assertEqual(vix["VIX Chart"].worker_id, "charting_forced")
        self.assertEqual(vix["VIX Chart"].worker_kwargs, {"series_ids": ["VIXCLS"]})
        self.assertEqual(vix["Chart Telegram"].worker_id, "notifications_album_forced")
        self.assertEqual(vix["Chart Telegram"].worker_kwargs["include"], ["vix"])

    def test_rebuild_fred_passes_force_full(self) -> None:
        rebuild = {stage.name: stage for stage in build_rebuild_stages(None)}
        self.assertEqual(rebuild["FRED Update"].worker_kwargs, {"force_full": True})


class FredGateTests(unittest.TestCase):
    def test_missing_update_state_selects_no_charts(self) -> None:
        selection = select_fred_chart_work(None)
        self.assertEqual(selection.generate_individuals, ())
        self.assertEqual(selection.generate_combined, ())
        self.assertEqual(selection.send_individuals, ())
        self.assertEqual(selection.send_combined, ())


class PipelineReportTests(unittest.TestCase):
    def test_report_timestamps_are_utc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = _settings()
            settings.data_dir = Path(temp_dir)
            report_path = Path(temp_dir) / "pipeline_report.json"
            runner = PipelineRunner(
                settings,
                [
                    StageConfig(
                        name="noop",
                        description="noop",
                        run_fn=lambda _settings: True,
                    )
                ],
                report_path=report_path,
            )
            self.assertTrue(runner.run())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(
                payload["timestamp"].endswith("Z") or "+" in payload["timestamp"]
            )
            self.assertTrue(
                payload["pipeline_start"].endswith("+00:00")
                or payload["pipeline_start"].endswith("Z")
                or "+" in payload["pipeline_start"]
            )
            self.assertEqual(payload["stages_summary"]["skipped"], 0)


class FredDeleteSeriesTests(unittest.TestCase):
    def test_delete_series_removes_only_that_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = connect(Path(temp_dir) / "t.db")
            repo = FredRepository(conn)
            try:
                repo.upsert_only_changed(
                    "UNRATE",
                    [{"date": "2026-01-01", "value": 4.0}],
                    "m",
                    "percent",
                    {},
                )
                repo.upsert_only_changed(
                    "PAYEMS",
                    [{"date": "2026-01-01", "value": 100.0}],
                    "m",
                    "thousands",
                    {},
                )
                self.assertEqual(repo.delete_series("UNRATE"), 1)
                self.assertEqual(repo.get_observation_count("UNRATE"), 0)
                self.assertEqual(repo.get_observation_count("PAYEMS"), 1)
            finally:
                conn.close()


class ChartGenerateSkipTests(unittest.TestCase):
    def test_supertrend_run_fails_without_database(self) -> None:
        from analystbot.services import market_charts

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.db_path = Path(temp_dir) / "missing.db"
            self.assertFalse(market_charts.run(settings))


if __name__ == "__main__":
    unittest.main()
