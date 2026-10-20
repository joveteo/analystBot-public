from __future__ import annotations

import asyncio
import gc
import threading
import time
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from analystbot.api.tastytrade import (
    BarRequest,
    TastytradeClient,
    TastytradeConfig,
    _await_deadline,
    _run_event_loop,
    from_tastytrade_event_symbol,
    split_candle_batches,
    to_tastytrade_symbol,
)
from analystbot.core.config import Settings
from analystbot.db.date_utils import date_str_to_unix


def _ms(date_str: str) -> int:
    return date_str_to_unix(date_str) * 1000


def _candle(
    event_symbol: str,
    date_str: str,
    *,
    open_: float = 100.0,
    high: float = 110.0,
    low: float = 95.0,
    close: float = 105.0,
    volume: float | None = 1_234_567,
    vwap: float | None = 103.5,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_symbol=event_symbol,
        time=_ms(date_str),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap=vwap,
    )


class FakeWebsocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)


class FakeStreamer:
    def __init__(self, candles: list[SimpleNamespace] | None = None) -> None:
        self._candles = list(candles or [])
        self.subscribe_calls: list[tuple] = []
        self._channels = {"Candle": 3}
        self._websocket = FakeWebsocket()
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeStreamer:
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if not self.entered:
            raise RuntimeError(
                f"this {type(self).__qualname__} has not been entered yet"
            )
        self.exited = True
        return False

    async def subscribe_candle(
        self,
        symbols,
        interval,
        start_time=None,
        extended_trading_hours=False,
    ) -> None:
        self.subscribe_calls.append(
            (list(symbols), interval, start_time, extended_trading_hours)
        )

    async def get_event(self, _event_class):
        if not self._candles:
            raise TimeoutError("idle")
        return self._candles.pop(0)


class RateLimitError(Exception):
    status_code = 429


class AuthError(Exception):
    status_code = 401


class GrantRevokedError(Exception):
    pass


GRANT_REVOKED_MESSAGE = (
    "Couldn't parse response: {'error_code': 'invalid_grant', "
    "'error_description': 'Grant revoked'}"
)


class SymbolMapTests(unittest.TestCase):
    def test_brk_b_maps_to_tastytrade_slash(self) -> None:
        self.assertEqual(to_tastytrade_symbol("BRK.B"), "BRK/B")
        self.assertEqual(to_tastytrade_symbol("aapl"), "AAPL")

    def test_event_symbol_maps_back_to_db_ticker(self) -> None:
        self.assertEqual(from_tastytrade_event_symbol("BRK/B{=1d,tho=true}"), "BRK.B")
        self.assertEqual(from_tastytrade_event_symbol("AAPL{=1d}"), "AAPL")
        self.assertEqual(from_tastytrade_event_symbol("PH{=d}"), "PH")


class BatchingTests(unittest.TestCase):
    def test_101_symbols_split_into_two_batches_of_at_most_100(self) -> None:
        symbols = [f"S{i:03d}" for i in range(101)]
        batches = split_candle_batches(symbols, 100)
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0]), 100)
        self.assertEqual(len(batches[1]), 1)
        self.assertTrue(all(len(batch) <= 100 for batch in batches))


class CandleMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TastytradeClient("secret", "refresh")

    def test_volume_and_vwap_are_mapped(self) -> None:
        bar = self.client.candle_to_bar(_candle("AAPL{=1d,tho=true}", "2026-01-02"))
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar["symbol"], "AAPL")
        self.assertEqual(bar["volume"], 1_234_567)
        self.assertEqual(bar["vwap"], 103.5)
        self.assertEqual(bar["open"], 100.0)
        self.assertEqual(bar["date"], date_str_to_unix("2026-01-02"))

    def test_nan_volume_and_vwap_map_to_none(self) -> None:
        bar = self.client.candle_to_bar(
            _candle(
                "AAPL{=1d,tho=true}",
                "2026-01-02",
                volume=float("nan"),
                vwap=float("nan"),
            )
        )
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertIsNone(bar["volume"])
        self.assertIsNone(bar["vwap"])

    def test_brk_b_candle_stores_dotted_symbol(self) -> None:
        bar = self.client.candle_to_bar(_candle("BRK/B{=1d,tho=true}", "2026-01-02"))
        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar["symbol"], "BRK.B")


class AwaitDeadlineTests(unittest.TestCase):
    def test_outer_cancellation_cancels_the_inner_task(self) -> None:
        inner_cancelled = asyncio.Event()

        async def inner() -> None:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                inner_cancelled.set()
                raise

        async def scenario() -> bool:
            outer = asyncio.ensure_future(_await_deadline(inner(), 5.0, "inner"))
            await asyncio.sleep(0.01)
            outer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await outer
            await asyncio.sleep(0.01)
            return inner_cancelled.is_set()

        self.assertTrue(asyncio.run(scenario()))

    def test_abandoned_task_exception_is_retrieved(self) -> None:
        retrieved: list[BaseException | None] = []

        async def inner() -> None:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise ConnectionError("late failure") from None

        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(
                lambda _l, ctx: retrieved.append(ctx.get("exception"))
            )
            with self.assertRaises(TimeoutError):
                await _await_deadline(inner(), 0.01, "inner")
            await asyncio.sleep(0.05)

        asyncio.run(scenario())
        gc.collect()
        self.assertEqual(retrieved, [])


class RunEventLoopTests(unittest.TestCase):
    def test_returns_result_and_closes_the_loop(self) -> None:
        async def work() -> int:
            return 7

        self.assertEqual(_run_event_loop(work(), timeout=1.0, shutdown_timeout=0.1), 7)

    def test_leftover_tasks_that_ignore_cancellation_are_abandoned(self) -> None:
        async def work() -> int:
            async def stubborn() -> None:
                try:
                    await asyncio.sleep(2)
                except asyncio.CancelledError:
                    await asyncio.sleep(2)

            asyncio.ensure_future(stubborn())
            await asyncio.sleep(0)
            return 1

        with self.assertLogs("api.tastytrade", level="WARNING") as logs:
            started = time.monotonic()
            result = _run_event_loop(work(), timeout=1.0, shutdown_timeout=0.05)
        self.assertEqual(result, 1)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(any("Abandoning 1 DXLink task" in m for m in logs.output))

    def test_shutdown_failure_does_not_mask_the_result(self) -> None:
        async def work() -> int:
            return 3

        with (
            patch(
                "analystbot.api.tastytrade._shutdown_loop",
                side_effect=RuntimeError("boom"),
            ),
            self.assertLogs("api.tastytrade", level="ERROR") as logs,
        ):
            result = _run_event_loop(work(), timeout=1.0, shutdown_timeout=0.05)
        self.assertEqual(result, 3)
        self.assertTrue(any("shutdown failed" in m for m in logs.output))

    def test_hung_finalizer_is_abandoned_with_a_warning(self) -> None:
        async def work() -> int:
            return 1

        async def hang() -> None:
            await asyncio.sleep(5)

        with (
            patch.object(
                asyncio.BaseEventLoop,
                "shutdown_default_executor",
                lambda self, timeout=None: hang(),
            ),
            self.assertLogs("api.tastytrade", level="WARNING") as logs,
        ):
            result = _run_event_loop(work(), timeout=1.0, shutdown_timeout=0.05)
        self.assertEqual(result, 1)
        self.assertTrue(
            any("Abandoned DXLink default executor shutdown" in m for m in logs.output)
        )

    def test_timeout_raises_when_main_ignores_cancellation(self) -> None:
        async def work() -> None:
            try:
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                await asyncio.sleep(2)

        with self.assertRaisesRegex(TimeoutError, "fetch timed out"):
            _run_event_loop(work(), timeout=0.05, shutdown_timeout=0.05)


class DeadlineBudgetTests(unittest.TestCase):
    def test_defaults_fit_inside_the_database_update_stage_timeout(self) -> None:
        from analystbot.pipeline.stages import DATABASE_UPDATE_TIMEOUT_SECONDS

        cfg = TastytradeConfig()
        worst_case = (
            cfg.FETCH_DEADLINE_SECONDS
            + 6 * cfg.LOOP_SHUTDOWN_TIMEOUT_SECONDS
            + cfg.WALL_CLOCK_MARGIN_SECONDS
        )
        self.assertLess(worst_case, DATABASE_UPDATE_TIMEOUT_SECONDS)

    def test_handshake_retries_fit_inside_the_fetch_deadline(self) -> None:
        cfg = TastytradeConfig()
        self.assertLess(
            cfg.MAX_REST_RETRIES * cfg.STREAMER_HANDSHAKE_TIMEOUT_SECONDS,
            cfg.FETCH_DEADLINE_SECONDS,
        )


class FetchAndRateLimitTests(unittest.TestCase):
    def _client(
        self,
        streamer: FakeStreamer,
        *,
        session_factory=None,
        sleeper=None,
        config: TastytradeConfig | None = None,
    ) -> TastytradeClient:
        cfg = config or TastytradeConfig(
            CANDLE_IDLE_TIMEOUT_SECONDS=0.05,
            INTER_BATCH_SETTLE_SECONDS=0.0,
            REST_MIN_INTERVAL_SECONDS=0.0,
        )
        return TastytradeClient(
            "secret",
            "refresh",
            config=cfg,
            session_factory=session_factory or (lambda: object()),
            streamer_factory=lambda _session: streamer,
            sleeper=sleeper or (lambda _delay: None),
        )

    def test_drops_candles_after_end_date(self) -> None:
        streamer = FakeStreamer(
            [
                _candle("AAPL{=1d,tho=true}", "2026-01-05"),
                _candle("AAPL{=1d,tho=true}", "2026-01-02"),
            ]
        )
        client = self._client(streamer)
        bars = client.get_daily_bars_range("AAPL", "2026-01-01", "2026-01-02")
        self.assertIsNotNone(bars)
        assert bars is not None
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["date"], date_str_to_unix("2026-01-02"))

    def test_keeps_completed_days_inside_range(self) -> None:
        streamer = FakeStreamer(
            [
                _candle("AAPL{=1d,tho=true}", "2026-01-02"),
                _candle("AAPL{=1d,tho=true}", "2026-01-01"),
            ]
        )
        client = self._client(streamer)
        bars = client.get_daily_bars_range("AAPL", "2026-01-01", "2026-01-02")
        assert bars is not None
        self.assertEqual(
            [bar["date"] for bar in bars],
            [date_str_to_unix("2026-01-01"), date_str_to_unix("2026-01-02")],
        )

    def test_subscribes_to_official_daily_session_not_tho(self) -> None:
        streamer = FakeStreamer()
        client = self._client(streamer)
        client.get_daily_bars_range("PH", "2026-09-14", "2026-09-14")
        self.assertEqual(len(streamer.subscribe_calls), 1)
        _symbols, interval, _start, extended = streamer.subscribe_calls[0]
        self.assertEqual(interval, "1d")
        self.assertTrue(extended)

    def test_prefers_official_daily_bar_over_tho_even_if_tho_arrives_last(self) -> None:
        streamer = FakeStreamer(
            [
                _candle(
                    "PH{=1d}",
                    "2026-09-14",
                    open_=929.82,
                    high=936.37,
                    low=914.825,
                    close=928.83,
                    volume=663_640,
                ),
                _candle(
                    "PH{=1d,tho=true}",
                    "2026-09-14",
                    open_=928.75,
                    high=930.51,
                    low=914.825,
                    close=929.42,
                    volume=200_564,
                ),
                _candle(
                    "PH{=1d}",
                    "2026-09-10",
                    open_=939.65,
                    high=944.46,
                    low=929.24,
                    close=934.34,
                    volume=655_770,
                ),
            ]
        )
        client = self._client(streamer)
        bars = client.get_daily_bars_range("PH", "2026-09-10", "2026-09-14")
        assert bars is not None
        by_date = {bar["date"]: bar for bar in bars}
        self.assertEqual(by_date[date_str_to_unix("2026-09-14")]["high"], 936.37)
        self.assertEqual(by_date[date_str_to_unix("2026-09-14")]["close"], 928.83)
        self.assertEqual(by_date[date_str_to_unix("2026-09-14")]["open"], 929.82)

    def test_idle_timeout_without_candles_is_a_miss(self) -> None:
        client = self._client(FakeStreamer())
        result = client.get_daily_bars_batch(
            [BarRequest(symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02")]
        )
        self.assertIsNone(result["AAPL"])

    def test_candles_before_start_are_confirmed_empty(self) -> None:
        streamer = FakeStreamer([_candle("AAPL{=1d,tho=true}", "2026-01-01")])
        client = self._client(streamer)
        result = client.get_daily_bars_batch(
            [BarRequest(symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02")]
        )
        self.assertEqual(result["AAPL"], [])

    def test_101_symbols_use_two_candle_subs_and_one_streamer(self) -> None:
        streamers: list[FakeStreamer] = []

        def factory(_session: object) -> FakeStreamer:
            streamer = FakeStreamer()
            streamers.append(streamer)
            return streamer

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                CANDLE_IDLE_TIMEOUT_SECONDS=0.01,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
            ),
            session_factory=lambda: object(),
            streamer_factory=factory,
            sleeper=lambda _delay: None,
        )
        requests = [
            BarRequest(symbol=f"S{i:03d}", from_date="2026-01-02", to_date="2026-01-02")
            for i in range(101)
        ]
        client.get_daily_bars_batch(requests)

        self.assertEqual(len(streamers), 1)
        self.assertEqual(len(streamers[0].subscribe_calls), 2)
        self.assertEqual(len(streamers[0].subscribe_calls[0][0]), 100)
        self.assertEqual(len(streamers[0].subscribe_calls[1][0]), 1)
        removes = [
            msg
            for msg in streamers[0]._websocket.messages
            if msg.get("type") == "FEED_SUBSCRIPTION" and "remove" in msg
        ]
        self.assertEqual(len(removes), 1)
        self.assertEqual(len(removes[0]["remove"]), 100)

    def test_candle_unsubscribe_hang_times_out(self) -> None:
        class HangingUnsubscribeWebsocket(FakeWebsocket):
            async def send_json(self, message: dict) -> None:
                if "remove" in message:
                    try:
                        await asyncio.sleep(3)
                    except asyncio.CancelledError:
                        await asyncio.sleep(3)
                await super().send_json(message)

        streamer = FakeStreamer()
        streamer._websocket = HangingUnsubscribeWebsocket()
        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                CANDLE_BATCH_SIZE=1,
                CANDLE_IDLE_TIMEOUT_SECONDS=0.01,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=1,
                CANDLE_SUBSCRIBE_TIMEOUT_SECONDS=0.05,
                LOOP_SHUTDOWN_TIMEOUT_SECONDS=0.05,
                FETCH_DEADLINE_SECONDS=1.0,
            ),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: streamer,
            sleeper=lambda _delay: None,
        )
        with self.assertRaisesRegex(
            TimeoutError,
            "tastytrade DXLink candle unsubscribe timed out",
        ):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL",
                        from_date="2026-01-02",
                        to_date="2026-01-02",
                    ),
                    BarRequest(
                        symbol="MSFT",
                        from_date="2026-01-02",
                        to_date="2026-01-02",
                    ),
                ]
            )

    def test_inter_batch_settle_pause(self) -> None:
        sleeps: list[float] = []
        streamer = FakeStreamer()
        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                CANDLE_BATCH_SIZE=100,
                CANDLE_IDLE_TIMEOUT_SECONDS=0.01,
                INTER_BATCH_SETTLE_SECONDS=1.5,
                REST_MIN_INTERVAL_SECONDS=0.0,
            ),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: streamer,
            sleeper=sleeps.append,
        )
        requests = [
            BarRequest(symbol=f"S{i:03d}", from_date="2026-01-02", to_date="2026-01-02")
            for i in range(101)
        ]
        client.get_daily_bars_batch(requests)
        self.assertIn(1.5, sleeps)

    def test_second_live_streamer_is_rejected(self) -> None:
        client = TastytradeClient("secret", "refresh")
        client._claim_streamer()
        with self.assertRaises(RuntimeError):
            client._claim_streamer()
        client._release_streamer()
        client._claim_streamer()

    def test_oauth_429_backs_off_instead_of_spinning(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def factory() -> object:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitError("slow down")
            return object()

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            session_factory=factory,
            sleeper=sleeps.append,
        )
        session = client._create_session()
        self.assertIsNotNone(session)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(delay > 0 for delay in sleeps))

    def test_oauth_401_does_not_retry(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def factory() -> object:
            attempts["n"] += 1
            raise AuthError("nope")

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            session_factory=factory,
            sleeper=sleeps.append,
        )
        with self.assertRaises(AuthError):
            client._create_session()
        self.assertEqual(attempts["n"], 1)
        self.assertEqual(sleeps, [])

    def test_invalid_grant_refresh_does_not_retry(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        class FakeSession:
            async def refresh(self) -> None:
                attempts["n"] += 1
                raise GrantRevokedError(GRANT_REVOKED_MESSAGE)

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            sleeper=sleeps.append,
        )
        with self.assertRaises(GrantRevokedError):
            asyncio.run(client._refresh_session(FakeSession()))
        self.assertEqual(attempts["n"], 1)
        self.assertEqual(sleeps, [])

    def test_batch_fetch_raises_when_oauth_grant_revoked(self) -> None:

        class FakeSession:
            async def refresh(self) -> None:
                raise GrantRevokedError(GRANT_REVOKED_MESSAGE)

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                CANDLE_IDLE_TIMEOUT_SECONDS=0.01,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
            ),
            session_factory=lambda: FakeSession(),
            streamer_factory=lambda _session: FakeStreamer(),
            sleeper=lambda _delay: None,
        )
        with self.assertRaises(RuntimeError) as ctx:
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertIn("OAuth", str(ctx.exception))
        self.assertIn("grant", str(ctx.exception).lower())

    def test_quote_token_403_requires_read_scope(self) -> None:
        class ScopeError(Exception):
            status_code = 403

            def __str__(self) -> str:
                return "Token has insufficient scopes for this request"

        class BoomStreamer:
            entered = False

            async def __aenter__(self) -> BoomStreamer:
                raise ScopeError()

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                if not self.entered:
                    raise RuntimeError(
                        f"this {type(self).__qualname__} has not been entered yet"
                    )
                return False

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=1),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: BoomStreamer(),
            sleeper=lambda _delay: None,
        )
        with self.assertRaises(RuntimeError) as ctx:
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertIn("read", str(ctx.exception))
        self.assertIn("insufficient scopes", str(ctx.exception).lower())

    def test_empty_tastytrade_error_requires_read_scope(self) -> None:
        class TastytradeError(Exception):
            pass

        class BoomStreamer:
            entered = False

            async def __aenter__(self) -> BoomStreamer:
                raise TastytradeError("")

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                if not self.entered:
                    raise RuntimeError(
                        f"this {type(self).__qualname__} has not been entered yet"
                    )
                return False

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=1),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: BoomStreamer(),
            sleeper=lambda _delay: None,
        )
        with self.assertRaises(RuntimeError) as ctx:
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertIn("read", str(ctx.exception))

    def test_empty_refresh_error_is_not_read_scope(self) -> None:
        class TastytradeError(Exception):
            pass

        class FakeSession:
            async def refresh(self) -> None:
                raise TastytradeError("")

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=1),
            session_factory=lambda: FakeSession(),
            streamer_factory=lambda _session: FakeStreamer(),
            sleeper=lambda _delay: None,
        )
        with self.assertRaises(TastytradeError) as ctx:
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertNotIn("read", str(ctx.exception).lower())

    def test_batch_fetch_raises_when_session_is_unauthorized(self) -> None:
        def factory() -> object:
            raise AuthError("nope")

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            session_factory=factory,
            streamer_factory=lambda _session: FakeStreamer(),
            sleeper=lambda _delay: None,
        )
        with self.assertRaises(RuntimeError) as ctx:
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertIn("OAuth", str(ctx.exception))

    def test_batch_fetch_raises_when_streamer_handshake_fails(self) -> None:
        class BoomStreamer:
            entered = False

            async def __aenter__(self) -> BoomStreamer:
                raise RuntimeError("quote token failed")

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                if not self.entered:
                    raise RuntimeError(
                        f"this {type(self).__qualname__} has not been entered yet"
                    )
                return False

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=1,
                INTER_BATCH_SETTLE_SECONDS=0.0,
            ),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: BoomStreamer(),
            sleeper=lambda _delay: None,
        )
        with self.assertRaises(RuntimeError) as ctx:
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertIn("quote token failed", str(ctx.exception))
        self.assertNotIn("has not been entered yet", str(ctx.exception))

    def test_refresh_429_backs_off(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        class FakeSession:
            async def refresh(self) -> None:
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise RateLimitError("slow")

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            sleeper=sleeps.append,
        )
        asyncio.run(client._refresh_session(FakeSession()))
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_oauth_timeout_retries_then_succeeds(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def factory() -> object:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TimeoutError("connect timed out")
            return object()

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            session_factory=factory,
            sleeper=sleeps.append,
        )
        session = client._create_session()
        self.assertIsNotNone(session)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_oauth_connection_error_retries_then_succeeds(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        def factory() -> object:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("Name or service not known")
            return object()

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            session_factory=factory,
            sleeper=sleeps.append,
        )
        self.assertIsNotNone(client._create_session())
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(len(sleeps), 1)

    def test_refresh_hang_times_out_and_retries(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        class FakeSession:
            async def refresh(self) -> None:
                attempts["n"] += 1
                await asyncio.sleep(0.3)

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=3,
                REST_ATTEMPT_TIMEOUT_SECONDS=0.05,
            ),
            sleeper=sleeps.append,
        )
        with self.assertRaises(TimeoutError):
            asyncio.run(client._refresh_session(FakeSession()))
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_streamer_handshake_hang_times_out_and_retries(self) -> None:
        attempts = {"n": 0}
        exit_calls = {"n": 0}
        sleeps: list[float] = []

        class HangingStreamer:
            def __init__(self) -> None:
                self.entered = False

            async def __aenter__(self) -> HangingStreamer:
                attempts["n"] += 1
                await asyncio.sleep(0.3)
                self.entered = True
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                exit_calls["n"] += 1
                if not self.entered:
                    raise RuntimeError(
                        f"this {type(self).__qualname__} has not been entered yet"
                    )
                return False

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=3,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                STREAMER_HANDSHAKE_TIMEOUT_SECONDS=0.05,
            ),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: HangingStreamer(),
            sleeper=sleeps.append,
        )
        with self.assertRaises(TimeoutError) as ctx:
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertIn("handshake timed out", str(ctx.exception))
        self.assertNotIn("has not been entered yet", str(ctx.exception))
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(exit_calls["n"], 0)
        self.assertEqual(len(sleeps), 2)

    def test_sdk_connect_timeout_keeps_its_own_message(self) -> None:
        class ConnectTimeoutStreamer:
            async def __aenter__(self) -> ConnectTimeoutStreamer:
                raise TimeoutError("connect timed out talking to dxlink")

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=1,
                STREAMER_HANDSHAKE_TIMEOUT_SECONDS=30.0,
            ),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: ConnectTimeoutStreamer(),
            sleeper=lambda _delay: None,
        )
        with self.assertRaisesRegex(TimeoutError, "connect timed out talking"):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )

    def test_candle_subscribe_hang_times_out_and_retries(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        class HangingSubscribeStreamer:
            async def __aenter__(self) -> HangingSubscribeStreamer:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def subscribe_candle(self, *args: object, **kwargs: object) -> None:
                attempts["n"] += 1
                await asyncio.sleep(0.3)

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=3,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                CANDLE_SUBSCRIBE_TIMEOUT_SECONDS=0.05,
                STREAMER_CLOSE_TIMEOUT_SECONDS=0.05,
            ),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: HangingSubscribeStreamer(),
            sleeper=sleeps.append,
        )
        with self.assertRaises(TimeoutError):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_uncancellable_subscribe_hang_does_not_pin_the_event_loop(self) -> None:
        attempts = {"n": 0}

        class UncancellableSubscribeStreamer:
            async def __aenter__(self) -> UncancellableSubscribeStreamer:
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def subscribe_candle(self, *args: object, **kwargs: object) -> None:
                attempts["n"] += 1
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    await asyncio.sleep(3)

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=1,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                CANDLE_SUBSCRIBE_TIMEOUT_SECONDS=0.05,
                STREAMER_CLOSE_TIMEOUT_SECONDS=0.05,
                LOOP_SHUTDOWN_TIMEOUT_SECONDS=0.05,
                FETCH_DEADLINE_SECONDS=1.0,
            ),
            session_factory=lambda: object(),
            streamer_factory=lambda _session: UncancellableSubscribeStreamer(),
            sleeper=lambda _delay: None,
        )
        started = time.monotonic()
        with self.assertRaisesRegex(
            TimeoutError,
            "tastytrade DXLink candle subscribe timed out",
        ):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(attempts["n"], 1)

    def test_streamer_close_hang_does_not_block_returned_bars(self) -> None:
        class HangingCloseStreamer(FakeStreamer):
            async def __aexit__(self, exc_type, exc, tb) -> bool:
                await asyncio.sleep(3)
                self.exited = True
                return False

        streamer = HangingCloseStreamer(
            [_candle("AAPL{=1d}", "2026-01-02", volume=1_000, vwap=100.0)]
        )
        client = self._client(
            streamer,
            config=TastytradeConfig(
                CANDLE_IDLE_TIMEOUT_SECONDS=0.05,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
                STREAMER_CLOSE_TIMEOUT_SECONDS=0.05,
            ),
        )
        started = time.monotonic()
        result = client.get_daily_bars_batch(
            [BarRequest(symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02")]
        )
        self.assertLess(time.monotonic() - started, 1.5)
        bars = result["AAPL"]
        self.assertIsNotNone(bars)
        assert bars is not None
        self.assertEqual(bars[0]["close"], 105.0)

    def test_uncancellable_close_does_not_pin_get_daily_bars_batch(self) -> None:

        class UncancellableCloseStreamer(FakeStreamer):
            async def __aexit__(self, exc_type, exc, tb) -> bool:
                if not self.entered:
                    raise RuntimeError(
                        f"this {type(self).__qualname__} has not been entered yet"
                    )
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    await asyncio.sleep(3)
                self.exited = True
                return False

        deadline = 0.3
        shutdown = 0.05
        streamer = UncancellableCloseStreamer(
            [_candle("AAPL{=1d}", "2026-01-02", volume=1_000, vwap=100.0)]
        )
        client = self._client(
            streamer,
            config=TastytradeConfig(
                CANDLE_IDLE_TIMEOUT_SECONDS=0.05,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
                STREAMER_CLOSE_TIMEOUT_SECONDS=0.05,
                LOOP_SHUTDOWN_TIMEOUT_SECONDS=shutdown,
                FETCH_DEADLINE_SECONDS=deadline,
            ),
        )
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, deadline + 6 * shutdown + 0.5)
        self.assertLess(elapsed, 2.0)

    def test_uncancellable_candle_listen_respects_idle_deadline(self) -> None:

        class UncancellableListenStreamer(FakeStreamer):
            async def get_event(self, _event_class):
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    await asyncio.sleep(3)

        deadline = 1.0
        shutdown = 0.05
        client = self._client(
            UncancellableListenStreamer(),
            config=TastytradeConfig(
                CANDLE_IDLE_TIMEOUT_SECONDS=0.05,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
                STREAMER_CLOSE_TIMEOUT_SECONDS=0.05,
                LOOP_SHUTDOWN_TIMEOUT_SECONDS=shutdown,
                FETCH_DEADLINE_SECONDS=deadline,
            ),
        )
        started = time.monotonic()
        result = client.get_daily_bars_batch(
            [BarRequest(symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02")]
        )
        elapsed = time.monotonic() - started
        self.assertIsNone(result["AAPL"])
        self.assertLess(elapsed, 0.9)

    def test_wall_clock_backstop_bounds_a_blocked_event_loop(self) -> None:
        closed = threading.Event()

        class BlockingSubscribeStreamer(FakeStreamer):
            async def subscribe_candle(
                self,
                symbols,
                interval,
                start_time=None,
                extended_trading_hours=False,
            ) -> None:
                time.sleep(0.4)
                await super().subscribe_candle(
                    symbols,
                    interval,
                    start_time=start_time,
                    extended_trading_hours=extended_trading_hours,
                )

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                try:
                    return await super().__aexit__(exc_type, exc, tb)
                finally:
                    closed.set()

        deadline = 0.1
        shutdown = 0.02
        client = self._client(
            BlockingSubscribeStreamer(
                [_candle("AAPL{=1d}", "2026-01-02", volume=1_000, vwap=100.0)]
            ),
            config=TastytradeConfig(
                CANDLE_IDLE_TIMEOUT_SECONDS=0.01,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=1,
                CANDLE_SUBSCRIBE_TIMEOUT_SECONDS=1.0,
                STREAMER_CLOSE_TIMEOUT_SECONDS=0.05,
                LOOP_SHUTDOWN_TIMEOUT_SECONDS=shutdown,
                FETCH_DEADLINE_SECONDS=deadline,
                WALL_CLOCK_MARGIN_SECONDS=0.0,
            ),
        )
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "wall-clock limit"):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, deadline + (2 * shutdown) + 0.3)
        with self.assertRaisesRegex(RuntimeError, "Only one DXLink streamer"):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        for _ in range(100):
            if client._active_streamers == 0:
                break
            time.sleep(0.02)
        self.assertTrue(closed.wait(2.0))
        self.assertEqual(client._active_streamers, 0)

    def test_queued_candles_use_nowait_fast_path(self) -> None:
        class QueuedStreamer(FakeStreamer):
            def __init__(self, candles) -> None:
                super().__init__(candles)
                self.nowait_calls = 0

            def get_event_nowait(self, _event_class):
                self.nowait_calls += 1
                return self._candles.pop(0) if self._candles else None

            async def get_event(self, _event_class):
                await asyncio.sleep(5)

        streamer = QueuedStreamer(
            [_candle("AAPL{=1d}", "2026-01-02", volume=1_000, vwap=100.0)]
        )
        client = self._client(streamer)
        result = client.get_daily_bars_batch(
            [BarRequest(symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02")]
        )
        self.assertGreaterEqual(streamer.nowait_calls, 1)
        self.assertEqual(len(result["AAPL"] or []), 1)

    def test_genuine_close_timeout_error_is_logged_not_reported_as_hung(self) -> None:
        class FailingCloseStreamer(FakeStreamer):
            async def __aexit__(self, exc_type, exc, tb) -> bool:
                raise TimeoutError("websocket close failed")

        client = self._client(
            FailingCloseStreamer(
                [_candle("AAPL{=1d}", "2026-01-02", volume=1_000, vwap=100.0)]
            ),
            config=TastytradeConfig(
                CANDLE_IDLE_TIMEOUT_SECONDS=0.05,
                INTER_BATCH_SETTLE_SECONDS=0.0,
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=1,
                STREAMER_CLOSE_TIMEOUT_SECONDS=5.0,
            ),
        )
        with self.assertLogs("api.tastytrade", level="WARNING") as logs:
            result = client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertEqual(len(result["AAPL"] or []), 1)
        self.assertTrue(any("close failed" in line for line in logs.output))
        self.assertFalse(any("hung DXLink" in line for line in logs.output))

    def test_slot_is_released_when_the_worker_thread_cannot_start(self) -> None:
        client = self._client(FakeStreamer())
        with (
            patch("threading.Thread.start", side_effect=RuntimeError("no threads")),
            self.assertRaisesRegex(RuntimeError, "no threads"),
        ):
            client.get_daily_bars_batch(
                [
                    BarRequest(
                        symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02"
                    )
                ]
            )
        self.assertEqual(client._active_streamers, 0)

    def test_streamer_enter_and_exit_run_on_the_same_task(self) -> None:
        tasks: list[asyncio.Task[Any] | None] = []

        class TaskRecordingStreamer(FakeStreamer):
            async def __aenter__(self) -> TaskRecordingStreamer:
                tasks.append(asyncio.current_task())
                return await super().__aenter__()

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                tasks.append(asyncio.current_task())
                return await super().__aexit__(exc_type, exc, tb)

        streamer = TaskRecordingStreamer(
            [_candle("AAPL{=1d}", "2026-01-02", volume=1_000, vwap=100.0)]
        )
        client = self._client(streamer)
        result = client.get_daily_bars_batch(
            [BarRequest(symbol="AAPL", from_date="2026-01-02", to_date="2026-01-02")]
        )
        self.assertIsNotNone(result["AAPL"])
        self.assertEqual(len(tasks), 2)
        self.assertIs(tasks[0], tasks[1])
        self.assertIsNotNone(tasks[0])

    def test_session_uses_connect_and_read_timeouts(self) -> None:
        captured: dict[str, object] = {}

        class FakeSession:
            def __init__(self, *args: object, **kwargs: object) -> None:
                captured["kwargs"] = kwargs
                self._client = SimpleNamespace(headers={})

        client = TastytradeClient("secret", "refresh")
        with patch("analystbot.api.tastytrade.Session", FakeSession):
            client._default_session_factory()
        timeout = captured["kwargs"]["timeout"]
        self.assertAlmostEqual(timeout.connect, 10.0)
        self.assertAlmostEqual(timeout.read, 30.0)

    def test_rest_pacing_sleeps_when_calls_are_too_close(self) -> None:
        sleeps: list[float] = []
        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.1),
            sleeper=sleeps.append,
        )
        monotonic_values = [10.0, 10.02, 10.02]
        with patch(
            "analystbot.api.tastytrade.time.monotonic", side_effect=monotonic_values
        ):
            client._pace_rest()
            client._pace_rest()
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.08, places=5)


class SettingsMarketDataTests(unittest.TestCase):
    def test_market_data_requires_tastytrade_not_polygon(self) -> None:
        settings = Settings(_loaded=True)
        ok, missing = settings.validate("market_data")
        self.assertFalse(ok)
        self.assertIn("TASTYTRADE_CLIENT_SECRET", missing)
        self.assertIn("TASTYTRADE_REFRESH_TOKEN", missing)
        self.assertNotIn("POLYGON_API_KEY", missing)

    def test_market_data_valid_when_oauth_present(self) -> None:
        settings = Settings(
            _loaded=True,
            tastytrade_client_secret="secret",
            tastytrade_refresh_token="refresh",
        )
        ok, missing = settings.validate("market_data")
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_secret_fields_are_hidden_from_repr(self) -> None:
        settings = Settings(
            _loaded=True,
            tastytrade_client_secret="secret-value",
            tastytrade_refresh_token="refresh-value",
            telegram_bot_token="bot-value",
        )
        text = repr(settings)
        self.assertNotIn("secret-value", text)
        self.assertNotIn("refresh-value", text)
        self.assertNotIn("bot-value", text)

    def test_sandbox_env_selects_cert_api(self) -> None:
        sandbox = TastytradeClient("secret", "refresh", environment="sandbox")
        production = TastytradeClient("secret", "refresh", environment="production")
        default = TastytradeClient("secret", "refresh")
        self.assertTrue(sandbox.is_test)
        self.assertFalse(production.is_test)
        self.assertFalse(default.is_test)


class TimestampHelperTests(unittest.TestCase):
    def test_millisecond_candle_time_normalizes_to_utc_midnight(self) -> None:
        client = TastytradeClient("secret", "refresh")
        ts_ms = int(datetime(2026, 1, 2, 5, 0, tzinfo=UTC).timestamp() * 1000)
        candle = SimpleNamespace(
            event_symbol="SPY{=1d,tho=true}",
            time=ts_ms,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            vwap=1.0,
        )
        bar = client.candle_to_bar(candle)
        assert bar is not None
        self.assertEqual(bar["date"], date_str_to_unix("2026-01-02"))


class MarketCapTests(unittest.TestCase):
    def _client(self, fetcher) -> TastytradeClient:
        return TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(REST_MIN_INTERVAL_SECONDS=0.0, MAX_REST_RETRIES=3),
            session_factory=lambda: object(),
            metrics_fetcher=fetcher,
            sleeper=lambda _delay: None,
        )

    def test_brk_b_maps_back_to_stored_ticker(self) -> None:
        seen: list[list[str]] = []

        def fetch(_session, symbols):
            seen.append(list(symbols))
            return [SimpleNamespace(symbol="BRK/B", market_cap=900.0)]

        caps = self._client(fetch).get_market_caps(["BRK.B"])
        self.assertEqual(caps, {"BRK.B": 900.0})
        self.assertEqual(seen, [["BRK/B"]])

    def test_skips_non_positive_and_non_finite_caps(self) -> None:
        def fetch(_session, symbols):
            return [
                SimpleNamespace(symbol="AAPL", market_cap=100.0),
                SimpleNamespace(symbol="MSFT", market_cap=0),
                SimpleNamespace(symbol="NVDA", market_cap=-5),
                SimpleNamespace(symbol="AMZN", market_cap=float("nan")),
            ]

        caps = self._client(fetch).get_market_caps(["AAPL", "MSFT", "NVDA", "AMZN"])
        self.assertEqual(caps, {"AAPL": 100.0})

    def test_empty_symbol_list_skips_network(self) -> None:
        def fetch(_session, symbols):
            raise AssertionError("should not fetch")

        self.assertEqual(self._client(fetch).get_market_caps([]), {})

    def test_rate_limit_retries_then_succeeds(self) -> None:
        attempts = {"n": 0}

        def fetch(_session, symbols):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RateLimitError("slow down")
            return [SimpleNamespace(symbol="AAPL", market_cap=50.0)]

        caps = self._client(fetch).get_market_caps(["AAPL"])
        self.assertEqual(caps, {"AAPL": 50.0})
        self.assertEqual(attempts["n"], 2)

    def test_metrics_hang_times_out_and_retries(self) -> None:
        attempts = {"n": 0}

        async def fetch(_session, symbols):
            attempts["n"] += 1
            await asyncio.sleep(0.3)
            return []

        client = TastytradeClient(
            "secret",
            "refresh",
            config=TastytradeConfig(
                REST_MIN_INTERVAL_SECONDS=0.0,
                MAX_REST_RETRIES=3,
                REST_ATTEMPT_TIMEOUT_SECONDS=0.05,
            ),
            session_factory=lambda: object(),
            metrics_fetcher=fetch,
            sleeper=lambda _delay: None,
        )
        with self.assertRaises(TimeoutError):
            client.get_market_caps(["AAPL"])
        self.assertEqual(attempts["n"], 3)


if __name__ == "__main__":
    unittest.main()
