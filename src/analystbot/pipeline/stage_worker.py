from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable
from typing import Any, cast

from analystbot.api.http_client import SessionManager
from analystbot.core.config import Settings
from analystbot.core.log_config import setup_logger

logger = setup_logger("pipeline.stage_worker")

_HANDLERS: dict[str, tuple[str, str]] = {
    "market_data": ("analystbot.services.market_data", "run"),
    "heatmap_charts": ("analystbot.services.heatmap_charts", "run"),
    "fred_data": ("analystbot.services.fred_data", "run"),
    "charting": ("analystbot.services.charting", "run"),
    "charting_forced": (
        "analystbot.pipeline.stage_worker",
        "_run_charting_forced",
    ),
    "notifications_delivery": (
        "analystbot.services.notifications.delivery",
        "run_delivery",
    ),
    "notifications_fred_forced": (
        "analystbot.pipeline.stage_worker",
        "_run_fred_telegram_forced",
    ),
    "notifications_album_forced": (
        "analystbot.services.notifications.delivery",
        "run_chart_album_forced",
    ),
}


def _run_charting_forced(settings: Settings, series_ids: list[str]) -> bool:
    from analystbot.pipeline.stages import _get_config_by_series_id
    from analystbot.services import charting

    plot_configs = [_get_config_by_series_id(sid) for sid in series_ids]
    return charting.run_forced(settings, plot_configs=plot_configs)


def _run_fred_telegram_forced(settings: Settings, series_ids: list[str]) -> bool:
    from analystbot.services import notifications

    return notifications.run_fred_telegram_forced(settings, series_ids=series_ids)


def _load_handler(worker_id: str) -> Callable[..., bool]:
    module_name, attr = _HANDLERS[worker_id]
    module = importlib.import_module(module_name)
    return cast(Callable[..., bool], getattr(module, attr))


def _build_handlers() -> dict[str, Callable[..., bool]]:
    return {worker_id: _load_handler(worker_id) for worker_id in _HANDLERS}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        logger.error(
            "Usage: python -m analystbot.pipeline.stage_worker "
            "<worker_id> [json_kwargs]"
        )
        return 2

    worker_id = args[0]
    kwargs: dict[str, Any] = {}
    if len(args) > 1:
        try:
            loaded = json.loads(args[1])
        except json.JSONDecodeError as exc:
            logger.error(f"Invalid worker kwargs JSON: {exc}")
            return 2
        if not isinstance(loaded, dict):
            logger.error("Worker kwargs must be a JSON object")
            return 2
        kwargs = loaded

    if worker_id not in _HANDLERS:
        logger.error(f"Unknown worker_id: {worker_id!r}")
        return 2

    settings = Settings()
    try:
        handler = _load_handler(worker_id)
        success = bool(handler(settings, **kwargs))
        return 0 if success else 1
    except Exception as exc:
        logger.error(f"Stage worker {worker_id!r} failed: {exc}", exc_info=True)
        return 2
    finally:
        SessionManager.close_session()


if __name__ == "__main__":
    sys.exit(main())
