from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from analystbot.db.date_utils import date_str_to_unix
from analystbot.db.sqlite import connect, ensure_schema_version
from analystbot.db.stock_repo import StockRepository


def _bar(symbol: str = "AAPL", date: str = "2026-01-02") -> dict[str, object]:
    return {
        "symbol": symbol,
        "date": date_str_to_unix(date),
        "open": 100.0,
        "high": 110.0,
        "low": 95.0,
        "close": 105.0,
        "volume": 1_234_567.0,
        "vwap": 103.5,
    }


class SchemaMigrationTests(unittest.TestCase):
    def test_fresh_schema_uses_version_three_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            conn = connect(db_path, pragmas=False)
            StockRepository(conn)

            columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_data)")}
            versions = {
                row[0] for row in conn.execute("SELECT version FROM schema_version")
            }
            conn.close()

            self.assertIn("volume", columns)
            self.assertIn("vwap", columns)
            self.assertIn("sma_200", columns)
            self.assertEqual(versions, {3})

    def test_v1_to_v3_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            original = sqlite3.connect(db_path)
            original.executescript("""
                CREATE TABLE stock_data (
                    symbol TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_price REAL,
                    btd_22 REAL,
                    str_22 REAL,
                    PRIMARY KEY (symbol, date)
                );
                CREATE TABLE schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00+00:00');
                INSERT INTO stock_data (
                    symbol, date, open_price, high_price, low_price, close_price
                ) VALUES ('AAPL', 1767312000, 100, 110, 95, 105);
            """)
            original.close()

            conn = connect(db_path, pragmas=False)
            ensure_schema_version(conn)

            columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_data)")}
            version_three_rows = conn.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 3"
            ).fetchone()[0]
            preserved_rows = conn.execute(
                "SELECT COUNT(*) FROM stock_data WHERE symbol = 'AAPL'"
            ).fetchone()[0]
            conn.close()

            self.assertIn("volume", columns)
            self.assertIn("vwap", columns)
            self.assertIn("sma_200", columns)
            self.assertEqual(version_three_rows, 1)
            self.assertEqual(preserved_rows, 1)

    def test_partially_applied_migrations_add_only_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            original = sqlite3.connect(db_path)
            original.executescript("""
                CREATE TABLE stock_data (
                    symbol TEXT NOT NULL,
                    date INTEGER NOT NULL,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_price REAL,
                    volume REAL,
                    btd_22 REAL,
                    str_22 REAL,
                    PRIMARY KEY (symbol, date)
                );
                CREATE TABLE schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_version VALUES (1, '2026-01-01T00:00:00+00:00');
            """)
            original.close()

            conn = connect(db_path, pragmas=False)
            ensure_schema_version(conn)

            columns = [row[1] for row in conn.execute("PRAGMA table_info(stock_data)")]
            version_three_rows = conn.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version = 3"
            ).fetchone()[0]
            conn.close()

            self.assertEqual(columns.count("volume"), 1)
            self.assertEqual(columns.count("vwap"), 1)
            self.assertEqual(columns.count("sma_200"), 1)
            self.assertEqual(version_three_rows, 1)


class StockRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.repo = StockRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_upsert_persists_volume_vwap_and_preserves_indicators(self) -> None:
        first = _bar()
        self.assertTrue(self.repo.upsert_ohlc(first))
        self.assertTrue(
            self.repo.update_indicators(
                "AAPL", {"btd_22": -1.5, "str_22": 2.5, "sma_200": 100.0}
            )
        )

        updated = {**first, "close": 106.0, "volume": 2_000_000.0, "vwap": 104.0}
        self.assertTrue(self.repo.upsert_ohlc(updated))

        row = self.conn.execute(
            """
            SELECT close_price, volume, vwap, btd_22, str_22, sma_200
            FROM stock_data WHERE symbol = 'AAPL'
            """
        ).fetchone()
        self.assertEqual(row, (106.0, 2_000_000.0, 104.0, -1.5, 2.5, 100.0))

        recent = self.repo.get_stock_data("AAPL")
        ranged = self.repo.get_stock_data_range(
            "AAPL", date_str_to_unix("2026-01-01"), date_str_to_unix("2026-01-03")
        )
        self.assertEqual(recent[0]["volume"], 2_000_000.0)
        self.assertEqual(recent[0]["vwap"], 104.0)
        self.assertEqual(ranged[0]["volume"], 2_000_000.0)
        self.assertEqual(ranged[0]["vwap"], 104.0)

    def test_missing_volume_and_vwap_are_null(self) -> None:
        bar = _bar()
        bar.pop("volume")
        bar.pop("vwap")

        self.assertTrue(self.repo.upsert_ohlc(bar))
        row = self.conn.execute(
            "SELECT volume, vwap FROM stock_data WHERE symbol = 'AAPL'"
        ).fetchone()
        self.assertEqual(row, (None, None))

    def test_invalid_volume_and_vwap_are_rejected(self) -> None:
        self.assertFalse(self.repo.upsert_ohlc({**_bar(), "volume": -1}))
        self.assertFalse(self.repo.upsert_ohlc({**_bar(), "volume": float("nan")}))
        self.assertFalse(self.repo.upsert_ohlc({**_bar(), "vwap": -1}))
        self.assertFalse(self.repo.upsert_ohlc({**_bar(), "vwap": float("inf")}))

    def test_upsert_batch_skips_invalid_and_preserves_indicators(self) -> None:
        first = _bar("AAPL", "2026-01-02")
        inserted, skipped = self.repo.upsert_ohlc_batch([first])
        self.assertEqual((inserted, skipped), (1, 0))
        self.assertTrue(
            self.repo.update_indicators(
                "AAPL", {"btd_22": -1.5, "str_22": 2.5, "sma_200": 100.0}
            )
        )

        updated = {**first, "close": 106.0, "volume": 2_000_000.0, "vwap": 104.0}
        extra = _bar("AAPL", "2026-01-05")
        invalid = {**_bar("AAPL", "2026-01-06"), "volume": -1}
        inserted, skipped = self.repo.upsert_ohlc_batch([updated, extra, invalid])
        self.assertEqual((inserted, skipped), (2, 1))

        rows = self.conn.execute(
            """
            SELECT date, close_price, volume, vwap, btd_22, str_22, sma_200
            FROM stock_data WHERE symbol = 'AAPL' ORDER BY date
            """
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:], (106.0, 2_000_000.0, 104.0, -1.5, 2.5, 100.0))
        self.assertEqual(rows[1][1:], (105.0, 1_234_567.0, 103.5, None, None, None))

    def test_upsert_batch_empty_or_all_invalid_inserts_nothing(self) -> None:
        self.assertEqual(self.repo.upsert_ohlc_batch([]), (0, 0))
        self.assertEqual(
            self.repo.upsert_ohlc_batch([{**_bar(), "volume": float("nan")}]),
            (0, 1),
        )
        self.assertEqual(self.repo.get_all_symbols(), [])

    def test_get_signals_derives_percentage_from_close_and_sma(self) -> None:
        self.repo.upsert_ohlc(_bar("AAPL"))
        self.repo.update_indicators(
            "AAPL", {"btd_22": -1.5, "str_22": 2.5, "sma_200": 100.0}
        )
        self.repo.upsert_ohlc(_bar("MSFT"))
        self.repo.update_indicators("MSFT", {"btd_22": -2.0})

        buy_signals = {
            signal["symbol"]: signal for signal in self.repo.get_signals("btd")
        }
        sell_signals = self.repo.get_signals("str")

        self.assertAlmostEqual(buy_signals["AAPL"]["pct_from_sma_200"], 5.0)
        self.assertEqual(buy_signals["AAPL"]["sma_200"], 100.0)
        self.assertIsNone(buy_signals["MSFT"]["pct_from_sma_200"])
        self.assertAlmostEqual(sell_signals[0]["pct_from_sma_200"], 5.0)

    def test_get_signals_drops_symbols_without_the_latest_session(self) -> None:
        self.repo.upsert_ohlc(_bar("AAPL", "2026-01-02"))
        self.repo.update_indicators("AAPL", {"btd_22": -1.5, "sma_200": 100.0})
        self.repo.upsert_ohlc(_bar("MSFT", "2026-01-05"))
        self.repo.update_indicators("MSFT", {"btd_22": -2.0, "sma_200": 100.0})

        buy_signals = self.repo.get_signals("btd")
        self.assertEqual([row["symbol"] for row in buy_signals], ["MSFT"])
        self.assertEqual(
            self.repo.get_latest_session_timestamp(), _bar("MSFT", "2026-01-05")["date"]
        )

    def test_delete_symbols_except_only_removes_outside_rows(self) -> None:
        self.repo.upsert_ohlc(_bar("AAPL"))
        self.repo.upsert_ohlc(_bar("SATS"))

        self.assertEqual(self.repo.count_symbols_except({"AAPL"}), (1, 1))
        self.assertEqual(self.repo.delete_symbols_except({"AAPL"}), (1, 1))
        self.assertEqual(self.repo.get_all_symbols(), ["AAPL"])

    def test_delete_symbols_except_rejects_empty_allowlist(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.delete_symbols_except(set())

    def test_delete_symbols_except_binds_allowlist_values(self) -> None:
        unusual_symbol = "AAPL') OR 1=1 --"
        self.repo.upsert_ohlc(_bar("AAPL"))
        self.repo.upsert_ohlc(_bar(unusual_symbol))

        self.assertEqual(self.repo.delete_symbols_except({unusual_symbol}), (1, 1))
        self.assertEqual(self.repo.get_all_symbols(), [unusual_symbol])

    def test_delete_symbols_except_rolls_back_on_failure(self) -> None:
        self.repo.upsert_ohlc(_bar("AAPL"))
        self.repo.upsert_ohlc(_bar("SATS"))
        self.conn.execute("""
            CREATE TRIGGER block_sats_delete
            BEFORE DELETE ON stock_data
            WHEN OLD.symbol = 'SATS'
            BEGIN
                SELECT RAISE(ABORT, 'blocked');
            END
        """)
        self.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.delete_symbols_except({"AAPL"})

        self.assertEqual(self.repo.get_all_symbols(), ["AAPL", "SATS"])


if __name__ == "__main__":
    unittest.main()
