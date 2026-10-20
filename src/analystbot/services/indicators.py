from datetime import datetime
from math import isfinite

import pandas as pd

from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.db import StockRepository, connect

logger = setup_logger("services.indicators")


class VixFixCalculator:
    PERIODS = [22]
    SMA_PERIOD = 200
    DECIMAL_PLACES = 2
    MIN_DATA_DAYS = 220
    MIN_CALCULATION_DAYS = 23

    def calculate_btd_str(
        self, df: "pd.DataFrame", period: int
    ) -> tuple[float | None, float | None]:
        if len(df) < period + 1:
            return None, None

        current_day = df.iloc[-1]
        current_high = current_day["high_price"]
        current_low = current_day["low_price"]

        past_data = df.iloc[-(period + 1) : -1]
        lowest_close = past_data["close_price"].min()
        highest_close = past_data["close_price"].max()

        btd = None
        if lowest_close > 0:
            btd = ((current_high - lowest_close) / lowest_close) * 100
            btd = round(btd, self.DECIMAL_PLACES)

        str_value = None
        if highest_close > 0:
            str_value = ((current_low - highest_close) / highest_close) * 100
            str_value = round(str_value, self.DECIMAL_PLACES)

        return btd, str_value

    def calculate_sma(self, df: "pd.DataFrame", period: int) -> float | None:
        closes = pd.to_numeric(df["close_price"], errors="coerce").dropna()
        if len(closes) < period:
            return None

        sma = float(closes.iloc[-period:].mean())
        if not isfinite(sma) or sma <= 0:
            return None
        return round(sma, self.DECIMAL_PLACES)

    def calculate_all_periods(self, df: "pd.DataFrame") -> dict[str, float | None]:
        results = {}

        for period in self.PERIODS:
            btd, str_val = self.calculate_btd_str(df, period)
            results[f"btd_{period}"] = btd
            results[f"str_{period}"] = str_val

        results[f"sma_{self.SMA_PERIOD}"] = self.calculate_sma(df, self.SMA_PERIOD)

        return results


class IndicatorService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.calculator = VixFixCalculator()

    def process_symbol(self, symbol: str, repo: StockRepository) -> bool:
        try:
            rows = repo.get_stock_data(symbol, days=self.calculator.MIN_DATA_DAYS)

            if not rows:
                logger.warning(f"No data available for {symbol}")
                return False

            df = pd.DataFrame(rows)

            if len(df) < self.calculator.MIN_CALCULATION_DAYS:
                logger.warning(
                    f"Insufficient data for {symbol}: "
                    f"{len(df)} < {self.calculator.MIN_CALCULATION_DAYS}"
                )
                return False

            indicators = self.calculator.calculate_all_periods(df)

            success = repo.update_indicators(symbol, indicators)

            if success:
                valid = {k: v for k, v in indicators.items() if v is not None}
                logger.debug(f"{symbol}: {valid}")

            return success

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            return False


def run(settings: Settings, symbols: list[str] | None = None) -> bool:
    log_script_start(logger, "Technical Indicators Calculation")
    start_time = datetime.now()

    try:
        conn = connect(settings.db_path)
        repo = StockRepository(conn)

        service = IndicatorService(settings)

        if symbols:
            target_symbols = [s.upper() for s in symbols]
            logger.info(f"Processing specific symbols: {', '.join(target_symbols)}")
        else:
            target_symbols = repo.get_all_symbols()
            if not target_symbols:
                logger.warning("No symbols found in database")
                conn.close()
                log_script_end(
                    logger, "Technical Indicators Calculation", start_time, False
                )
                return False
            logger.info(f"Processing all {len(target_symbols)} symbols")

        successful = 0
        failed_symbols = []

        try:
            for symbol in target_symbols:
                try:
                    if service.process_symbol(symbol, repo):
                        successful += 1
                    else:
                        failed_symbols.append(symbol)
                except Exception as e:
                    logger.error(f"Failed to process {symbol}: {e}")
                    failed_symbols.append(symbol)

        finally:
            conn.close()

        total = len(target_symbols)
        success_rate = (successful / total * 100) if total > 0 else 0
        workflow_success = successful > 0 and success_rate >= 80

        logger.info("Workflow summary:")
        logger.info(f"   Symbols processed: {successful}/{total} ({success_rate:.1f}%)")

        if failed_symbols:
            logger.warning(f"   Failed: {', '.join(failed_symbols[:10])}")
            if len(failed_symbols) > 10:
                logger.warning(f"   ... and {len(failed_symbols) - 10} more")

        log_script_end(
            logger, "Technical Indicators Calculation", start_time, workflow_success
        )

        return workflow_success

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "Technical Indicators Calculation", start_time, False)
        return False
