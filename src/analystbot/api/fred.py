import calendar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Optional

from analystbot.api.http_client import (
    HttpClient,
    RetryPolicy,
)
from analystbot.core.log_config import setup_logger

if TYPE_CHECKING:
    import requests

logger = setup_logger("api.fred")


@dataclass
class FredConfig:
    BASE_URL: str = "https://api.stlouisfed.org/fred/series/observations"
    SERIES_RELEASE_URL: str = "https://api.stlouisfed.org/fred/series/release"
    RELEASE_DATES_URL: str = "https://api.stlouisfed.org/fred/release/dates"
    RATE_LIMIT_DELAY: float = 0.5
    MAX_RETRIES: int = 3
    HISTORY_YEARS: int = 10
    DEFAULT_HEADERS: dict[str, str] = field(
        default_factory=lambda: {
            "User-Agent": "analystbot-fred/2.0",
            "Accept": "application/json",
        }
    )


@dataclass
class FredSeriesConfig:
    series_id: str
    frequency: str
    units: str
    description: str
    fred_units: str = "lin"


DEFAULT_FRED_SERIES = [
    FredSeriesConfig(
        series_id="GDPC1",
        frequency="q",
        units="Percent Change",
        description="Real Gross Domestic Product",
        fred_units="pch",
    ),
    FredSeriesConfig(
        series_id="PCECC96",
        frequency="q",
        units="Percent Change",
        description="Real Personal Consumption Expenditures",
        fred_units="pch",
    ),
    FredSeriesConfig(
        series_id="CPIAUCSL",
        frequency="m",
        units="Percent Change from Year Ago",
        description="Consumer Price Index for All Urban Consumers",
        fred_units="pc1",
    ),
    FredSeriesConfig(
        series_id="PCEPI",
        frequency="m",
        units="Percent Change from Year Ago",
        description="Personal Consumption Expenditures Chain-type Price Index",
        fred_units="pc1",
    ),
    FredSeriesConfig(
        series_id="PAYEMS",
        frequency="m",
        units="Thousands of Persons",
        description="All Employees, Total Nonfarm",
    ),
    FredSeriesConfig(
        series_id="UNRATE",
        frequency="m",
        units="Percent",
        description="Unemployment Rate",
    ),
    FredSeriesConfig(
        series_id="HOUST",
        frequency="m",
        units="Thousands of Units",
        description="Housing Starts: Total New Privately Owned Housing Units",
    ),
    FredSeriesConfig(
        series_id="DGS10",
        frequency="d",
        units="Percent",
        description="Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
    ),
    FredSeriesConfig(
        series_id="T10Y2Y",
        frequency="d",
        units="Percent",
        description="10-Year Treasury Constant Maturity Minus 2-Year Treasury",
    ),
    FredSeriesConfig(
        series_id="STLFSI4",
        frequency="w",
        units="Index",
        description="St. Louis Fed Financial Stress Index",
    ),
    FredSeriesConfig(
        series_id="VIXCLS",
        frequency="d",
        units="Index",
        description="CBOE Volatility Index: VIX",
    ),
]

FRED_SERIES_LABEL = ", ".join(series.series_id for series in DEFAULT_FRED_SERIES)

NEXT_RELEASE_SERIES_IDS = frozenset(
    {"GDPC1", "PCECC96", "CPIAUCSL", "PCEPI", "UNRATE", "PAYEMS", "HOUST"}
)


def get_date_years_ago(years: int) -> str:
    today = datetime.now(UTC).date()
    target_year = today.year - years
    target_month = today.month
    target_day = today.day

    max_day_in_target_month = calendar.monthrange(target_year, target_month)[1]
    if target_day > max_day_in_target_month:
        target_day = max_day_in_target_month

    target_date = date(target_year, target_month, target_day)
    return target_date.strftime("%Y-%m-%d")


class FredClient:
    def __init__(
        self,
        api_key: str,
        config: FredConfig | None = None,
        session: Optional["requests.Session"] = None,
    ):
        if not api_key:
            raise ValueError("FRED API key is required")

        self.api_key = api_key
        self.config = config or FredConfig()

        policy = RetryPolicy(
            max_retries=self.config.MAX_RETRIES,
            base_delay=1.0,
            max_delay=60.0,
            jitter=0.1,
            timeout=(10, 30),
            rate_limit_delay=self.config.RATE_LIMIT_DELAY,
            default_headers=self.config.DEFAULT_HEADERS,
        )
        self._http = HttpClient(policy=policy, session=session)

        logger.info("FRED API client initialized")

    def fetch_series_observations(
        self,
        series_id: str,
        observation_start: str | None = None,
        units: str = "lin",
    ) -> list[dict] | None:
        if observation_start is None:
            observation_start = get_date_years_ago(self.config.HISTORY_YEARS)

        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "observation_start": observation_start,
            "sort_order": "asc",
            "units": units,
        }

        response = self._http.get(self.config.BASE_URL, params=params)

        if response is None:
            logger.warning(f"Request failed for {series_id}")
            return None

        if not response.ok:
            logger.warning(f"API error for {series_id}: HTTP {response.status_code}")
            return None

        try:
            data = response.json()
        except ValueError:
            logger.warning(f"Invalid JSON response for {series_id}")
            return None

        observations = data.get("observations", [])

        normalized = []
        for obs in observations:
            date_str = obs.get("date")
            value_str = obs.get("value", ".")

            if value_str == ".":
                value = None
            else:
                try:
                    value = float(value_str)
                except (ValueError, TypeError):
                    logger.debug(
                        f"Invalid value for {series_id}/{date_str}: {value_str}"
                    )
                    value = None

            normalized.append({"date": date_str, "value": value})

        logger.info(f"Fetched {len(normalized)} observations for {series_id}")
        return normalized

    def fetch_series_release_id(self, series_id: str) -> int | None:
        data = self._get_json(
            self.config.SERIES_RELEASE_URL,
            {
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
            },
            context=series_id,
        )
        if data is None:
            return None

        releases = data.get("releases") or []
        if not isinstance(releases, list) or not releases:
            logger.warning("No FRED release found for %s", series_id)
            return None

        release_id = releases[0].get("id") if isinstance(releases[0], dict) else None
        if release_id is None:
            logger.warning("Invalid FRED release id for %s: %s", series_id, release_id)
            return None
        try:
            return int(release_id)
        except (TypeError, ValueError):
            logger.warning("Invalid FRED release id for %s: %s", series_id, release_id)
            return None

    def fetch_next_release_date(
        self, release_id: int, *, as_of: date | None = None
    ) -> str | None:
        data = self._get_json(
            self.config.RELEASE_DATES_URL,
            {
                "release_id": str(release_id),
                "api_key": self.api_key,
                "file_type": "json",
                "include_release_dates_with_no_data": "true",
                "sort_order": "desc",
                "limit": "100",
            },
            context=f"release {release_id}",
        )
        if data is None:
            return None

        entries = data.get("release_dates") or []
        if not isinstance(entries, list):
            logger.warning("Unexpected release-dates payload for %s", release_id)
            return None

        today = (as_of or datetime.now(UTC).date()).isoformat()
        next_date: str | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            date_str = entry.get("date")
            if not isinstance(date_str, str) or date_str < today:
                continue
            if next_date is None or date_str < next_date:
                next_date = date_str
        return next_date

    def _get_json(
        self, url: str, params: dict[str, str], *, context: str
    ) -> dict | None:
        response = self._http.get(url, params=params)
        if response is None:
            logger.warning("Request failed for %s", context)
            return None
        if not response.ok:
            logger.warning("API error for %s: HTTP %s", context, response.status_code)
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.warning("Invalid JSON response for %s", context)
            return None
        if not isinstance(payload, dict):
            logger.warning("Unexpected JSON payload for %s", context)
            return None
        return payload
