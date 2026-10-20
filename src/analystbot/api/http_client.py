import random
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from analystbot.core.log_config import setup_logger

logger = setup_logger("api.http_client")

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "access_token",
        "token",
        "refresh_token",
        "client_secret",
        "client_id",
    }
)
_TELEGRAM_BOT_PATH_RE = re.compile(r"(bot)[^/]+", re.IGNORECASE)


def redact_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        query_pairs = [
            (key, "REDACTED" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        redacted_path = _TELEGRAM_BOT_PATH_RE.sub(r"\1REDACTED", parsed.path)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                redacted_path,
                parsed.params,
                urlencode(query_pairs),
                parsed.fragment,
            )
        )
    except Exception:
        return "<unparseable-url>"


def format_request_error(exc: BaseException, url: str | None = None) -> str:
    exc_type = type(exc).__name__
    if url:
        return f"{exc_type} for {redact_url(url)}"
    return exc_type


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.1
    timeout: tuple[int, int] = (10, 30)
    rate_limit_delay: float = 0.5
    default_headers: dict[str, str] = field(
        default_factory=lambda: {
            "User-Agent": "analystbot/5.3",
            "Accept": "application/json",
        }
    )


def calculate_backoff_delay(
    attempt: int, base_delay: float = 1.0, max_delay: float = 60.0, jitter: float = 0.1
) -> float:
    delay = min(base_delay * (2**attempt), max_delay)
    delay += random.uniform(0, delay * jitter)
    return float(delay)


def create_session(headers: dict[str, str] | None = None) -> "requests.Session":
    session = requests.Session()

    default_headers = RetryPolicy().default_headers.copy()
    if headers:
        default_headers.update(headers)

    session.headers.update(default_headers)
    logger.debug("Created HTTP session with connection pooling")

    return session


class SessionManager:
    _session: Optional["requests.Session"] = None

    @classmethod
    def get_session(cls) -> "requests.Session":
        if cls._session is None:
            cls._session = create_session()
            logger.info("Created shared HTTP session with connection pooling")
        return cls._session

    @classmethod
    def close_session(cls) -> None:
        if cls._session is not None:
            cls._session.close()
            cls._session = None
            logger.debug("Closed shared HTTP session")


class HttpClient:
    def __init__(
        self,
        policy: RetryPolicy | None = None,
        session: Optional["requests.Session"] = None,
    ):
        self.policy = policy or RetryPolicy()
        self._session = session
        self._last_request_time: float | None = None

    @property
    def session(self) -> "requests.Session":
        if self._session is not None:
            return self._session
        return SessionManager.get_session()

    def _apply_rate_limit(self) -> None:
        if self._last_request_time is not None:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.policy.rate_limit_delay:
                sleep_time = self.policy.rate_limit_delay - elapsed
                time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _retry_after_seconds(
        self, response: Optional["requests.Response"]
    ) -> float | None:
        if response is None:
            return None
        values: list[float] = []
        header = response.headers.get("Retry-After")
        if header:
            with suppress(ValueError):
                values.append(float(header))
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            params = payload.get("parameters")
            if isinstance(params, dict) and params.get("retry_after") is not None:
                with suppress(TypeError, ValueError):
                    values.append(float(params["retry_after"]))
        if not values:
            return None
        return max(values)

    def _handle_retry(
        self, attempt: int, response: Optional["requests.Response"] = None
    ) -> float:
        if response is not None and response.status_code in (429, 503):
            retry_after = self._retry_after_seconds(response)
            if retry_after is not None:
                return max(retry_after, self.policy.rate_limit_delay)
            return self.policy.rate_limit_delay

        return calculate_backoff_delay(
            attempt, self.policy.base_delay, self.policy.max_delay, self.policy.jitter
        )

    def get(
        self, url: str, params: dict | None = None, **kwargs
    ) -> Optional["requests.Response"]:
        kwargs.setdefault("timeout", self.policy.timeout)

        for attempt in range(self.policy.max_retries):
            try:
                self._apply_rate_limit()
                response = self.session.get(url, params=params, **kwargs)

                if response.ok:
                    return response

                if response.status_code == 429:
                    delay = self._handle_retry(attempt, response)
                    logger.warning(
                        f"Rate limited (429), waiting {delay:.1f}s "
                        f"(attempt {attempt + 1}/{self.policy.max_retries})"
                    )
                    time.sleep(delay)
                    continue

                logger.warning(
                    f"HTTP {response.status_code} "
                    f"(attempt {attempt + 1}/{self.policy.max_retries})"
                )

                if attempt < self.policy.max_retries - 1:
                    delay = self._handle_retry(attempt)
                    time.sleep(delay)
                    continue

                return response

            except requests.Timeout:
                logger.warning(
                    f"Request timeout for {redact_url(url)} "
                    f"(attempt {attempt + 1}/{self.policy.max_retries})"
                )
                if attempt < self.policy.max_retries - 1:
                    delay = self._handle_retry(attempt)
                    time.sleep(delay)
                    continue
                return None

            except requests.RequestException as e:
                logger.warning(
                    f"Request failed: {format_request_error(e, url)} "
                    f"(attempt {attempt + 1}/{self.policy.max_retries})"
                )
                if attempt < self.policy.max_retries - 1:
                    delay = self._handle_retry(attempt)
                    time.sleep(delay)
                    continue
                return None

        return None

    def post(
        self,
        url: str,
        data: dict | None = None,
        json: dict | None = None,
        **kwargs,
    ) -> Optional["requests.Response"]:
        kwargs.setdefault("timeout", self.policy.timeout)

        for attempt in range(self.policy.max_retries):
            try:
                self._apply_rate_limit()
                response = self.session.post(url, data=data, json=json, **kwargs)

                if response.ok:
                    return response

                if response.status_code == 429:
                    delay = self._handle_retry(attempt, response)
                    logger.warning(f"Rate limited, waiting {delay:.1f}s")
                    time.sleep(delay)
                    continue

                if response.status_code in (400, 401, 403, 404):
                    return response

                if attempt < self.policy.max_retries - 1:
                    delay = self._handle_retry(attempt)
                    time.sleep(delay)
                    continue

                return response

            except requests.Timeout:
                logger.warning(
                    f"POST timeout for {redact_url(url)} (attempt {attempt + 1}); "
                    "not retrying in case the server already processed the request"
                )
                return None

            except requests.RequestException as e:
                logger.warning(f"POST failed: {format_request_error(e, url)}")
                if attempt < self.policy.max_retries - 1:
                    delay = self._handle_retry(attempt)
                    time.sleep(delay)
                    continue
                return None

        return None
