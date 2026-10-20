from __future__ import annotations

import asyncio
import inspect
import math
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from httpx2 import Timeout
from tastytrade import DXLinkStreamer, Session
from tastytrade.dxfeed import Candle
from tastytrade.metrics import get_market_metrics

from analystbot.api.http_client import calculate_backoff_delay
from analystbot.core.config import Settings
from analystbot.core.log_config import setup_logger
from analystbot.core.validation import validate_symbol
from analystbot.db.date_utils import date_str_to_unix, normalize_daily_unix_timestamp

logger = setup_logger("api.tastytrade")


class _WorkerAbandonedError(TimeoutError):
    pass


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _await_deadline[T](awaitable: Awaitable[T], timeout: float, what: str) -> T:
    task = asyncio.ensure_future(awaitable)
    task.add_done_callback(_consume_task_exception)
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    logger.warning("%s timed out after %.1fs", what, timeout)
    raise TimeoutError(f"{what} timed out after {timeout:.1f}s")


def _once(fn: Callable[[], None]) -> Callable[[], None]:
    lock = threading.Lock()
    called = False

    def wrapper() -> None:
        nonlocal called
        with lock:
            if called:
                return
            called = True
        fn()

    return wrapper


def _poll_event(streamer: Any) -> Any | None:
    nowait = getattr(streamer, "get_event_nowait", None)
    return nowait(Candle) if callable(nowait) else None


def _run_event_loop[T](
    coro: Coroutine[Any, Any, T], *, timeout: float, shutdown_timeout: float
) -> T:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main: asyncio.Task[T] = loop.create_task(coro)
    try:
        done, _pending = loop.run_until_complete(asyncio.wait({main}, timeout=timeout))
        if main in done:
            return main.result()
        main.cancel()
        loop.run_until_complete(asyncio.wait({main}, timeout=shutdown_timeout))
        raise TimeoutError(f"DXLink fetch timed out after {timeout:.1f}s")
    finally:
        try:
            _shutdown_loop(loop, shutdown_timeout)
        except Exception:
            logger.exception("DXLink event-loop shutdown failed")
        finally:
            try:
                loop.close()
            except Exception:
                logger.exception("DXLink event loop close failed")
            finally:
                asyncio.set_event_loop(None)


def _shutdown_loop(loop: asyncio.AbstractEventLoop, shutdown_timeout: float) -> None:
    leftover = {task for task in asyncio.all_tasks(loop) if not task.done()}
    for task in leftover:
        task.cancel()
    if leftover:
        _done, stuck = loop.run_until_complete(
            asyncio.wait(leftover, timeout=shutdown_timeout)
        )
        if stuck:
            logger.warning(
                "Abandoning %d DXLink task(s) that ignored cancellation", len(stuck)
            )
    if any(not task.done() for task in asyncio.all_tasks(loop)):
        return
    for what, make_finalizer in (
        ("async generator shutdown", loop.shutdown_asyncgens),
        ("default executor shutdown", loop.shutdown_default_executor),
    ):
        task = loop.create_task(make_finalizer())
        loop.run_until_complete(asyncio.wait({task}, timeout=shutdown_timeout))
        if not task.done():
            task.cancel()
            loop.run_until_complete(asyncio.wait({task}, timeout=shutdown_timeout))
            logger.warning("Abandoned DXLink %s after %.1fs", what, shutdown_timeout)


def _run_async[T](
    coro: Coroutine[Any, Any, T],
    *,
    timeout: float,
    shutdown_timeout: float,
    wall_clock_margin: float = 0.0,
) -> T:
    values: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            values.append(
                _run_event_loop(
                    coro,
                    timeout=timeout,
                    shutdown_timeout=shutdown_timeout,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(
        target=target,
        name="analystbot-dxlink",
        daemon=True,
    )
    worker.start()
    wall_timeout = timeout + (6 * shutdown_timeout) + wall_clock_margin
    worker.join(wall_timeout)
    if worker.is_alive():
        logger.error(
            "DXLink fetch exceeded %.1fs wall-clock limit; abandoning worker "
            "thread (its streamer slot stays claimed until it finishes)",
            wall_timeout,
        )
        raise _WorkerAbandonedError(
            f"DXLink fetch exceeded {wall_timeout:.1f}s wall-clock limit"
        )
    if errors:
        raise errors[0]
    if not values:
        raise RuntimeError("DXLink fetch exited without a result")
    return values[0]


class ValidationResult:
    def __init__(self, is_valid: bool, errors: list[str]):
        self.is_valid = is_valid
        self.errors = errors

    @staticmethod
    def valid() -> ValidationResult:
        return ValidationResult(is_valid=True, errors=[])

    @staticmethod
    def invalid(errors: list[str]) -> ValidationResult:
        return ValidationResult(is_valid=False, errors=errors)


@dataclass(frozen=True)
class BarRequest:
    symbol: str
    from_date: str
    to_date: str


@dataclass
class TastytradeConfig:
    CANDLE_BATCH_SIZE: int = 100
    REST_MIN_INTERVAL_SECONDS: float = 0.1
    MAX_REST_RETRIES: int = 3
    INTER_BATCH_SETTLE_SECONDS: float = 1.0
    MAX_CONCURRENT_STREAMERS: int = 1
    CANDLE_INTERVAL: str = "1d"
    CANDLE_IDLE_TIMEOUT_SECONDS: float = 8.0
    REST_CONNECT_TIMEOUT_SECONDS: float = 10.0
    REST_READ_TIMEOUT_SECONDS: float = 30.0
    REST_ATTEMPT_TIMEOUT_SECONDS: float = 40.0
    STREAMER_HANDSHAKE_TIMEOUT_SECONDS: float = 30.0
    CANDLE_SUBSCRIBE_TIMEOUT_SECONDS: float = 20.0
    STREAMER_CLOSE_TIMEOUT_SECONDS: float = 10.0
    LOOP_SHUTDOWN_TIMEOUT_SECONDS: float = 2.0
    FETCH_DEADLINE_SECONDS: float = 180.0
    WALL_CLOCK_MARGIN_SECONDS: float = 5.0
    DEFAULT_USER_AGENT: str = "analystbot/5.3"
    EXTENDED_TRADING_HOURS: bool = True

    MIN_PRICE: float = 0.001
    MAX_PRICE: float = 1_000_000.0
    MAX_DAILY_RANGE_RATIO: float = 0.5


class _StreamerFactory(Protocol):
    def __call__(self, session: Any) -> Any: ...


class _MetricsFetcher(Protocol):
    def __call__(self, session: Any, symbols: Sequence[str]) -> Any: ...


def to_tastytrade_symbol(symbol: str) -> str:
    return symbol.upper().replace(".", "/")


def from_tastytrade_event_symbol(event_symbol: str) -> str:
    base = event_symbol.split("{", 1)[0].strip()
    return base.replace("/", ".")


def _is_trading_hours_only_event(event_symbol: str) -> bool:
    if "{" not in event_symbol:
        return False
    suffix = event_symbol.split("{", 1)[1].lower().replace(" ", "")
    return "tho=true" in suffix


def split_candle_batches(symbols: Sequence[str], batch_size: int) -> list[list[str]]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [
        list(symbols[i : i + batch_size]) for i in range(0, len(symbols), batch_size)
    ]


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _candle_unix_seconds(raw_time: Any) -> int | None:
    if raw_time is None:
        return None
    try:
        timestamp = int(raw_time)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    if timestamp <= 0:
        return None
    return timestamp


def _http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None


_AUTH_MESSAGE_MARKERS = (
    "invalid_grant",
    "grant revoked",
    "invalid_client",
    "invalid_token",
    "invalid_credentials",
    "unauthorized",
)


def _is_auth_failure(exc: BaseException) -> bool:
    if _http_status(exc) in {401, 403}:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_MESSAGE_MARKERS)


def _is_quote_token_handshake_failure(exc: BaseException) -> bool:
    text = str(exc).strip()
    if "missing the `read` scope" in text:
        return False
    if "insufficient scope" in text.lower():
        return True
    if _http_status(exc) == 403:
        return True
    return type(exc).__name__ == "TastytradeError" and not text


def _quote_token_failure_from_exc(exc: BaseException) -> RuntimeError:
    status = _http_status(exc) or 403
    text = str(exc).strip()
    payload: dict[str, Any] = {"error": {"message": text}} if text else {}
    return _quote_token_failure(status, payload)


def _quote_token_failure(status_code: int, payload: Any) -> RuntimeError:
    error = payload.get("error") if isinstance(payload, dict) else None
    message = ""
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("code") or "").strip()
    elif isinstance(error, str):
        message = error.strip()
    if status_code == 403 or "insufficient scope" in message.lower():
        return RuntimeError(
            "tastytrade OAuth grant is missing the `read` scope required for "
            "DXLink candles (GET /api-quote-tokens). Recreate the grant with "
            "`read` (openid alone is not enough), then update "
            "TASTYTRADE_REFRESH_TOKEN. "
            f"Provider error: {message or status_code}"
        )
    return RuntimeError(
        f"tastytrade quote-token request failed ({status_code}): {message or payload}"
    )


def _oauth_failure(exc: BaseException, *, is_test: bool) -> RuntimeError:
    api = "sandbox" if is_test else "production"
    return RuntimeError(
        f"tastytrade OAuth failed against the {api} API "
        "(credentials rejected, grant revoked, or environment mismatch). "
        "Sandbox and production grants are not interchangeable. "
        "Create a new OAuth grant at my.tastytrade.com if needed and update "
        "TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN. "
        f"Provider error: {exc}"
    )


def _is_rate_limited(exc: BaseException) -> bool:
    return _http_status(exc) == 429


_TRANSIENT_EXC_NAMES = frozenset(
    {
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ConnectError",
        "NetworkError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "LocalProtocolError",
        "ProxyError",
        "TransportError",
    }
)


def _is_transient_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError, socket.gaierror)):
            return True
        if type(current).__name__ in _TRANSIENT_EXC_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def _rest_timeout(config: TastytradeConfig) -> Timeout:
    return Timeout(
        connect=config.REST_CONNECT_TIMEOUT_SECONDS,
        read=config.REST_READ_TIMEOUT_SECONDS,
        write=config.REST_READ_TIMEOUT_SECONDS,
        pool=config.REST_CONNECT_TIMEOUT_SECONDS,
    )


def _is_sandbox_env(env: str | None) -> bool:
    raw = (env or "production").strip().lower()
    return raw in {"sandbox", "cert", "test"}


def _candle_symbol(ticker: str, interval: str, extended_hours: bool) -> str:
    if extended_hours:
        return f"{ticker}{{={interval}}}"
    return f"{ticker}{{={interval},tho=true}}"


class TastytradeClient:
    def __init__(
        self,
        client_secret: str,
        refresh_token: str,
        *,
        environment: str | None = "production",
        user_agent: str | None = None,
        config: TastytradeConfig | None = None,
        session_factory: Callable[..., Any] | None = None,
        streamer_factory: _StreamerFactory | None = None,
        metrics_fetcher: _MetricsFetcher | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if not client_secret:
            raise ValueError("tastytrade client secret is required")
        if not refresh_token:
            raise ValueError("tastytrade refresh token is required")

        self.client_secret = client_secret.strip()
        self.refresh_token = refresh_token.strip()
        self.is_test = _is_sandbox_env(environment)
        self.user_agent = user_agent or TastytradeConfig.DEFAULT_USER_AGENT
        self.config = config or TastytradeConfig()
        self._session_factory = session_factory or self._default_session_factory
        self._streamer_factory = streamer_factory or self._default_streamer_factory
        self._metrics_fetcher = metrics_fetcher
        self._sleep = sleeper
        self._last_rest_time: float | None = None
        self._streamer_lock = threading.Lock()
        self._active_streamers = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        config: TastytradeConfig | None = None,
    ) -> TastytradeClient:
        if (
            not settings.tastytrade_client_secret
            or not settings.tastytrade_refresh_token
        ):
            raise ValueError("tastytrade OAuth credentials are required")
        return cls(
            settings.tastytrade_client_secret,
            settings.tastytrade_refresh_token,
            environment=settings.tastytrade_env,
            user_agent=settings.tastytrade_user_agent,
            config=config,
        )

    def _default_session_factory(self) -> Any:
        logger.info(
            "Creating tastytrade session (%s)",
            "sandbox" if self.is_test else "production",
        )
        session = Session(
            self.client_secret,
            self.refresh_token,
            is_test=self.is_test,
            timeout=_rest_timeout(self.config),
        )
        client = getattr(session, "_client", None)
        if client is not None:
            client.headers["User-Agent"] = self.user_agent
        logger.info("tastytrade HTTP client created")
        return session

    def _default_streamer_factory(self, session: Any) -> Any:
        return DXLinkStreamer(session)

    def _pace_rest(self) -> None:
        interval = self.config.REST_MIN_INTERVAL_SECONDS
        if self._last_rest_time is not None and interval > 0:
            elapsed = time.monotonic() - self._last_rest_time
            remaining = interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_rest_time = time.monotonic()

    def _claim_streamer(self) -> None:
        with self._streamer_lock:
            if self._active_streamers >= self.config.MAX_CONCURRENT_STREAMERS:
                raise RuntimeError(
                    "Only one DXLink streamer is allowed per tastytrade client "
                    "(a previous fetch may still be running or was abandoned)"
                )
            self._active_streamers += 1

    def _release_streamer(self) -> None:
        with self._streamer_lock:
            if self._active_streamers > 0:
                self._active_streamers -= 1

    def _retry_or_raise(self, exc: BaseException, attempt: int, attempts: int) -> None:
        if _is_auth_failure(exc):
            logger.error("tastytrade OAuth rejected; not retrying auth failure")
            raise exc
        retryable = _is_rate_limited(exc) or _is_transient_failure(exc)
        if retryable and attempt < attempts - 1:
            delay = calculate_backoff_delay(attempt)
            reason = (
                "rate limited (429)" if _is_rate_limited(exc) else type(exc).__name__
            )
            logger.warning(
                "tastytrade REST failed (%s), waiting %.1fs (attempt %s/%s)",
                reason,
                delay,
                attempt + 1,
                attempts,
            )
            self._sleep(delay)
            return
        raise exc

    def _create_session(self) -> Any:
        last_error: BaseException | None = None
        attempts = max(1, self.config.MAX_REST_RETRIES)
        for attempt in range(attempts):
            self._pace_rest()
            try:
                return self._session_factory()
            except Exception as exc:
                last_error = exc
                self._retry_or_raise(exc, attempt, attempts)
        assert last_error is not None
        raise last_error

    async def _refresh_session(self, session: Any) -> None:
        refresh = getattr(session, "refresh", None)
        if refresh is None:
            return
        attempts = max(1, self.config.MAX_REST_RETRIES)
        timeout = self.config.REST_ATTEMPT_TIMEOUT_SECONDS
        for attempt in range(attempts):
            self._pace_rest()
            try:
                logger.info("Refreshing tastytrade OAuth token")
                result = refresh()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=timeout)
                logger.info("tastytrade OAuth token refreshed")
                return
            except Exception as exc:
                self._retry_or_raise(exc, attempt, attempts)

    def validate_bar(self, bar: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        o_raw = bar.get("o", bar.get("open"))
        h_raw = bar.get("h", bar.get("high"))
        l_raw = bar.get("l", bar.get("low"))
        c_raw = bar.get("c", bar.get("close"))

        if o_raw is None or h_raw is None or l_raw is None or c_raw is None:
            return ValidationResult.invalid(["Missing OHLC values"])

        o = float(o_raw)
        h = float(h_raw)
        low = float(l_raw)
        c = float(c_raw)

        for name, price in [("open", o), ("high", h), ("low", low), ("close", c)]:
            if price < self.config.MIN_PRICE or price > self.config.MAX_PRICE:
                errors.append(f"{name} price {price} outside bounds")

        if h < max(o, c, low):
            errors.append(f"High {h} < max(O,C,L)")
        if low > min(o, c, h):
            errors.append(f"Low {low} > min(O,C,H)")

        if c > 0 and (h - low) / c > self.config.MAX_DAILY_RANGE_RATIO:
            errors.append(f"Daily range {(h - low) / c:.2%} exceeds threshold")

        if errors:
            return ValidationResult.invalid(errors)
        return ValidationResult.valid()

    def candle_to_bar(self, candle: Any) -> dict[str, Any] | None:
        event_symbol = getattr(candle, "event_symbol", None) or getattr(
            candle, "eventSymbol", None
        )
        if not event_symbol:
            return None

        symbol = from_tastytrade_event_symbol(str(event_symbol))
        if not validate_symbol(symbol):
            logger.debug("Skipping candle with unmapped symbol %s", event_symbol)
            return None

        unix_ts = _candle_unix_seconds(getattr(candle, "time", None))
        if unix_ts is None:
            return None

        open_px = _finite_or_none(getattr(candle, "open", None))
        high_px = _finite_or_none(getattr(candle, "high", None))
        low_px = _finite_or_none(getattr(candle, "low", None))
        close_px = _finite_or_none(getattr(candle, "close", None))
        volume = _finite_or_none(getattr(candle, "volume", None))
        vwap = _finite_or_none(getattr(candle, "vwap", None))

        mapped = {
            "symbol": symbol.upper(),
            "date": normalize_daily_unix_timestamp(unix_ts),
            "open": open_px,
            "high": high_px,
            "low": low_px,
            "close": close_px,
            "volume": volume,
            "vwap": vwap,
        }
        validation = self.validate_bar(mapped)
        if not validation.is_valid:
            logger.debug(
                "Invalid candle for %s on %s: %s",
                symbol,
                mapped["date"],
                ", ".join(validation.errors),
            )
            return None
        return mapped

    def get_market_caps(self, symbols: Sequence[str]) -> dict[str, float]:
        wanted: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            upper = symbol.upper()
            if upper not in seen:
                wanted.append(upper)
                seen.add(upper)
        if not wanted:
            return {}
        return asyncio.run(self._get_market_caps_async(wanted))

    async def _get_market_caps_async(self, symbols: list[str]) -> dict[str, float]:
        try:
            session = self._create_session()
        except Exception as exc:
            logger.exception("Failed to create tastytrade session")
            if _is_auth_failure(exc):
                raise _oauth_failure(exc, is_test=self.is_test) from exc
            raise

        wanted = set(symbols)
        stored_by_tasty = {to_tastytrade_symbol(symbol): symbol for symbol in symbols}
        tasty_symbols = [to_tastytrade_symbol(symbol) for symbol in symbols]
        caps: dict[str, float] = {}

        try:
            async with self._open_session(session):
                await self._refresh_session(session)
                batches = split_candle_batches(
                    tasty_symbols, self.config.CANDLE_BATCH_SIZE
                )
                for batch in batches:
                    rows = await self._fetch_market_metrics_batch(session, batch)
                    for row in rows:
                        stored = self._stored_symbol_for_metric(
                            getattr(row, "symbol", None), stored_by_tasty
                        )
                        if stored is None or stored not in wanted:
                            continue
                        cap = _finite_or_none(getattr(row, "market_cap", None))
                        if cap is None or cap <= 0:
                            logger.warning(
                                "Skipping %s market cap %s",
                                stored,
                                getattr(row, "market_cap", None),
                            )
                            continue
                        caps[stored] = cap
        except Exception as exc:
            logger.exception("tastytrade market-metrics fetch failed")
            if _is_auth_failure(exc):
                raise _oauth_failure(exc, is_test=self.is_test) from exc
            raise
        return caps

    def _stored_symbol_for_metric(
        self, raw_symbol: Any, stored_by_tasty: dict[str, str]
    ) -> str | None:
        if raw_symbol is None:
            return None
        raw = str(raw_symbol).strip()
        if not raw:
            return None
        if raw in stored_by_tasty:
            return stored_by_tasty[raw]
        mapped = from_tastytrade_event_symbol(raw)
        return mapped or None

    def _default_metrics_fetcher(self, session: Any, symbols: Sequence[str]) -> Any:
        return get_market_metrics(session, symbols)

    async def _fetch_market_metrics_batch(
        self, session: Any, tasty_symbols: Sequence[str]
    ) -> list[Any]:
        fetcher = self._metrics_fetcher or self._default_metrics_fetcher
        attempts = max(1, self.config.MAX_REST_RETRIES)
        timeout = self.config.REST_ATTEMPT_TIMEOUT_SECONDS
        last_error: BaseException | None = None
        for attempt in range(attempts):
            self._pace_rest()
            try:
                result = fetcher(session, tasty_symbols)
                if inspect.isawaitable(result):
                    rows = await asyncio.wait_for(result, timeout=timeout)
                else:
                    rows = result
                return list(rows)
            except Exception as exc:
                last_error = exc
                self._retry_or_raise(exc, attempt, attempts)
        assert last_error is not None
        raise last_error

    def get_daily_bars_range(
        self, symbol: str, from_date: str, to_date: str
    ) -> list[dict[str, Any]] | None:
        return self.get_daily_bars_batch(
            [BarRequest(symbol=symbol, from_date=from_date, to_date=to_date)]
        ).get(symbol)

    def get_daily_bars_batch(
        self, requests: Sequence[BarRequest]
    ) -> dict[str, list[dict[str, Any]] | None]:
        if not requests:
            return {}
        self._claim_streamer()
        release = _once(self._release_streamer)
        try:
            return _run_async(
                self._get_daily_bars_batch_async(list(requests), release),
                timeout=self.config.FETCH_DEADLINE_SECONDS,
                shutdown_timeout=self.config.LOOP_SHUTDOWN_TIMEOUT_SECONDS,
                wall_clock_margin=self.config.WALL_CLOCK_MARGIN_SECONDS,
            )
        except _WorkerAbandonedError:
            raise
        except Exception:
            release()
            raise

    async def _get_daily_bars_batch_async(
        self, requests: list[BarRequest], release: Callable[[], None]
    ) -> dict[str, list[dict[str, Any]] | None]:
        try:
            results: dict[str, list[dict[str, Any]] | None] = {
                req.symbol.upper(): None for req in requests
            }
            try:
                session = self._create_session()
            except Exception as exc:
                logger.exception("Failed to create tastytrade session")
                if _is_auth_failure(exc):
                    raise _oauth_failure(exc, is_test=self.is_test) from exc
                raise

            try:
                async with self._open_session(session):
                    await self._refresh_session(session)
                    await self._run_with_streamer(session, requests, results)
            except Exception as exc:
                logger.exception("tastytrade DXLink fetch failed")
                if "missing the `read` scope" in str(exc):
                    raise
                if _is_auth_failure(exc):
                    raise _oauth_failure(exc, is_test=self.is_test) from exc
                raise
            return results
        finally:
            release()

    @asynccontextmanager
    async def _open_session(self, session: Any) -> AsyncIterator[Any]:
        if hasattr(session, "__aenter__"):
            async with session:
                yield session
        else:
            yield session

    @asynccontextmanager
    async def _open_streamer(self, session: Any) -> AsyncIterator[Any]:
        streamer_cm = self._streamer_factory(session)
        logger.info("Starting tastytrade DXLink handshake")
        handshake_scope = asyncio.timeout(
            self.config.STREAMER_HANDSHAKE_TIMEOUT_SECONDS
        )
        try:
            async with handshake_scope:
                streamer = await streamer_cm.__aenter__()
        except TimeoutError:
            if not handshake_scope.expired():
                raise
            logger.warning(
                "tastytrade DXLink handshake timed out after %.1fs",
                self.config.STREAMER_HANDSHAKE_TIMEOUT_SECONDS,
            )
            raise TimeoutError(
                "tastytrade DXLink handshake timed out after "
                f"{self.config.STREAMER_HANDSHAKE_TIMEOUT_SECONDS:.1f}s"
            ) from None
        logger.info("tastytrade DXLink handshake complete")
        try:
            yield streamer
        finally:
            close_scope = asyncio.timeout(self.config.STREAMER_CLOSE_TIMEOUT_SECONDS)
            try:
                async with close_scope:
                    await streamer_cm.__aexit__(*sys.exc_info())
            except Exception as exc:
                if isinstance(exc, TimeoutError) and close_scope.expired():
                    logger.warning(
                        "Abandoning hung DXLink websocket after close timeout"
                    )
                else:
                    logger.warning("DXLink websocket close failed", exc_info=True)

    async def _run_with_streamer(
        self,
        session: Any,
        requests: list[BarRequest],
        results: dict[str, list[dict[str, Any]] | None],
    ) -> None:
        attempts = max(1, self.config.MAX_REST_RETRIES)
        for attempt in range(attempts):
            self._pace_rest()
            try:
                async with self._open_streamer(session) as streamer:
                    batches = split_candle_batches(
                        [req.symbol.upper() for req in requests],
                        self.config.CANDLE_BATCH_SIZE,
                    )
                    request_by_symbol = {req.symbol.upper(): req for req in requests}
                    for batch_index, batch_symbols in enumerate(batches):
                        batch_requests = [request_by_symbol[s] for s in batch_symbols]
                        batch_results = await self._fetch_candle_batch(
                            streamer, batch_requests
                        )
                        results.update(batch_results)
                        if batch_index < len(batches) - 1:
                            await _await_deadline(
                                self._unsubscribe_candle_batch(streamer, batch_symbols),
                                self.config.CANDLE_SUBSCRIBE_TIMEOUT_SECONDS,
                                "tastytrade DXLink candle unsubscribe",
                            )
                            if self.config.INTER_BATCH_SETTLE_SECONDS > 0:
                                self._sleep(self.config.INTER_BATCH_SETTLE_SECONDS)
                    return
            except Exception as exc:
                if _is_quote_token_handshake_failure(exc):
                    raise _quote_token_failure_from_exc(exc) from exc
                self._retry_or_raise(exc, attempt, attempts)

    async def _fetch_candle_batch(
        self, streamer: Any, requests: list[BarRequest]
    ) -> dict[str, list[dict[str, Any]] | None]:
        start_times = [
            datetime.strptime(req.from_date, "%Y-%m-%d").replace(tzinfo=UTC)
            for req in requests
        ]
        earliest_start = min(start_times)
        tickers = [to_tastytrade_symbol(req.symbol) for req in requests]
        logger.info("Subscribing to %s DXLink candle symbols", len(tickers))
        await _await_deadline(
            streamer.subscribe_candle(
                tickers,
                self.config.CANDLE_INTERVAL,
                start_time=earliest_start,
                extended_trading_hours=self.config.EXTENDED_TRADING_HOURS,
            ),
            self.config.CANDLE_SUBSCRIBE_TIMEOUT_SECONDS,
            "tastytrade DXLink candle subscribe",
        )

        wanted = {req.symbol.upper() for req in requests}
        start_ts = {
            req.symbol.upper(): date_str_to_unix(req.from_date) for req in requests
        }
        end_ts = {req.symbol.upper(): date_str_to_unix(req.to_date) for req in requests}
        collected: dict[str, dict[int, dict[str, Any]]] = {s: {} for s in wanted}
        session_rank: dict[str, dict[int, int]] = {s: {} for s in wanted}
        reached_start: set[str] = set()

        while reached_start != wanted:
            try:
                candle = _poll_event(streamer)
                if candle is not None:
                    await asyncio.sleep(0)
                else:
                    candle = await _await_deadline(
                        streamer.get_event(Candle),
                        self.config.CANDLE_IDLE_TIMEOUT_SECONDS,
                        "tastytrade DXLink candle listen",
                    )
            except TimeoutError:
                logger.debug(
                    "Candle idle timeout; received bars for %s/%s symbols",
                    len(reached_start),
                    len(wanted),
                )
                break
            except Exception:
                logger.exception("Error while listening for candles")
                break

            bar = self.candle_to_bar(candle)
            if bar is None:
                continue
            symbol = bar["symbol"]
            if symbol not in wanted:
                continue
            ts = bar["date"]
            if ts > end_ts[symbol]:
                continue
            if ts >= start_ts[symbol]:
                event_symbol = str(
                    getattr(candle, "event_symbol", None)
                    or getattr(candle, "eventSymbol", None)
                    or ""
                )
                rank = 0 if _is_trading_hours_only_event(event_symbol) else 1
                prev_rank = session_rank[symbol].get(ts)
                if prev_rank is None or rank >= prev_rank:
                    collected[symbol][ts] = bar
                    session_rank[symbol][ts] = rank
            if ts <= start_ts[symbol]:
                reached_start.add(symbol)

        return {
            symbol: (
                sorted(collected[symbol].values(), key=lambda item: item["date"])
                if collected[symbol]
                else ([] if symbol in reached_start else None)
            )
            for symbol in wanted
        }

    async def _unsubscribe_candle_batch(
        self, streamer: Any, symbols: Sequence[str]
    ) -> None:
        interval = self.config.CANDLE_INTERVAL
        extended = self.config.EXTENDED_TRADING_HOURS
        remove = [
            {
                "symbol": _candle_symbol(
                    to_tastytrade_symbol(symbol), interval, extended
                ),
                "type": "Candle",
            }
            for symbol in symbols
        ]
        websocket = getattr(streamer, "_websocket", None)
        channels = getattr(streamer, "_channels", None)
        if websocket is None or channels is None:
            unsubscribe = getattr(streamer, "unsubscribe_candle", None)
            if unsubscribe is None:
                return
            for symbol in symbols:
                await unsubscribe(
                    to_tastytrade_symbol(symbol),
                    interval,
                    extended_trading_hours=extended,
                )
            return
        message = {
            "type": "FEED_SUBSCRIPTION",
            "channel": channels["Candle"],
            "remove": remove,
        }
        await websocket.send_json(message)
