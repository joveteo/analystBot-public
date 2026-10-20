from __future__ import annotations

import unittest
from datetime import UTC, datetime

from analystbot.db.date_utils import (
    date_str_to_unix,
    datetime_to_unix,
    get_current_unix_timestamp,
    get_unix_timestamp_for_date,
    normalize_daily_unix_timestamp,
    unix_ms_to_unix,
    unix_to_date_str,
    unix_to_datetime,
    validate_unix_timestamp,
)


class DateUtilsTests(unittest.TestCase):
    def test_unix_and_date_string_round_trip_midnight_utc(self) -> None:
        timestamp = date_str_to_unix("2026-10-05")
        self.assertEqual(unix_to_date_str(timestamp), "2026-10-05")
        self.assertEqual(unix_to_datetime(timestamp), datetime(2026, 10, 5, tzinfo=UTC))

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        naive = datetime(2026, 1, 2, 15, 30, 0)
        self.assertEqual(
            datetime_to_unix(naive), datetime_to_unix(naive.replace(tzinfo=UTC))
        )

    def test_unix_ms_converts_to_seconds(self) -> None:
        self.assertEqual(unix_ms_to_unix(1_700_000_000_123), 1_700_000_000)

    def test_normalize_daily_bar_drops_intraday_offset(self) -> None:
        four_am = date_str_to_unix("2026-03-10") + 4 * 3600
        self.assertEqual(
            normalize_daily_unix_timestamp(four_am),
            date_str_to_unix("2026-03-10"),
        )

    def test_validate_unix_timestamp_bounds(self) -> None:
        self.assertTrue(validate_unix_timestamp(0))
        self.assertTrue(validate_unix_timestamp(date_str_to_unix("2026-01-01")))
        self.assertFalse(validate_unix_timestamp(-1))
        self.assertFalse(validate_unix_timestamp(4_102_444_801))

    def test_named_date_helper_matches_date_string(self) -> None:
        self.assertEqual(
            get_unix_timestamp_for_date(2026, 10, 5),
            date_str_to_unix("2026-10-05"),
        )

    def test_current_unix_timestamp_is_recent(self) -> None:
        now = get_current_unix_timestamp()
        self.assertGreater(now, date_str_to_unix("2026-01-01"))
        self.assertTrue(validate_unix_timestamp(now))


if __name__ == "__main__":
    unittest.main()
