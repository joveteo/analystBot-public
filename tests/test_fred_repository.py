from __future__ import annotations

import sqlite3
import unittest

from analystbot.db.fred_repo import FredRepository


class FredRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.repo = FredRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_empty_series_queries(self) -> None:
        self.assertIsNone(self.repo.get_latest_observation("CPIAUCSL"))
        self.assertEqual(self.repo.get_observation_count("CPIAUCSL"), 0)
        self.assertEqual(self.repo.get_series_data("CPIAUCSL"), [])
        self.assertEqual(
            self.repo.get_series_stats("CPIAUCSL"),
            {
                "count": 0,
                "min_date": None,
                "max_date": None,
                "latest_value": None,
            },
        )

    def test_upsert_observations_skips_invalid_dates(self) -> None:
        inserted, skipped = self.repo.upsert_observations(
            "CPIAUCSL",
            [
                {"date": "2026-01-01", "value": 3.1},
                {"date": "2026-13-01", "value": 9.9},
                {"date": None, "value": 1.0},
            ],
            frequency="m",
            units="Percent",
        )
        self.assertEqual(inserted, 1)
        self.assertEqual(skipped, 2)
        self.assertEqual(
            self.repo.get_latest_observation("CPIAUCSL"), ("2026-01-01", 3.1)
        )

    def test_upsert_only_changed_counts_revisions_and_unchanged(self) -> None:
        self.repo.upsert_observations(
            "UNRATE",
            [
                {"date": "2026-01-01", "value": 4.0},
                {"date": "2026-02-01", "value": 4.1},
            ],
            frequency="m",
            units="Percent",
        )
        existing = self.repo.get_observations_since("UNRATE", "2026-01-01")
        inserted, unchanged, skipped = self.repo.upsert_only_changed(
            "UNRATE",
            [
                {"date": "2026-01-01", "value": 4.0},
                {"date": "2026-02-01", "value": 4.2},
                {"date": "2026-03-01", "value": 4.3},
                {"date": "bad", "value": 1.0},
            ],
            frequency="m",
            units="Percent",
            existing_data=existing,
        )
        self.assertEqual((inserted, unchanged, skipped), (2, 1, 1))
        self.assertEqual(
            self.repo.get_latest_observation("UNRATE"), ("2026-03-01", 4.3)
        )
        self.assertEqual(
            self.repo.get_observations_since("UNRATE", "2026-02-01"),
            {"2026-02-01": 4.2, "2026-03-01": 4.3},
        )

    def test_series_stats_ignore_null_values(self) -> None:
        self.repo.upsert_observations(
            "PAYEMS",
            [
                {"date": "2026-01-01", "value": None},
                {"date": "2026-02-01", "value": 100.0},
                {"date": "2026-03-01", "value": 110.0},
            ],
            frequency="m",
            units="Thousands",
        )
        self.assertEqual(
            self.repo.get_series_stats("PAYEMS"),
            {
                "count": 2,
                "min_date": "2026-02-01",
                "max_date": "2026-03-01",
                "latest_value": 110.0,
            },
        )
        self.assertEqual(
            self.repo.get_series_data("PAYEMS"),
            [
                ("2026-01-01", None),
                ("2026-02-01", 100.0),
                ("2026-03-01", 110.0),
            ],
        )

    def test_empty_upsert_short_circuits(self) -> None:
        self.assertEqual(self.repo.upsert_observations("X", [], "d", "x"), (0, 0))
        self.assertEqual(
            self.repo.upsert_only_changed("X", [], "d", "x", {}),
            (0, 0, 0),
        )

    def test_values_equal_handles_none_and_float_epsilon(self) -> None:
        self.assertTrue(FredRepository._values_equal(None, None))
        self.assertFalse(FredRepository._values_equal(None, 1.0))
        self.assertTrue(FredRepository._values_equal(1.0, 1.0 + 1e-10))
        self.assertFalse(FredRepository._values_equal(1.0, 1.1))

    def test_missing_schema_columns_raise(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE fred_data (
                series_id TEXT NOT NULL,
                observation_date TEXT NOT NULL,
                value REAL,
                frequency TEXT NOT NULL,
                last_updated_utc TEXT,
                PRIMARY KEY (series_id, observation_date)
            )
        """)
        conn.commit()
        with self.assertRaises(RuntimeError):
            FredRepository(conn)
        conn.close()


if __name__ == "__main__":
    unittest.main()
