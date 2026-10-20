from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from analystbot.core.config import Settings
from analystbot.db.date_utils import date_str_to_unix, unix_to_datetime
from analystbot.services.market_data import MarketDataService

_NY = ZoneInfo("America/New_York")
_ASIA_TZ = ZoneInfo("Asia/Tokyo")


def _weekday_trading_day(timestamp: int) -> bool:
    return unix_to_datetime(timestamp).weekday() < 5


class LatestCompletedMarketDateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MarketDataService(Settings(_loaded=True))

    def _completed_on(self, current_time: datetime) -> date:
        with patch.object(
            self.service.calendar,
            "is_trading_day",
            side_effect=_weekday_trading_day,
        ):
            return self.service.get_latest_completed_market_date(current_time)

    def test_cron_0535_asia_includes_monday_session_after_us_close(self) -> None:
        now = datetime(2026, 9, 15, 5, 35, tzinfo=_ASIA_TZ)
        self.assertEqual(self._completed_on(now), date(2026, 9, 14))

    def test_utc_calendar_minus_one_day_would_skip_monday(self) -> None:
        now = datetime(2026, 9, 14, 21, 35, tzinfo=UTC)
        self.assertEqual(self._completed_on(now), date(2026, 9, 14))

    def test_before_regular_close_uses_previous_session(self) -> None:
        now = datetime(2026, 9, 14, 15, 59, tzinfo=_NY)
        self.assertEqual(self._completed_on(now), date(2026, 9, 11))

    def test_at_regular_close_uses_that_session(self) -> None:
        now = datetime(2026, 9, 14, 16, 0, tzinfo=_NY)
        self.assertEqual(self._completed_on(now), date(2026, 9, 14))

    def test_saturday_morning_asia_uses_friday_session(self) -> None:
        now = datetime(2026, 9, 12, 5, 35, tzinfo=_ASIA_TZ)
        self.assertEqual(self._completed_on(now), date(2026, 9, 11))

    def test_holiday_walks_back_to_prior_session(self) -> None:
        def is_trading(timestamp: int) -> bool:
            day = unix_to_datetime(timestamp).date()
            if day == date(2026, 9, 7):
                return False
            return day.weekday() < 5

        with patch.object(
            self.service.calendar, "is_trading_day", side_effect=is_trading
        ):
            now = datetime(2026, 9, 7, 17, 35, tzinfo=_NY)
            self.assertEqual(
                self.service.get_latest_completed_market_date(now),
                date(2026, 9, 4),
            )


class PlanUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MarketDataService(Settings(_loaded=True))

    def test_incremental_fetch_includes_latest_stored_date(self) -> None:
        repo = MagicMock()
        repo.symbol_exists.return_value = True
        repo.get_latest_timestamp.return_value = date_str_to_unix("2026-09-14")
        with patch.object(
            self.service,
            "get_latest_completed_market_date",
            return_value=date(2026, 9, 14),
        ):
            plan = self.service.plan_update("PH", repo)

        assert plan is not None
        self.assertEqual(plan.from_date, "2026-09-14")
        self.assertEqual(plan.to_date, "2026-09-14")

    def test_incremental_fetch_starts_at_stored_date_not_the_next_day(self) -> None:
        repo = MagicMock()
        repo.symbol_exists.return_value = True
        repo.get_latest_timestamp.return_value = date_str_to_unix("2026-09-11")
        with patch.object(
            self.service,
            "get_latest_completed_market_date",
            return_value=date(2026, 9, 14),
        ):
            plan = self.service.plan_update("AMAT", repo)

        assert plan is not None
        self.assertEqual(plan.from_date, "2026-09-11")
        self.assertEqual(plan.to_date, "2026-09-14")


if __name__ == "__main__":
    unittest.main()
