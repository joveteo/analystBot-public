#!/usr/bin/env python3

import argparse
import sys

from analystbot.core.config import Settings
from analystbot.core.log_config import setup_logger
from analystbot.core.validation import validate_symbol
from analystbot.pipeline import StageConfig, run_pipeline
from analystbot.pipeline.stages import (
    build_default_stages,
    build_forced_chart_stages,
    build_rebuild_stages,
)
from analystbot.services.fred_chart_catalog import FRED_FORCE_FLAG_SPECS

logger = setup_logger("main")

FORCE_FLAGS = (
    "supertrend",
    *(dest for dest, _flag, _series, _help in FRED_FORCE_FLAG_SPECS),
    "vix",
    "heatmap",
    "rrg",
)


def _parse_symbols_arg(value: str) -> list[str]:
    symbols = []
    seen = set()

    for raw_symbol in value.split(","):
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        if not validate_symbol(symbol):
            raise argparse.ArgumentTypeError(
                f"Invalid symbol {symbol!r}. "
                "Use 1-10 uppercase letters, digits, or periods."
            )
        if symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)

    if not symbols:
        raise argparse.ArgumentTypeError("At least one valid symbol is required.")

    return symbols


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analystbot",
        description="Financial market analysis bot",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m analystbot\n"
            "  python -m analystbot --rebuild\n"
            "  python -m analystbot --rebuild --symbols AAPL,MSFT\n"
            "  python -m analystbot --supertrend\n"
            "  python -m analystbot --gdp --housing --stress\n"
            "  python -m analystbot --unemployment --yield-spread\n"
            "  python -m analystbot --vix\n"
            "  python -m analystbot --heatmap\n"
            "  python -m analystbot --rrg"
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Rebuild the entire database: force a full historical refresh from "
            "tastytrade and FRED, then recalculate indicators (no Telegram)"
        ),
    )
    parser.add_argument(
        "--symbols",
        type=_parse_symbols_arg,
        help=(
            "Scope a rebuild to the provided comma-separated symbols, e.g. "
            "AAPL,MSFT. Must be used together with --rebuild"
        ),
    )
    parser.add_argument(
        "--supertrend",
        action="store_true",
        help=(
            "Force-generate the combined SPY/QQQ/IWM Supertrend chart and send "
            "it to Telegram"
        ),
    )
    for dest, flag, _series, help_text in FRED_FORCE_FLAG_SPECS:
        parser.add_argument(flag, dest=dest, action="store_true", help=help_text)
    parser.add_argument(
        "--vix",
        action="store_true",
        help=(
            "Force-generate the VIX (VIXCLS) chart from FRED data and send it "
            "to the chart Telegram topic"
        ),
    )
    parser.add_argument(
        "--heatmap",
        action="store_true",
        help=(
            "Force-generate the top-100 market-cap heatmap (1-day) and send "
            "it to the chart Telegram topic"
        ),
    )
    parser.add_argument(
        "--rrg",
        action="store_true",
        help=(
            "Force-generate the weekly S&P 500 sector RRG chart (bypassing the "
            "Saturday gate) and send it to Telegram"
        ),
    )

    args = parser.parse_args(argv)
    _validate_flag_combo(parser, args)
    return args


def _force_flags_set(args: argparse.Namespace) -> list[str]:
    return [name for name in FORCE_FLAGS if getattr(args, name)]


def _validate_flag_combo(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    force_flags = _force_flags_set(args)

    if args.symbols and not args.rebuild:
        parser.error("--symbols requires --rebuild (scoped rebuild)")

    if force_flags and (args.rebuild or args.symbols):
        parser.error("force-chart flags cannot be combined with --rebuild/--symbols")


def _get_pipeline_stages(args: argparse.Namespace) -> list[StageConfig]:
    if _force_flags_set(args):
        return build_forced_chart_stages(
            supertrend=args.supertrend,
            vix=args.vix,
            heatmap=args.heatmap,
            rrg=args.rrg,
            fred_flags={
                dest: getattr(args, dest)
                for dest, _flag, _series, _help in FRED_FORCE_FLAG_SPECS
            },
        )

    if args.rebuild:
        return build_rebuild_stages(symbols=args.symbols)

    return build_default_stages()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        settings = Settings()
        stages = _get_pipeline_stages(args)

        active_flags = []
        if args.rebuild:
            active_flags.append("--rebuild")
        if args.symbols:
            active_flags.append(f"--symbols={','.join(args.symbols)}")
        if args.supertrend:
            active_flags.append("--supertrend")
        for dest, flag, _series, _help in FRED_FORCE_FLAG_SPECS:
            if getattr(args, dest):
                active_flags.append(flag)
        if args.vix:
            active_flags.append("--vix")
        if args.heatmap:
            active_flags.append("--heatmap")
        if args.rrg:
            active_flags.append("--rrg")

        if active_flags:
            logger.info("Starting Analyst Bot Pipeline (%s)", ", ".join(active_flags))
        else:
            logger.info("Starting Analyst Bot Pipeline")

        success = run_pipeline(settings, stages=stages)
        return 0 if success else 1

    except Exception as e:
        logger.error(f"Pipeline failed with unexpected error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
