from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from analystbot.api.tastytrade import BarRequest, TastytradeClient
from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.core.validation import validate_symbol
from analystbot.db import StockRepository, connect
from analystbot.db.date_utils import datetime_to_unix, unix_to_datetime

logger = setup_logger("services.market_data")

_NYSE_TZ = ZoneInfo("America/New_York")
_NYSE_REGULAR_CLOSE = time(16, 0)


ETF_WATCHLIST = (
    "TLT",
    "SCHD",
    "PHYS",
    "GLD",
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
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

SHARE_CLASS_ALIASES: dict[str, str] = {
    "GOOG": "GOOGL",
    "GOOGM": "GOOGL",
    "GOOGN": "GOOGL",
    "BRK.A": "BRK.B",
}


def canonical_company_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper().replace("-", ".")
    return SHARE_CLASS_ALIASES.get(normalized, normalized)


def collapse_share_classes(
    ranked_symbols: Sequence[str], *, limit: int = 100
) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw in ranked_symbols:
        if not raw or not raw.strip():
            continue
        symbol = canonical_company_symbol(raw)
        if not validate_symbol(symbol):
            raise RuntimeError(f"Invalid hard-coded symbol: {raw}")
        if symbol in seen:
            continue
        seen.add(symbol)
        selected.append(symbol)
        if len(selected) >= limit:
            break
    if len(selected) != limit:
        raise RuntimeError(
            "Need "
            f"{limit} unique companies after collapsing share classes, "
            f"got {len(selected)}"
        )
    return tuple(selected)


_TOP_COMPANIES_RANKED = (
    "NVDA",
    "AAPL",
    "GOOG",
    "MSFT",
    "AMZN",
    "SPCX",
    "AVGO",
    "META",
    "TSLA",
    "MU",
    "BRK.B",
    "LLY",
    "JPM",
    "WMT",
    "AMD",
    "V",
    "XOM",
    "JNJ",
    "INTC",
    "MA",
    "ABBV",
    "CSCO",
    "BAC",
    "ORCL",
    "CVX",
    "PLTR",
    "COST",
    "KO",
    "CAT",
    "LRCX",
    "AMAT",
    "DELL",
    "MRK",
    "UNH",
    "PG",
    "MS",
    "GE",
    "NFLX",
    "HD",
    "GS",
    "PM",
    "WFC",
    "PANW",
    "RTX",
    "GEV",
    "ANET",
    "TXN",
    "SNDK",
    "KLAC",
    "C",
    "IBM",
    "TMO",
    "AXP",
    "MRVL",
    "CRWD",
    "VZ",
    "APH",
    "AMGN",
    "CRM",
    "TMUS",
    "QCOM",
    "PEP",
    "SCHW",
    "DIS",
    "ADI",
    "DE",
    "MCD",
    "T",
    "GILD",
    "ABT",
    "BLK",
    "NEE",
    "WELL",
    "UNP",
    "BA",
    "COP",
    "SCCO",
    "WDC",
    "PFE",
    "IBKR",
    "BX",
    "UBER",
    "GLW",
    "DHR",
    "TJX",
    "NOW",
    "NEM",
    "ISRG",
    "PLD",
    "BKNG",
    "VRTX",
    "BMY",
    "COF",
    "PGR",
    "SPGI",
    "CVS",
    "LMT",
    "PH",
    "SNOW",
    "MO",
)
TOP_100_COMPANIES = collapse_share_classes(_TOP_COMPANIES_RANKED)


def _build_default_watchlist() -> tuple[str, ...]:
    if len(TOP_100_COMPANIES) != 100 or len(set(TOP_100_COMPANIES)) != 100:
        raise RuntimeError("TOP_100_COMPANIES must contain 100 unique symbols")
    leftover_classes = sorted(
        symbol
        for symbol in TOP_100_COMPANIES
        if canonical_company_symbol(symbol) != symbol
    )
    if leftover_classes:
        raise RuntimeError(
            "TOP_100_COMPANIES contains extra share classes: "
            + ", ".join(leftover_classes)
        )

    all_symbols = (*ETF_WATCHLIST, *TOP_100_COMPANIES)
    invalid = [symbol for symbol in all_symbols if not validate_symbol(symbol)]
    if invalid:
        raise RuntimeError(f"Invalid hard-coded symbols: {', '.join(invalid)}")

    return tuple(sorted(set(all_symbols)))


DEFAULT_WATCHLIST = _build_default_watchlist()

REQUIRED_MARKET_SYMBOLS = frozenset(
    {
        "SPY",
        "QQQ",
        "IWM",
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
    }
)


class TradingCalendar:
    def __init__(self):
        self.calendar = mcal.get_calendar("NYSE")

    def get_trading_days(self, start_date: datetime, end_date: datetime) -> set[int]:
        schedule = self.calendar.schedule(start_date=start_date, end_date=end_date)
        return {
            datetime_to_unix(datetime.combine(d, datetime.min.time()))
            for d in schedule.index.date
        }

    def is_trading_day(self, timestamp: int) -> bool:
        dt = unix_to_datetime(timestamp)
        days = self.get_trading_days(dt, dt)
        return timestamp in days


class MarketDataService:
    def __init__(self, settings: Settings, historical_days: int = 365):
        self.settings = settings
        self.historical_days = historical_days
        self.calendar = TradingCalendar()

    def get_latest_completed_market_date(
        self, current_time: datetime | None = None
    ) -> date:
        now = current_time or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        ny_now = now.astimezone(_NYSE_TZ)
        candidate = ny_now.date()
        if ny_now.time() < _NYSE_REGULAR_CLOSE:
            candidate -= timedelta(days=1)

        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)

        while True:
            candidate_ts = datetime_to_unix(
                datetime.combine(candidate, datetime.min.time())
            )
            if self.calendar.is_trading_day(candidate_ts):
                return candidate
            candidate -= timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate -= timedelta(days=1)

    def plan_update(
        self,
        symbol: str,
        repo: StockRepository,
        force_full: bool = False,
    ) -> BarRequest | None:
        today = datetime.now(UTC).date()
        end_date = self.get_latest_completed_market_date()

        if force_full or not repo.symbol_exists(symbol):
            start_date = today - timedelta(days=self.historical_days)
            logger.info(f"{symbol}: Full fetch from {start_date}")
        else:
            latest_ts = repo.get_latest_timestamp(symbol)
            if latest_ts:
                latest_dt = unix_to_datetime(latest_ts)
                start_date = latest_dt.date()
            else:
                start_date = today - timedelta(days=self.historical_days)

        if start_date > end_date:
            logger.debug(f"{symbol}: Already up to date")
            return None

        return BarRequest(
            symbol=symbol.upper(),
            from_date=start_date.strftime("%Y-%m-%d"),
            to_date=end_date.strftime("%Y-%m-%d"),
        )

    def persist_bars(
        self,
        symbol: str,
        bars: list[dict[str, Any]],
        repo: StockRepository,
    ) -> tuple[int, int, int]:
        fetched = len(bars)
        inserted, skipped = repo.upsert_ohlc_batch(bars)
        logger.info(
            f"{symbol}: fetched={fetched}, inserted={inserted}, skipped={skipped}"
        )
        return fetched, inserted, skipped

    def update_symbol(
        self,
        symbol: str,
        client: TastytradeClient,
        repo: StockRepository,
        force_full: bool = False,
    ) -> tuple[int, int, int]:
        if not validate_symbol(symbol):
            logger.warning(f"Invalid symbol format: {symbol}")
            return 0, 0, 1

        plan = self.plan_update(symbol, repo, force_full)
        if plan is None:
            return 0, 0, 0

        bars = client.get_daily_bars_range(plan.symbol, plan.from_date, plan.to_date)
        if bars is None:
            logger.warning(f"{symbol}: API fetch failed")
            return 0, 0, 1
        return self.persist_bars(symbol, bars, repo)


def run(
    settings: Settings,
    symbols: list[str] | None = None,
    force_full: bool = False,
) -> bool:
    log_script_start(logger, "Market Data Update")
    start_time = datetime.now()

    is_valid, missing = settings.validate("market_data")
    if not is_valid:
        logger.error(f"Missing required settings: {missing}")
        log_script_end(logger, "Market Data Update", start_time, False)
        return False

    try:
        client = TastytradeClient.from_settings(settings)
        service = MarketDataService(settings)

        if symbols:
            target_symbols = [s.upper() for s in symbols if validate_symbol(s)]
        else:
            target_symbols = list(DEFAULT_WATCHLIST)

        if not target_symbols:
            logger.error("No valid symbols to process")
            log_script_end(logger, "Market Data Update", start_time, False)
            return False

        logger.info(f"Processing {len(target_symbols)} symbols")

        conn = connect(settings.db_path)
        repo = StockRepository(conn)

        total_fetched = 0
        total_inserted = 0
        total_skipped = 0
        failed_symbols = []

        try:
            if symbols is None:
                deleted_symbols, deleted_rows = repo.delete_symbols_except(
                    set(DEFAULT_WATCHLIST)
                )
                if deleted_symbols:
                    logger.info(
                        "Pruned %s leftover symbols (%s rows) outside the watchlist",
                        deleted_symbols,
                        deleted_rows,
                    )

            plans: list[BarRequest] = []
            for symbol in target_symbols:
                plan = service.plan_update(symbol, repo, force_full)
                if plan is None:
                    continue
                plans.append(plan)

            fetched_by_symbol: dict[str, list[dict[str, Any]] | None] = {}
            if plans:
                fetched_by_symbol = client.get_daily_bars_batch(plans)

            planned = {plan.symbol.upper() for plan in plans}
            for symbol in target_symbols:
                try:
                    if symbol not in planned:
                        continue
                    bars = fetched_by_symbol.get(symbol)
                    if bars is None:
                        logger.warning(f"{symbol}: API fetch failed")
                        failed_symbols.append(symbol)
                        continue
                    fetched, inserted, skipped = service.persist_bars(
                        symbol, bars, repo
                    )
                    total_fetched += fetched
                    total_inserted += inserted
                    total_skipped += skipped

                    if (fetched == 0 and skipped > 0) or (
                        fetched > 0 and inserted == 0
                    ):
                        failed_symbols.append(symbol)
                except Exception as e:
                    logger.error(f"Failed to update {symbol}: {e}")
                    failed_symbols.append(symbol)

            if failed_symbols:
                retry_plans = [
                    plan
                    for plan in (
                        service.plan_update(symbol, repo, force_full)
                        for symbol in failed_symbols
                    )
                    if plan is not None
                ]
                if retry_plans:
                    logger.info("Retrying %s failed symbol(s) once", len(retry_plans))
                    retry_fetched = client.get_daily_bars_batch(retry_plans)
                    recovered: list[str] = []
                    for symbol in failed_symbols:
                        bars = retry_fetched.get(symbol)
                        if bars is None:
                            continue
                        try:
                            fetched, inserted, skipped = service.persist_bars(
                                symbol, bars, repo
                            )
                        except Exception as e:
                            logger.error("Retry failed for %s: %s", symbol, e)
                            continue
                        total_fetched += fetched
                        total_inserted += inserted
                        total_skipped += skipped
                        if fetched > 0 and inserted > 0:
                            recovered.append(symbol)
                    if recovered:
                        logger.info(
                            "Recovered %s symbol(s) on retry: %s",
                            len(recovered),
                            ", ".join(recovered[:10]),
                        )
                        failed_symbols = [
                            symbol
                            for symbol in failed_symbols
                            if symbol not in recovered
                        ]

        finally:
            conn.close()

        total = len(target_symbols)
        successful = total - len(failed_symbols)
        success_rate = (successful / total * 100) if total > 0 else 0
        missing_required = sorted(
            symbol
            for symbol in REQUIRED_MARKET_SYMBOLS
            if symbol in failed_symbols
            and (symbols is None or symbol in {s.upper() for s in symbols})
        )
        workflow_success = successful > 0 and success_rate >= 80
        if missing_required:
            logger.error(
                "Required chart symbols failed: %s", ", ".join(missing_required)
            )
            workflow_success = False

        logger.info("Market data update summary:")
        logger.info(f"   Symbols processed: {successful}/{total} ({success_rate:.1f}%)")
        logger.info(f"   Total fetched: {total_fetched}")
        logger.info(f"   Total inserted: {total_inserted}")
        logger.info(f"   Total skipped: {total_skipped}")

        if failed_symbols:
            logger.warning(f"   Failed: {', '.join(failed_symbols[:10])}")
            if len(failed_symbols) > 10:
                logger.warning(f"   ... and {len(failed_symbols) - 10} more")

        log_script_end(logger, "Market Data Update", start_time, workflow_success)
        return workflow_success

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "Market Data Update", start_time, False)
        return False
