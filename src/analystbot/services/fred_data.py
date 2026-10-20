import json
import re
from datetime import UTC, date, datetime
from typing import Any

from analystbot.api.fred import (
    DEFAULT_FRED_SERIES,
    NEXT_RELEASE_SERIES_IDS,
    FredClient,
    FredConfig,
    FredSeriesConfig,
    get_date_years_ago,
)
from analystbot.api.http_client import SessionManager
from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.db import FredRepository, connect

logger = setup_logger("services.fred_data")

FRED_UPDATE_STATE_FILENAME = "fred_update_state.json"
FRED_RELEASE_CALENDAR_FILENAME = "fred_release_calendar.json"
FRED_RELEASE_IDS_FILENAME = "fred_release_ids.json"
_RELEASE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def upcoming_release_date(value: str, *, as_of: date | None = None) -> str | None:
    if not isinstance(value, str) or _RELEASE_DATE_RE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = as_of or datetime.now(UTC).date()
    if parsed < today:
        return None
    return value


def get_fred_update_state_path(settings: Settings):
    return settings.data_dir / FRED_UPDATE_STATE_FILENAME


def write_fred_update_state(
    settings: Settings, updated_series_ids: list[str], processed_series_ids: list[str]
) -> None:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "updated_series_ids": updated_series_ids,
        "processed_series_ids": processed_series_ids,
    }
    state_path = get_fred_update_state_path(settings)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2))


def read_fred_update_state(settings: Settings) -> dict[str, Any] | None:
    state_path = get_fred_update_state_path(settings)
    if not state_path.exists():
        return None

    try:
        data = json.loads(state_path.read_text())
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid FRED update state artifact: %s", state_path)
        return None

    if not isinstance(data, dict):
        logger.warning("Ignoring malformed FRED update state artifact: %s", state_path)
        return None

    updated_series_ids = data.get("updated_series_ids")
    processed_series_ids = data.get("processed_series_ids")
    if not isinstance(updated_series_ids, list) or not isinstance(
        processed_series_ids, list
    ):
        logger.warning("Ignoring incomplete FRED update state artifact: %s", state_path)
        return None

    data["updated_series_ids"] = [str(series_id) for series_id in updated_series_ids]
    data["processed_series_ids"] = [
        str(series_id) for series_id in processed_series_ids
    ]
    return data


def get_fred_release_ids_path(settings: Settings):
    return settings.data_dir / FRED_RELEASE_IDS_FILENAME


def read_fred_release_ids(settings: Settings) -> dict[str, int]:
    path = get_fred_release_ids_path(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring invalid FRED release-id cache: %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    ids: dict[str, int] = {}
    raw = data.get("series_release_ids", data)
    if not isinstance(raw, dict):
        return {}
    for series_id, value in raw.items():
        try:
            ids[str(series_id)] = int(value)
        except (TypeError, ValueError):
            continue
    return ids


def write_fred_release_ids(settings: Settings, ids: dict[str, int]) -> None:
    path = get_fred_release_ids_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"series_release_ids": ids}, indent=2),
        encoding="utf-8",
    )


def get_fred_release_calendar_path(settings: Settings):
    return settings.data_dir / FRED_RELEASE_CALENDAR_FILENAME


def write_fred_release_calendar(
    settings: Settings, next_release_dates: dict[str, str]
) -> None:
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "next_release_dates": next_release_dates,
    }
    calendar_path = get_fred_release_calendar_path(settings)
    calendar_path.parent.mkdir(parents=True, exist_ok=True)
    calendar_path.write_text(json.dumps(payload, indent=2))


def read_fred_release_calendar(
    settings: Settings, *, as_of: date | None = None
) -> dict[str, str]:
    calendar_path = get_fred_release_calendar_path(settings)
    if not calendar_path.exists():
        return {}

    try:
        data = json.loads(calendar_path.read_text())
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid FRED release calendar: %s", calendar_path)
        return {}

    if not isinstance(data, dict):
        logger.warning("Ignoring malformed FRED release calendar: %s", calendar_path)
        return {}

    raw = data.get("next_release_dates", data)
    if not isinstance(raw, dict):
        logger.warning("Ignoring malformed FRED release calendar: %s", calendar_path)
        return {}

    calendar: dict[str, str] = {}
    for series_id, value in raw.items():
        if series_id == "timestamp" or not isinstance(value, str) or not value:
            continue
        upcoming = upcoming_release_date(value, as_of=as_of)
        if upcoming is None:
            continue
        calendar[str(series_id)] = upcoming
    return calendar


def refresh_fred_release_calendar(
    settings: Settings, client: FredClient, series_ids: list[str]
) -> None:
    calendar = read_fred_release_calendar(settings)
    release_dates: dict[int, str] = {}
    cached_ids = read_fred_release_ids(settings)
    ids_dirty = False
    fetched_any = False
    for series_id in series_ids:
        if series_id not in NEXT_RELEASE_SERIES_IDS:
            continue
        try:
            release_id = cached_ids.get(series_id)
            if release_id is None:
                release_id = client.fetch_series_release_id(series_id)
                if release_id is None:
                    continue
                cached_ids[series_id] = release_id
                ids_dirty = True
            if release_id not in release_dates:
                next_date = client.fetch_next_release_date(release_id)
                if next_date is None:
                    continue
                release_dates[release_id] = next_date
            calendar[series_id] = release_dates[release_id]
            fetched_any = True
        except Exception as e:
            logger.warning("Failed to fetch next release date for %s: %s", series_id, e)

    if ids_dirty:
        write_fred_release_ids(settings, cached_ids)

    if fetched_any:
        write_fred_release_calendar(settings, calendar)
        logger.info(
            "Updated FRED release calendar for %s series",
            len(calendar),
        )
    else:
        logger.warning("No FRED next-release dates fetched; leaving calendar unchanged")


def align_observation_start(observation_start: str, frequency: str) -> str:
    dt = datetime.strptime(observation_start, "%Y-%m-%d")
    if frequency == "m":
        aligned = dt.replace(day=1)
    elif frequency == "q":
        quarter_month = ((dt.month - 1) // 3) * 3 + 1
        aligned = dt.replace(month=quarter_month, day=1)
    else:
        return observation_start
    return aligned.strftime("%Y-%m-%d")


class FredDataService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.config = FredConfig()

    def update_series(
        self, series_config: FredSeriesConfig, client: FredClient, repo: FredRepository
    ) -> tuple[int, int, int, int]:
        series_id = series_config.series_id
        is_monthly = series_config.frequency == "m"

        logger.info(f"Updating {series_id} ({series_config.description})")

        latest = repo.get_latest_observation(series_id)
        if latest:
            logger.info(f"Latest {series_id}: {latest[0]} = {latest[1]}")

        observation_start = align_observation_start(
            get_date_years_ago(self.config.HISTORY_YEARS),
            series_config.frequency,
        )
        if series_config.frequency in {"d", "w"} and latest:
            latest_date = latest[0]
            if latest_date > observation_start:
                observation_start = latest_date
                logger.info(
                    "%s: Incremental fetch from latest stored date %s",
                    series_id,
                    observation_start,
                )
            else:
                logger.info(
                    f"{series_id}: Using {self.config.HISTORY_YEARS}-year lookback from {observation_start}"
                )
        else:
            logger.info(
                f"{series_id}: Using {self.config.HISTORY_YEARS}-year lookback from {observation_start}"
            )

        observations = client.fetch_series_observations(
            series_id,
            observation_start=observation_start,
            units=series_config.fred_units,
        )

        if observations is None:
            logger.error(f"Failed to fetch {series_id}")
            return 0, 0, 0, 0

        if not observations:
            logger.info(f"No observations returned for {series_id}")
            return 0, 0, 0, 0

        fetched = len(observations)

        if is_monthly:
            existing_data = repo.get_observations_since(series_id, observation_start)

            inserted, unchanged, skipped = repo.upsert_only_changed(
                series_id=series_id,
                observations=observations,
                frequency=series_config.frequency,
                units=series_config.units,
                existing_data=existing_data,
            )

            logger.info(
                f"{series_id}: fetched={fetched}, inserted={inserted}, "
                f"unchanged={unchanged}, skipped={skipped}"
            )
            return fetched, inserted, unchanged, skipped
        else:
            existing_data = repo.get_observations_since(series_id, observation_start)

            inserted, unchanged, skipped = repo.upsert_only_changed(
                series_id=series_id,
                observations=observations,
                frequency=series_config.frequency,
                units=series_config.units,
                existing_data=existing_data,
            )

            logger.info(
                f"{series_id}: fetched={fetched}, inserted={inserted}, "
                f"unchanged={unchanged}, skipped={skipped}"
            )
            return fetched, inserted, unchanged, skipped


def run(
    settings: Settings,
    series: list[FredSeriesConfig] | None = None,
    force_full: bool = False,
) -> bool:
    log_script_start(logger, "FRED Data Update")
    start_time = datetime.now()

    write_fred_update_state(settings, updated_series_ids=[], processed_series_ids=[])

    is_valid, missing = settings.validate("fred")
    if not is_valid:
        logger.warning("FRED_API_KEY not configured - skipping (non-critical)")
        log_script_end(logger, "FRED Data Update", start_time, True)
        return True

    try:
        session = SessionManager.get_session()

        assert settings.fred_api_key is not None
        client = FredClient(settings.fred_api_key, session=session)
        service = FredDataService(settings)

        conn = connect(settings.db_path)
        repo = FredRepository(conn)

        target_series = series or DEFAULT_FRED_SERIES
        processed_series_ids = [
            series_config.series_id for series_config in target_series
        ]

        total_fetched = 0
        total_inserted = 0
        total_unchanged = 0
        total_skipped = 0
        failed_series = []
        updated_series_ids = []

        try:
            if force_full:
                for series_config in target_series:
                    deleted = repo.delete_series(series_config.series_id)
                    logger.info(
                        "%s: rebuild deleted %s stored observations",
                        series_config.series_id,
                        deleted,
                    )

            for series_config in target_series:
                try:
                    fetched, inserted, unchanged, skipped = service.update_series(
                        series_config, client, repo
                    )
                    total_fetched += fetched
                    total_inserted += inserted
                    total_unchanged += unchanged
                    total_skipped += skipped

                    if inserted > 0:
                        updated_series_ids.append(series_config.series_id)

                    if fetched == 0:
                        failed_series.append(series_config.series_id)

                except Exception as e:
                    logger.error(f"Failed to update {series_config.series_id}: {e}")
                    failed_series.append(series_config.series_id)

        finally:
            conn.close()

        logger.info("FRED update summary:")
        logger.info(f"   Series processed: {len(target_series)}")
        logger.info(f"   Total fetched: {total_fetched}")
        logger.info(f"   Total inserted: {total_inserted}")
        logger.info(f"   Total unchanged: {total_unchanged}")
        logger.info(f"   Total skipped: {total_skipped}")

        if failed_series:
            logger.warning(f"   Failed: {', '.join(failed_series)}")

        total = len(target_series)
        successful = total - len(failed_series)
        success_rate = (successful / total * 100) if total > 0 else 0
        workflow_success = successful > 0 and success_rate >= 80
        logger.info(f"   Series succeeded: {successful}/{total} ({success_rate:.1f}%)")

        write_fred_update_state(settings, updated_series_ids, processed_series_ids)
        try:
            refresh_fred_release_calendar(settings, client, processed_series_ids)
        except Exception as e:
            logger.warning("Failed to refresh FRED release calendar: %s", e)

        log_script_end(logger, "FRED Data Update", start_time, workflow_success)
        return workflow_success

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        write_fred_update_state(
            settings, updated_series_ids=[], processed_series_ids=[]
        )
        log_script_end(logger, "FRED Data Update", start_time, False)
        return False
