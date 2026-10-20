from dataclasses import dataclass
from typing import Literal

_BLUE = "#1f77b4"
_ORANGE = "#ff7f0e"
_GREEN = "#2ca02c"
_RED = "#d62728"
_STRESS_EASY = "#2ca02c"
_STRESS_ELEVATED = "#d62728"


@dataclass
class SeriesPlotConfig:
    series_id: str
    title: str
    ylabel: str
    filename: str
    description: str
    value_suffix: str = "%"
    show_zero_line: bool = False
    highlight_bands: tuple[tuple[float, float | None, str, float], ...] = ()
    source: Literal["fred", "stock"] = "fred"
    symbol: str | None = None
    kind: Literal["line", "bar"] = "line"
    frequency: str = "d"
    caption_note: str | None = None
    mark_latest: bool = True


@dataclass(frozen=True)
class CombinedSeriesSpec:
    series_id: str
    label: str
    color: str
    panel: int = 0
    panel_title: str | None = None


@dataclass(frozen=True)
class FredCombinedPlotConfig:
    combined_id: str
    title: str
    filename: str
    description: str
    series: tuple[CombinedSeriesSpec, ...]
    figure_size: tuple[int, int] | None = None


@dataclass(frozen=True)
class FredChartSelection:
    generate_individuals: tuple[SeriesPlotConfig, ...]
    generate_combined: tuple[FredCombinedPlotConfig, ...]
    send_individuals: tuple[SeriesPlotConfig, ...]
    send_combined: tuple[FredCombinedPlotConfig, ...]


FRED_PLOT_CONFIGS: list[SeriesPlotConfig] = [
    SeriesPlotConfig(
        series_id="GDPC1",
        title="Real GDP Percent Change",
        ylabel="Percent Change",
        filename="gdpc1.png",
        description="Real Gross Domestic Product, percent change",
        kind="bar",
        frequency="q",
        caption_note="Inflation-adjusted output. Up = expanding economy.",
    ),
    SeriesPlotConfig(
        series_id="PCECC96",
        title="Real PCE Percent Change",
        ylabel="Percent Change",
        filename="pcecc96.png",
        description="Real Personal Consumption Expenditures, percent change",
        frequency="q",
    ),
    SeriesPlotConfig(
        series_id="CPIAUCSL",
        title="CPI Percent Change from Year Ago",
        ylabel="Percent Change from Year Ago",
        filename="cpiaucsl.png",
        description="Consumer Price Index, percent change from year ago",
        frequency="m",
        caption_note="Headline consumer inflation vs a year ago.",
    ),
    SeriesPlotConfig(
        series_id="PCEPI",
        title="PCE Price Index Percent Change from Year Ago",
        ylabel="Percent Change from Year Ago",
        filename="pcepi.png",
        description="PCE price index, percent change from year ago",
        frequency="m",
        caption_note="Fed's preferred inflation gauge vs a year ago.",
    ),
    SeriesPlotConfig(
        series_id="PAYEMS",
        title="Total Nonfarm Payroll Employment",
        ylabel="Thousands of Persons",
        filename="payems.png",
        description="All Employees, Total Nonfarm",
        value_suffix="",
        frequency="m",
        caption_note="Jobs on nonfarm payrolls. Up = hiring.",
    ),
    SeriesPlotConfig(
        series_id="UNRATE",
        title="U.S. Unemployment Rate",
        ylabel="Rate (%)",
        filename="unrate.png",
        description="Unemployment Rate",
        frequency="m",
        caption_note="Share of labor force without work. Up = weaker labor market.",
    ),
    SeriesPlotConfig(
        series_id="HOUST",
        title="Housing Starts",
        ylabel="Thousands of Units",
        filename="houst.png",
        description="Housing Starts: Total New Privately Owned Housing Units",
        value_suffix="",
        frequency="m",
        caption_note="New homes started. Leading read on construction.",
    ),
    SeriesPlotConfig(
        series_id="DGS10",
        title="10-Year Treasury Rate",
        ylabel="Rate (%)",
        filename="dgs10.png",
        description="Benchmark long-term U.S. Treasury yield.",
        frequency="d",
        caption_note="Benchmark long-term U.S. Treasury yield.",
    ),
    SeriesPlotConfig(
        series_id="T10Y2Y",
        title="10Y-2Y Spread",
        ylabel="Spread (%)",
        filename="t10y2y.png",
        description="Below 0: inverted curve (recession risk).",
        frequency="d",
        caption_note="Below 0: inverted curve (recession risk).",
    ),
    SeriesPlotConfig(
        series_id="STLFSI4",
        title="St. Louis Fed Financial Stress Index",
        ylabel="Index",
        filename="stlfsi4.png",
        description="St. Louis Fed Financial Stress Index",
        value_suffix="",
        highlight_bands=(
            (float("-inf"), 0.0, _STRESS_EASY, 0.25),
            (0.0, None, _STRESS_ELEVATED, 0.25),
        ),
        frequency="w",
    ),
]

VIX_SERIES_ID = "VIXCLS"
VIX_PLOT_CONFIG = SeriesPlotConfig(
    series_id=VIX_SERIES_ID,
    title="CBOE Volatility Index (VIX)",
    ylabel="Index Level",
    filename="vix.png",
    description="VIX Volatility Index",
    value_suffix="",
    highlight_bands=(
        (0.0, 20.0, "#2dc937", 0.25),
        (20.0, 30.0, "#e7b416", 0.25),
        (30.0, None, "#cc3232", 0.25),
    ),
    source="fred",
)

DEFAULT_PLOT_CONFIGS: list[SeriesPlotConfig] = FRED_PLOT_CONFIGS + [VIX_PLOT_CONFIG]

JOBS_COMBINED_ID = "UNRATE_PAYEMS"
RATES_COMBINED_ID = "DGS10_T10Y2Y"
PCE_INFLATION_COMBINED_ID = "PCECC96_CPI_PCEPI"

FRED_COMBINED_PLOT_CONFIGS: list[FredCombinedPlotConfig] = [
    FredCombinedPlotConfig(
        combined_id=JOBS_COMBINED_ID,
        title="Unemployment Rate and Nonfarm Payrolls",
        filename="unrate_payems.png",
        description="Unemployment rate with nonfarm payroll employment",
        figure_size=(12, 10),
        series=(
            CombinedSeriesSpec(
                series_id="UNRATE",
                label="Unemployment Rate",
                color=_BLUE,
                panel=0,
                panel_title="Unemployment Rate",
            ),
            CombinedSeriesSpec(
                series_id="PAYEMS",
                label="Nonfarm Payrolls",
                color=_ORANGE,
                panel=1,
                panel_title="Nonfarm Payrolls",
            ),
        ),
    ),
    FredCombinedPlotConfig(
        combined_id=RATES_COMBINED_ID,
        title="10-Year Treasury Rate and 10Y-2Y Spread",
        filename="dgs10_t10y2y.png",
        description="Benchmark 10-year yield with the 10y–2y spread.",
        figure_size=(12, 10),
        series=(
            CombinedSeriesSpec(
                series_id="DGS10",
                label="10-Year Treasury Rate",
                color=_BLUE,
                panel=0,
                panel_title="10-Year Treasury Rate",
            ),
            CombinedSeriesSpec(
                series_id="T10Y2Y",
                label="10Y-2Y Spread",
                color=_BLUE,
                panel=1,
                panel_title="10Y-2Y Spread",
            ),
        ),
    ),
    FredCombinedPlotConfig(
        combined_id=PCE_INFLATION_COMBINED_ID,
        title="Real PCE and Consumer Inflation (CPI vs PCE)",
        filename="pcecc96_cpi_pcepi.png",
        description="Real PCE percent change with CPI and PCE inflation",
        figure_size=(12, 10),
        series=(
            CombinedSeriesSpec(
                series_id="PCECC96",
                label="Real PCE",
                color=_BLUE,
                panel=0,
                panel_title="Real PCE Percent Change",
            ),
            CombinedSeriesSpec(
                series_id="CPIAUCSL",
                label="CPI",
                color=_RED,
                panel=1,
                panel_title="CPI vs PCE Inflation",
            ),
            CombinedSeriesSpec(
                series_id="PCEPI",
                label="PCE Price Index",
                color=_GREEN,
                panel=1,
            ),
        ),
    ),
]


UNGROUPED_SEND_SERIES_IDS = frozenset({"GDPC1", "HOUST", "STLFSI4"})
JOBS_SERIES_IDS = frozenset({"UNRATE", "PAYEMS"})
RATES_SERIES_IDS = frozenset({"DGS10", "T10Y2Y"})
GROUPED_FORCE_PAIRS: tuple[tuple[str, str], ...] = (
    ("UNRATE", "PAYEMS"),
    ("DGS10", "T10Y2Y"),
)
FRED_FORCE_FLAG_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "gdp",
        "--gdp",
        "GDPC1",
        "Force-generate the real GDP (GDPC1) chart and send it to Telegram",
    ),
    (
        "real_pce",
        "--real-pce",
        "PCECC96",
        "Force-generate the real PCE (PCECC96) chart and send it to Telegram",
    ),
    (
        "cpi",
        "--cpi",
        "CPIAUCSL",
        "Force-generate the CPI (CPIAUCSL) chart and send it to Telegram",
    ),
    (
        "pce",
        "--pce",
        "PCEPI",
        "Force-generate the PCE price index (PCEPI) chart and send it to Telegram",
    ),
    (
        "nonfarm",
        "--nonfarm",
        "PAYEMS",
        "Force-generate unemployment and nonfarm payrolls as one image and send it",
    ),
    (
        "unemployment",
        "--unemployment",
        "UNRATE",
        "Force-generate unemployment and nonfarm payrolls as one image and send it",
    ),
    (
        "housing",
        "--housing",
        "HOUST",
        "Force-generate the housing starts (HOUST) chart and send it to Telegram",
    ),
    (
        "treasury_10y",
        "--treasury-10y",
        "DGS10",
        "Force-generate the 10-year Treasury and 10Y-2Y spread as one image and send it",
    ),
    (
        "yield_spread",
        "--yield-spread",
        "T10Y2Y",
        "Force-generate the 10-year Treasury and 10Y-2Y spread as one image and send it",
    ),
    (
        "stress",
        "--stress",
        "STLFSI4",
        "Force-generate the St. Louis Fed stress index chart and send it to Telegram",
    ),
)
FRED_FORCE_FLAG_TO_SERIES = {
    dest: series_id for dest, _flag, series_id, _help in FRED_FORCE_FLAG_SPECS
}


def plot_config_for(series_id: str) -> SeriesPlotConfig:
    for config in DEFAULT_PLOT_CONFIGS:
        if config.series_id == series_id:
            return config
    raise KeyError(f"No plot config for series id {series_id!r}")


def select_fred_chart_work(
    updated_series_ids: set[str] | None,
) -> FredChartSelection:
    if updated_series_ids is None:
        updated_series_ids = set()

    generate_individuals: list[SeriesPlotConfig] = []
    for config in FRED_PLOT_CONFIGS:
        if config.series_id in updated_series_ids:
            generate_individuals.append(config)

    generate_combined: list[FredCombinedPlotConfig] = []
    for combined in FRED_COMBINED_PLOT_CONFIGS:
        member_ids = {spec.series_id for spec in combined.series}
        if bool(member_ids & updated_series_ids):
            generate_combined.append(combined)

    send_individuals = tuple(
        config
        for config in generate_individuals
        if config.series_id in UNGROUPED_SEND_SERIES_IDS
    )
    return FredChartSelection(
        generate_individuals=tuple(generate_individuals),
        generate_combined=tuple(generate_combined),
        send_individuals=send_individuals,
        send_combined=tuple(generate_combined),
    )


def combined_configs_for_series(
    series_ids: list[str],
) -> list[FredCombinedPlotConfig]:
    wanted = set(series_ids)
    return [
        config
        for config in FRED_COMBINED_PLOT_CONFIGS
        if any(spec.series_id in wanted for spec in config.series)
    ]


def expand_grouped_series_ids(series_ids: list[str]) -> list[str]:
    expanded = list(series_ids)
    have = set(expanded)
    for pair in GROUPED_FORCE_PAIRS:
        if not (set(pair) & have):
            continue
        for series_id in pair:
            if series_id not in have:
                expanded.append(series_id)
                have.add(series_id)
    return expanded


def expand_forced_plot_configs(
    plot_configs: list[SeriesPlotConfig],
) -> list[SeriesPlotConfig]:
    by_id = {config.series_id: config for config in DEFAULT_PLOT_CONFIGS}
    expanded_ids = expand_grouped_series_ids(
        [config.series_id for config in plot_configs]
    )
    expanded = list(plot_configs)
    have = {config.series_id for config in expanded}
    for series_id in expanded_ids:
        if series_id not in have and series_id in by_id:
            expanded.append(by_id[series_id])
            have.add(series_id)
    return expanded
