from datetime import UTC, datetime


def unix_to_date_str(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")


def date_str_to_unix(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp())


def datetime_to_unix(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def unix_to_datetime(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=UTC)


def unix_ms_to_unix(timestamp_ms: int) -> int:
    return timestamp_ms // 1000


def normalize_daily_unix_timestamp(timestamp: int) -> int:
    dt = unix_to_datetime(timestamp)
    normalized = datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
    return int(normalized.timestamp())


def validate_unix_timestamp(timestamp: int) -> bool:
    MIN_TIMESTAMP = 0
    MAX_TIMESTAMP = 4102444800

    return MIN_TIMESTAMP <= timestamp <= MAX_TIMESTAMP


def get_current_unix_timestamp() -> int:
    return int(datetime.now(UTC).timestamp())


def get_unix_timestamp_for_date(year: int, month: int, day: int) -> int:
    dt = datetime(year, month, day, tzinfo=UTC)
    return int(dt.timestamp())
