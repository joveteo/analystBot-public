from __future__ import annotations

from typing import Any

__all__ = [
    "market_data",
    "indicators",
    "market_charts",
    "heatmap_charts",
    "rrg_charts",
    "fred_data",
    "charting",
    "notifications",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = import_module(f"{__name__}.{name}")
    globals()[name] = value
    return value
