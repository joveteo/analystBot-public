import sqlite3
from datetime import UTC, datetime
from typing import Any

from analystbot.core.log_config import setup_logger

logger = setup_logger("db.fred_repo")


class FredRepository:
    REQUIRED_COLUMNS = {
        "series_id",
        "observation_date",
        "value",
        "frequency",
        "units",
        "last_updated_utc",
    }

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ensure_table_exists()
        self._validate_schema()

    def _ensure_table_exists(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fred_data (
                series_id TEXT NOT NULL,
                observation_date TEXT NOT NULL,
                value REAL,
                frequency TEXT NOT NULL,
                units TEXT,
                last_updated_utc TEXT,
                PRIMARY KEY (series_id, observation_date)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_fred_data_series_date
            ON fred_data(series_id, observation_date)
        """)
        self.conn.commit()
        logger.debug("FRED data table schema validated")

    def _validate_schema(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(fred_data)")
        columns = {row[1] for row in cursor.fetchall()}

        missing = self.REQUIRED_COLUMNS - columns
        if missing:
            raise RuntimeError(f"fred_data table missing required columns: {missing}")

    def get_latest_observation(self, series_id: str) -> tuple[str, float | None] | None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT observation_date, value
            FROM fred_data
            WHERE series_id = ?
            ORDER BY observation_date DESC
            LIMIT 1
        """,
            (series_id,),
        )
        result = cursor.fetchone()
        return result if result else None

    def get_observation_count(self, series_id: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM fred_data WHERE series_id = ?", (series_id,)
        )
        return int(cursor.fetchone()[0])

    def delete_series(self, series_id: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM fred_data WHERE series_id = ?", (series_id,))
        self.conn.commit()
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        logger.info("Deleted %s rows for %s", deleted, series_id)
        return int(deleted)

    def get_observations_since(
        self, series_id: str, start_date: str
    ) -> dict[str, float | None]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT observation_date, value
            FROM fred_data
            WHERE series_id = ? AND observation_date >= ?
            ORDER BY observation_date ASC
        """,
            (series_id, start_date),
        )
        return {row[0]: row[1] for row in cursor.fetchall()}

    def get_series_data(self, series_id: str) -> list[tuple[str, float | None]]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT observation_date, value
            FROM fred_data
            WHERE series_id = ?
            ORDER BY observation_date ASC
        """,
            (series_id,),
        )
        return cursor.fetchall()

    def get_series_stats(self, series_id: str) -> dict[str, Any]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) as count,
                MIN(observation_date) as min_date,
                MAX(observation_date) as max_date
            FROM fred_data
            WHERE series_id = ? AND value IS NOT NULL
        """,
            (series_id,),
        )
        row = cursor.fetchone()

        cursor.execute(
            """
            SELECT value FROM fred_data
            WHERE series_id = ? AND value IS NOT NULL
            ORDER BY observation_date DESC
            LIMIT 1
        """,
            (series_id,),
        )
        latest = cursor.fetchone()

        return {
            "count": row[0] if row else 0,
            "min_date": row[1] if row else None,
            "max_date": row[2] if row else None,
            "latest_value": latest[0] if latest else None,
        }

    def upsert_observations(
        self, series_id: str, observations: list[dict], frequency: str, units: str
    ) -> tuple[int, int]:
        if not observations:
            return 0, 0

        inserted = 0
        skipped = 0
        timestamp = datetime.now(UTC).isoformat()

        cursor = self.conn.cursor()

        for obs in observations:
            date_str = obs.get("date")
            value = obs.get("value")

            if not date_str or not self._validate_date_format(date_str):
                logger.warning(f"Invalid date format: {date_str}")
                skipped += 1
                continue

            try:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO fred_data
                    (series_id, observation_date, value, frequency, units, last_updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (series_id, date_str, value, frequency, units, timestamp),
                )
                inserted += 1
            except sqlite3.Error as e:
                logger.warning(f"Failed to insert {series_id}/{date_str}: {e}")
                skipped += 1

        self.conn.commit()
        return inserted, skipped

    def upsert_only_changed(
        self,
        series_id: str,
        observations: list[dict],
        frequency: str,
        units: str,
        existing_data: dict[str, float | None],
    ) -> tuple[int, int, int]:
        if not observations:
            return 0, 0, 0

        inserted = 0
        unchanged = 0
        skipped = 0
        timestamp = datetime.now(UTC).isoformat()

        cursor = self.conn.cursor()

        for obs in observations:
            date_str = obs.get("date")
            new_value = obs.get("value")

            if not date_str or not self._validate_date_format(date_str):
                logger.warning(f"Invalid date format: {date_str}")
                skipped += 1
                continue

            if date_str in existing_data:
                existing_value = existing_data[date_str]
                if self._values_equal(new_value, existing_value):
                    unchanged += 1
                    continue
                else:
                    logger.debug(
                        f"{series_id}/{date_str} revised: {existing_value} -> {new_value}"
                    )

            try:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO fred_data
                    (series_id, observation_date, value, frequency, units, last_updated_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (series_id, date_str, new_value, frequency, units, timestamp),
                )
                inserted += 1
            except sqlite3.Error as e:
                logger.warning(f"Failed to insert {series_id}/{date_str}: {e}")
                skipped += 1

        self.conn.commit()
        return inserted, unchanged, skipped

    @staticmethod
    def _values_equal(val1: float | None, val2: float | None) -> bool:
        if val1 is None and val2 is None:
            return True
        if val1 is None or val2 is None:
            return False
        return abs(val1 - val2) < 1e-9

    @staticmethod
    def _validate_date_format(date_str: str) -> bool:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return True
        except ValueError:
            return False
