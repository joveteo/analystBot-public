import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matplotlib
import pandas as pd

from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.db import StockRepository, connect
from analystbot.db.date_utils import unix_to_datetime

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

logger = setup_logger("services.rrg_charts")

BENCHMARK_SYMBOL = "SPY"
RRG_CHART_TITLE = "S&P 500 Sectors vs SPY RRG"
RRG_PERIOD_LABEL = "Weekly Friday"


def format_rrg_heading(date_str: str) -> str:
    return f"{RRG_CHART_TITLE}, {RRG_PERIOD_LABEL}, {date_str}"


SECTOR_ETFS: tuple[str, ...] = (
    "XLC",
    "XLY",
    "XLP",
    "XLF",
    "XLV",
    "XLE",
    "XLB",
    "XLI",
    "XLRE",
    "XLU",
    "XLK",
)

SECTOR_NAMES: dict[str, str] = {
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLB": "Materials",
    "XLI": "Industrials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLK": "Technology",
}

STATUS_LEADING = "Leading"
STATUS_WEAKENING = "Weakening"
STATUS_LAGGING = "Lagging"
STATUS_IMPROVING = "Improving"


def sector_label(symbol: str) -> str:
    return SECTOR_NAMES.get(symbol, symbol)


def classify_quadrant(rs_ratio: float, rs_momentum: float) -> str:
    strong_ratio = rs_ratio >= 100.0
    strong_momentum = rs_momentum >= 100.0
    if strong_ratio and strong_momentum:
        return STATUS_LEADING
    if strong_ratio and not strong_momentum:
        return STATUS_WEAKENING
    if not strong_ratio and strong_momentum:
        return STATUS_IMPROVING
    return STATUS_LAGGING


def latest_sector_statuses(
    sector_tails: "dict[str, pd.DataFrame]",
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for symbol, tail in sector_tails.items():
        if tail.empty:
            continue
        latest = tail.iloc[-1]
        statuses[symbol] = classify_quadrant(
            float(latest["rs_ratio"]), float(latest["rs_momentum"])
        )
    return statuses


RRG_OUTPUT_DIRNAME = "chart_graphs"
RRG_OUTPUT_FILENAME = "rrg_sp500_sectors_weekly.png"
RRG_STATE_FILENAME = "rrg_state.json"

_FRIDAY = 4
_SATURDAY = 5


@dataclass(frozen=True)
class RrgChartConfig:
    benchmark: str = BENCHMARK_SYMBOL
    sectors: tuple[str, ...] = SECTOR_ETFS
    smoothing_window: int = 14
    momentum_roc_period: int = 52
    tail_length: int = 5
    fetch_rows: int = 520
    figure_size: tuple[int, int] = (12, 11)
    height_ratios: tuple[float, float] = (3.0, 1.1)
    dpi: int = 150
    style: str = "seaborn-v0_8-whitegrid"
    quadrant_alpha: float = 0.18
    improving_color: str = "#cfe3f5"
    leading_color: str = "#d2ebcd"
    lagging_color: str = "#f6d2d2"
    weakening_color: str = "#fbf2cc"
    output_dirname: str = RRG_OUTPUT_DIRNAME
    output_filename: str = RRG_OUTPUT_FILENAME
    sector_colors: tuple[str, ...] = field(
        default_factory=lambda: (
            "#1f77b4",
            "#ff7f0e",
            "#2ca02c",
            "#d62728",
            "#9467bd",
            "#8c564b",
            "#e377c2",
            "#7f7f7f",
            "#bcbd22",
            "#17becf",
            "#393b79",
        )
    )

    @property
    def min_weeks_required(self) -> int:
        return 2 * self.smoothing_window + self.momentum_roc_period - 1


@dataclass(frozen=True)
class WeeklyDecision:
    action: str
    week_ending: date | None = None


def resolve_run_timezone(settings: Settings) -> tzinfo:
    name = settings.rrg_run_timezone
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Invalid RRG_RUN_TIMEZONE %r; falling back to UTC", name)
        return UTC


def most_recent_friday(reference: date) -> date:
    offset = (reference.weekday() - _FRIDAY) % 7
    return reference - timedelta(days=offset)


def get_output_path(settings: Settings) -> Path:
    return settings.data_dir / RRG_OUTPUT_DIRNAME / RRG_OUTPUT_FILENAME


def get_state_path(settings: Settings) -> Path:
    return settings.data_dir / RRG_OUTPUT_DIRNAME / RRG_STATE_FILENAME


def read_rrg_state(settings: Settings) -> str | None:
    state_path = get_state_path(settings)
    if not state_path.exists():
        return None

    try:
        data = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Ignoring invalid RRG state artifact: %s", state_path)
        return None

    if not isinstance(data, dict):
        logger.warning("Ignoring malformed RRG state artifact: %s", state_path)
        return None

    last_sent = data.get("last_sent_week_ending")
    return str(last_sent) if last_sent is not None else None


def write_rrg_state(settings: Settings, week_ending: date) -> None:
    payload = {
        "last_sent_week_ending": week_ending.isoformat(),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    state_path = get_state_path(settings)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2))


def _zscore(series: "pd.Series", window: int) -> "pd.Series":
    mean = series.rolling(window).mean()
    std = series.rolling(window).std(ddof=1)
    return (series - mean) / std.where(std != 0)


def compute_rrg_series(
    sector_close: "pd.Series",
    benchmark_close: "pd.Series",
    window: int,
    roc_period: int,
) -> "pd.DataFrame":
    aligned = pd.concat(
        [sector_close, benchmark_close], axis=1, keys=["sector", "benchmark"]
    ).dropna()

    relative_strength = 100.0 * (aligned["sector"] / aligned["benchmark"])
    rs_ratio = 100.0 + _zscore(relative_strength, window)
    rs_roc = (rs_ratio / rs_ratio.shift(roc_period) - 1.0) * 100.0
    rs_momentum = 100.0 + _zscore(rs_roc, window)

    return pd.DataFrame({"rs_ratio": rs_ratio, "rs_momentum": rs_momentum}).dropna()


def _weekly_close_series(rows: list[dict[str, Any]]) -> "pd.Series":
    dates: list[date] = []
    closes: list[float] = []
    for row in rows:
        close = row["close_price"]
        if close is None:
            continue
        dates.append(unix_to_datetime(int(row["date"])).date())
        closes.append(float(close))

    if not closes:
        return pd.Series(dtype="float64")

    series = pd.Series(closes, index=pd.DatetimeIndex(pd.to_datetime(dates)))
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series.resample("W-FRI").last().dropna()


class RrgChartService:
    def __init__(self, settings: Settings, config: RrgChartConfig | None = None):
        self.settings = settings
        self.config = config or RrgChartConfig()
        self.output_dir = settings.data_dir / self.config.output_dirname
        self.output_path = self.output_dir / self.config.output_filename

    def build_weekly_frame(self, repo: StockRepository) -> "pd.DataFrame":
        symbols = (self.config.benchmark, *self.config.sectors)
        columns: dict[str, pd.Series] = {}
        for symbol in symbols:
            rows = repo.get_stock_data(symbol, days=self.config.fetch_rows)
            columns[symbol] = _weekly_close_series(rows)
        return pd.DataFrame(columns)

    def latest_week_ending(self, frame: "pd.DataFrame") -> date | None:
        if self.config.benchmark not in frame.columns:
            return None
        benchmark = frame[self.config.benchmark].dropna()
        if benchmark.empty:
            return None
        latest = benchmark.index[-1]
        return date(latest.year, latest.month, latest.day)

    def latest_week_ending_from_repo(self, repo: StockRepository) -> date | None:
        rows = repo.get_stock_data(self.config.benchmark, days=self.config.fetch_rows)
        series = _weekly_close_series(rows)
        if series.empty:
            return None
        latest = series.index[-1]
        return date(latest.year, latest.month, latest.day)

    def generate_chart(self, frame: "pd.DataFrame", week_ending: date) -> bool:
        benchmark = self.config.benchmark
        if benchmark not in frame.columns or frame[benchmark].dropna().empty:
            logger.warning("No benchmark (%s) data available for RRG", benchmark)
            return False

        sector_tails = self._build_sector_tails(frame, week_ending)
        if not sector_tails:
            logger.warning("No sector had enough weekly data to plot an RRG tail")
            return False

        try:
            with suppress(OSError):
                plt.style.use(self.config.style)

            fig, (ax, ax_table) = plt.subplots(
                nrows=2,
                ncols=1,
                figsize=self.config.figure_size,
                gridspec_kw={"height_ratios": list(self.config.height_ratios)},
            )

            color_map = self._assign_colors(sector_tails)
            self._draw_quadrants(ax, sector_tails)
            self._draw_sector_tails(ax, sector_tails, color_map)

            ax.set_title(
                format_rrg_heading(week_ending.isoformat()),
                fontsize=14,
                fontweight="bold",
            )
            ax.set_xlabel("JdK RS-Ratio", fontsize=11)
            ax.set_ylabel("JdK RS-Momentum", fontsize=11)
            ax.grid(True, alpha=0.3)

            self._draw_status_table(ax_table, sector_tails, color_map)

            fig.subplots_adjust(hspace=0.22)

            self.output_dir.mkdir(parents=True, exist_ok=True)
            fig.savefig(
                self.output_path,
                dpi=self.config.dpi,
                bbox_inches="tight",
                facecolor="white",
                edgecolor="none",
            )
            plt.close(fig)

            logger.info("Generated RRG chart: %s", self.output_path)
            return True
        except Exception as exc:
            logger.error("Failed to generate RRG chart: %s", exc, exc_info=True)
            plt.close("all")
            return False

    def _build_sector_tails(
        self, frame: "pd.DataFrame", week_ending: date | None = None
    ) -> "dict[str, pd.DataFrame]":
        benchmark_close = frame[self.config.benchmark]
        tails: dict[str, pd.DataFrame] = {}
        max_week = pd.Timestamp(week_ending) if week_ending is not None else None
        for symbol in self.config.sectors:
            if symbol not in frame.columns:
                logger.warning("%s: missing from weekly frame, skipping", symbol)
                continue
            track = compute_rrg_series(
                frame[symbol],
                benchmark_close,
                self.config.smoothing_window,
                self.config.momentum_roc_period,
            )
            if max_week is not None:
                track = track.loc[track.index <= max_week]
            tail = track.tail(self.config.tail_length)
            if tail.empty:
                logger.warning("%s: insufficient weekly data for RRG tail", symbol)
                continue
            tails[symbol] = tail
        return tails

    def _axis_limits(
        self, sector_tails: "dict[str, pd.DataFrame]"
    ) -> tuple[float, float]:
        half_x = 1.0
        half_y = 1.0
        for tail in sector_tails.values():
            half_x = max(half_x, float((tail["rs_ratio"] - 100.0).abs().max()))
            half_y = max(half_y, float((tail["rs_momentum"] - 100.0).abs().max()))
        return half_x * 1.2, half_y * 1.2

    def _draw_quadrants(self, ax: Any, sector_tails: "dict[str, pd.DataFrame]") -> None:
        half_x, half_y = self._axis_limits(sector_tails)
        x_min, x_max = 100.0 - half_x, 100.0 + half_x
        y_min, y_max = 100.0 - half_y, 100.0 + half_y

        alpha = self.config.quadrant_alpha
        quadrants = (
            (100.0, 100.0, self.config.leading_color),
            (x_min, 100.0, self.config.improving_color),
            (x_min, y_min, self.config.lagging_color),
            (100.0, y_min, self.config.weakening_color),
        )
        for x0, y0, color in quadrants:
            ax.add_patch(
                Rectangle(
                    (x0, y0),
                    half_x,
                    half_y,
                    facecolor=color,
                    edgecolor="none",
                    alpha=alpha,
                    zorder=0,
                )
            )

        ax.axhline(100.0, color="#888888", linestyle="--", linewidth=1.0, zorder=1)
        ax.axvline(100.0, color="#888888", linestyle="--", linewidth=1.0, zorder=1)
        ax.set_xlim(x_min, x_max)
        ax.set_ylim(y_min, y_max)

        corner_labels = (
            (0.99, 0.99, "right", "top", "LEADING"),
            (0.01, 0.99, "left", "top", "IMPROVING"),
            (0.01, 0.01, "left", "bottom", "LAGGING"),
            (0.99, 0.01, "right", "bottom", "WEAKENING"),
        )
        for x, y, ha, va, label in corner_labels:
            ax.text(
                x,
                y,
                label,
                transform=ax.transAxes,
                ha=ha,
                va=va,
                fontsize=11,
                fontweight="bold",
                color="#666666",
                alpha=0.8,
            )

    def _assign_colors(self, sector_tails: "dict[str, pd.DataFrame]") -> dict[str, str]:
        colors = self.config.sector_colors
        return {
            symbol: colors[index % len(colors)]
            for index, symbol in enumerate(sorted(sector_tails))
        }

    def _draw_sector_tails(
        self,
        ax: Any,
        sector_tails: "dict[str, pd.DataFrame]",
        color_map: dict[str, str],
    ) -> None:
        for symbol in sorted(sector_tails):
            tail = sector_tails[symbol]
            color = color_map[symbol]
            x_values = tail["rs_ratio"].tolist()
            y_values = tail["rs_momentum"].tolist()

            ax.plot(
                x_values,
                y_values,
                color=color,
                linewidth=1.4,
                alpha=0.9,
                marker="o",
                markersize=3,
                zorder=3,
            )
            head_x = x_values[-1]
            head_y = y_values[-1]
            ax.plot(
                head_x,
                head_y,
                marker="o",
                color=color,
                markersize=10,
                zorder=4,
            )
            ax.annotate(
                sector_label(symbol),
                xy=(head_x, head_y),
                xytext=(6, 6),
                textcoords="offset points",
                fontsize=8,
                fontweight="bold",
                color=color,
                zorder=5,
                bbox={
                    "boxstyle": "round,pad=0.15",
                    "facecolor": "white",
                    "edgecolor": "none",
                    "alpha": 0.6,
                },
            )

    def _status_colors(self) -> dict[str, str]:
        return {
            STATUS_LEADING: self.config.leading_color,
            STATUS_WEAKENING: self.config.weakening_color,
            STATUS_LAGGING: self.config.lagging_color,
            STATUS_IMPROVING: self.config.improving_color,
        }

    def _draw_status_table(
        self,
        ax: Any,
        sector_tails: "dict[str, pd.DataFrame]",
        color_map: dict[str, str],
    ) -> None:
        ax.axis("off")
        status_colors = self._status_colors()
        sector_statuses = latest_sector_statuses(sector_tails)

        symbols = sorted(sector_tails)
        names: list[str] = []
        statuses: list[str] = []
        name_colors: list[str] = []
        status_backgrounds: list[str] = []
        for symbol in symbols:
            status = sector_statuses[symbol]
            names.append(sector_label(symbol).replace(" ", "\n"))
            statuses.append(status)
            name_colors.append(color_map[symbol])
            status_backgrounds.append(status_colors[status])

        cell_text = [
            ["Sector", *names],
            ["Status", *statuses],
        ]

        label_width = 0.09
        col_width = (1.0 - label_width) / max(len(symbols), 1)
        table = ax.table(
            cellText=cell_text,
            cellLoc="center",
            colLoc="center",
            colWidths=[label_width, *([col_width] * len(symbols))],
            bbox=[0.0, 0.12, 1.0, 0.76],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(7.0)

        for (row, col), cell in table.get_celld().items():
            cell.set_edgecolor("#cccccc")
            if col == 0:
                cell.set_facecolor("#ededed")
                cell.set_text_props(fontweight="bold", color="#333333")
                continue
            data_index = col - 1
            if row == 0:
                cell.set_text_props(color=name_colors[data_index], fontweight="bold")
            else:
                cell.set_facecolor(status_backgrounds[data_index])
                cell.set_text_props(color="#333333", fontweight="bold")


def evaluate_weekly_decision(
    settings: Settings, frame: "pd.DataFrame", now: datetime
) -> WeeklyDecision:
    if now.weekday() != _SATURDAY:
        return WeeklyDecision("not_saturday")

    service = RrgChartService(settings)
    latest = service.latest_week_ending(frame)
    if latest is None:
        return WeeklyDecision("no_data")

    expected = most_recent_friday(now.date())
    if latest != expected:
        return WeeklyDecision("not_ready", latest)

    if read_rrg_state(settings) == expected.isoformat():
        return WeeklyDecision("already_sent", expected)

    return WeeklyDecision("due", expected)


def resolve_test_week_ending(settings: Settings, repo: StockRepository) -> date | None:
    return RrgChartService(settings).latest_week_ending_from_repo(repo)


def run_forced(settings: Settings) -> bool:
    log_script_start(logger, "Sector RRG Chart Forced Generation")
    start_time = datetime.now()

    if not settings.db_path.exists():
        logger.warning("Database not found: %s", settings.db_path)
        log_script_end(logger, "Sector RRG Chart Forced Generation", start_time, False)
        return False

    try:
        service = RrgChartService(settings)
        conn = connect(settings.db_path)
        try:
            repo = StockRepository(conn)
            frame = service.build_weekly_frame(repo)
        finally:
            conn.close()

        week_ending = service.latest_week_ending(frame)
        if week_ending is None:
            logger.warning("No benchmark weekly data available for RRG forced chart")
            log_script_end(
                logger, "Sector RRG Chart Forced Generation", start_time, False
            )
            return False

        success = service.generate_chart(frame, week_ending)
        if success:
            logger.info("Forced RRG chart written to: %s", service.output_path)
        log_script_end(
            logger, "Sector RRG Chart Forced Generation", start_time, success
        )
        return success
    except Exception as exc:
        logger.error("Forced RRG generation failed: %s", exc, exc_info=True)
        log_script_end(logger, "Sector RRG Chart Forced Generation", start_time, False)
        return False


def run(settings: Settings) -> bool:
    log_script_start(logger, "Sector RRG Chart Generation")
    start_time = datetime.now()

    if not settings.db_path.exists():
        logger.warning("Database not found: %s", settings.db_path)
        log_script_end(logger, "Sector RRG Chart Generation", start_time, True)
        return True

    now = datetime.now(resolve_run_timezone(settings))

    try:
        service = RrgChartService(settings)
        conn = connect(settings.db_path)
        try:
            repo = StockRepository(conn)
            frame = service.build_weekly_frame(repo)
        finally:
            conn.close()

        decision = evaluate_weekly_decision(settings, frame, now)
        if decision.action != "due":
            logger.info("RRG chart not due (%s); skipping generation", decision.action)
            log_script_end(logger, "Sector RRG Chart Generation", start_time, True)
            return True

        assert decision.week_ending is not None
        success = service.generate_chart(frame, decision.week_ending)
        log_script_end(logger, "Sector RRG Chart Generation", start_time, success)
        return success
    except Exception as exc:
        logger.error("Workflow failed: %s", exc, exc_info=True)
        log_script_end(logger, "Sector RRG Chart Generation", start_time, False)
        return False
