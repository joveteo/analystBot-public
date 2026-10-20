from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from analystbot.api.telegram import TelegramClient
from analystbot.core.config import Settings
from analystbot.db.sqlite import connect
from analystbot.services.notifications.delivery import (
    NotificationService,
    run_chart_album_forced,
    run_delivery,
    run_market_heatmap_telegram,
    run_market_supertrend_telegram,
    run_rrg_telegram,
    run_rrg_telegram_forced,
    run_vix_telegram,
    run_vixfix,
)
from analystbot.services.rrg_charts import WeeklyDecision


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.ok = status_code == 200
        self.text = "ok"

    def json(self) -> dict[str, object]:
        return {"ok": True}


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    def post(self, url, json=None, data=None, files=None):
        self.posts.append({"url": url, "json": json, "data": data})
        return FakeResponse()


class TelegramClientMuteTests(unittest.TestCase):
    def _client(self) -> tuple[TelegramClient, FakeHttp]:
        client = TelegramClient("token", "chat")
        fake = FakeHttp()
        client._http = fake
        return client, fake

    def test_send_message_includes_disable_notification(self) -> None:
        client, fake = self._client()
        self.assertTrue(client.send_message("hello", disable_notification=True))
        payload = fake.posts[0]["json"]
        assert isinstance(payload, dict)
        self.assertTrue(payload["disable_notification"])

        fake.posts.clear()
        self.assertTrue(client.send_message("hello", disable_notification=False))
        payload = fake.posts[0]["json"]
        assert isinstance(payload, dict)
        self.assertFalse(payload["disable_notification"])

    def test_send_photo_sends_multipart_boolean_strings(self) -> None:
        client, fake = self._client()
        with tempfile.TemporaryDirectory() as temp_dir:
            photo = Path(temp_dir) / "vix.png"
            photo.write_bytes(b"png")
            self.assertTrue(
                client.send_photo(photo, caption="cap", disable_notification=True)
            )
        form = fake.posts[0]["data"]
        assert isinstance(form, dict)
        self.assertEqual(form["disable_notification"], "true")

    def test_topic_fallback_preserves_mute_flag(self) -> None:
        client, fake = self._client()

        def post(url, json=None, data=None, files=None):
            fake.posts.append({"url": url, "json": json, "data": data})
            if json and json.get("message_thread_id"):
                return FakeResponse(status_code=400)
            return FakeResponse()

        fake.post = post
        self.assertTrue(
            client.send_message(
                "hello",
                topic_id="99",
                disable_notification=True,
            )
        )
        self.assertEqual(len(fake.posts), 2)
        retry_payload = fake.posts[1]["json"]
        assert isinstance(retry_payload, dict)
        self.assertTrue(retry_payload["disable_notification"])
        self.assertNotIn("message_thread_id", retry_payload)


class VixFixMuteTests(unittest.TestCase):
    def _settings(self) -> Settings:
        return Settings(
            _loaded=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_chat_vixfix_id="vixfix-topic",
        )

    def _send(self, btd_data: list[dict], str_data: list[dict]) -> list:
        settings = self._settings()
        client = MagicMock()
        client.send_message.return_value = True
        repo = MagicMock()
        repo.get_signals.side_effect = lambda kind, limit=None: (
            btd_data if kind == "btd" else str_data
        )

        with (
            patch(
                "analystbot.services.notifications.delivery.TelegramClient",
                return_value=client,
            ),
            patch(
                "analystbot.services.notifications.delivery.connect",
                return_value=MagicMock(),
            ),
            patch(
                "analystbot.services.notifications.delivery.StockRepository",
                return_value=repo,
            ),
            patch(
                "analystbot.services.notifications.delivery.already_sent",
                return_value=False,
            ),
            patch(
                "analystbot.services.notifications.delivery.mark_sent",
            ),
        ):
            self.assertTrue(NotificationService(settings).send_vixfix_watchlist())
        return client.send_message.call_args_list

    def test_nonempty_buy_notifies_and_sell_is_silent(self) -> None:
        btd = [
            {
                "symbol": "AAPL",
                "close_price": 100.0,
                "btd_22": -1.2,
                "pct_from_sma_200": -3.0,
            }
        ]
        str_rows = [
            {
                "symbol": "MSFT",
                "close_price": 200.0,
                "str_22": 1.1,
                "pct_from_sma_200": 2.0,
            }
        ]
        calls = self._send(btd, str_rows)
        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse(calls[0].kwargs["disable_notification"])
        self.assertTrue(calls[-1].kwargs["disable_notification"])

    def test_empty_buy_is_silent(self) -> None:
        calls = self._send([], [])
        self.assertTrue(calls[0].kwargs["disable_notification"])
        self.assertTrue(calls[-1].kwargs["disable_notification"])


class VixChartTopicTests(unittest.TestCase):
    def test_forced_vix_send_uses_chart_topic_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_chat_chart_id="chart-topic",
            )
            settings.data_dir = Path(temp_dir)
            graphs = settings.data_dir / "fred_graphs"
            graphs.mkdir()
            (graphs / "vix.png").write_bytes(b"png")

            client = MagicMock()
            client.send_photo.return_value = True

            with (
                patch(
                    "analystbot.services.notifications.delivery.TelegramClient",
                    return_value=client,
                ),
                patch(
                    "analystbot.services.notifications.delivery.connect",
                    return_value=MagicMock(),
                ),
                patch(
                    "analystbot.services.notifications.delivery.FredRepository",
                ),
                patch(
                    "analystbot.services.notifications.delivery.StockRepository",
                ),
                patch.object(
                    NotificationService,
                    "_build_chart_caption",
                    return_value="caption",
                ),
            ):
                self.assertTrue(NotificationService(settings).send_vix_chart_forced())

            kwargs = client.send_photo.call_args.kwargs
            self.assertEqual(kwargs["topic_id"], "chart-topic")
            self.assertTrue(kwargs["disable_notification"])

    def test_gated_vix_send_skips_when_vixcls_unchanged(self) -> None:
        settings = Settings(
            _loaded=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_chat_chart_id="chart-topic",
        )
        client = MagicMock()
        with (
            patch(
                "analystbot.services.notifications.delivery.read_fred_update_state",
                return_value={"updated_series_ids": ["UNRATE"]},
            ),
            patch(
                "analystbot.services.notifications.delivery.TelegramClient",
                return_value=client,
            ),
        ):
            self.assertTrue(NotificationService(settings).send_vix_chart())
        client.send_photo.assert_not_called()

    def test_gated_vix_send_skips_when_update_state_missing(self) -> None:
        settings = Settings(
            _loaded=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_chat_chart_id="chart-topic",
        )
        client = MagicMock()
        with (
            patch(
                "analystbot.services.notifications.delivery.read_fred_update_state",
                return_value=None,
            ),
            patch(
                "analystbot.services.notifications.delivery.TelegramClient",
                return_value=client,
            ),
        ):
            self.assertTrue(NotificationService(settings).send_vix_chart())
        client.send_photo.assert_not_called()


class HeatmapChartTopicTests(unittest.TestCase):
    def test_heatmap_send_uses_chart_topic_silently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_chat_chart_id="chart-topic",
            )
            settings.data_dir = Path(temp_dir)
            graphs = settings.data_dir / "chart_graphs"
            graphs.mkdir()
            (graphs / "market_heatmap_top100_d1.png").write_bytes(b"png")
            (graphs / "market_heatmap_top100_d1.json").write_text(
                '{"date_str":"2026-01-05","tile_count":2,"universe_count":100,'
                '"advancers":1,"decliners":1,"median_pct":0.5}',
                encoding="utf-8",
            )

            client = MagicMock()
            client.send_photo.return_value = True
            with patch(
                "analystbot.services.notifications.delivery.TelegramClient",
                return_value=client,
            ):
                self.assertTrue(
                    NotificationService(settings).send_market_heatmap_chart()
                )

            kwargs = client.send_photo.call_args.kwargs
            self.assertEqual(kwargs["topic_id"], "chart-topic")
            self.assertTrue(kwargs["disable_notification"])
            self.assertIn("Advancers / Decliners: 1 / 1", kwargs["caption"])
            self.assertIn(
                "Top 100 Market Heatmap, Daily, 2026-01-05", kwargs["caption"]
            )
            self.assertNotIn("Coverage", kwargs["caption"])
            self.assertIn("Median 1D: +0.50%", kwargs["caption"])


class ChartAlbumTests(unittest.TestCase):
    def test_two_chart_photos_use_media_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_chat_chart_id="chart-topic",
            )
            settings.data_dir = Path(temp_dir)
            graphs = settings.data_dir / "chart_graphs"
            graphs.mkdir()
            supertrend = graphs / "supertrend_spy_qqq_iwm_1y.png"
            heatmap = graphs / "market_heatmap_top100_d1.png"
            supertrend.write_bytes(b"png")
            heatmap.write_bytes(b"png")
            (graphs / "market_heatmap_top100_d1.json").write_text(
                '{"date_str":"2026-01-05","tile_count":2,"universe_count":100,'
                '"advancers":1,"decliners":1,"median_pct":0.5}',
                encoding="utf-8",
            )

            client = MagicMock()
            client.send_media_group.return_value = True
            with (
                patch(
                    "analystbot.services.notifications.delivery.TelegramClient",
                    return_value=client,
                ),
                patch.object(
                    NotificationService,
                    "_album_supertrend_item",
                    return_value=(supertrend, "supertrend-caption"),
                ),
                patch.object(
                    NotificationService,
                    "_album_heatmap_item",
                    return_value=(heatmap, "heatmap-caption"),
                ),
                patch.object(NotificationService, "_album_vix_item", return_value=None),
                patch.object(NotificationService, "_album_rrg_item", return_value=None),
            ):
                self.assertTrue(NotificationService(settings).send_chart_album())

            client.send_photo.assert_not_called()
            items = client.send_media_group.call_args.args[0]
            self.assertEqual(len(items), 2)
            self.assertTrue(
                client.send_media_group.call_args.kwargs["disable_notification"]
            )
            self.assertEqual(
                client.send_media_group.call_args.kwargs["topic_id"], "chart-topic"
            )

    def test_no_due_charts_is_success_without_send(self) -> None:
        settings = Settings(
            _loaded=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_chat_chart_id="chart-topic",
        )
        client = MagicMock()
        with patch(
            "analystbot.services.notifications.delivery.TelegramClient",
            return_value=client,
        ):
            self.assertTrue(
                NotificationService(settings).send_chart_album(succeeded_stages=[])
            )
        client.send_photo.assert_not_called()
        client.send_media_group.assert_not_called()

    def test_one_chart_photo_uses_send_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_chat_chart_id="chart-topic",
            )
            settings.data_dir = Path(temp_dir)
            heatmap = Path(temp_dir) / "heatmap.png"
            heatmap.write_bytes(b"png")
            client = MagicMock()
            client.send_photo.return_value = True
            with (
                patch(
                    "analystbot.services.notifications.delivery.TelegramClient",
                    return_value=client,
                ),
                patch.object(
                    NotificationService,
                    "_album_heatmap_item",
                    return_value=(heatmap, "heatmap-caption"),
                ),
                patch.object(
                    NotificationService, "_album_supertrend_item", return_value=None
                ),
                patch.object(NotificationService, "_album_vix_item", return_value=None),
                patch.object(NotificationService, "_album_rrg_item", return_value=None),
                patch("analystbot.services.notifications.delivery.mark_sent") as marked,
            ):
                self.assertTrue(NotificationService(settings).send_chart_album())
            client.send_media_group.assert_not_called()
            client.send_photo.assert_called_once()
            self.assertEqual(client.send_photo.call_args.args[0], heatmap)
            self.assertTrue(client.send_photo.call_args.kwargs["disable_notification"])
            marked.assert_called_once_with(settings, "heatmap")

    def test_heatmap_album_item_uses_summary_caption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            graphs = settings.data_dir / "chart_graphs"
            graphs.mkdir()
            chart = graphs / "market_heatmap_top100_d1.png"
            chart.write_bytes(b"png")
            (graphs / "market_heatmap_top100_d1.json").write_text(
                '{"date_str":"2026-01-05","tile_count":2,"universe_count":100,'
                '"advancers":1,"decliners":1,"median_pct":0.5}',
                encoding="utf-8",
            )
            item = NotificationService(settings)._album_heatmap_item(
                forced=True, succeeded_stages=None
            )
            self.assertIsNotNone(item)
            assert item is not None
            path, caption = item
            self.assertEqual(path, chart)
            self.assertIn("Advancers / Decliners: 1 / 1", caption)
            self.assertIn("Median 1D: +0.50%", caption)

    def test_supertrend_album_item_skips_missing_stage_or_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            connect(settings.db_path).close()
            service = NotificationService(settings)
            self.assertIsNone(
                service._album_supertrend_item(
                    forced=True, succeeded_stages=["Market Heatmap Chart"]
                )
            )
            self.assertIsNone(
                service._album_supertrend_item(forced=True, succeeded_stages=None)
            )
            chart = settings.data_dir / "chart_graphs"
            chart.mkdir()
            png = chart / "supertrend_spy_qqq_iwm_1y.png"
            png.write_bytes(b"png")
            item = service._album_supertrend_item(forced=True, succeeded_stages=None)
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item[0], png)
            self.assertIn("Supertrend", item[1])
            self.assertIn("n/a", item[1])

    def test_supertrend_album_item_skips_already_sent(self) -> None:
        settings = Settings(_loaded=True)
        with patch(
            "analystbot.services.notifications.delivery.already_sent",
            return_value=True,
        ):
            self.assertIsNone(
                NotificationService(settings)._album_supertrend_item(
                    forced=False, succeeded_stages=None
                )
            )

    def test_vix_album_item_skips_when_not_updated(self) -> None:
        settings = Settings(_loaded=True)
        with patch(
            "analystbot.services.notifications.delivery.read_fred_update_state",
            return_value={"updated_series_ids": []},
        ):
            self.assertIsNone(
                NotificationService(settings)._album_vix_item(
                    forced=False, succeeded_stages=None
                )
            )

    def test_rrg_album_item_skips_when_not_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            connect(settings.db_path).close()
            with patch(
                "analystbot.services.notifications.delivery.evaluate_weekly_decision",
                return_value=WeeklyDecision("not_saturday"),
            ):
                self.assertIsNone(
                    NotificationService(settings)._album_rrg_item(
                        forced=False, succeeded_stages=None
                    )
                )

    def test_rrg_album_item_forced_uses_week_ending_caption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            connect(settings.db_path).close()
            graphs = settings.data_dir / "chart_graphs"
            graphs.mkdir()
            png = graphs / "rrg_sp500_sectors_weekly.png"
            png.write_bytes(b"png")
            with patch(
                "analystbot.services.notifications.delivery.resolve_test_week_ending",
                return_value=date(2026, 10, 2),
            ):
                item = NotificationService(settings)._album_rrg_item(
                    forced=True, succeeded_stages=None
                )
            self.assertIsNotNone(item)
            assert item is not None
            (path, caption), week_ending = item
            self.assertEqual(path, png)
            self.assertEqual(week_ending.isoformat(), "2026-10-02")
            self.assertIn("2026-10-02", caption)

    def test_send_rrg_chart_skips_when_not_due(self) -> None:
        settings = Settings(
            _loaded=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_chat_chart_id="9",
        )
        with (
            patch("analystbot.services.notifications.delivery.connect") as db_connect,
            patch(
                "analystbot.services.notifications.delivery.evaluate_weekly_decision",
                return_value=WeeklyDecision("not_saturday"),
            ),
            patch(
                "analystbot.services.notifications.delivery.TelegramClient"
            ) as client_cls,
        ):
            db_connect.return_value = MagicMock()
            self.assertTrue(NotificationService(settings).send_rrg_chart())
        client_cls.assert_not_called()

    def test_send_market_supertrend_chart_requires_credentials(self) -> None:
        settings = Settings(_loaded=True)
        self.assertFalse(NotificationService(settings).send_market_supertrend_chart())
        self.assertFalse(NotificationService(settings).send_rrg_chart())
        self.assertFalse(NotificationService(settings).send_rrg_chart_forced())

    def test_send_market_supertrend_chart_posts_photo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_chat_chart_id="9",
            )
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            connect(settings.db_path).close()
            graphs = settings.data_dir / "chart_graphs"
            graphs.mkdir()
            png = graphs / "supertrend_spy_qqq_iwm_1y.png"
            png.write_bytes(b"png")
            client = MagicMock()
            client.send_photo.return_value = True
            with patch(
                "analystbot.services.notifications.delivery.TelegramClient",
                return_value=client,
            ):
                self.assertTrue(
                    NotificationService(settings).send_market_supertrend_chart()
                )
            client.send_photo.assert_called_once()
            self.assertEqual(client.send_photo.call_args.args[0], png)
            self.assertTrue(client.send_photo.call_args.kwargs["disable_notification"])

    def test_send_rrg_chart_writes_state_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_chat_chart_id="9",
            )
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            connect(settings.db_path).close()
            graphs = settings.data_dir / "chart_graphs"
            graphs.mkdir()
            png = graphs / "rrg_sp500_sectors_weekly.png"
            png.write_bytes(b"png")
            client = MagicMock()
            client.send_photo.return_value = True
            with (
                patch(
                    "analystbot.services.notifications.delivery.evaluate_weekly_decision",
                    return_value=WeeklyDecision("due", date(2026, 10, 2)),
                ),
                patch(
                    "analystbot.services.notifications.delivery.TelegramClient",
                    return_value=client,
                ),
                patch(
                    "analystbot.services.notifications.delivery.write_rrg_state"
                ) as write_state,
            ):
                self.assertTrue(NotificationService(settings).send_rrg_chart())
            write_state.assert_called_once_with(settings, date(2026, 10, 2))

    def test_send_rrg_chart_forced_does_not_write_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                telegram_bot_token="token",
                telegram_chat_id="chat",
                telegram_chat_chart_id="9",
            )
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            connect(settings.db_path).close()
            graphs = settings.data_dir / "chart_graphs"
            graphs.mkdir()
            png = graphs / "rrg_sp500_sectors_weekly.png"
            png.write_bytes(b"png")
            client = MagicMock()
            client.send_photo.return_value = True
            with (
                patch(
                    "analystbot.services.notifications.delivery.resolve_test_week_ending",
                    return_value=date(2026, 10, 2),
                ),
                patch(
                    "analystbot.services.notifications.delivery.TelegramClient",
                    return_value=client,
                ),
                patch(
                    "analystbot.services.notifications.delivery.write_rrg_state"
                ) as write_state,
            ):
                self.assertTrue(NotificationService(settings).send_rrg_chart_forced())
            write_state.assert_not_called()

    def test_run_wrappers_delegate_to_service(self) -> None:
        settings = Settings(_loaded=True)
        with patch.object(
            NotificationService, "send_vixfix_watchlist", return_value=True
        ) as method:
            self.assertTrue(run_vixfix(settings))
        method.assert_called_once()
        with patch.object(
            NotificationService, "send_vix_chart", return_value=False
        ) as method:
            self.assertFalse(run_vix_telegram(settings))
        method.assert_called_once()
        with patch.object(
            NotificationService, "send_market_supertrend_chart", return_value=True
        ):
            self.assertTrue(run_market_supertrend_telegram(settings))
        with patch.object(
            NotificationService, "send_market_heatmap_chart", return_value=True
        ):
            self.assertTrue(run_market_heatmap_telegram(settings))
        with patch.object(NotificationService, "send_rrg_chart", return_value=True):
            self.assertTrue(run_rrg_telegram(settings))
        with patch.object(
            NotificationService, "send_rrg_chart_forced", return_value=True
        ):
            self.assertTrue(run_rrg_telegram_forced(settings))

    def test_run_delivery_skips_fred_when_graphs_did_not_succeed(self) -> None:
        settings = Settings(_loaded=True)
        with (
            patch.object(
                NotificationService, "send_vixfix_watchlist", return_value=True
            ),
            patch.object(
                NotificationService, "send_fred_charts", return_value=False
            ) as fred,
            patch.object(
                NotificationService, "send_chart_album", return_value=True
            ) as album,
        ):
            self.assertTrue(
                run_delivery(settings, succeeded_stages=["Market Supertrend Charts"])
            )
        fred.assert_not_called()
        album.assert_called_once()
        self.assertEqual(
            album.call_args.kwargs["succeeded_stages"],
            ["Market Supertrend Charts"],
        )

    def test_run_chart_album_forced_passes_include(self) -> None:
        settings = Settings(_loaded=True)
        with patch.object(
            NotificationService, "send_chart_album", return_value=True
        ) as album:
            self.assertTrue(run_chart_album_forced(settings, include=["vix"]))
        self.assertEqual(album.call_args.kwargs["include"], ["vix"])
        self.assertTrue(album.call_args.kwargs["forced"])


class SessionSkipTests(unittest.TestCase):
    def test_already_delivered_session_skips_vixfix(self) -> None:
        settings = Settings(
            _loaded=True,
            telegram_bot_token="token",
            telegram_chat_id="chat",
            telegram_chat_vixfix_id="vixfix-topic",
        )
        client = MagicMock()
        with (
            patch(
                "analystbot.services.notifications.delivery.already_sent",
                return_value=True,
            ),
            patch(
                "analystbot.services.notifications.delivery.TelegramClient",
                return_value=client,
            ),
        ):
            self.assertTrue(NotificationService(settings).send_vixfix_watchlist())
        client.send_message.assert_not_called()


class TelegramAlbumClientTests(unittest.TestCase):
    def test_send_media_group_posts_attach_urls(self) -> None:
        client = TelegramClient("token", "chat")
        fake = FakeHttp()
        client._http = fake
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "a.png"
            second = Path(temp_dir) / "b.png"
            first.write_bytes(b"png")
            second.write_bytes(b"png")
            self.assertTrue(
                client.send_media_group(
                    [(first, "one"), (second, "two")],
                    topic_id="9",
                    disable_notification=True,
                )
            )
        form = fake.posts[0]["data"]
        assert isinstance(form, dict)
        self.assertEqual(form["disable_notification"], "true")
        self.assertEqual(form["message_thread_id"], "9")
        self.assertIn("attach://photo0", form["media"])
        self.assertIn("attach://photo1", form["media"])

    def test_send_media_group_requires_two_photos(self) -> None:
        client = TelegramClient("token", "chat")
        with tempfile.TemporaryDirectory() as temp_dir:
            photo = Path(temp_dir) / "a.png"
            photo.write_bytes(b"png")
            with self.assertRaises(ValueError):
                client.send_media_group([(photo, "one")])

    def test_send_media_group_missing_file_returns_false(self) -> None:
        client = TelegramClient("token", "chat")
        missing = Path("/tmp/analystbot-missing-album-a.png")
        missing2 = Path("/tmp/analystbot-missing-album-b.png")
        self.assertFalse(client.send_media_group([(missing, "one"), (missing2, "two")]))

    def test_send_media_group_topic_fallback(self) -> None:
        client = TelegramClient("token", "chat")
        fake = FakeHttp()
        posts: list[dict[str, object]] = []

        def post(url, json=None, data=None, files=None):
            posts.append({"url": url, "data": data})
            if data and data.get("message_thread_id"):
                return FakeResponse(status_code=400)
            return FakeResponse()

        fake.post = post
        client._http = fake
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "a.png"
            second = Path(temp_dir) / "b.png"
            first.write_bytes(b"png")
            second.write_bytes(b"png")
            self.assertTrue(
                client.send_media_group(
                    [(first, "one"), (second, "two")],
                    topic_id="9",
                    disable_notification=True,
                )
            )
        self.assertEqual(len(posts), 2)
        first_form = posts[0]["data"]
        second_form = posts[1]["data"]
        assert isinstance(first_form, dict)
        assert isinstance(second_form, dict)
        self.assertEqual(first_form["message_thread_id"], "9")
        self.assertNotIn("message_thread_id", second_form)


if __name__ == "__main__":
    unittest.main()
