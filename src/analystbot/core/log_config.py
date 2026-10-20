import logging
import os
from datetime import datetime
from pathlib import Path

from analystbot.core.config import get_project_root

DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _get_logs_dir() -> Path:
    project_root = get_project_root()
    return project_root / "logs"


def _create_formatter(
    log_format: str = DEFAULT_LOG_FORMAT, date_format: str = DEFAULT_DATE_FORMAT
) -> logging.Formatter:
    return logging.Formatter(log_format, datefmt=date_format)


def _create_file_handler(
    log_file: Path,
    level: int = logging.DEBUG,
    formatter: logging.Formatter | None = None,
) -> logging.FileHandler:
    handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    handler.setLevel(level)
    if formatter:
        handler.setFormatter(formatter)
    return handler


def _create_console_handler(
    level: int = logging.INFO, formatter: logging.Formatter | None = None
) -> logging.StreamHandler:
    handler = logging.StreamHandler()
    handler.setLevel(level)
    if formatter:
        handler.setFormatter(formatter)
    return handler


def _file_logging_enabled() -> bool:
    raw = os.environ.get("ANALYSTBOT_LOG_TO_FILE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def setup_logger(name: str, level: int = DEFAULT_LOG_LEVEL) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = _create_formatter()
    logger.addHandler(_create_console_handler(logging.INFO, formatter))

    if _file_logging_enabled():
        logs_dir = _get_logs_dir()
        logs_dir.mkdir(exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = logs_dir / f"analystbot_{today}.log"
        logger.addHandler(_create_file_handler(log_file, logging.DEBUG, formatter))

    return logger


def log_script_start(logger: logging.Logger, script_name: str) -> None:
    timestamp = datetime.now().strftime(DEFAULT_DATE_FORMAT)
    separator = "=" * 60

    logger.info(separator)
    logger.info(f"Starting {script_name}")
    logger.info(f"Started at: {timestamp}")
    logger.info(separator)


def log_script_end(
    logger: logging.Logger, script_name: str, start_time: datetime, success: bool
) -> None:
    end_time = (
        datetime.now(start_time.tzinfo)
        if start_time.tzinfo is not None
        else datetime.now()
    )
    duration = end_time - start_time
    status_label = "SUCCESS" if success else "FAILED"
    separator = "=" * 60

    log_func = logger.info if success else logger.error

    log_func(separator)
    log_func(f"{status_label}: {script_name} completed in {duration}")
    log_func(f"Ended at: {end_time.strftime(DEFAULT_DATE_FORMAT)}")
    log_func(separator)
