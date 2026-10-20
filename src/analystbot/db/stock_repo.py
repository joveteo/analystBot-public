import sqlite3
from dataclasses import dataclass
from math import isfinite
from typing import Any

from analystbot.core.log_config import setup_logger
from analystbot.db.date_utils import (
    normalize_daily_unix_timestamp,
    validate_unix_timestamp,
)

logger = setup_logger("db.stock_repo")


ALLOWED_INDICATOR_COLS = {"btd_22", "str_22", "sma_200"}


@dataclass
class DatabaseConfig:
    TABLE_NAME: str = "stock_data"
    MIN_PRICE: float = 0.001
    MAX_PRICE: float = 1000000.0
    MAX_DAILY_RANGE_RATIO: float = 0.5
    BATCH_SIZE: int = 100


class StockRepository:
    def __init__(self, conn: sqlite3.Connection, config: DatabaseConfig | None = None):
        self.conn = conn
        self.config = config or DatabaseConfig()
        self._ensure_table_exists()

    def _ensure_table_exists(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.config.TABLE_NAME} (
                symbol TEXT NOT NULL,
                date INTEGER NOT NULL,
                open_price REAL,
                high_price REAL,
                low_price REAL,
                close_price REAL,
                volume REAL,
                vwap REAL,
                btd_22 REAL,
                str_22 REAL,
                sma_200 REAL,
                PRIMARY KEY (symbol, date)
            )
        """)
        self.conn.commit()
        logger.debug("Stock data table schema validated")

    def upsert_ohlc(self, data: dict[str, Any]) -> bool:
        row = self._prepare_ohlc_row(data)
        if row is None:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute(self._upsert_ohlc_sql(), row)
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"Database insertion failed: {e}")
            return False

    def upsert_ohlc_batch(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        prepared: list[tuple[Any, ...]] = []
        skipped = 0
        for data in rows:
            row = self._prepare_ohlc_row(data)
            if row is None:
                skipped += 1
            else:
                prepared.append(row)
        if not prepared:
            return 0, skipped
        try:
            with self.conn:
                self.conn.executemany(self._upsert_ohlc_sql(), prepared)
            return len(prepared), skipped
        except sqlite3.Error as e:
            logger.error(f"Database batch insertion failed: {e}")
            return 0, skipped + len(prepared)

    def _upsert_ohlc_sql(self) -> str:
        return f"""
            INSERT INTO {self.config.TABLE_NAME}
            (
                symbol, date, open_price, high_price, low_price, close_price,
                volume, vwap
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open_price = excluded.open_price,
                high_price = excluded.high_price,
                low_price = excluded.low_price,
                close_price = excluded.close_price,
                volume = excluded.volume,
                vwap = excluded.vwap
        """

    def _prepare_ohlc_row(self, data: dict[str, Any]) -> tuple[Any, ...] | None:
        normalized_data = dict(data)
        date_val = normalized_data.get("date")
        if isinstance(date_val, int):
            normalized_data["date"] = normalize_daily_unix_timestamp(date_val)
        if not self._validate_data(normalized_data):
            return None
        return (
            normalized_data["symbol"],
            normalized_data["date"],
            normalized_data["open"],
            normalized_data["high"],
            normalized_data["low"],
            normalized_data["close"],
            normalized_data.get("volume"),
            normalized_data.get("vwap"),
        )

    def _validate_data(self, data: dict[str, Any]) -> bool:
        if not data.get("symbol") or data.get("date") is None:
            return False

        date_val = data.get("date")
        if not isinstance(date_val, int) or not validate_unix_timestamp(date_val):
            logger.warning(
                f"Invalid UNIX timestamp for {data.get('symbol')}: {date_val}"
            )
            return False

        for price_field in ["open", "high", "low", "close"]:
            price = data.get(price_field)
            if price is not None and (
                not isinstance(price, (int, float))
                or not isfinite(price)
                or price < self.config.MIN_PRICE
                or price > self.config.MAX_PRICE
            ):
                logger.warning(
                    f"Invalid {price_field} price for {data['symbol']}: {price}"
                )
                return False

        volume = data.get("volume")
        if volume is not None and (
            not isinstance(volume, (int, float)) or not isfinite(volume) or volume < 0
        ):
            logger.warning(f"Invalid volume for {data['symbol']}: {volume}")
            return False

        vwap = data.get("vwap")
        if vwap is not None and (
            not isinstance(vwap, (int, float))
            or not isfinite(vwap)
            or vwap < self.config.MIN_PRICE
            or vwap > self.config.MAX_PRICE
        ):
            logger.warning(f"Invalid VWAP for {data['symbol']}: {vwap}")
            return False

        return True

    def update_indicators(
        self, symbol: str, indicators: dict[str, float | None]
    ) -> bool:
        valid_indicators = {
            k: v
            for k, v in indicators.items()
            if v is not None and k in ALLOWED_INDICATOR_COLS
        }

        if not valid_indicators:
            logger.debug(f"No valid indicators to update for {symbol}")
            return False

        try:
            cursor = self.conn.cursor()

            set_clause = ", ".join([f"{col} = ?" for col in valid_indicators])
            query = f"""
                UPDATE {self.config.TABLE_NAME}
                SET {set_clause}
                WHERE symbol = ? AND date = (
                    SELECT MAX(date) FROM {self.config.TABLE_NAME} WHERE symbol = ?
                )
            """

            params = list(valid_indicators.values()) + [symbol, symbol]
            cursor.execute(query, params)

            updated_rows = cursor.rowcount
            if updated_rows > 0:
                self.conn.commit()
                logger.debug(f"{symbol}: Updated {list(valid_indicators.keys())}")
                return True
            else:
                logger.warning(f"No rows updated for {symbol} - symbol may not exist")
                return False

        except sqlite3.Error as e:
            logger.error(f"Database error updating {symbol}: {e}")
            return False

    def symbol_exists(self, symbol: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            f"SELECT 1 FROM {self.config.TABLE_NAME} WHERE symbol = ? LIMIT 1",
            (symbol,),
        )
        return cursor.fetchone() is not None

    def get_latest_session_timestamp(self) -> int | None:
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT MAX(date) FROM {self.config.TABLE_NAME}")
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def get_latest_timestamp(self, symbol: str) -> int | None:
        cursor = self.conn.cursor()
        cursor.execute(
            f"SELECT MAX(date) FROM {self.config.TABLE_NAME} WHERE symbol = ?",
            (symbol,),
        )
        result = cursor.fetchone()[0]
        return int(result) if result is not None else None

    def get_existing_timestamps(
        self, symbol: str, limit: int | None = None
    ) -> set[int]:
        query = f"SELECT date FROM {self.config.TABLE_NAME} WHERE symbol = ? ORDER BY date DESC"
        if limit:
            query += f" LIMIT {limit}"

        cursor = self.conn.cursor()
        cursor.execute(query, (symbol,))
        return {row[0] for row in cursor.fetchall()}

    def get_all_symbols(self) -> list[str]:
        cursor = self.conn.cursor()
        cursor.execute(
            f"SELECT DISTINCT symbol FROM {self.config.TABLE_NAME} ORDER BY symbol"
        )
        return [row[0] for row in cursor.fetchall()]

    def get_symbol_count(self) -> int:
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT COUNT(DISTINCT symbol) FROM {self.config.TABLE_NAME}")
        return int(cursor.fetchone()[0])

    def count_symbols_except(self, allowlist: set[str]) -> tuple[int, int]:
        normalized = self._normalize_allowlist(allowlist)
        placeholders = ", ".join("?" for _ in normalized)
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            SELECT COUNT(DISTINCT symbol), COUNT(*)
            FROM {self.config.TABLE_NAME}
            WHERE symbol NOT IN ({placeholders})
            """,
            normalized,
        )
        symbol_count, row_count = cursor.fetchone()
        return int(symbol_count), int(row_count)

    def delete_symbols_except(self, allowlist: set[str]) -> tuple[int, int]:
        normalized = self._normalize_allowlist(allowlist)
        placeholders = ", ".join("?" for _ in normalized)

        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(
                    f"""
                    SELECT COUNT(DISTINCT symbol)
                    FROM {self.config.TABLE_NAME}
                    WHERE symbol NOT IN ({placeholders})
                    """,
                    normalized,
                )
                deleted_symbols = int(cursor.fetchone()[0])
                cursor.execute(
                    f"""
                    DELETE FROM {self.config.TABLE_NAME}
                    WHERE symbol NOT IN ({placeholders})
                    """,
                    normalized,
                )
                deleted_rows = cursor.rowcount
        except sqlite3.Error:
            logger.exception("Failed to delete symbols outside the allowlist")
            raise

        return deleted_symbols, deleted_rows

    @staticmethod
    def _normalize_allowlist(allowlist: set[str]) -> tuple[str, ...]:
        if not allowlist:
            raise ValueError("allowlist must not be empty")
        if any(not isinstance(symbol, str) or not symbol for symbol in allowlist):
            raise ValueError("allowlist must contain non-empty symbol strings")
        return tuple(sorted({symbol.upper() for symbol in allowlist}))

    def get_stock_data(self, symbol: str, days: int = 150) -> list[dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            SELECT
                date, open_price, high_price, low_price, close_price, volume, vwap
            FROM {self.config.TABLE_NAME}
            WHERE symbol = ?
            ORDER BY date DESC
            LIMIT ?
        """,
            (symbol, days),
        )

        rows = cursor.fetchall()
        rows.reverse()

        return [
            {
                "date": row[0],
                "open_price": row[1],
                "high_price": row[2],
                "low_price": row[3],
                "close_price": row[4],
                "volume": row[5],
                "vwap": row[6],
            }
            for row in rows
        ]

    def get_stock_data_range(
        self, symbol: str, start_ts: int, end_ts: int
    ) -> list[dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            f"""
            SELECT
                date, open_price, high_price, low_price, close_price, volume, vwap
            FROM {self.config.TABLE_NAME}
            WHERE symbol = ?
              AND date BETWEEN ? AND ?
            ORDER BY date ASC
        """,
            (symbol, start_ts, end_ts),
        )

        return [
            {
                "date": row[0],
                "open_price": row[1],
                "high_price": row[2],
                "low_price": row[3],
                "close_price": row[4],
                "volume": row[5],
                "vwap": row[6],
            }
            for row in cursor.fetchall()
        ]

    def get_signals(
        self, signal_type: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        normalized = signal_type.lower()
        if normalized == "btd":
            filter_condition = "btd_22 < 0"
            indicator_col = "btd_22"
        elif normalized == "str":
            filter_condition = "str_22 > 0"
            indicator_col = "str_22"
        else:
            raise ValueError(
                f"Unknown signal_type {signal_type!r}; expected 'btd' or 'str'"
            )

        query = f"""
            SELECT
                symbol,
                {indicator_col},
                close_price,
                sma_200,
                CASE
                    WHEN close_price IS NULL
                        OR sma_200 IS NULL
                        OR sma_200 = 0
                    THEN NULL
                    ELSE ((close_price - sma_200) / sma_200) * 100
                END AS pct_from_sma_200
            FROM {self.config.TABLE_NAME} s1
            WHERE s1.date = (
                SELECT MAX(date) FROM {self.config.TABLE_NAME}
            )
            AND {filter_condition}
            AND {indicator_col} IS NOT NULL
            ORDER BY close_price DESC
        """

        cursor = self.conn.cursor()
        if limit is not None:
            query += " LIMIT ?"
            cursor.execute(query, (limit,))
        else:
            cursor.execute(query)

        return [
            {
                "symbol": row[0],
                indicator_col: row[1],
                "close_price": row[2],
                "sma_200": row[3],
                "pct_from_sma_200": row[4],
            }
            for row in cursor.fetchall()
        ]
