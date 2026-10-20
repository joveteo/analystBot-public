import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from analystbot.core.log_config import setup_logger

logger = setup_logger("db.sqlite")

SCHEMA_VERSION = 3


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _stock_data_columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[1]) for row in conn.execute("PRAGMA table_info(stock_data)").fetchall()
    }


def _migrate_stock_data_v2(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "stock_data"):
        return

    columns = _stock_data_columns(conn)
    if "volume" not in columns:
        conn.execute("ALTER TABLE stock_data ADD COLUMN volume REAL")
    if "vwap" not in columns:
        conn.execute("ALTER TABLE stock_data ADD COLUMN vwap REAL")


def _migrate_stock_data_v3(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "stock_data"):
        return

    columns = _stock_data_columns(conn)
    if "sma_200" not in columns:
        conn.execute("ALTER TABLE stock_data ADD COLUMN sma_200 REAL")


def configure_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    logger.debug("SQLite pragmas configured: WAL mode, busy_timeout=5000ms")


def ensure_schema_version(conn: sqlite3.Connection) -> None:
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
            """)

            _migrate_stock_data_v2(conn)
            _migrate_stock_data_v3(conn)

            conn.execute(
                """
                INSERT OR IGNORE INTO schema_version (version, applied_at)
                VALUES (?, ?)
                """,
                (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )
    except sqlite3.Error:
        logger.exception("Schema migration to version %d failed", SCHEMA_VERSION)
        raise

    logger.debug(f"Schema version {SCHEMA_VERSION} verified")


def connect(
    db_path: Path,
    *,
    timeout: float = 30.0,
    pragmas: bool = True,
    schema_version: bool = True,
) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=timeout)

    if pragmas:
        configure_pragmas(conn)

    if schema_version:
        ensure_schema_version(conn)

    return conn


@contextmanager
def connection(
    db_path: Path,
    *,
    timeout: float = 30.0,
    pragmas: bool = True,
    schema_version: bool = True,
) -> Generator[sqlite3.Connection, None, None]:
    conn = connect(
        db_path, timeout=timeout, pragmas=pragmas, schema_version=schema_version
    )
    try:
        yield conn
    finally:
        conn.close()
