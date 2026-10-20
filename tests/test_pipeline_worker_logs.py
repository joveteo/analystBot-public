from __future__ import annotations

import io
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from analystbot.core.config import Settings
from analystbot.pipeline.runner import (
    PipelineRunner,
    StageConfig,
    StageTimeoutError,
    select_worker_log_lines,
)
from analystbot.pipeline.stages import (
    build_default_stages,
    build_forced_chart_stages,
    build_rebuild_stages,
)


def _rebuild_stderr() -> str:
    lines = [
        "2026-09-08 00:14:54 - api.tastytrade - ERROR - tastytrade DXLink fetch failed",
        "Traceback (most recent call last):",
        '  File "/tmp/tastytrade.py", line 425, in _get_daily_bars_batch_async',
        "    await self._refresh_session(session)",
        "tastytrade.utils.TastytradeError: Couldn't parse response: "
        "{'error_code': 'invalid_grant', 'error_description': 'Grant revoked'}",
    ]
    lines.extend(
        f"2026-09-08 00:14:54 - services.market_data - WARNING - S{i:03d}: API fetch failed"
        for i in range(119)
    )
    lines.extend(
        [
            "2026-09-08 00:14:54 - services.market_data - INFO -    Symbols processed: 0/119 (0.0%)",
            "2026-09-08 00:14:54 - services.market_data - ERROR - FAILED: Market Data Update completed",
        ]
    )
    return "\n".join(lines)


class WorkerLogSelectionTests(unittest.TestCase):
    def test_revoked_grant_is_not_hidden_by_per_symbol_warnings(self) -> None:
        selected = select_worker_log_lines(_rebuild_stderr())
        text = "\n".join(selected)
        self.assertIn("invalid_grant", text)
        self.assertIn("Grant revoked", text)
        self.assertIn("ERROR", text)
        warning_count = sum(1 for line in selected if "API fetch failed" in line)
        self.assertLess(warning_count, 10)


class WorkerStderrForwardingTests(unittest.TestCase):
    def _timed_stage(self) -> tuple[PipelineRunner, StageConfig]:
        settings = Settings(_loaded=True)
        stage = StageConfig(
            name="Database Update",
            description="fetch",
            run_fn=lambda _settings: True,
            timeout_seconds=30,
            worker_id="market_data",
        )
        return PipelineRunner(settings, [stage]), stage

    def _proc(self, returncode: int, stderr: str) -> MagicMock:
        proc = MagicMock()
        proc.communicate.return_value = ("", stderr)
        proc.returncode = returncode
        return proc

    def test_success_does_not_forward_worker_stderr(self) -> None:
        runner, stage = self._timed_stage()
        with (
            patch(
                "analystbot.pipeline.runner.subprocess.Popen",
                return_value=self._proc(0, _rebuild_stderr()),
            ),
            patch("analystbot.pipeline.runner.logger.error") as error,
        ):
            self.assertTrue(runner._execute_with_timeout(stage))
        messages = [str(call.args[0]) for call in error.call_args_list]
        self.assertFalse(any("[worker]" in message for message in messages))

    def test_failure_forwards_first_error(self) -> None:
        runner, stage = self._timed_stage()
        with (
            patch(
                "analystbot.pipeline.runner.subprocess.Popen",
                return_value=self._proc(1, _rebuild_stderr()),
            ),
            self.assertLogs("pipeline.runner", level="ERROR") as captured,
            patch("sys.stderr", new=io.StringIO()) as stderr,
        ):
            self.assertFalse(runner._execute_with_timeout(stage))
        text = "\n".join(captured.output) + stderr.getvalue()
        self.assertIn("[worker]", text)
        self.assertIn("invalid_grant", text)

    def test_timeout_forwards_worker_stderr(self) -> None:
        runner, stage = self._timed_stage()
        proc = MagicMock()
        proc.pid = 99
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["python"], timeout=30, stderr=_rebuild_stderr()
        )
        with (
            patch("analystbot.pipeline.runner.subprocess.Popen", return_value=proc),
            patch("analystbot.pipeline.runner._kill_process_group"),
            self.assertLogs("pipeline.runner", level="ERROR") as captured,
            patch("sys.stderr", new=io.StringIO()) as stderr,
            self.assertRaises(StageTimeoutError),
        ):
            runner._execute_with_timeout(stage)
        self.assertIn("invalid_grant", "\n".join(captured.output) + stderr.getvalue())


class PipelineStageProcessTests(unittest.TestCase):
    def test_cheap_default_stages_run_in_process(self) -> None:
        by_name = {stage.name: stage for stage in build_default_stages()}
        for name in (
            "Indicator Calculation",
            "Market Supertrend Charts",
            "Sector RRG Chart",
        ):
            self.assertIsNone(by_name[name].timeout_seconds)
            self.assertIsNone(by_name[name].worker_id)

    def test_network_default_stages_keep_subprocess_timeouts(self) -> None:
        by_name = {stage.name: stage for stage in build_default_stages()}
        self.assertEqual(by_name["Database Update"].timeout_seconds, 210)
        self.assertEqual(by_name["Database Update"].max_attempts, 3)
        self.assertEqual(by_name["Database Update"].worker_id, "market_data")
        self.assertEqual(by_name["FRED Update"].timeout_seconds, 300)
        self.assertEqual(by_name["FRED Update"].worker_id, "fred_data")
        self.assertEqual(by_name["FRED Graphs"].timeout_seconds, 300)
        self.assertEqual(by_name["FRED Graphs"].worker_id, "charting")
        self.assertEqual(by_name["Telegram Delivery"].timeout_seconds, 300)
        self.assertEqual(
            by_name["Telegram Delivery"].worker_id, "notifications_delivery"
        )
        self.assertEqual(by_name["Market Heatmap Chart"].timeout_seconds, 300)
        self.assertEqual(by_name["Market Heatmap Chart"].worker_id, "heatmap_charts")
        self.assertNotIn("VIX Telegram", by_name)
        self.assertNotIn("Market Heatmap Telegram", by_name)

    def test_rebuild_and_forced_charts_keep_network_timeouts(self) -> None:
        rebuild = {stage.name: stage for stage in build_rebuild_stages(None)}
        self.assertEqual(rebuild["Database Rebuild"].timeout_seconds, 210)
        self.assertEqual(rebuild["Database Rebuild"].max_attempts, 3)
        self.assertIsNone(rebuild["Indicator Calculation"].timeout_seconds)
        self.assertEqual(rebuild["FRED Update"].timeout_seconds, 300)

        forced = {
            stage.name: stage
            for stage in build_forced_chart_stages(
                supertrend=True,
                vix=False,
                rrg=True,
                heatmap=False,
                fred_flags={"unemployment": True},
            )
        }
        self.assertIsNone(forced["Market Supertrend Charts"].timeout_seconds)
        self.assertEqual(forced["Chart Telegram"].timeout_seconds, 300)
        self.assertEqual(
            forced["Chart Telegram"].worker_kwargs["include"], ["supertrend", "rrg"]
        )
        self.assertEqual(forced["FRED Graphs"].timeout_seconds, 300)
        self.assertEqual(forced["FRED Graphs"].worker_id, "charting_forced")
        self.assertEqual(forced["FRED Telegram"].timeout_seconds, 300)
        self.assertIsNone(forced["Sector RRG Chart"].timeout_seconds)

        vix_forced = {
            stage.name: stage
            for stage in build_forced_chart_stages(
                supertrend=False,
                vix=True,
                rrg=False,
                heatmap=False,
                fred_flags={"unemployment": True},
            )
        }
        self.assertIn("VIX Chart", vix_forced)
        self.assertIn("Chart Telegram", vix_forced)
        self.assertNotIn(
            "VIXCLS", vix_forced["FRED Telegram"].worker_kwargs["series_ids"]
        )
        self.assertEqual(vix_forced["VIX Chart"].timeout_seconds, 300)
        self.assertEqual(vix_forced["VIX Chart"].worker_id, "charting_forced")
        self.assertEqual(
            vix_forced["Chart Telegram"].validation_stage, "chart_telegram"
        )
        self.assertEqual(
            vix_forced["Chart Telegram"].worker_id, "notifications_album_forced"
        )
        self.assertEqual(vix_forced["Chart Telegram"].timeout_seconds, 300)
        vix_only = [
            stage.name
            for stage in build_forced_chart_stages(
                supertrend=False,
                vix=True,
                rrg=False,
                heatmap=False,
            )
        ]
        self.assertEqual(vix_only, ["VIX Chart", "Chart Telegram"])
        heatmap_only = [
            stage.name
            for stage in build_forced_chart_stages(
                supertrend=False,
                vix=False,
                rrg=False,
                heatmap=True,
            )
        ]
        self.assertEqual(heatmap_only, ["Market Heatmap Chart", "Chart Telegram"])
        heatmap_forced = {
            stage.name: stage
            for stage in build_forced_chart_stages(
                supertrend=False,
                vix=False,
                rrg=False,
                heatmap=True,
            )
        }
        self.assertEqual(
            heatmap_forced["Market Heatmap Chart"].worker_id, "heatmap_charts"
        )
        self.assertEqual(heatmap_forced["Market Heatmap Chart"].timeout_seconds, 300)
        self.assertEqual(
            heatmap_forced["Chart Telegram"].worker_id, "notifications_album_forced"
        )


if __name__ == "__main__":
    unittest.main()
