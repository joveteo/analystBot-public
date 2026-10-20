from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from analystbot import __main__
from analystbot.api.tastytrade import BarRequest
from analystbot.core.config import Settings
from analystbot.core.validation import validate_symbol
from analystbot.db.date_utils import date_str_to_unix
from analystbot.db.sqlite import connect
from analystbot.db.stock_repo import StockRepository
from analystbot.services import market_data


def _bar(symbol: str, date: str = "2026-01-02") -> dict[str, object]:
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


class StaticWatchlistTests(unittest.TestCase):
    def test_static_universe_has_expected_membership(self) -> None:
        self.assertEqual(len(market_data.TOP_100_COMPANIES), 100)
        self.assertEqual(len(set(market_data.TOP_100_COMPANIES)), 100)
        self.assertTrue(all(map(validate_symbol, market_data.TOP_100_COMPANIES)))

        expected = set(market_data.ETF_WATCHLIST) | set(market_data.TOP_100_COMPANIES)
        self.assertEqual(set(market_data.DEFAULT_WATCHLIST), expected)
        self.assertEqual(len(market_data.DEFAULT_WATCHLIST), len(expected))
        self.assertIn("GOOGL", market_data.TOP_100_COMPANIES)
        self.assertNotIn("GOOG", market_data.TOP_100_COMPANIES)
        self.assertNotIn("GOOG", market_data.DEFAULT_WATCHLIST)
        self.assertNotIn("GOOGM", market_data.TOP_100_COMPANIES)
        self.assertNotIn("GOOGN", market_data.TOP_100_COMPANIES)
        self.assertNotIn("BRK.A", market_data.TOP_100_COMPANIES)
        self.assertEqual(
            market_data.TOP_100_COMPANIES,
            market_data.collapse_share_classes(market_data._TOP_COMPANIES_RANKED),
        )

    def test_dynamic_watchlist_manager_is_removed(self) -> None:
        self.assertFalse(hasattr(market_data, "WatchlistManager"))
        self.assertFalse(hasattr(market_data, "SLICKCHARTS_URL"))
        self.assertFalse(hasattr(market_data, "SP500_CACHE_TTL"))

    def test_no_sp500_cli_option_is_removed(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            __main__._parse_args(["--no-sp500"])


class CollapseShareClassTests(unittest.TestCase):
    def test_extra_google_classes_do_not_occupy_slots(self) -> None:
        ranked = (
            "NVDA",
            "GOOGL",
            "GOOG",
            "GOOGM",
            "GOOGN",
            "AAPL",
            "MSFT",
            "AMZN",
        )
        self.assertEqual(
            market_data.collapse_share_classes(ranked, limit=4),
            ("NVDA", "GOOGL", "AAPL", "MSFT"),
        )

    def test_goog_alone_is_rewritten_to_googl(self) -> None:
        ranked = ("NVDA", "GOOG", "AAPL")
        self.assertEqual(
            market_data.collapse_share_classes(ranked, limit=3),
            ("NVDA", "GOOGL", "AAPL"),
        )

    def test_brk_a_collapses_to_brk_b(self) -> None:
        ranked = ("BRK.B", "BRK.A", "JPM")
        self.assertEqual(
            market_data.collapse_share_classes(ranked, limit=2),
            ("BRK.B", "JPM"),
        )

    def test_hyphenated_class_tickers_normalize(self) -> None:
        self.assertEqual(market_data.canonical_company_symbol("BRK-B"), "BRK.B")
        self.assertEqual(market_data.canonical_company_symbol("brk-a"), "BRK.B")
        self.assertEqual(market_data.canonical_company_symbol("GOOGL"), "GOOGL")

    def test_underfilled_rank_list_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unique companies"):
            market_data.collapse_share_classes(("NVDA", "GOOG", "GOOGL"), limit=3)


class WatchlistPruneTests(unittest.TestCase):
    def _settings(self, db_path: Path) -> Settings:
        settings = Settings(
            _loaded=True,
            tastytrade_client_secret="secret",
            tastytrade_refresh_token="refresh",
        )
        settings.db_path = db_path
        return settings

    def _seed(self, db_path: Path) -> None:
        conn = connect(db_path, pragmas=False)
        repo = StockRepository(conn)
        repo.upsert_ohlc(_bar("AAPL"))
        repo.upsert_ohlc(_bar("SATS"))
        conn.close()

    def _symbols(self, db_path: Path) -> list[str]:
        conn = connect(db_path, pragmas=False)
        symbols = StockRepository(conn).get_all_symbols()
        conn.close()
        return symbols

    def test_full_run_prunes_leftover_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            self._seed(db_path)
            settings = self._settings(db_path)
            with (
                patch(
                    "analystbot.services.market_data.TastytradeClient.from_settings"
                ) as from_settings,
                patch.object(
                    market_data.MarketDataService, "plan_update", return_value=None
                ),
            ):
                from_settings.return_value.get_daily_bars_batch.return_value = {}
                self.assertTrue(market_data.run(settings))
            self.assertEqual(self._symbols(db_path), ["AAPL"])

    def test_scoped_run_keeps_leftover_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            self._seed(db_path)
            settings = self._settings(db_path)
            with (
                patch(
                    "analystbot.services.market_data.TastytradeClient.from_settings"
                ) as from_settings,
                patch.object(
                    market_data.MarketDataService, "plan_update", return_value=None
                ),
            ):
                from_settings.return_value.get_daily_bars_batch.return_value = {}
                self.assertTrue(market_data.run(settings, symbols=["AAPL"]))
            self.assertEqual(self._symbols(db_path), ["AAPL", "SATS"])

    def test_planned_none_bars_count_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            conn = connect(db_path, pragmas=False)
            StockRepository(conn)
            conn.close()
            settings = self._settings(db_path)
            plan = BarRequest(
                symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
            )
            with (
                patch(
                    "analystbot.services.market_data.TastytradeClient.from_settings"
                ) as from_settings,
                patch.object(
                    market_data.MarketDataService, "plan_update", return_value=plan
                ),
            ):
                from_settings.return_value.get_daily_bars_batch.return_value = {
                    "AAPL": None
                }
                self.assertFalse(market_data.run(settings, symbols=["AAPL"]))
                self.assertEqual(
                    from_settings.return_value.get_daily_bars_batch.call_count, 2
                )

    def test_failed_symbol_is_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            conn = connect(db_path, pragmas=False)
            StockRepository(conn)
            conn.close()
            settings = self._settings(db_path)
            plan = BarRequest(
                symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
            )
            client = MagicMock()
            client.get_daily_bars_batch.side_effect = [
                {"AAPL": None},
                {"AAPL": [_bar("AAPL")]},
            ]
            with (
                patch(
                    "analystbot.services.market_data.TastytradeClient.from_settings",
                    return_value=client,
                ),
                patch.object(
                    market_data.MarketDataService, "plan_update", return_value=plan
                ),
            ):
                self.assertTrue(market_data.run(settings, symbols=["AAPL"]))
            self.assertEqual(client.get_daily_bars_batch.call_count, 2)

    def test_required_chart_symbol_failure_fails_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "stocks.db"
            conn = connect(db_path, pragmas=False)
            StockRepository(conn)
            conn.close()
            settings = self._settings(db_path)
            plan = BarRequest(
                symbol="SPY", from_date="2026-01-02", to_date="2026-01-02"
            )
            with (
                patch(
                    "analystbot.services.market_data.TastytradeClient.from_settings"
                ) as from_settings,
                patch.object(
                    market_data.MarketDataService, "plan_update", return_value=plan
                ),
                patch(
                    "analystbot.services.market_data.DEFAULT_WATCHLIST",
                    ("SPY", "AAPL"),
                ),
            ):
                from_settings.return_value.get_daily_bars_batch.return_value = {
                    "SPY": None
                }
                self.assertFalse(market_data.run(settings))


if __name__ == "__main__":
    unittest.main()
