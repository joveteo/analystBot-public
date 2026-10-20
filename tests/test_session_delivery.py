from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from analystbot.core.config import Settings
from analystbot.db.date_utils import date_str_to_unix
from analystbot.db.sqlite import connect
from analystbot.db.stock_repo import StockRepository
from analystbot.services.session_delivery import (
    already_sent,
    get_session_send_state_path,
    latest_session_date,
    mark_sent,
)


class SessionDeliveryStateTests(unittest.TestCase):
    def test_mark_sent_is_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "stocks.db"
            conn = connect(settings.db_path, pragmas=False)
            repo = StockRepository(conn)
            repo.upsert_ohlc(
                {
                    "symbol": "SPY",
                    "date": date_str_to_unix("2026-10-02"),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "vwap": 1,
                }
            )
            conn.close()

            self.assertFalse(already_sent(settings, "vixfix"))
            mark_sent(settings, "vixfix")
            self.assertTrue(already_sent(settings, "vixfix"))
            self.assertFalse(already_sent(settings, "heatmap"))

    def test_empty_database_is_not_already_sent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "stocks.db"
            conn = connect(settings.db_path, pragmas=False)
            StockRepository(conn)
            conn.close()
            self.assertIsNone(latest_session_date(settings))
            self.assertFalse(already_sent(settings, "vixfix"))
            mark_sent(settings, "vixfix")
            self.assertFalse(get_session_send_state_path(settings).exists())

    def test_invalid_state_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "stocks.db"
            conn = connect(settings.db_path, pragmas=False)
            StockRepository(conn).upsert_ohlc(
                {
                    "symbol": "SPY",
                    "date": date_str_to_unix("2026-10-02"),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                    "vwap": 1,
                }
            )
            conn.close()
            path = get_session_send_state_path(settings)
            path.write_text("{not json", encoding="utf-8")
            self.assertFalse(already_sent(settings, "vixfix"))
            path.write_text("[]", encoding="utf-8")
            self.assertFalse(already_sent(settings, "vixfix"))
            mark_sent(settings, "heatmap")
            self.assertTrue(already_sent(settings, "heatmap"))

    def test_new_session_clears_previous_sent_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "stocks.db"
            get_session_send_state_path(settings).write_text(
                '{"session_date":"2026-10-01","sent":["vixfix"]}',
                encoding="utf-8",
            )
            mark_sent(settings, "heatmap", session_date="2026-10-02")
            self.assertFalse(
                already_sent(settings, "vixfix", session_date="2026-10-02")
            )
            self.assertTrue(
                already_sent(settings, "heatmap", session_date="2026-10-02")
            )
