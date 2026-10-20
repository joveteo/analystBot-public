import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analystbot.api.http_client import SessionManager
from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger

logger = setup_logger("pipeline.runner")

_WORKER_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def select_worker_log_lines(
    stderr: str, *, tail_lines: int = 20, max_lines: int = 40
) -> list[str]:
    lines = stderr.strip().splitlines()
    if not lines:
        return []

    first_error = next(
        (
            index
            for index, line in enumerate(lines)
            if " - ERROR - " in line
            or " - CRITICAL - " in line
            or line.startswith("Traceback ")
        ),
        None,
    )
    if first_error is None:
        return lines[-tail_lines:]

    selected: list[str] = []
    for line in lines[first_error:]:
        if (
            selected
            and _is_worker_log_record(line)
            and " - ERROR - " not in line
            and " - CRITICAL - " not in line
        ):
            break
        selected.append(line)
        if len(selected) >= max_lines:
            break

    for line in lines[-8:]:
        if line in selected:
            continue
        if "FAILED" in line or "SUCCESS" in line or "Workflow failed" in line:
            selected.append(line)
    return selected


def _is_worker_log_record(line: str) -> bool:
    return any(f" - {level} - " in line for level in _WORKER_LOG_LEVELS)


def _decode_stdio(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _forward_worker_stderr(stderr: str) -> None:
    lines = select_worker_log_lines(stderr)
    if not lines:
        return
    logger.error("Stage worker failed; details are in the daily log (also on stderr)")
    for line in lines:
        print(f"[worker] {line}", file=sys.stderr, flush=True)


def _kill_process_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()
    except PermissionError:
        proc.kill()


def _critical_stages_succeeded(
    results: list["StageResult"], stages: list["StageConfig"]
) -> bool:
    critical_names = {stage.name for stage in stages if stage.critical}
    return all(
        result.status == "success"
        for result in results
        if result.name in critical_names
    )


def _status_counts(results: list["StageResult"]) -> dict[str, int]:
    return {
        "total": len(results),
        "success": sum(1 for result in results if result.status == "success"),
        "failed": sum(1 for result in results if result.status == "failed"),
        "timeout": sum(1 for result in results if result.status == "timeout"),
        "skipped": sum(1 for result in results if result.status == "skipped"),
    }


class StageTimeoutError(TimeoutError):
    pass


@dataclass
class StageConfig:
    name: str
    description: str
    run_fn: Callable[[Settings], bool]
    critical: bool = False
    timeout_seconds: int | None = None
    validation_stage: str | None = None
    worker_id: str | None = None
    worker_kwargs: dict[str, Any] = field(default_factory=dict)
    depends_on: str | None = None
    max_attempts: int = 1
    parallel_group: str | None = None
    inject_succeeded_stages: bool = False


@dataclass
class StageResult:
    name: str
    status: str
    duration_seconds: float
    error_message: str | None = None


@dataclass
class PipelineReport:
    timestamp: str
    pipeline_start: str
    pipeline_duration_seconds: float
    stages: list[dict[str, Any]]
    overall_success: bool
    stages_summary: dict[str, int]


class PipelineRunner:
    def __init__(
        self,
        settings: Settings,
        stages: list[StageConfig],
        report_path: Path | None = None,
    ):
        self.settings = settings
        self.stages = stages
        self.report_path = report_path or (settings.data_dir / "pipeline_report.json")
        self.results: list[StageResult] = []

    def execute_stage(self, stage: StageConfig) -> StageResult:
        logger.info(f"Executing stage: {stage.name}")
        logger.info(f"   Description: {stage.description}")
        logger.info(f"   Critical: {stage.critical}")
        if stage.timeout_seconds:
            logger.info(f"   Timeout: {stage.timeout_seconds}s (subprocess)")

        start_time = datetime.now(UTC)
        attempts = max(1, stage.max_attempts)

        try:
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    if stage.timeout_seconds:
                        success = self._execute_with_timeout(stage)
                    else:
                        success = stage.run_fn(self.settings)
                    last_error = None
                    break
                except StageTimeoutError as exc:
                    last_error = exc
                    if attempt < attempts:
                        logger.warning(
                            "%s timed out (attempt %s/%s); retrying",
                            stage.name,
                            attempt,
                            attempts,
                        )
                        continue
                    raise
            else:
                success = False

            if last_error is not None:
                raise last_error

            duration = (datetime.now(UTC) - start_time).total_seconds()

            if success:
                logger.info(f"{stage.name} completed successfully in {duration:.1f}s")
                return StageResult(
                    name=stage.name, status="success", duration_seconds=duration
                )
            else:
                logger.error(f"{stage.name} failed after {duration:.1f}s")
                return StageResult(
                    name=stage.name,
                    status="failed",
                    duration_seconds=duration,
                    error_message="Stage returned False",
                )

        except StageTimeoutError:
            duration = (datetime.now(UTC) - start_time).total_seconds()
            logger.error(
                f"{stage.name} timed out after {duration:.1f}s "
                f"(limit: {stage.timeout_seconds}s); child process killed"
            )
            return StageResult(
                name=stage.name,
                status="timeout",
                duration_seconds=duration,
                error_message=f"Stage timed out after {stage.timeout_seconds}s",
            )

        except Exception as e:
            duration = (datetime.now(UTC) - start_time).total_seconds()
            logger.error(
                f"{stage.name} failed with exception after {duration:.1f}s: {e}"
            )
            return StageResult(
                name=stage.name,
                status="failed",
                duration_seconds=duration,
                error_message=str(e),
            )

    def _worker_command(self, stage: StageConfig) -> list[str]:
        if not stage.worker_id:
            raise RuntimeError(f"Stage {stage.name!r} has no worker_id")
        kwargs = dict(stage.worker_kwargs)
        if stage.inject_succeeded_stages:
            kwargs["succeeded_stages"] = [
                result.name for result in self.results if result.status == "success"
            ]
        cmd = [
            sys.executable,
            "-m",
            "analystbot.pipeline.stage_worker",
            stage.worker_id,
        ]
        if kwargs:
            cmd.append(json.dumps(kwargs))
        return cmd

    def _popen_worker(self, stage: StageConfig) -> subprocess.Popen:
        cmd = self._worker_command(stage)
        logger.debug(f"Starting stage subprocess: {' '.join(cmd[:4])} ...")
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def _wait_worker(self, stage: StageConfig, proc: subprocess.Popen) -> bool:
        try:
            _stdout, stderr = proc.communicate(timeout=stage.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            stderr_text = _decode_stdio(exc.stderr)
            _kill_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if stderr_text:
                _forward_worker_stderr(stderr_text)
            if stage.worker_id == "fred_data":
                from analystbot.services.fred_data import write_fred_update_state

                write_fred_update_state(
                    self.settings, updated_series_ids=[], processed_series_ids=[]
                )
            raise StageTimeoutError(
                f"Stage {stage.name!r} exceeded {stage.timeout_seconds}s"
            ) from exc

        if proc.returncode != 0 and stderr:
            _forward_worker_stderr(stderr)

        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise RuntimeError(
            f"Stage worker {stage.worker_id!r} exited with code {proc.returncode}"
        )

    def _execute_with_timeout(self, stage: StageConfig) -> bool:
        if not stage.timeout_seconds:
            raise RuntimeError(f"Stage {stage.name!r} has no timeout_seconds")
        if not stage.worker_id:
            raise RuntimeError(
                f"Stage {stage.name!r} has timeout_seconds but no worker_id"
            )
        return self._wait_worker(stage, self._popen_worker(stage))

    def _dependency_skip(self, stage: StageConfig) -> StageResult | None:
        if not stage.depends_on:
            return None
        dependency = next(
            (result for result in self.results if result.name == stage.depends_on),
            None,
        )
        if dependency is not None and dependency.status == "success":
            return None
        reason = "did not run" if dependency is None else f"{dependency.status}"
        logger.warning(f"Skipping {stage.name}: {stage.depends_on} {reason}")
        return StageResult(
            name=stage.name,
            status="skipped",
            duration_seconds=0.0,
            error_message=f"Skipped: {stage.depends_on} did not succeed",
        )

    def _execute_parallel_group(self, stages: list[StageConfig]) -> list[StageResult]:
        names = ", ".join(stage.name for stage in stages)
        logger.info("Running %s stages in parallel: %s", len(stages), names)
        pending: list[tuple[StageConfig, subprocess.Popen, datetime, int]] = []
        for stage in stages:
            logger.info("Executing stage: %s", stage.name)
            logger.info("   Description: %s", stage.description)
            logger.info("   Critical: %s", stage.critical)
            if stage.timeout_seconds:
                logger.info("   Timeout: %ss (subprocess)", stage.timeout_seconds)
            pending.append((stage, self._popen_worker(stage), datetime.now(UTC), 1))

        results: dict[str, StageResult] = {}
        while pending:
            stage, proc, start_time, attempt = pending.pop(0)
            attempts = max(1, stage.max_attempts)
            try:
                success = self._wait_worker(stage, proc)
            except StageTimeoutError:
                duration = (datetime.now(UTC) - start_time).total_seconds()
                if attempt < attempts:
                    logger.warning(
                        "%s timed out (attempt %s/%s); retrying",
                        stage.name,
                        attempt,
                        attempts,
                    )
                    pending.append(
                        (
                            stage,
                            self._popen_worker(stage),
                            datetime.now(UTC),
                            attempt + 1,
                        )
                    )
                    continue
                logger.error(
                    "%s timed out after %.1fs (limit: %ss); child process killed",
                    stage.name,
                    duration,
                    stage.timeout_seconds,
                )
                results[stage.name] = StageResult(
                    name=stage.name,
                    status="timeout",
                    duration_seconds=duration,
                    error_message=f"Stage timed out after {stage.timeout_seconds}s",
                )
                continue
            except Exception as exc:
                duration = (datetime.now(UTC) - start_time).total_seconds()
                logger.error(
                    "%s failed with exception after %.1fs: %s",
                    stage.name,
                    duration,
                    exc,
                )
                results[stage.name] = StageResult(
                    name=stage.name,
                    status="failed",
                    duration_seconds=duration,
                    error_message=str(exc),
                )
                continue

            duration = (datetime.now(UTC) - start_time).total_seconds()
            if success:
                logger.info("%s completed successfully in %.1fs", stage.name, duration)
                results[stage.name] = StageResult(
                    name=stage.name, status="success", duration_seconds=duration
                )
            else:
                logger.error("%s failed after %.1fs", stage.name, duration)
                results[stage.name] = StageResult(
                    name=stage.name,
                    status="failed",
                    duration_seconds=duration,
                    error_message="Stage returned False",
                )

        return [results[stage.name] for stage in stages]

    def run(self) -> bool:
        log_script_start(logger, "Analyst Bot Pipeline")
        pipeline_start = datetime.now().astimezone()

        self.results = []
        overall_success = False

        try:
            logger.info(f"Starting pipeline with {len(self.stages)} stages")

            index = 0
            while index < len(self.stages):
                stage = self.stages[index]
                group = [stage]
                if stage.parallel_group:
                    look = index + 1
                    while (
                        look < len(self.stages)
                        and self.stages[look].parallel_group == stage.parallel_group
                    ):
                        group.append(self.stages[look])
                        look += 1

                for offset, member in enumerate(group):
                    logger.info(
                        "Stage %s/%s: %s",
                        index + offset + 1,
                        len(self.stages),
                        member.name,
                    )

                to_run: list[StageConfig] = []
                for member in group:
                    skipped = self._dependency_skip(member)
                    if skipped is not None:
                        self.results.append(skipped)
                    else:
                        to_run.append(member)

                stop = False
                if len(to_run) > 1 and all(
                    member.timeout_seconds and member.worker_id for member in to_run
                ):
                    for result, member in zip(
                        self._execute_parallel_group(to_run), to_run, strict=True
                    ):
                        self.results.append(result)
                        if result.status in ("failed", "timeout") and member.critical:
                            logger.error(
                                "Critical stage %s: %s", result.status, member.name
                            )
                            stop = True
                        elif result.status in ("failed", "timeout"):
                            logger.warning(
                                "Non-critical stage %s: %s",
                                result.status,
                                member.name,
                            )
                else:
                    for member in to_run:
                        result = self.execute_stage(member)
                        self.results.append(result)
                        if result.status in ("failed", "timeout") and member.critical:
                            logger.error(
                                "Critical stage %s: %s", result.status, member.name
                            )
                            logger.error("Stopping pipeline execution")
                            stop = True
                            break
                        if result.status in ("failed", "timeout"):
                            logger.warning(
                                "Non-critical stage %s: %s",
                                result.status,
                                member.name,
                            )
                            logger.info("Continuing with next stage")

                if stop:
                    if len(to_run) > 1:
                        logger.error("Stopping pipeline execution")
                    break
                index += len(group)

            counts = _status_counts(self.results)
            overall_success = _critical_stages_succeeded(self.results, self.stages)

            logger.info("Pipeline summary:")
            logger.info(f"   Completed: {counts['success']}/{counts['total']} stages")
            if counts["failed"] > 0:
                failed_names = [r.name for r in self.results if r.status == "failed"]
                logger.info(f"   Failed: {', '.join(failed_names)}")
            if counts["timeout"] > 0:
                timeout_names = [r.name for r in self.results if r.status == "timeout"]
                logger.info(f"   Timed out: {', '.join(timeout_names)}")
            if counts["skipped"] > 0:
                skipped_names = [r.name for r in self.results if r.status == "skipped"]
                logger.info(f"   Skipped: {', '.join(skipped_names)}")

            self._save_report(pipeline_start)

        finally:
            SessionManager.close_session()
            logger.debug("Closed shared HTTP session at pipeline end")

            log_script_end(
                logger, "Analyst Bot Pipeline", pipeline_start, overall_success
            )

        return overall_success

    def _save_report(self, pipeline_start: datetime) -> None:
        now = datetime.now(UTC)
        duration = (now - pipeline_start.astimezone(UTC)).total_seconds()

        report = PipelineReport(
            timestamp=now.isoformat(),
            pipeline_start=pipeline_start.astimezone(UTC).isoformat(),
            pipeline_duration_seconds=duration,
            stages=[asdict(r) for r in self.results],
            overall_success=_critical_stages_succeeded(self.results, self.stages),
            stages_summary=_status_counts(self.results),
        )

        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(json.dumps(asdict(report), indent=2))
            logger.info(f"Pipeline report saved to: {self.report_path}")
        except Exception as e:
            logger.warning(f"Failed to save pipeline report: {e}")


def run_pipeline(
    settings: Settings,
    stages: list[StageConfig],
) -> bool:
    missing: list[str] = []
    seen_missing = set()
    for stage in stages:
        if not stage.critical or not stage.validation_stage:
            continue
        is_valid, stage_missing = settings.validate(stage.validation_stage)
        if is_valid:
            continue
        for var_name in stage_missing:
            if var_name not in seen_missing:
                missing.append(var_name)
                seen_missing.add(var_name)

    if missing:
        logger.error(f"Missing required settings for critical stages: {missing}")
        return False

    settings.ensure_directories()

    runner = PipelineRunner(settings, stages)
    return runner.run()
