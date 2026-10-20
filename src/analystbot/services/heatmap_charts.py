from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

import matplotlib

from analystbot.api.tastytrade import TastytradeClient
from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.db import StockRepository, connect
from analystbot.db.date_utils import unix_to_date_str
from analystbot.services.market_data import TOP_100_COMPANIES
from analystbot.services.period_headings import format_period_heading

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Rectangle

logger = setup_logger("services.heatmap_charts")


GICS_SECTORS: frozenset[str] = frozenset(
    {
        "Communication Services",
        "Consumer Discretionary",
        "Consumer Staples",
        "Energy",
        "Financials",
        "Health Care",
        "Industrials",
        "Materials",
        "Real Estate",
        "Technology",
        "Utilities",
    }
)

SECTOR_LABELS: dict[str, str] = {
    "Communication Services": "COMMUNICATION SERVICES",
    "Consumer Discretionary": "CONSUMER CYCLICAL",
    "Consumer Staples": "CONSUMER DEFENSIVE",
    "Energy": "ENERGY",
    "Financials": "FINANCIAL",
    "Health Care": "HEALTHCARE",
    "Industrials": "INDUSTRIALS",
    "Materials": "BASIC MATERIALS",
    "Real Estate": "REAL ESTATE",
    "Technology": "TECHNOLOGY",
    "Utilities": "UTILITIES",
}

COMPANY_GROUPS: dict[str, tuple[str, str]] = {
    "AAPL": ("Technology", "Consumer Electronics"),
    "ABBV": ("Health Care", "Drug Manufacturers - General"),
    "ABT": ("Health Care", "Medical Devices"),
    "ADI": ("Technology", "Semiconductors"),
    "AMAT": ("Technology", "Semiconductor Equipment & Materials"),
    "AMD": ("Technology", "Semiconductors"),
    "AMGN": ("Health Care", "Drug Manufacturers - General"),
    "AMZN": ("Consumer Discretionary", "Internet Retail"),
    "ANET": ("Technology", "Computer Hardware"),
    "APH": ("Technology", "Electronic Components"),
    "AVGO": ("Technology", "Semiconductors"),
    "AXP": ("Financials", "Credit Services"),
    "BA": ("Industrials", "Aerospace & Defense"),
    "BAC": ("Financials", "Banks - Diversified"),
    "BKNG": ("Consumer Discretionary", "Travel Services"),
    "BLK": ("Financials", "Asset Management"),
    "BMY": ("Health Care", "Drug Manufacturers - General"),
    "BRK.B": ("Financials", "Insurance - Diversified"),
    "BX": ("Financials", "Asset Management"),
    "C": ("Financials", "Banks - Diversified"),
    "CAT": ("Industrials", "Farm & Heavy Construction Machinery"),
    "COF": ("Financials", "Credit Services"),
    "COP": ("Energy", "Oil & Gas E&P"),
    "COST": ("Consumer Staples", "Discount Stores"),
    "CRM": ("Technology", "Software - Application"),
    "CRWD": ("Technology", "Software - Infrastructure"),
    "CSCO": ("Technology", "Communication Equipment"),
    "CVS": ("Health Care", "Healthcare Plans"),
    "CVX": ("Energy", "Oil & Gas Integrated"),
    "DE": ("Industrials", "Farm & Heavy Construction Machinery"),
    "DELL": ("Technology", "Computer Hardware"),
    "DHR": ("Health Care", "Diagnostics & Research"),
    "DIS": ("Communication Services", "Entertainment"),
    "GE": ("Industrials", "Aerospace & Defense"),
    "GEV": ("Industrials", "Specialty Industrial Machinery"),
    "GILD": ("Health Care", "Drug Manufacturers - General"),
    "GLW": ("Technology", "Electronic Components"),
    "GOOGL": ("Communication Services", "Internet Content & Information"),
    "GS": ("Financials", "Capital Markets"),
    "HD": ("Consumer Discretionary", "Home Improvement Retail"),
    "IBM": ("Technology", "Information Technology Services"),
    "IBKR": ("Financials", "Capital Markets"),
    "INTC": ("Technology", "Semiconductors"),
    "ISRG": ("Health Care", "Medical Devices"),
    "JNJ": ("Health Care", "Drug Manufacturers - General"),
    "JPM": ("Financials", "Banks - Diversified"),
    "KLAC": ("Technology", "Semiconductor Equipment & Materials"),
    "KO": ("Consumer Staples", "Beverages - Non-Alcoholic"),
    "LMT": ("Industrials", "Aerospace & Defense"),
    "LRCX": ("Technology", "Semiconductor Equipment & Materials"),
    "LLY": ("Health Care", "Drug Manufacturers - General"),
    "MA": ("Financials", "Credit Services"),
    "MCD": ("Consumer Discretionary", "Restaurants"),
    "META": ("Communication Services", "Internet Content & Information"),
    "MO": ("Consumer Staples", "Tobacco"),
    "MRK": ("Health Care", "Drug Manufacturers - General"),
    "MRVL": ("Technology", "Semiconductors"),
    "MS": ("Financials", "Capital Markets"),
    "MSFT": ("Technology", "Software - Infrastructure"),
    "MU": ("Technology", "Semiconductors"),
    "NEE": ("Utilities", "Utilities - Regulated Electric"),
    "NEM": ("Materials", "Gold"),
    "NFLX": ("Communication Services", "Entertainment"),
    "NOW": ("Technology", "Software - Application"),
    "NVDA": ("Technology", "Semiconductors"),
    "ORCL": ("Technology", "Software - Infrastructure"),
    "PANW": ("Technology", "Software - Infrastructure"),
    "PEP": ("Consumer Staples", "Beverages - Non-Alcoholic"),
    "PFE": ("Health Care", "Drug Manufacturers - General"),
    "PG": ("Consumer Staples", "Household & Personal Products"),
    "PGR": ("Financials", "Insurance - Property & Casualty"),
    "PH": ("Industrials", "Specialty Industrial Machinery"),
    "PLD": ("Real Estate", "REIT - Industrial"),
    "PLTR": ("Technology", "Software - Infrastructure"),
    "PM": ("Consumer Staples", "Tobacco"),
    "QCOM": ("Technology", "Semiconductors"),
    "RTX": ("Industrials", "Aerospace & Defense"),
    "SCCO": ("Materials", "Copper"),
    "SCHW": ("Financials", "Capital Markets"),
    "SNDK": ("Technology", "Semiconductors"),
    "SNOW": ("Technology", "Software - Application"),
    "SPCX": ("Industrials", "Aerospace & Defense"),
    "SPGI": ("Financials", "Financial Data & Stock Exchanges"),
    "T": ("Communication Services", "Telecom Services"),
    "TJX": ("Consumer Discretionary", "Apparel Retail"),
    "TMO": ("Health Care", "Diagnostics & Research"),
    "TMUS": ("Communication Services", "Telecom Services"),
    "TSLA": ("Consumer Discretionary", "Auto Manufacturers"),
    "TXN": ("Technology", "Semiconductors"),
    "UBER": ("Industrials", "Rental & Leasing Services"),
    "UNH": ("Health Care", "Healthcare Plans"),
    "UNP": ("Industrials", "Railroads"),
    "V": ("Financials", "Credit Services"),
    "VRTX": ("Health Care", "Biotechnology"),
    "VZ": ("Communication Services", "Telecom Services"),
    "WDC": ("Technology", "Computer Hardware"),
    "WELL": ("Real Estate", "REIT - Healthcare Facilities"),
    "WFC": ("Financials", "Banks - Diversified"),
    "WMT": ("Consumer Staples", "Discount Stores"),
    "XOM": ("Energy", "Oil & Gas Integrated"),
}

COMPANY_SECTORS: dict[str, str] = {
    symbol: sector for symbol, (sector, _) in COMPANY_GROUPS.items()
}

HEATMAP_OUTPUT_DIRNAME = "chart_graphs"
HEATMAP_OUTPUT_FILENAME = "market_heatmap_top100_d1.png"
HEATMAP_SUMMARY_FILENAME = "market_heatmap_top100_d1.json"
HEATMAP_TITLE = "Top 100 Market Heatmap"

PCT_CLIP_DEFAULT = 3.0
SECTOR_HEADER_DEFAULT = 0.028
INDUSTRY_HEADER_DEFAULT = 0.019
HEADER_MAX_FRACTION = 0.20
MIN_SECTOR_HEADER = 0.014
MIN_INDUSTRY_HEADER = 0.011

HEATMAP_FONT_CANDIDATES: tuple[str, ...] = (
    "Liberation Sans",
    "Nimbus Sans",
    "FreeSans",
    "DejaVu Sans",
)


def _pick_heatmap_font() -> str:
    available = {item.name for item in font_manager.fontManager.ttflist}
    for name in HEATMAP_FONT_CANDIDATES:
        if name in available:
            return name
    return "DejaVu Sans"


HEATMAP_FONT = _pick_heatmap_font()

AXES_BOUNDS = (0.008, 0.048, 0.984, 0.900)
LEGEND_WIDTH = 0.20
LEGEND_HEIGHT = 0.028
LEGEND_BOTTOM = 0.010
LEGEND_LEFT = (1.0 - LEGEND_WIDTH) / 2.0
BACKGROUND_HEX = "#1d1f26"
LABEL_HEX = "#f4f5f7"
INDUSTRY_LABEL_HEX = "#c5c7ce"
LEGEND_LABEL_HEX = "#9aa0ab"


def _rgb8(red: int, green: int, blue: int) -> tuple[float, float, float]:
    return (red / 255.0, green / 255.0, blue / 255.0)


RED_RGB = _rgb8(246, 53, 56)
MID_RED_RGB = _rgb8(191, 64, 69)
DIM_RED_RGB = _rgb8(139, 68, 78)
GRAY_RGB = _rgb8(65, 69, 84)
DIM_GREEN_RGB = _rgb8(53, 118, 78)
MID_GREEN_RGB = _rgb8(47, 158, 79)
GREEN_RGB = _rgb8(48, 204, 90)

COLOR_STOPS: tuple[tuple[float, tuple[float, float, float]], ...] = (
    (-1.0, RED_RGB),
    (-2.0 / 3.0, MID_RED_RGB),
    (-1.0 / 3.0, DIM_RED_RGB),
    (0.0, GRAY_RGB),
    (1.0 / 3.0, DIM_GREEN_RGB),
    (2.0 / 3.0, MID_GREEN_RGB),
    (1.0, GREEN_RGB),
)


def _validate_company_groups() -> None:
    expected = set(TOP_100_COMPANIES)
    actual = set(COMPANY_GROUPS)
    if expected != actual:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise RuntimeError(
            "COMPANY_GROUPS must match TOP_100_COMPANIES "
            f"(missing: {missing}; extra: {extra})"
        )
    invalid = sorted(
        {
            f"{symbol}={sector}"
            for symbol, (sector, industry) in COMPANY_GROUPS.items()
            if sector not in GICS_SECTORS or not industry.strip()
        }
    )
    if invalid:
        raise RuntimeError(
            "Unknown GICS sector or empty industry: " + ", ".join(invalid)
        )
    missing_labels = sorted(GICS_SECTORS - set(SECTOR_LABELS))
    extra_labels = sorted(set(SECTOR_LABELS) - GICS_SECTORS)
    if missing_labels or extra_labels:
        raise RuntimeError(
            "SECTOR_LABELS must match GICS_SECTORS "
            f"(missing: {', '.join(missing_labels) or 'none'}; "
            f"extra: {', '.join(extra_labels) or 'none'})"
        )


_validate_company_groups()


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def top(self) -> float:
        return self.y + self.height

    @property
    def right(self) -> float:
        return self.x + self.width


@dataclass(frozen=True)
class HeatmapTile:
    symbol: str
    sector: str
    industry: str
    market_cap: float
    pct_1d: float
    date_ts: int


@dataclass(frozen=True)
class HeatmapSummary:
    date_str: str
    tile_count: int
    universe_count: int
    advancers: int
    decliners: int
    median_pct: float | None


@dataclass(frozen=True)
class HeatmapLayout:
    sectors: list[tuple[str, Rect]]
    industries: list[tuple[str, str, Rect]]
    sector_headers: list[tuple[str, Rect]]
    industry_headers: list[tuple[str, str, Rect]]
    tiles: list[tuple[HeatmapTile, Rect]]


@dataclass(frozen=True)
class HeatmapChartConfig:
    symbols: tuple[str, ...] = TOP_100_COMPANIES
    pct_clip: float = PCT_CLIP_DEFAULT
    sector_header: float = SECTOR_HEADER_DEFAULT
    industry_header: float = INDUSTRY_HEADER_DEFAULT
    sector_gap: float = 0.0022
    industry_gap: float = 0.0009
    tile_gap: float = 0.00055
    figure_size: tuple[float, float] = (20.0, 11.4)
    dpi: int = 160
    title_fontsize: int = 26
    header_fontsize: float = 15.0
    industry_fontsize: float = 8.0
    ticker_fontsize: int = 44
    pct_fontsize: int = 26
    output_dirname: str = HEATMAP_OUTPUT_DIRNAME
    output_filename: str = HEATMAP_OUTPUT_FILENAME
    summary_filename: str = HEATMAP_SUMMARY_FILENAME


def get_output_path(settings: Settings) -> Path:
    return settings.data_dir / HEATMAP_OUTPUT_DIRNAME / HEATMAP_OUTPUT_FILENAME


def get_summary_path(settings: Settings) -> Path:
    return settings.data_dir / HEATMAP_OUTPUT_DIRNAME / HEATMAP_SUMMARY_FILENAME


def pct_change_1d(prev: float, last: float) -> float | None:
    try:
        previous = float(prev)
        current = float(last)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(previous) or not math.isfinite(current) or previous == 0:
        return None
    return (current / previous - 1.0) * 100.0


def color_for_pct(
    pct: float, clip: float = PCT_CLIP_DEFAULT
) -> tuple[float, float, float]:
    if clip <= 0:
        raise ValueError("clip must be positive")
    t = max(-1.0, min(1.0, pct / clip))
    for index, (t1, color1) in enumerate(COLOR_STOPS):
        if t > t1 and index < len(COLOR_STOPS) - 1:
            continue
        if index == 0:
            return color1
        t0, color0 = COLOR_STOPS[index - 1]
        span = t1 - t0
        blend = 0.0 if span == 0 else (t - t0) / span
        return (
            color0[0] + blend * (color1[0] - color0[0]),
            color0[1] + blend * (color1[1] - color0[1]),
            color0[2] + blend * (color1[2] - color0[2]),
        )
    return COLOR_STOPS[-1][1]


def inset_rect(rect: Rect, pad: float) -> Rect:
    pad_x = min(max(pad, 0.0), rect.width / 2.0)
    pad_y = min(max(pad, 0.0), rect.height / 2.0)
    return Rect(
        rect.x + pad_x,
        rect.y + pad_y,
        max(rect.width - 2.0 * pad_x, 0.0),
        max(rect.height - 2.0 * pad_y, 0.0),
    )


def fitted_fontsize(
    text: str,
    rect: Rect,
    *,
    axes_inches: tuple[float, float],
    max_size: float,
    width_ratio: float,
    height_ratio: float,
    char_em: float = 0.50,
) -> float:
    ax_w, ax_h = axes_inches
    inch_w = max(rect.width * ax_w, 1e-9)
    inch_h = max(rect.height * ax_h, 1e-9)
    nchars = max(len(text), 1)
    by_width = (inch_w * width_ratio * 72.0) / (char_em * nchars)
    by_height = inch_h * height_ratio * 72.0
    return max(0.0, min(max_size, by_width, by_height))


def latest_two_closes(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[float, float, int] | None:
    valid: list[tuple[float, int]] = []
    for row in rows:
        close = row.get("close_price")
        date_ts = row.get("date")
        if close is None or date_ts is None:
            continue
        try:
            close_value = float(close)
            timestamp = int(date_ts)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close_value):
            continue
        valid.append((close_value, timestamp))
    if len(valid) < 2:
        return None
    prev_close, _ = valid[-2]
    last_close, last_ts = valid[-1]
    return prev_close, last_close, last_ts


def build_heatmap_tiles(
    symbols: Sequence[str],
    *,
    groups: Mapping[str, tuple[str, str]],
    market_caps: Mapping[str, float],
    closes: Mapping[str, tuple[float, float, int]],
) -> list[HeatmapTile]:
    tiles: list[HeatmapTile] = []
    for symbol in symbols:
        group = groups.get(symbol)
        cap = market_caps.get(symbol)
        pair = closes.get(symbol)
        if group is None:
            logger.warning("Skipping %s: no GICS sector/industry mapping", symbol)
            continue
        sector, industry = group
        if cap is None or not math.isfinite(cap) or cap <= 0:
            logger.warning("Skipping %s: missing or non-positive market cap", symbol)
            continue
        if pair is None:
            logger.warning("Skipping %s: need two closes for 1-day change", symbol)
            continue
        prev_close, last_close, date_ts = pair
        pct = pct_change_1d(prev_close, last_close)
        if pct is None:
            logger.warning("Skipping %s: undefined 1-day change", symbol)
            continue
        tiles.append(
            HeatmapTile(
                symbol=symbol,
                sector=sector,
                industry=industry,
                market_cap=float(cap),
                pct_1d=pct,
                date_ts=date_ts,
            )
        )
    return tiles


def summarize_tiles(
    tiles: Sequence[HeatmapTile], *, universe_count: int
) -> HeatmapSummary:
    if not tiles:
        return HeatmapSummary(
            date_str="N/A",
            tile_count=0,
            universe_count=universe_count,
            advancers=0,
            decliners=0,
            median_pct=None,
        )
    pcts = [tile.pct_1d for tile in tiles]
    return HeatmapSummary(
        date_str=unix_to_date_str(max(tile.date_ts for tile in tiles)),
        tile_count=len(tiles),
        universe_count=universe_count,
        advancers=sum(1 for pct in pcts if pct > 0),
        decliners=sum(1 for pct in pcts if pct < 0),
        median_pct=float(median(pcts)),
    )


def write_heatmap_summary(path: Path, summary: HeatmapSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date_str": summary.date_str,
        "tile_count": summary.tile_count,
        "universe_count": summary.universe_count,
        "advancers": summary.advancers,
        "decliners": summary.decliners,
        "median_pct": summary.median_pct,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_heatmap_summary(path: Path) -> HeatmapSummary | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        median_raw = payload.get("median_pct")
        median_pct = float(median_raw) if median_raw is not None else None
        return HeatmapSummary(
            date_str=str(payload.get("date_str") or "N/A"),
            tile_count=int(payload.get("tile_count") or 0),
            universe_count=int(payload.get("universe_count") or 0),
            advancers=int(payload.get("advancers") or 0),
            decliners=int(payload.get("decliners") or 0),
            median_pct=median_pct,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read heatmap summary %s: %s", path, exc)
        return None


def squarify(items: Sequence[tuple[str, float]], rect: Rect) -> list[tuple[str, Rect]]:
    positive = [(key, weight) for key, weight in items if weight > 0]
    total = sum(weight for _, weight in positive)
    if total <= 0 or rect.width <= 0 or rect.height <= 0:
        return []
    scaled = [
        (key, weight / total * rect.area)
        for key, weight in sorted(positive, key=lambda item: item[1], reverse=True)
    ]
    return _squarify_layout(scaled, rect)


def _aspect_worst(areas: Sequence[float], length: float) -> float:
    if not areas or length <= 0:
        return float("inf")
    total = sum(areas)
    if total <= 0:
        return float("inf")
    return max(
        max(area * length * length / (total * total) for area in areas),
        max(total * total / (area * length * length) for area in areas),
    )


def _layout_row(row: list[tuple[str, float]], rect: Rect) -> list[tuple[str, Rect]]:
    total = sum(area for _, area in row)
    if total <= 0:
        return []
    placed: list[tuple[str, Rect]] = []
    if rect.width >= rect.height:
        strip_w = total / rect.height if rect.height else 0.0
        y = rect.y
        for index, (key, area) in enumerate(row):
            height = area / strip_w if strip_w else 0.0
            if index == len(row) - 1:
                height = rect.y + rect.height - y
            placed.append((key, Rect(rect.x, y, strip_w, height)))
            y += height
    else:
        strip_h = total / rect.width if rect.width else 0.0
        x = rect.x
        for index, (key, area) in enumerate(row):
            width = area / strip_h if strip_h else 0.0
            if index == len(row) - 1:
                width = rect.x + rect.width - x
            placed.append((key, Rect(x, rect.y, width, strip_h)))
            x += width
    return placed


def _remaining_after_row(row: list[tuple[str, float]], rect: Rect) -> Rect:
    total = sum(area for _, area in row)
    if rect.width >= rect.height:
        strip_w = total / rect.height if rect.height else 0.0
        return Rect(
            rect.x + strip_w,
            rect.y,
            max(rect.width - strip_w, 0.0),
            rect.height,
        )
    strip_h = total / rect.width if rect.width else 0.0
    return Rect(
        rect.x,
        rect.y + strip_h,
        rect.width,
        max(rect.height - strip_h, 0.0),
    )


def _layout_row_fill(
    row: list[tuple[str, float]], rect: Rect
) -> list[tuple[str, Rect]]:
    total = sum(area for _, area in row) or 1.0
    placed: list[tuple[str, Rect]] = []
    if rect.width >= rect.height:
        y = rect.y
        for index, (key, area) in enumerate(row):
            height = rect.height * (area / total)
            if index == len(row) - 1:
                height = rect.y + rect.height - y
            placed.append((key, Rect(rect.x, y, rect.width, height)))
            y += height
    else:
        x = rect.x
        for index, (key, area) in enumerate(row):
            width = rect.width * (area / total)
            if index == len(row) - 1:
                width = rect.x + rect.width - x
            placed.append((key, Rect(x, rect.y, width, rect.height)))
            x += width
    return placed


def _squarify_layout(
    items: list[tuple[str, float]], rect: Rect
) -> list[tuple[str, Rect]]:
    if not items:
        return []
    if len(items) == 1:
        return [(items[0][0], rect)]

    length = min(rect.width, rect.height)
    row: list[tuple[str, float]] = []
    remaining = list(items)
    while remaining:
        key, area = remaining[0]
        trial = [*row, (key, area)]
        if row and _aspect_worst(
            [item_area for _, item_area in trial], length
        ) > _aspect_worst([item_area for _, item_area in row], length):
            placed = _layout_row(row, rect)
            leftover = _remaining_after_row(row, rect)
            return placed + _squarify_layout(remaining, leftover)
        row = trial
        remaining = remaining[1:]
    return _layout_row_fill(row, rect)


def header_height(
    rect: Rect,
    desired: float,
    *,
    minimum: float,
    max_fraction: float = HEADER_MAX_FRACTION,
) -> float:
    if desired <= 0 or rect.height <= 0 or rect.width <= 0:
        return 0.0
    height = min(desired, rect.height * max_fraction)
    if height < minimum:
        return 0.0
    return height


def split_header(
    rect: Rect,
    desired: float,
    *,
    minimum: float,
    max_fraction: float = HEADER_MAX_FRACTION,
) -> tuple[Rect, Rect]:
    height = header_height(rect, desired, minimum=minimum, max_fraction=max_fraction)
    header = Rect(rect.x, rect.top - height, rect.width, height)
    body = Rect(rect.x, rect.y, rect.width, max(rect.height - height, 0.0))
    return header, body


def layout_heatmap(
    tiles: Sequence[HeatmapTile],
    rect: Rect,
    *,
    sector_header: float = SECTOR_HEADER_DEFAULT,
    industry_header: float = INDUSTRY_HEADER_DEFAULT,
    sector_gap: float = 0.0022,
    industry_gap: float = 0.0009,
) -> HeatmapLayout:
    by_sector: dict[str, list[HeatmapTile]] = {}
    for tile in tiles:
        by_sector.setdefault(tile.sector, []).append(tile)

    sector_weights = [
        (sector, sum(item.market_cap for item in group))
        for sector, group in by_sector.items()
    ]
    sector_rects = squarify(sector_weights, rect)
    industry_rects: list[tuple[str, str, Rect]] = []
    sector_headers: list[tuple[str, Rect]] = []
    industry_headers: list[tuple[str, str, Rect]] = []
    placed_tiles: list[tuple[HeatmapTile, Rect]] = []

    for sector, sector_rect in sector_rects:
        sector_inner = inset_rect(sector_rect, sector_gap)
        sector_header_rect, sector_body = split_header(
            sector_inner, sector_header, minimum=MIN_SECTOR_HEADER
        )
        if sector_header_rect.height > 0:
            sector_headers.append((sector, sector_header_rect))
        by_industry: dict[str, list[HeatmapTile]] = {}
        for tile in by_sector[sector]:
            by_industry.setdefault(tile.industry, []).append(tile)
        industry_weights = [
            (industry, sum(item.market_cap for item in group))
            for industry, group in by_industry.items()
        ]
        for industry, industry_rect in squarify(industry_weights, sector_body):
            industry_inner = inset_rect(industry_rect, industry_gap)
            industry_rects.append((sector, industry, industry_inner))
            industry_header_rect, industry_body = split_header(
                industry_inner, industry_header, minimum=MIN_INDUSTRY_HEADER
            )
            if industry_header_rect.height > 0:
                industry_headers.append((sector, industry, industry_header_rect))
            group = by_industry[industry]
            tile_by_symbol = {item.symbol: item for item in group}
            stock_items = [(item.symbol, item.market_cap) for item in group]
            for symbol, stock_rect in squarify(stock_items, industry_body):
                placed_tiles.append((tile_by_symbol[symbol], stock_rect))

    return HeatmapLayout(
        sectors=sector_rects,
        industries=industry_rects,
        sector_headers=sector_headers,
        industry_headers=industry_headers,
        tiles=placed_tiles,
    )


class HeatmapChartService:
    def __init__(self, settings: Settings, config: HeatmapChartConfig | None = None):
        self.settings = settings
        self.config = config or HeatmapChartConfig()
        self.output_dir = settings.data_dir / self.config.output_dirname
        self.output_path = self.output_dir / self.config.output_filename
        self.summary_path = self.output_dir / self.config.summary_filename

    def collect_closes(
        self, repo: StockRepository
    ) -> dict[str, tuple[float, float, int]]:
        closes: dict[str, tuple[float, float, int]] = {}
        for symbol in self.config.symbols:
            pair = latest_two_closes(repo.get_stock_data(symbol, days=5))
            if pair is not None:
                closes[symbol] = pair
        return closes

    def generate_chart(
        self, repo: StockRepository, market_caps: Mapping[str, float]
    ) -> bool:
        closes = self.collect_closes(repo)
        tiles = build_heatmap_tiles(
            self.config.symbols,
            groups=COMPANY_GROUPS,
            market_caps=market_caps,
            closes=closes,
        )
        if not tiles:
            logger.warning("No heatmap tiles available to plot")
            return False

        summary = summarize_tiles(tiles, universe_count=len(self.config.symbols))
        layout = layout_heatmap(
            tiles,
            Rect(0.0, 0.0, 1.0, 1.0),
            sector_header=self.config.sector_header,
            industry_header=self.config.industry_header,
            sector_gap=self.config.sector_gap,
            industry_gap=self.config.industry_gap,
        )

        try:
            fig = plt.figure(
                figsize=self.config.figure_size,
                facecolor=BACKGROUND_HEX,
            )
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["font.sans-serif"] = [HEATMAP_FONT, "DejaVu Sans"]
            ax = fig.add_axes(AXES_BOUNDS)
            ax.set_facecolor(BACKGROUND_HEX)
            ax.set_xlim(0.0, 1.0)
            ax.set_ylim(0.0, 1.0)
            ax.set_axis_off()
            fig.text(
                0.5,
                0.978,
                format_period_heading(HEATMAP_TITLE, "d", summary.date_str),
                color=LABEL_HEX,
                fontsize=self.config.title_fontsize,
                fontweight="bold",
                fontfamily=HEATMAP_FONT,
                ha="center",
                va="top",
            )

            for tile, tile_rect in layout.tiles:
                self._draw_stock_tile(ax, tile, tile_rect)
            for _, sector_rect in layout.sectors:
                self._draw_sector_border(ax, sector_rect)
            for _, industry, header_rect in layout.industry_headers:
                self._draw_group_label(
                    ax,
                    industry.upper(),
                    header_rect,
                    fontsize=self.config.industry_fontsize,
                    color=INDUSTRY_LABEL_HEX,
                    fontweight="normal",
                    zorder=6,
                )
            for sector, header_rect in layout.sector_headers:
                self._draw_group_label(
                    ax,
                    SECTOR_LABELS.get(sector, sector.upper()),
                    header_rect,
                    fontsize=self.config.header_fontsize,
                    color=LABEL_HEX,
                    fontweight="bold",
                    zorder=7,
                )

            self._draw_legend(fig)

            self.output_dir.mkdir(parents=True, exist_ok=True)
            fig.savefig(
                self.output_path,
                dpi=self.config.dpi,
                facecolor=BACKGROUND_HEX,
                edgecolor="none",
            )
            plt.close(fig)
            write_heatmap_summary(self.summary_path, summary)
            logger.info("Generated market heatmap: %s", self.output_path)
            return True
        except Exception as exc:
            logger.error("Failed to generate market heatmap: %s", exc, exc_info=True)
            plt.close("all")
            return False

    def _axes_size_inches(self) -> tuple[float, float]:
        fig_w, fig_h = self.config.figure_size
        return fig_w * AXES_BOUNDS[2], fig_h * AXES_BOUNDS[3]

    def _fitted_fontsize(
        self,
        text: str,
        rect: Rect,
        *,
        max_size: float,
        width_ratio: float,
        height_ratio: float,
    ) -> float:
        return fitted_fontsize(
            text,
            rect,
            axes_inches=self._axes_size_inches(),
            max_size=max_size,
            width_ratio=width_ratio,
            height_ratio=height_ratio,
        )

    def _draw_stock_tile(self, ax: Any, tile: HeatmapTile, rect: Rect) -> None:
        draw = inset_rect(rect, self.config.tile_gap / 2.0)
        if draw.width <= 0 or draw.height <= 0:
            return
        face = color_for_pct(tile.pct_1d, self.config.pct_clip)
        ax.add_patch(
            Rectangle(
                (draw.x, draw.y),
                draw.width,
                draw.height,
                facecolor=face,
                edgecolor=face,
                linewidth=0.0,
                zorder=1,
            )
        )
        ticker_size = self._fitted_fontsize(
            tile.symbol,
            draw,
            max_size=self.config.ticker_fontsize,
            width_ratio=0.80,
            height_ratio=0.36,
        )
        pct_text = f"{tile.pct_1d:+.2f}%"
        pct_size = self._fitted_fontsize(
            pct_text,
            draw,
            max_size=min(self.config.pct_fontsize, ticker_size * 0.62),
            width_ratio=0.76,
            height_ratio=0.20,
        )
        _, ax_h = self._axes_size_inches()
        gap_in = max(ticker_size * 0.14, 0.8) / 72.0
        stack_in = (ticker_size + pct_size) / 72.0 + gap_in
        show_pct = pct_size >= 4.0 and stack_in <= draw.height * ax_h * 0.90
        if not show_pct:
            ticker_size = self._fitted_fontsize(
                tile.symbol,
                draw,
                max_size=self.config.ticker_fontsize,
                width_ratio=0.84,
                height_ratio=0.52,
            )
            if ticker_size < 3.5:
                return
            ax.text(
                draw.x + draw.width / 2.0,
                draw.y + draw.height / 2.0,
                tile.symbol,
                ha="center",
                va="center",
                fontsize=ticker_size,
                color=LABEL_HEX,
                fontweight="bold",
                fontfamily=HEATMAP_FONT,
                clip_on=True,
                zorder=2,
            )
            return
        if ticker_size < 3.5:
            return
        cx = draw.x + draw.width / 2.0
        cy = draw.y + draw.height / 2.0
        ax.text(
            cx,
            cy + draw.height * 0.11,
            tile.symbol,
            ha="center",
            va="center",
            fontsize=ticker_size,
            color=LABEL_HEX,
            fontweight="bold",
            fontfamily=HEATMAP_FONT,
            clip_on=True,
            zorder=2,
        )
        ax.text(
            cx,
            cy - draw.height * 0.17,
            pct_text,
            ha="center",
            va="center",
            fontsize=pct_size,
            color=LABEL_HEX,
            fontweight="normal",
            fontfamily=HEATMAP_FONT,
            clip_on=True,
            zorder=2,
        )

    def _draw_group_label(
        self,
        ax: Any,
        label: str,
        rect: Rect,
        *,
        fontsize: float,
        color: str,
        fontweight: str,
        zorder: int,
    ) -> None:
        if rect.width <= 0 or rect.height <= 0:
            return
        size = self._fitted_fontsize(
            label,
            rect,
            max_size=fontsize,
            width_ratio=0.96,
            height_ratio=0.72,
        )
        if size < 4.5:
            return
        ax.text(
            rect.x + 0.0035,
            rect.y + rect.height / 2.0,
            label,
            ha="left",
            va="center",
            fontsize=size,
            color=color,
            fontweight=fontweight,
            fontfamily=HEATMAP_FONT,
            clip_on=True,
            zorder=zorder,
        )

    def _draw_sector_border(self, ax: Any, rect: Rect) -> None:
        ax.add_patch(
            Rectangle(
                (rect.x, rect.y),
                rect.width,
                rect.height,
                facecolor="none",
                edgecolor="#32363f",
                linewidth=0.9,
                zorder=5,
            )
        )

    def _draw_legend(self, fig: Any) -> None:
        legend_ax = fig.add_axes(
            (LEGEND_LEFT, LEGEND_BOTTOM, LEGEND_WIDTH, LEGEND_HEIGHT)
        )
        legend_ax.set_xlim(0.0, 7.0)
        legend_ax.set_ylim(0.0, 1.0)
        legend_ax.set_axis_off()
        legend_ax.set_facecolor(BACKGROUND_HEX)
        clip = self.config.pct_clip
        steps = (
            -clip,
            -2.0 * clip / 3.0,
            -clip / 3.0,
            0.0,
            clip / 3.0,
            2.0 * clip / 3.0,
            clip,
        )
        for index, step in enumerate(steps):
            face = color_for_pct(step, clip)
            legend_ax.add_patch(
                Rectangle(
                    (index + 0.08, 0.42),
                    0.84,
                    0.50,
                    facecolor=face,
                    edgecolor=BACKGROUND_HEX,
                    linewidth=0.4,
                    zorder=1,
                )
            )
            label = "0%" if step == 0 else f"{step:+.0f}%"
            legend_ax.text(
                index + 0.5,
                0.02,
                label,
                ha="center",
                va="bottom",
                fontsize=6,
                color=LEGEND_LABEL_HEX,
                fontfamily=HEATMAP_FONT,
                zorder=2,
            )


def run(settings: Settings) -> bool:
    log_script_start(logger, "Market Heatmap Chart Generation")
    start_time = datetime.now()

    if not settings.db_path.exists():
        logger.warning("Database not found: %s", settings.db_path)
        log_script_end(logger, "Market Heatmap Chart Generation", start_time, False)
        return False

    is_valid, missing = settings.validate("market_data")
    if not is_valid:
        logger.error("Missing tastytrade credentials for heatmap: %s", missing)
        log_script_end(logger, "Market Heatmap Chart Generation", start_time, False)
        return False

    try:
        client = TastytradeClient.from_settings(settings)
        market_caps = client.get_market_caps(TOP_100_COMPANIES)
        service = HeatmapChartService(settings)
        conn = connect(settings.db_path)
        try:
            repo = StockRepository(conn)
            success = service.generate_chart(repo, market_caps)
        finally:
            conn.close()

        log_script_end(logger, "Market Heatmap Chart Generation", start_time, success)
        return success
    except Exception as exc:
        logger.error("Workflow failed: %s", exc, exc_info=True)
        log_script_end(logger, "Market Heatmap Chart Generation", start_time, False)
        return False
