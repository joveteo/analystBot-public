from functools import partial

from analystbot.pipeline.runner import StageConfig

DATABASE_UPDATE_TIMEOUT_SECONDS = 210
DATABASE_UPDATE_ATTEMPTS = 3
FETCH_PARALLEL_GROUP = "fetch"


def _get_config_by_series_id(series_id: str):
    from analystbot.services.fred_chart_catalog import plot_config_for

    return plot_config_for(series_id)


def build_default_stages() -> list[StageConfig]:
    from analystbot.api.fred import FRED_SERIES_LABEL
    from analystbot.services import (
        charting,
        fred_data,
        heatmap_charts,
        indicators,
        market_charts,
        market_data,
        notifications,
        rrg_charts,
    )

    return [
        StageConfig(
            name="Database Update",
            description="Fetch latest OHLCV/VWAP data from tastytrade DXLink candles",
            run_fn=market_data.run,
            critical=True,
            timeout_seconds=DATABASE_UPDATE_TIMEOUT_SECONDS,
            max_attempts=DATABASE_UPDATE_ATTEMPTS,
            parallel_group=FETCH_PARALLEL_GROUP,
            validation_stage="market_data",
            worker_id="market_data",
        ),
        StageConfig(
            name="FRED Update",
            description=f"Fetch FRED economic data ({FRED_SERIES_LABEL})",
            run_fn=fred_data.run,
            critical=False,
            timeout_seconds=300,
            parallel_group=FETCH_PARALLEL_GROUP,
            worker_id="fred_data",
        ),
        StageConfig(
            name="Indicator Calculation",
            description="Calculate VixFix BTD/STR indicators for all symbols",
            run_fn=indicators.run,
            critical=True,
        ),
        StageConfig(
            name="Market Supertrend Charts",
            description="Generate a combined SPY/QQQ/IWM Supertrend chart image",
            run_fn=market_charts.run,
            critical=False,
        ),
        StageConfig(
            name="Market Heatmap Chart",
            description="Generate the top-100 market-cap heatmap (1-day)",
            run_fn=heatmap_charts.run,
            critical=False,
            timeout_seconds=300,
            worker_id="heatmap_charts",
        ),
        StageConfig(
            name="Sector RRG Chart",
            description="Generate the weekly S&P 500 sector RRG chart (Saturdays)",
            run_fn=rrg_charts.run,
            critical=False,
        ),
        StageConfig(
            name="FRED Graphs",
            description="Generate FRED economic data visualizations",
            run_fn=charting.run,
            critical=False,
            timeout_seconds=300,
            worker_id="charting",
        ),
        StageConfig(
            name="Telegram Delivery",
            description="Send VixFix, FRED album, and chart-topic album to Telegram",
            run_fn=notifications.run_delivery,
            critical=False,
            timeout_seconds=300,
            worker_id="notifications_delivery",
            inject_succeeded_stages=True,
        ),
    ]


def build_rebuild_stages(symbols: list[str] | None) -> list[StageConfig]:
    from analystbot.api.fred import FRED_SERIES_LABEL
    from analystbot.services import fred_data, indicators, market_data

    if symbols:
        market_description = (
            f"Force full OHLCV/VWAP refresh for selected symbols ({', '.join(symbols)})"
        )
        indicator_description = (
            "Calculate VixFix BTD/STR indicators for selected symbols"
        )
    else:
        market_description = (
            "Force full 365-day OHLCV/VWAP refresh from tastytrade DXLink candles"
        )
        indicator_description = "Calculate VixFix BTD/STR indicators for all symbols"

    stages = [
        StageConfig(
            name="Database Rebuild",
            description=market_description,
            run_fn=partial(
                market_data.run,
                symbols=symbols,
                force_full=True,
            ),
            critical=True,
            timeout_seconds=DATABASE_UPDATE_TIMEOUT_SECONDS,
            max_attempts=DATABASE_UPDATE_ATTEMPTS,
            validation_stage="market_data",
            worker_id="market_data",
            worker_kwargs={"symbols": symbols, "force_full": True},
        ),
        StageConfig(
            name="Indicator Calculation",
            description=indicator_description,
            run_fn=partial(indicators.run, symbols=symbols)
            if symbols
            else indicators.run,
            critical=True,
        ),
    ]

    if not symbols:
        stages.append(
            StageConfig(
                name="FRED Update",
                description=f"Force full FRED refresh ({FRED_SERIES_LABEL})",
                run_fn=partial(fred_data.run, force_full=True),
                critical=False,
                timeout_seconds=300,
                worker_id="fred_data",
                worker_kwargs={"force_full": True},
            )
        )

    return stages


def build_forced_chart_stages(
    *,
    supertrend: bool = False,
    vix: bool = False,
    rrg: bool = False,
    heatmap: bool = False,
    fred_flags: dict[str, bool] | None = None,
) -> list[StageConfig]:
    from analystbot.services import (
        charting,
        heatmap_charts,
        market_charts,
        notifications,
        rrg_charts,
    )
    from analystbot.services.fred_chart_catalog import (
        FRED_FORCE_FLAG_TO_SERIES,
        expand_grouped_series_ids,
    )

    stages: list[StageConfig] = []
    album_include: list[str] = []

    if supertrend:
        stages.append(
            StageConfig(
                name="Market Supertrend Charts",
                description="Force-generate the combined SPY/QQQ/IWM Supertrend chart",
                run_fn=market_charts.run,
                critical=True,
            )
        )
        album_include.append("supertrend")

    flags = fred_flags or {}
    fred_series_ids = [
        FRED_FORCE_FLAG_TO_SERIES[dest]
        for dest in FRED_FORCE_FLAG_TO_SERIES
        if flags.get(dest)
    ]
    generate_series_ids = expand_grouped_series_ids(fred_series_ids)
    if fred_series_ids:
        fred_configs = [_get_config_by_series_id(sid) for sid in generate_series_ids]
        label = ", ".join(fred_series_ids)
        stages.append(
            StageConfig(
                name="FRED Graphs",
                description=f"Force-generate FRED chart(s): {label}",
                run_fn=partial(charting.run_forced, plot_configs=fred_configs),
                critical=True,
                timeout_seconds=300,
                worker_id="charting_forced",
                worker_kwargs={"series_ids": generate_series_ids},
            )
        )
        stages.append(
            StageConfig(
                name="FRED Telegram",
                description=f"Send FRED chart(s) to Telegram: {label}",
                run_fn=partial(
                    notifications.run_fred_telegram_forced,
                    series_ids=fred_series_ids,
                ),
                critical=True,
                timeout_seconds=300,
                validation_stage="fred_telegram",
                worker_id="notifications_fred_forced",
                worker_kwargs={"series_ids": fred_series_ids},
                depends_on="FRED Graphs",
            )
        )

    if vix:
        vix_config = _get_config_by_series_id("VIXCLS")
        stages.append(
            StageConfig(
                name="VIX Chart",
                description="Force-generate the VIX (VIXCLS) chart",
                run_fn=partial(charting.run_forced, plot_configs=[vix_config]),
                critical=True,
                timeout_seconds=300,
                worker_id="charting_forced",
                worker_kwargs={"series_ids": ["VIXCLS"]},
            )
        )
        album_include.append("vix")

    if heatmap:
        stages.append(
            StageConfig(
                name="Market Heatmap Chart",
                description="Force-generate the top-100 market-cap heatmap (1-day)",
                run_fn=heatmap_charts.run,
                critical=True,
                timeout_seconds=300,
                validation_stage="market_data",
                worker_id="heatmap_charts",
            )
        )
        album_include.append("heatmap")

    if rrg:
        stages.append(
            StageConfig(
                name="Sector RRG Chart",
                description="Force-generate the S&P 500 sector RRG chart (gate bypassed)",
                run_fn=rrg_charts.run_forced,
                critical=True,
            )
        )
        album_include.append("rrg")

    if album_include:
        label = ", ".join(album_include)
        stages.append(
            StageConfig(
                name="Chart Telegram",
                description=f"Send chart-topic image(s) to Telegram: {label}",
                run_fn=partial(
                    notifications.run_chart_album_forced,
                    include=album_include,
                ),
                critical=True,
                timeout_seconds=300,
                validation_stage="chart_telegram",
                worker_id="notifications_album_forced",
                worker_kwargs={"include": album_include, "forced": True},
                inject_succeeded_stages=True,
            )
        )

    return stages
