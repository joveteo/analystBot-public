from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib

from analystbot.core.config import Settings
from analystbot.core.indicators import compute_supertrend
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.db import StockRepository, connect, datetime_to_unix, unix_to_datetime

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

logger = setup_logger("services.market_charts")


MARKET_CHART_SYMBOLS: tuple[str, str, str] = ("SPY", "QQQ", "IWM")
MARKET_CHART_FILENAME = "supertrend_spy_qqq_iwm_1y.png"

TREND_UP = -1
TREND_DOWN = 1
UPTREND_EMOJI = "\U0001f7e2"
DOWNTREND_EMOJI = "\U0001f534"


def count_consecutive_trend_days(
    direction: list[int | None],
) -> tuple[int | None, int]:
    latest: int | None = None
    days = 0
    for value in reversed(direction):
        if value is None:
            continue
        if latest is None:
            latest = value
            days = 1
        elif value == latest:
            days += 1
        else:
            break
    return latest, days


@dataclass(frozen=True)
class MarketChartConfig:
    symbols: tuple[str, str, str] = MARKET_CHART_SYMBOLS
    lookback_days: int = 365
    atr_period: int = 10
    factor: float = 3.0
    figure_size: tuple[int, int] = (14, 15)
    dpi: int = 150
    style: str = "seaborn-v0_8-whitegrid"
    close_color: str = "#4f5b66"
    uptrend_color: str = "#2ca02c"
    downtrend_color: str = "#d62728"
    title_fontsize: int = 13
    label_fontsize: int = 10
    annotation_fontsize: int = 9
    output_dirname: str = "chart_graphs"
    output_filename: str = MARKET_CHART_FILENAME


def get_output_path(settings: Settings) -> Path:
    return settings.data_dir / "chart_graphs" / MARKET_CHART_FILENAME


class MarketChartService:
    def __init__(self, settings: Settings, config: MarketChartConfig | None = None):
        self.settings = settings
        self.config = config or MarketChartConfig()
        self.output_dir = settings.data_dir / self.config.output_dirname
        self.output_path = self.output_dir / self.config.output_filename

    def generate_chart(self, repo: StockRepository) -> bool:
        symbol_rows = {
            symbol: self._get_symbol_rows(repo, symbol)
            for symbol in self.config.symbols
        }

        try:
            with suppress(OSError):
                plt.style.use(self.config.style)

            fig, axes = plt.subplots(
                nrows=len(self.config.symbols),
                ncols=1,
                figsize=self.config.figure_size,
                sharex=False,
                sharey=False,
            )
            axes_list = list(axes)

            for ax, symbol in zip(axes_list, self.config.symbols, strict=False):
                rows = symbol_rows[symbol]
                if not rows:
                    self._plot_empty_panel(ax, symbol)
                    continue
                self._plot_symbol_panel(ax, symbol, rows)

            latest_dates = [
                unix_to_datetime(rows[-1]["date"]).strftime("%Y-%m-%d")
                for rows in symbol_rows.values()
                if rows
            ]
            symbols_label = " / ".join(self.config.symbols)
            date_suffix = f", {max(latest_dates)}" if latest_dates else ""
            fig.suptitle(
                (
                    f"Supertrend ({self.config.atr_period}, "
                    f"{self.config.factor:.1f}), 1 Year, Daily, "
                    f"{symbols_label}{date_suffix}"
                ),
                fontsize=15,
                fontweight="bold",
            )
            fig.tight_layout(rect=(0, 0, 1, 0.98))

            self.output_dir.mkdir(parents=True, exist_ok=True)
            fig.savefig(
                self.output_path,
                dpi=self.config.dpi,
                bbox_inches="tight",
                facecolor="white",
                edgecolor="none",
            )
            plt.close(fig)

            logger.info("Generated market chart: %s", self.output_path)
            return True
        except Exception as exc:
            logger.error("Failed to generate market chart: %s", exc, exc_info=True)
            plt.close("all")
            return False

    def compute_trend_summaries(
        self, repo: StockRepository
    ) -> dict[str, dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for symbol in self.config.symbols:
            rows = self._get_symbol_rows(repo, symbol)
            valid_rows = [
                row
                for row in rows
                if row["high_price"] is not None
                and row["low_price"] is not None
                and row["close_price"] is not None
            ]
            if len(valid_rows) < self.config.atr_period:
                continue
            high = [float(row["high_price"]) for row in valid_rows]
            low = [float(row["low_price"]) for row in valid_rows]
            close = [float(row["close_price"]) for row in valid_rows]
            _, direction = compute_supertrend(
                high,
                low,
                close,
                atr_period=self.config.atr_period,
                factor=self.config.factor,
            )
            trend, days = count_consecutive_trend_days(direction)
            if trend is None:
                continue
            emoji = UPTREND_EMOJI if trend == TREND_UP else DOWNTREND_EMOJI
            date_str = unix_to_datetime(int(valid_rows[-1]["date"])).strftime(
                "%Y-%m-%d"
            )
            summaries[symbol] = {
                "emoji": emoji,
                "trend": trend,
                "days": days,
                "date": date_str,
            }
        return summaries

    def _get_symbol_rows(
        self, repo: StockRepository, symbol: str
    ) -> list[dict[str, Any]]:
        latest_ts = repo.get_latest_timestamp(symbol)
        if latest_ts is None:
            logger.warning("%s: no stored OHLC data found for market chart", symbol)
            return []

        latest_dt = unix_to_datetime(latest_ts)
        start_ts = datetime_to_unix(
            latest_dt - timedelta(days=self.config.lookback_days)
        )
        rows = repo.get_stock_data_range(symbol, start_ts, latest_ts)
        if not rows:
            logger.warning("%s: no rows available in requested range", symbol)
            return []
        return rows

    def _plot_symbol_panel(
        self, ax: Any, symbol: str, rows: list[dict[str, Any]]
    ) -> None:
        valid_rows = [
            row
            for row in rows
            if row["high_price"] is not None
            and row["low_price"] is not None
            and row["close_price"] is not None
        ]
        dates = [unix_to_datetime(int(row["date"])) for row in valid_rows]
        close = [float(row["close_price"]) for row in valid_rows]
        high = [float(row["high_price"]) for row in valid_rows]
        low = [float(row["low_price"]) for row in valid_rows]

        if not valid_rows:
            self._plot_empty_panel(
                ax, symbol, message="Stored rows have incomplete OHLC values"
            )
            return

        ax.plot(
            dates,
            close,
            color=self.config.close_color,
            linewidth=1.2,
            alpha=0.9,
            label="Close",
        )

        if len(valid_rows) >= self.config.atr_period:
            supertrend, direction = compute_supertrend(
                high,
                low,
                close,
                atr_period=self.config.atr_period,
                factor=self.config.factor,
            )
            uptrend_line = [
                line if trend == -1 else None
                for line, trend in zip(supertrend, direction, strict=False)
            ]
            downtrend_line = [
                line if trend == 1 else None
                for line, trend in zip(supertrend, direction, strict=False)
            ]

            ax.plot(
                dates,
                uptrend_line,
                color=self.config.uptrend_color,
                linewidth=1.8,
                label="Supertrend Up",
            )
            ax.plot(
                dates,
                downtrend_line,
                color=self.config.downtrend_color,
                linewidth=1.8,
                label="Supertrend Down",
            )
        else:
            logger.warning(
                "%s: insufficient rows for Supertrend (%s < %s)",
                symbol,
                len(valid_rows),
                self.config.atr_period,
            )
            ax.text(
                0.01,
                0.92,
                f"Supertrend unavailable ({len(valid_rows)} bars)",
                transform=ax.transAxes,
                fontsize=self.config.annotation_fontsize,
                ha="left",
                va="top",
                bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "alpha": 0.8},
            )

        latest_close = close[-1]
        ax.set_title(
            f"{symbol}, Last close: {latest_close:.2f}",
            fontsize=self.config.title_fontsize,
            fontweight="bold",
        )
        ax.set_ylabel("Price ($)", fontsize=self.config.label_fontsize)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.tick_params(axis="x", rotation=45)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper left")

    def _plot_empty_panel(
        self, ax: Any, symbol: str, message: str = "No stored OHLC data available"
    ) -> None:
        ax.set_title(
            f"{symbol}",
            fontsize=self.config.title_fontsize,
            fontweight="bold",
        )
        ax.text(
            0.5,
            0.5,
            message,
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=self.config.label_fontsize,
        )
        ax.grid(True, alpha=0.2)
        ax.set_ylabel("Price ($)", fontsize=self.config.label_fontsize)


def run(settings: Settings) -> bool:
    log_script_start(logger, "Market Supertrend Chart Generation")
    start_time = datetime.now()

    if not settings.db_path.exists():
        logger.warning("Database not found: %s", settings.db_path)
        log_script_end(logger, "Market Supertrend Chart Generation", start_time, False)
        return False

    try:
        service = MarketChartService(settings)
        conn = connect(settings.db_path)
        try:
            repo = StockRepository(conn)
            success = service.generate_chart(repo)
        finally:
            conn.close()

        log_script_end(
            logger, "Market Supertrend Chart Generation", start_time, success
        )
        return success
    except Exception as exc:
        logger.error("Workflow failed: %s", exc, exc_info=True)
        log_script_end(logger, "Market Supertrend Chart Generation", start_time, False)
        return False
