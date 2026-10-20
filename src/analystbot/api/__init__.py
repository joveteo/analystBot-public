from __future__ import annotations

from typing import Any

__all__ = [
    "RetryPolicy",
    "calculate_backoff_delay",
    "create_session",
    "TastytradeClient",
    "FredClient",
    "TelegramClient",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "RetryPolicy": ("analystbot.api.http_client", "RetryPolicy"),
    "calculate_backoff_delay": (
        "analystbot.api.http_client",
        "calculate_backoff_delay",
    ),
    "create_session": ("analystbot.api.http_client", "create_session"),
    "TastytradeClient": ("analystbot.api.tastytrade", "TastytradeClient"),
    "FredClient": ("analystbot.api.fred", "FredClient"),
    "TelegramClient": ("analystbot.api.telegram", "TelegramClient"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    from importlib import import_module

    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value
