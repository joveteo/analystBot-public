from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

from analystbot.api.http_client import (
    HttpClient,
    RetryPolicy,
    SessionManager,
    calculate_backoff_delay,
    create_session,
    format_request_error,
    redact_url,
)


def _response(
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.headers = headers or {}
    return response


def _policy(**overrides: object) -> RetryPolicy:
    values: dict[str, object] = {
        "max_retries": 3,
        "base_delay": 1.0,
        "max_delay": 10.0,
        "jitter": 0.0,
        "timeout": (2, 4),
        "rate_limit_delay": 0.0,
    }
    values.update(overrides)
    return RetryPolicy(**values)


class UrlRedactionTests(unittest.TestCase):
    def test_redacts_sensitive_query_values_and_telegram_token(self) -> None:
        safe = redact_url(
            "https://api.telegram.org/bot123:secret/sendMessage"
            "?chat_id=42&API_KEY=secret&token=&page=2"
        )

        self.assertEqual(
            safe,
            "https://api.telegram.org/botREDACTED/sendMessage"
            "?chat_id=42&API_KEY=REDACTED&token=REDACTED&page=2",
        )
        self.assertNotIn("123:secret", safe)
        self.assertNotIn("API_KEY=secret", safe)

    def test_preserves_non_sensitive_url_components(self) -> None:
        url = "https://example.test/path?q=hello+world#section"
        self.assertEqual(redact_url(url), url)

    def test_request_error_excludes_exception_message(self) -> None:
        error = requests.RequestException("token=super-secret")
        self.assertEqual(
            format_request_error(error, "https://example.test?api_key=also-secret"),
            "RequestException for https://example.test?api_key=REDACTED",
        )
        self.assertEqual(format_request_error(error), "RequestException")


class SessionTests(unittest.TestCase):
    def tearDown(self) -> None:
        SessionManager.close_session()

    def test_create_session_merges_default_and_custom_headers(self) -> None:
        session = create_session({"User-Agent": "custom/1.0", "X-Test": "yes"})
        self.addCleanup(session.close)

        self.assertEqual(session.headers["User-Agent"], "custom/1.0")
        self.assertEqual(session.headers["Accept"], "application/json")
        self.assertEqual(session.headers["X-Test"], "yes")

    def test_session_manager_reuses_then_closes_shared_session(self) -> None:
        fake = MagicMock(spec=requests.Session)
        with patch(
            "analystbot.api.http_client.create_session",
            return_value=fake,
        ) as factory:
            self.assertIs(SessionManager.get_session(), fake)
            self.assertIs(SessionManager.get_session(), fake)
            factory.assert_called_once_with()

        SessionManager.close_session()
        fake.close.assert_called_once_with()
        self.assertIsNone(SessionManager._session)


class BackoffTests(unittest.TestCase):
    def test_exponential_delay_is_capped_before_jitter(self) -> None:
        with patch(
            "analystbot.api.http_client.random.uniform",
            return_value=0.5,
        ) as uniform:
            self.assertEqual(
                calculate_backoff_delay(5, base_delay=2, max_delay=10, jitter=0.2),
                10.5,
            )
        uniform.assert_called_once_with(0, 2.0)

    def test_retry_after_respects_minimum_rate_limit(self) -> None:
        client = HttpClient(policy=_policy(rate_limit_delay=2.5))

        self.assertEqual(
            client._handle_retry(0, _response(429, headers={"Retry-After": "1"})),
            2.5,
        )
        self.assertEqual(
            client._handle_retry(0, _response(503, headers={"Retry-After": "4"})),
            4.0,
        )
        self.assertEqual(
            client._handle_retry(
                0,
                _response(429, headers={"Retry-After": "not-a-number"}),
            ),
            2.5,
        )

    def test_rate_limit_only_sleeps_for_remaining_interval(self) -> None:
        client = HttpClient(policy=_policy(rate_limit_delay=1.0))
        with (
            patch(
                "analystbot.api.http_client.time.time",
                side_effect=[10.0, 10.4, 10.4],
            ),
            patch("analystbot.api.http_client.time.sleep") as sleep,
        ):
            client._apply_rate_limit()
            client._apply_rate_limit()

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.6)
        self.assertEqual(client._last_request_time, 10.4)


class GetRequestTests(unittest.TestCase):
    def test_success_uses_policy_timeout_and_returns_response(self) -> None:
        session = MagicMock(spec=requests.Session)
        expected = _response(200)
        session.get.return_value = expected
        client = HttpClient(policy=_policy(), session=session)

        actual = client.get("https://example.test", params={"q": "value"})

        self.assertIs(actual, expected)
        session.get.assert_called_once_with(
            "https://example.test",
            params={"q": "value"},
            timeout=(2, 4),
        )

    def test_caller_timeout_overrides_policy_timeout(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.get.return_value = _response(200)
        client = HttpClient(policy=_policy(), session=session)

        client.get("https://example.test", timeout=9)

        self.assertEqual(session.get.call_args.kwargs["timeout"], 9)

    def test_rate_limit_retries_using_retry_after(self) -> None:
        session = MagicMock(spec=requests.Session)
        limited = _response(429, headers={"Retry-After": "3"})
        success = _response(200)
        session.get.side_effect = [limited, success]
        client = HttpClient(policy=_policy(rate_limit_delay=1.0), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch("analystbot.api.http_client.time.sleep") as sleep,
        ):
            self.assertIs(client.get("https://example.test"), success)

        sleep.assert_called_once_with(3.0)
        self.assertEqual(session.get.call_count, 2)

    def test_server_error_retries_then_returns_last_response(self) -> None:
        session = MagicMock(spec=requests.Session)
        first = _response(500)
        last = _response(503)
        session.get.side_effect = [first, last]
        client = HttpClient(policy=_policy(max_retries=2), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch.object(client, "_handle_retry", return_value=0.25) as retry,
            patch("analystbot.api.http_client.time.sleep") as sleep,
        ):
            self.assertIs(client.get("https://example.test"), last)

        retry.assert_called_once_with(0)
        sleep.assert_called_once_with(0.25)

    def test_timeouts_exhaust_retries_and_return_none(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.Timeout("secret response")
        client = HttpClient(policy=_policy(max_retries=2), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch.object(client, "_handle_retry", return_value=0),
            patch("analystbot.api.http_client.time.sleep") as sleep,
        ):
            self.assertIsNone(
                client.get("https://example.test?access_token=super-secret")
            )

        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_request_errors_exhaust_retries_and_return_none(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("offline")
        client = HttpClient(policy=_policy(max_retries=2), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch.object(client, "_handle_retry", return_value=0),
            patch("analystbot.api.http_client.time.sleep"),
        ):
            self.assertIsNone(client.get("https://example.test"))

        self.assertEqual(session.get.call_count, 2)


class PostRequestTests(unittest.TestCase):
    def test_success_forwards_form_json_and_extra_kwargs(self) -> None:
        session = MagicMock(spec=requests.Session)
        expected = _response(200)
        session.post.return_value = expected
        client = HttpClient(policy=_policy(), session=session)

        actual = client.post(
            "https://example.test",
            data={"form": "value"},
            json={"json": "value"},
            files={"file": object()},
        )

        self.assertIs(actual, expected)
        call = session.post.call_args
        self.assertEqual(call.kwargs["data"], {"form": "value"})
        self.assertEqual(call.kwargs["json"], {"json": "value"})
        self.assertIn("file", call.kwargs["files"])
        self.assertEqual(call.kwargs["timeout"], (2, 4))

    def test_client_error_is_not_retried(self) -> None:
        session = MagicMock(spec=requests.Session)
        bad_request = _response(400)
        session.post.return_value = bad_request
        client = HttpClient(policy=_policy(), session=session)

        with patch.object(client, "_apply_rate_limit"):
            self.assertIs(client.post("https://example.test"), bad_request)

        session.post.assert_called_once()

    def test_server_error_retries_and_can_recover(self) -> None:
        session = MagicMock(spec=requests.Session)
        success = _response(200)
        session.post.side_effect = [_response(500), success]
        client = HttpClient(policy=_policy(), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch.object(client, "_handle_retry", return_value=0),
            patch("analystbot.api.http_client.time.sleep"),
        ):
            self.assertIs(client.post("https://example.test"), success)

        self.assertEqual(session.post.call_count, 2)

    def test_rate_limit_retries_using_retry_after(self) -> None:
        session = MagicMock(spec=requests.Session)
        success = _response(200)
        session.post.side_effect = [
            _response(429, headers={"Retry-After": "2"}),
            success,
        ]
        client = HttpClient(policy=_policy(), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch("analystbot.api.http_client.time.sleep") as sleep,
        ):
            self.assertIs(client.post("https://example.test"), success)

        sleep.assert_called_once_with(2.0)

    def test_post_timeout_is_not_retried(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = requests.Timeout("slow")
        client = HttpClient(policy=_policy(max_retries=3), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch("analystbot.api.http_client.time.sleep") as sleep,
        ):
            self.assertIsNone(client.post("https://example.test"))

        session.post.assert_called_once()
        sleep.assert_not_called()

    def test_connection_errors_exhaust_retries(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = requests.ConnectionError("offline")
        client = HttpClient(policy=_policy(max_retries=2), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch.object(client, "_handle_retry", return_value=0),
            patch("analystbot.api.http_client.time.sleep"),
        ):
            self.assertIsNone(client.post("https://example.test"))
        self.assertEqual(session.post.call_count, 2)

    def test_rate_limit_uses_telegram_json_retry_after(self) -> None:
        session = MagicMock(spec=requests.Session)
        limited = _response(429)
        limited.json.return_value = {
            "ok": False,
            "parameters": {"retry_after": 12},
        }
        success = _response(200)
        session.post.side_effect = [limited, success]
        client = HttpClient(policy=_policy(rate_limit_delay=0.1), session=session)

        with (
            patch.object(client, "_apply_rate_limit"),
            patch("analystbot.api.http_client.time.sleep") as sleep,
        ):
            self.assertIs(client.post("https://example.test"), success)

        sleep.assert_called_once_with(12.0)

    def test_invalid_retry_after_values_are_ignored(self) -> None:
        client = HttpClient(policy=_policy(), session=MagicMock(spec=requests.Session))
        self.assertIsNone(client._retry_after_seconds(None))

        header_only = _response(429, headers={"Retry-After": "later"})
        header_only.json.side_effect = ValueError("not json")
        self.assertIsNone(client._retry_after_seconds(header_only))

        body = _response(429)
        body.json.return_value = {"parameters": {"retry_after": object()}}
        self.assertIsNone(client._retry_after_seconds(body))


if __name__ == "__main__":
    unittest.main()
