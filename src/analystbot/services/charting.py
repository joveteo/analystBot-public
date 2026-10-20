import json
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from math import isfinite
from pathlib import Path

import matplotlib

from analystbot.api.fred import FredConfig, get_date_years_ago
from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.db import FredRepository, StockRepository, connect
from analystbot.db.date_utils import unix_to_date_str
from analystbot.services.fred_chart_catalog import (
    DEFAULT_PLOT_CONFIGS,
    VIX_PLOT_CONFIG,
    VIX_SERIES_ID,
    FredCombinedPlotConfig,
    SeriesPlotConfig,
    combined_configs_for_series,
    expand_forced_plot_configs,
    plot_config_for,
    select_fred_chart_work,
)
from analystbot.services.fred_data import read_fred_update_state
from analystbot.services.period_headings import (
    format_observation_value,
    format_period_heading,
)

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

logger = setup_logger("services.charting")


@dataclass
class ChartConfig:
    FIGURE_SIZE: tuple[int, int] = (12, 6)
    DPI: int = 150
    STYLE: str = "seaborn-v0_8-whitegrid"
    LINE_COLOR: str = "#1f77b4"
    ZERO_LINE_COLOR: str = "#d62728"
    NEGATIVE_FILL_GOOD_COLOR: str = "#2ca02c"
    TITLE_FONTSIZE: int = 14
    LABEL_FONTSIZE: int = 11
    ANNOTATION_FONTSIZE: int = 10


def _recessions_from_payload(data: object) -> list[tuple[datetime, datetime]]:
    if not isinstance(data, dict):
        return []

    recessions: list[tuple[datetime, datetime]] = []
    for entry in data.get("recessions", []):
        if not isinstance(entry, dict):
            continue
        peak = entry.get("peak")
        trough = entry.get("trough")
        if not peak or not trough:
            continue
        try:
            peak_dt = datetime.strptime(str(peak), "%Y-%m-%d")
            trough_dt = datetime.strptime(str(trough), "%Y-%m-%d")
        except ValueError:
            continue
        recessions.append((peak_dt, trough_dt))
    return recessions


def load_recessions(json_path: Path) -> list[tuple[datetime, datetime]]:
    if not json_path.exists():
        logger.warning(f"Recessions file not found: {json_path}")
        return []

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        recessions = _recessions_from_payload(data)
        logger.info(f"Loaded {len(recessions)} recession periods")
        return recessions
    except Exception as e:
        logger.error(f"Failed to load recessions: {e}")
        return []


def load_packaged_recessions() -> list[tuple[datetime, datetime]]:
    resource = files("analystbot.data").joinpath("recessions.json")
    try:
        with resource.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to load packaged recessions: %s", e)
        return []
    return _recessions_from_payload(data)


def load_recessions_for_settings(settings: Settings) -> list[tuple[datetime, datetime]]:
    override = settings.data_dir / "recessions.json"
    if override.exists():
        return load_recessions(override)
    recessions = load_packaged_recessions()
    if recessions:
        logger.info("Loaded %s packaged NBER recession periods", len(recessions))
    return recessions


class ChartingService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.output_dir = settings.data_dir / "fred_graphs"
        self.recessions_path = settings.data_dir / "recessions.json"
        self.config = ChartConfig()

    def generate_chart(
        self,
        data: list[tuple[str, float | None]],
        plot_config: SeriesPlotConfig,
        recessions: list[tuple[datetime, datetime]] | None = None,
    ) -> bool:
        if not data:
            logger.warning(f"No data for {plot_config.series_id}")
            return False

        valid_data = [(d, v) for d, v in data if v is not None]
        if plot_config.source == "fred":
            valid_data = _clip_to_fred_history(valid_data)

        if not valid_data:
            logger.warning(f"No valid data points for {plot_config.series_id}")
            return False

        try:
            dates = [datetime.strptime(d, "%Y-%m-%d") for d, _ in valid_data]
            values = [v for _, v in valid_data]

            with suppress(OSError):
                plt.style.use(self.config.STYLE)

            fig, ax = plt.subplots(figsize=self.config.FIGURE_SIZE)

            y_limits: tuple[float, float] | None = None
            if plot_config.highlight_bands:
                y_limits = _threshold_y_limits(values, plot_config.highlight_bands)
                _apply_threshold_background(ax, y_limits, plot_config.highlight_bands)
                ax.set_ylim(*y_limits)

            if recessions:
                chart_start = min(dates)
                chart_end = max(dates)
                self._apply_recession_shading(ax, recessions, chart_start, chart_end)

            _plot_xy(
                ax,
                dates,
                values,
                color=self.config.LINE_COLOR,
                kind=plot_config.kind,
                frequency=plot_config.frequency,
                zero_color=self.config.ZERO_LINE_COLOR,
                show_zero_line=plot_config.show_zero_line,
                fill_negative=plot_config.kind == "line" and plot_config.show_zero_line,
                mark_latest=plot_config.mark_latest,
                value_suffix=plot_config.value_suffix,
            )
            if y_limits is not None:
                ax.set_ylim(*y_limits)

            title = plot_config.title
            if dates:
                title = format_period_heading(
                    plot_config.title,
                    plot_config.frequency,
                    dates[-1].strftime("%Y-%m-%d"),
                )
            ax.set_title(
                title,
                fontsize=self.config.TITLE_FONTSIZE,
                fontweight="bold",
            )
            ax.set_xlabel("Date", fontsize=self.config.LABEL_FONTSIZE)
            ax.set_ylabel(plot_config.ylabel, fontsize=self.config.LABEL_FONTSIZE)

            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            ax.xaxis.set_major_locator(mdates.YearLocator())
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")

            ax.grid(True, alpha=0.3)
            plt.tight_layout()

            self.output_dir.mkdir(parents=True, exist_ok=True)

            output_path = self.output_dir / plot_config.filename
            plt.savefig(
                output_path,
                dpi=self.config.DPI,
                bbox_inches="tight",
                facecolor="white",
                edgecolor="none",
            )
            plt.close(fig)

            logger.info(f"Generated chart: {output_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to generate {plot_config.series_id}: {e}")
            plt.close("all")
            return False

    def _apply_recession_shading(
        self,
        ax,
        recessions: list[tuple[datetime, datetime]],
        chart_start: datetime,
        chart_end: datetime,
    ) -> None:
        for peak, trough in recessions:
            if trough < chart_start or peak > chart_end:
                continue

            shade_start = max(peak, chart_start)
            shade_end = min(trough, chart_end)

            ax.axvspan(shade_start, shade_end, color="gray", alpha=0.2, zorder=1)

    def generate_combined_chart(
        self,
        series_data: dict[str, list[tuple[str, float | None]]],
        plot_config: FredCombinedPlotConfig,
        recessions: list[tuple[datetime, datetime]] | None = None,
    ) -> bool:
        parsed: dict[str, tuple[list[datetime], list[float]]] = {}
        for spec in plot_config.series:
            dates, values = _valid_xy(
                _clip_to_fred_history(series_data.get(spec.series_id, []))
            )
            if not dates:
                logger.warning(
                    "No valid data for %s in combined chart %s",
                    spec.series_id,
                    plot_config.combined_id,
                )
                return False
            parsed[spec.series_id] = (dates, values)

        try:
            with suppress(OSError):
                plt.style.use(self.config.STYLE)

            figsize = plot_config.figure_size or self.config.FIGURE_SIZE
            fig = self._draw_stacked_combined(parsed, plot_config, recessions, figsize)

            self.output_dir.mkdir(parents=True, exist_ok=True)
            output_path = self.output_dir / plot_config.filename
            plt.savefig(
                output_path,
                dpi=self.config.DPI,
                bbox_inches="tight",
                facecolor="white",
                edgecolor="none",
            )
            plt.close(fig)
            logger.info("Generated chart: %s", output_path)
            return True
        except Exception as e:
            logger.error("Failed to generate %s: %s", plot_config.combined_id, e)
            plt.close("all")
            return False

    def _draw_stacked_combined(
        self,
        parsed: dict[str, tuple[list[datetime], list[float]]],
        plot_config: FredCombinedPlotConfig,
        recessions: list[tuple[datetime, datetime]] | None,
        figsize: tuple[int, int],
    ):
        panels = sorted({spec.panel for spec in plot_config.series})
        fig, axes = plt.subplots(
            nrows=len(panels),
            ncols=1,
            sharex=True,
            figsize=figsize,
        )
        axes_list = list(axes) if len(panels) > 1 else [axes]
        panel_titles = [
            next(
                (
                    spec.panel_title
                    for spec in plot_config.series
                    if spec.panel == panel and spec.panel_title
                ),
                None,
            )
            for panel in panels
        ]
        if not all(panel_titles):
            fig.suptitle(
                plot_config.title,
                fontsize=self.config.TITLE_FONTSIZE,
                fontweight="bold",
            )
        all_dates = [dt for dates, _values in parsed.values() for dt in dates]
        for ax, panel, panel_title in zip(axes_list, panels, panel_titles, strict=True):
            specs = [spec for spec in plot_config.series if spec.panel == panel]
            if recessions and all_dates:
                self._apply_recession_shading(
                    ax, recessions, min(all_dates), max(all_dates)
                )
            for spec in specs:
                dates, values = parsed[spec.series_id]
                plot = plot_config_for(spec.series_id)
                _plot_xy(
                    ax,
                    dates,
                    values,
                    color=spec.color,
                    kind=plot.kind,
                    frequency=plot.frequency,
                    label=spec.label,
                    zero_color=self.config.ZERO_LINE_COLOR,
                    show_zero_line=plot.show_zero_line,
                    fill_negative=plot.kind == "line" and plot.show_zero_line,
                    mark_latest=plot.mark_latest,
                    value_suffix=plot.value_suffix,
                )
            if panel_title:
                heading_spec = next(
                    (spec for spec in specs if spec.panel_title), specs[0]
                )
                heading_plot = plot_config_for(heading_spec.series_id)
                last_date = max(parsed[spec.series_id][0][-1] for spec in specs)
                ax.set_title(
                    format_period_heading(
                        panel_title,
                        heading_plot.frequency,
                        last_date.strftime("%Y-%m-%d"),
                    ),
                    fontsize=self.config.TITLE_FONTSIZE,
                    fontweight="bold",
                )
            ax.set_ylabel(
                plot_config_for(specs[0].series_id).ylabel,
                fontsize=self.config.LABEL_FONTSIZE,
            )
            ax.grid(True, alpha=0.3)
            if panel_title is None or len(specs) > 1:
                ax.legend()
            _format_year_axis(ax)
        axes_list[-1].set_xlabel("Date", fontsize=self.config.LABEL_FONTSIZE)
        plt.tight_layout()
        return fig


def _clip_to_fred_history[T](
    data: Sequence[tuple[str, T]],
) -> list[tuple[str, T]]:
    cutoff = get_date_years_ago(FredConfig.HISTORY_YEARS)
    return [(date_str, value) for date_str, value in data if date_str >= cutoff]


def _valid_xy(
    data: list[tuple[str, float | None]],
) -> tuple[list[datetime], list[float]]:
    valid = [(d, v) for d, v in data if v is not None]
    dates = [datetime.strptime(d, "%Y-%m-%d") for d, _ in valid]
    values = [v for _, v in valid]
    return dates, values


def _bar_width_days(frequency: str) -> float:
    return {"q": 70.0, "m": 20.0, "w": 5.0, "d": 0.8}.get(frequency, 20.0)


def _plot_xy(
    ax,
    dates: list[datetime],
    values: list[float],
    *,
    color: str,
    kind: str = "line",
    frequency: str = "d",
    label: str | None = None,
    zero_color: str,
    show_zero_line: bool = False,
    fill_negative: bool = False,
    fill_color: str | None = None,
    mark_latest: bool = False,
    value_suffix: str = "%",
) -> None:
    if kind == "bar":
        x = mdates.date2num(dates)
        width = _bar_width_days(frequency)
        bar_colors = [zero_color if v < 0 else color for v in values]
        ax.bar(
            x,
            values,
            width=width,
            color=bar_colors,
            align="center",
            zorder=3,
            label=label,
        )
        ax.xaxis_date()
    else:
        ax.plot(
            dates,
            values,
            color=color,
            linewidth=1.5,
            alpha=0.9,
            zorder=3,
            label=label,
        )
        if fill_negative:
            ax.fill_between(
                dates,
                values,
                0,
                where=[v < 0 for v in values],
                color=fill_color or zero_color,
                alpha=0.2,
                interpolate=True,
            )
    if show_zero_line:
        ax.axhline(
            y=0,
            color=zero_color,
            linestyle="--",
            linewidth=1.5,
            alpha=0.7,
        )
    if mark_latest and dates and values:
        _draw_latest_marker(
            ax,
            dates[-1],
            values[-1],
            color=zero_color,
            suffix=value_suffix,
        )


def _draw_latest_marker(
    ax,
    last_date: datetime,
    last_value: float,
    *,
    color: str,
    suffix: str,
) -> None:
    ax.axhline(
        y=last_value,
        color=color,
        linestyle=":",
        linewidth=1.5,
        alpha=0.8,
        zorder=4,
    )
    ax.annotate(
        format_observation_value(last_value, suffix),
        xy=(last_date, last_value),
        xytext=(6, 6),
        textcoords="offset points",
        color=color,
        fontsize=ChartConfig.ANNOTATION_FONTSIZE,
        fontweight="bold",
        ha="left",
        va="bottom",
        clip_on=False,
        zorder=5,
    )


def _format_year_axis(ax) -> None:
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator())
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")


def resolve_threshold_band(
    value: float,
    highlight_bands: tuple[tuple[float, float | None, str, float], ...],
) -> tuple[str, float] | None:
    for lower, upper, color, alpha in highlight_bands:
        in_band = value >= lower if upper is None else lower <= value < upper
        if in_band:
            return color, alpha
    return None


def _threshold_y_limits(
    values: list[float],
    highlight_bands: tuple[tuple[float, float | None, str, float], ...] = (),
) -> tuple[float, float]:
    edges = list(values)
    has_open_low = False
    for lower, upper, _color, _alpha in highlight_bands:
        if isfinite(lower):
            edges.append(lower)
        else:
            has_open_low = True
        if upper is not None and isfinite(upper):
            edges.append(upper)
    y_min = min(edges)
    y_max = max(edges)
    span = y_max - y_min
    padding = span * 0.05 if span > 0 else 1.0
    if has_open_low:
        y_min = min(y_min, 0.0) - padding
    return y_min, y_max + padding


def _apply_threshold_background(
    ax,
    y_limits: tuple[float, float],
    highlight_bands: tuple[tuple[float, float | None, str, float], ...],
) -> None:
    y_min, y_max = y_limits
    for lower, upper, color, alpha in highlight_bands:
        top = y_max if upper is None else min(upper, y_max)
        bottom = max(lower, y_min)
        if top <= bottom:
            continue
        ax.axhspan(bottom, top, color=color, alpha=alpha, zorder=0, linewidth=0)


def get_vix_chart_path(settings: Settings) -> Path:
    return settings.data_dir / "fred_graphs" / VIX_PLOT_CONFIG.filename


def _load_stock_series(
    repo: StockRepository, symbol: str
) -> list[tuple[str, float | None]]:
    rows = repo.get_stock_data(symbol, days=7300)
    return [
        (unix_to_date_str(row["date"]), row["close_price"])
        for row in rows
        if row["close_price"] is not None
    ]


def _generate_configs(
    settings: Settings,
    service: "ChartingService",
    recessions: list[tuple[datetime, datetime]] | None,
    configs: list[SeriesPlotConfig],
) -> tuple[int, int]:
    generated = 0
    failed = 0

    conn = connect(settings.db_path)
    try:
        fred_repo = FredRepository(conn)
        stock_repo = StockRepository(conn)

        for config in configs:
            logger.info(f"Processing {config.series_id}...")

            if config.source == "stock" and config.symbol:
                data = _load_stock_series(stock_repo, config.symbol)
                if not data:
                    logger.warning(f"No stock data found for {config.symbol}")
                    failed += 1
                    continue
                logger.info(
                    f"{config.series_id}: {len(data)} observations "
                    f"({data[0][0]} to {data[-1][0]})"
                )
            else:
                data = _clip_to_fred_history(
                    fred_repo.get_series_data(config.series_id)
                )
                if not data:
                    logger.warning(f"No data found for {config.series_id}")
                    failed += 1
                    continue
                logger.info(
                    f"{config.series_id}: {len(data)} observations "
                    f"({data[0][0]} to {data[-1][0]})"
                )

            if service.generate_chart(data, config, recessions):
                generated += 1
            else:
                failed += 1
    finally:
        conn.close()

    logger.info("Graph generation summary:")
    logger.info(f"   Generated: {generated}")
    logger.info(f"   Failed: {failed}")
    return generated, failed


def _generate_combined_configs(
    settings: Settings,
    service: "ChartingService",
    recessions: list[tuple[datetime, datetime]] | None,
    configs: list[FredCombinedPlotConfig],
) -> tuple[int, int]:
    generated = 0
    failed = 0
    if not configs:
        return generated, failed

    conn = connect(settings.db_path)
    try:
        fred_repo = FredRepository(conn)
        for config in configs:
            logger.info("Processing combined chart %s...", config.combined_id)
            series_data = {
                spec.series_id: _clip_to_fred_history(
                    fred_repo.get_series_data(spec.series_id)
                )
                for spec in config.series
            }
            if service.generate_combined_chart(series_data, config, recessions):
                generated += 1
            else:
                failed += 1
    finally:
        conn.close()
    return generated, failed


def run(settings: Settings, plot_configs: list[SeriesPlotConfig] | None = None) -> bool:
    log_script_start(logger, "FRED Graph Generation")
    start_time = datetime.now()

    if not settings.db_path.exists():
        logger.warning(f"Database not found: {settings.db_path}")
        log_script_end(logger, "FRED Graph Generation", start_time, False)
        return False

    try:
        service = ChartingService(settings)

        recessions = load_recessions_for_settings(settings)

        all_configs = plot_configs or DEFAULT_PLOT_CONFIGS

        update_state = read_fred_update_state(settings)
        updated_series_ids = (
            set(update_state["updated_series_ids"])
            if update_state is not None
            else set()
        )
        selection = select_fred_chart_work(updated_series_ids)

        allowed_ids = {config.series_id for config in all_configs}
        selected_fred = [
            config
            for config in selection.generate_individuals
            if config.series_id in allowed_ids
        ]
        for config in all_configs:
            if config.series_id != VIX_SERIES_ID or config.source != "fred":
                continue
            if config.series_id in updated_series_ids:
                selected_fred.append(config)

        stock_configs = [c for c in all_configs if c.source == "stock"]
        combined_to_generate = (
            list(selection.generate_combined) if plot_configs is None else []
        )
        configs_to_generate = selected_fred + stock_configs

        if not configs_to_generate and not combined_to_generate:
            logger.info(
                "No charts to generate (no updated FRED series, no stock charts)"
            )
            log_script_end(logger, "FRED Graph Generation", start_time, True)
            return True

        generated = 0
        if configs_to_generate:
            generated_individuals, _ = _generate_configs(
                settings, service, recessions, configs_to_generate
            )
            generated += generated_individuals
        if combined_to_generate:
            generated_combined, _ = _generate_combined_configs(
                settings, service, recessions, combined_to_generate
            )
            generated += generated_combined

        success = generated > 0
        log_script_end(logger, "FRED Graph Generation", start_time, success)
        return success

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "FRED Graph Generation", start_time, False)
        return False


def run_forced(settings: Settings, plot_configs: list[SeriesPlotConfig]) -> bool:
    log_script_start(logger, "FRED Graph Generation (forced)")
    start_time = datetime.now()

    if not settings.db_path.exists():
        logger.warning(f"Database not found: {settings.db_path}")
        log_script_end(logger, "FRED Graph Generation (forced)", start_time, False)
        return False

    if not plot_configs:
        logger.warning("No plot configs provided to forced chart generation")
        log_script_end(logger, "FRED Graph Generation (forced)", start_time, False)
        return False

    try:
        service = ChartingService(settings)
        recessions = load_recessions_for_settings(settings)
        plot_configs = expand_forced_plot_configs(plot_configs)
        generated, _ = _generate_configs(settings, service, recessions, plot_configs)
        related_combined = combined_configs_for_series(
            [config.series_id for config in plot_configs]
        )
        if related_combined:
            generated_combined, _ = _generate_combined_configs(
                settings, service, recessions, related_combined
            )
            generated += generated_combined

        success = generated > 0
        log_script_end(logger, "FRED Graph Generation (forced)", start_time, success)
        return success
    except Exception as e:
        logger.error(f"Forced workflow failed: {e}", exc_info=True)
        log_script_end(logger, "FRED Graph Generation (forced)", start_time, False)
        return False
