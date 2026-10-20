from datetime import date, datetime
from pathlib import Path

from analystbot.api.fred import NEXT_RELEASE_SERIES_IDS
from analystbot.api.http_client import SessionManager
from analystbot.api.telegram import TelegramClient
from analystbot.core.config import Settings
from analystbot.core.log_config import log_script_end, log_script_start, setup_logger
from analystbot.db import FredRepository, StockRepository, connect
from analystbot.db.date_utils import unix_to_date_str
from analystbot.services.charting import get_vix_chart_path
from analystbot.services.fred_chart_catalog import (
    FRED_COMBINED_PLOT_CONFIGS,
    FRED_PLOT_CONFIGS,
    JOBS_COMBINED_ID,
    JOBS_SERIES_IDS,
    RATES_COMBINED_ID,
    RATES_SERIES_IDS,
    VIX_PLOT_CONFIG,
    VIX_SERIES_ID,
    FredCombinedPlotConfig,
    SeriesPlotConfig,
    plot_config_for,
    select_fred_chart_work,
)
from analystbot.services.fred_data import (
    read_fred_release_calendar,
    read_fred_update_state,
    upcoming_release_date,
)
from analystbot.services.market_charts import (
    MARKET_CHART_SYMBOLS,
    MarketChartService,
    get_output_path,
)
from analystbot.services.notifications.formatters import (
    MessageGenerator,
    format_image_caption,
)
from analystbot.services.period_headings import (
    format_combined_heading,
    format_observation_value,
    format_period_heading,
)
from analystbot.services.rrg_charts import (
    RrgChartService,
    evaluate_weekly_decision,
    format_rrg_heading,
    resolve_run_timezone,
    resolve_test_week_ending,
    write_rrg_state,
)
from analystbot.services.rrg_charts import (
    get_output_path as get_rrg_output_path,
)
from analystbot.services.session_delivery import already_sent, mark_sent

logger = setup_logger("services.notifications")

_DAILY_CLOSE_SERIES = frozenset({"T10Y2Y", "DGS10", "VIXCLS"})


def _caption_next_releases(
    series_ids: list[str],
    calendar: dict[str, str],
    *,
    labels: dict[str, str] | None = None,
    as_of: date | None = None,
) -> list[tuple[str | None, str]]:
    dated: list[tuple[str, str]] = []
    for series_id in series_ids:
        if series_id not in NEXT_RELEASE_SERIES_IDS or series_id not in calendar:
            continue
        upcoming = upcoming_release_date(calendar[series_id], as_of=as_of)
        if upcoming is not None:
            dated.append((series_id, upcoming))
    if not dated:
        return []
    unique_dates = {release_date for _series_id, release_date in dated}
    if len(unique_dates) == 1:
        return [(None, next(iter(unique_dates)))]
    lines: list[tuple[str | None, str]] = []
    for series_id, upcoming in dated:
        label = (labels or {}).get(series_id) or series_id
        lines.append((label, upcoming))
    return lines


class NotificationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_vixfix_watchlist(self) -> bool:
        if already_sent(self.settings, "vixfix"):
            logger.info("VixFix already delivered for the latest session; skipping")
            return True

        is_valid, missing = self.settings.validate("telegram")
        if not is_valid:
            logger.error(f"Missing Telegram credentials: {missing}")
            return False

        try:
            session = SessionManager.get_session()

            assert self.settings.telegram_bot_token is not None
            assert self.settings.telegram_chat_id is not None
            client = TelegramClient(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
                session=session,
            )

            conn = connect(self.settings.db_path)
            repo = StockRepository(conn)

            try:
                btd_data = repo.get_signals("btd", limit=None)
                str_data = repo.get_signals("str", limit=None)
            finally:
                conn.close()

            logger.info(
                f"Retrieved {len(btd_data)} BTD signals, {len(str_data)} STR signals"
            )

            all_success = True
            messages_sent = 0

            buy_silent = not btd_data
            buy_chunks = MessageGenerator.generate_buy_message(btd_data)
            logger.info(f"Generated {len(buy_chunks)} buy message chunk(s)")

            for i, chunk in enumerate(buy_chunks):
                logger.debug(
                    f"Sending buy chunk {i + 1}/{len(buy_chunks)} ({len(chunk)} chars)"
                )
                success = client.send_message(
                    chunk,
                    topic_id=self.settings.telegram_chat_vixfix_id,
                    disable_notification=buy_silent,
                )
                if success:
                    messages_sent += 1
                else:
                    logger.error(f"Failed to send buy chunk {i + 1}")
                    all_success = False

            sell_chunks = MessageGenerator.generate_sell_message(str_data)
            logger.info(f"Generated {len(sell_chunks)} sell message chunk(s)")

            for i, chunk in enumerate(sell_chunks):
                logger.debug(
                    f"Sending sell chunk {i + 1}/{len(sell_chunks)} ({len(chunk)} chars)"
                )
                success = client.send_message(
                    chunk,
                    topic_id=self.settings.telegram_chat_vixfix_id,
                    disable_notification=True,
                )
                if success:
                    messages_sent += 1
                else:
                    logger.error(f"Failed to send sell chunk {i + 1}")
                    all_success = False

            if all_success:
                logger.info(
                    f"VixFix watchlist sent successfully ({messages_sent} messages)"
                )
                mark_sent(self.settings, "vixfix")
            else:
                logger.error(
                    f"Some messages failed to send ({messages_sent} succeeded)"
                )

            return all_success

        except Exception as e:
            logger.error(f"Error sending watchlist: {e}", exc_info=True)
            return False

    def send_fred_charts(self) -> bool:
        is_valid, missing = self.settings.validate("fred_telegram")
        if not is_valid:
            logger.warning("FRED Telegram not configured - skipping (non-critical)")
            return True

        try:
            update_state = read_fred_update_state(self.settings)
            updated_series_ids = (
                set(update_state["updated_series_ids"])
                if update_state is not None
                else None
            )
            selection = select_fred_chart_work(updated_series_ids)

            if not selection.send_individuals and not selection.send_combined:
                logger.info("No charts to send")
                return True

            return self._send_fred_targets(
                list(selection.send_individuals),
                list(selection.send_combined),
            )

        except Exception as e:
            logger.error(f"Error sending FRED charts: {e}", exc_info=True)
            return False

    def send_fred_charts_forced(self, series_ids: list[str]) -> bool:
        is_valid, missing = self.settings.validate("fred_telegram")
        if not is_valid:
            logger.error(f"Missing FRED Telegram credentials: {missing}")
            return False

        try:
            wanted = set(series_ids)
            grouped_ids = JOBS_SERIES_IDS | RATES_SERIES_IDS
            chart_configs = [
                config
                for config in FRED_PLOT_CONFIGS
                if config.series_id in wanted and config.series_id not in grouped_ids
            ]
            combined_configs: list[FredCombinedPlotConfig] = []
            for combined_id, members in (
                (JOBS_COMBINED_ID, JOBS_SERIES_IDS),
                (RATES_COMBINED_ID, RATES_SERIES_IDS),
            ):
                if wanted & members:
                    combined_configs.extend(
                        config
                        for config in FRED_COMBINED_PLOT_CONFIGS
                        if config.combined_id == combined_id
                    )
            if not chart_configs and not combined_configs:
                logger.warning(f"No FRED chart configs match {series_ids}")
                return False

            return self._send_fred_targets(chart_configs, combined_configs)

        except Exception as e:
            logger.error(f"Error sending forced FRED charts: {e}", exc_info=True)
            return False

    def _send_fred_targets(
        self,
        chart_configs: list[SeriesPlotConfig],
        combined_configs: list[FredCombinedPlotConfig],
    ) -> bool:
        session = SessionManager.get_session()

        assert self.settings.telegram_bot_token is not None
        assert self.settings.telegram_chat_id is not None
        client = TelegramClient(
            self.settings.telegram_bot_token,
            self.settings.telegram_chat_id,
            session=session,
        )

        graphs_dir = self.settings.data_dir / "fred_graphs"

        if not graphs_dir.exists():
            logger.warning(f"Graphs directory not found: {graphs_dir}")
            return True

        conn = connect(self.settings.db_path)
        fred_repo = FredRepository(conn)
        stock_repo = StockRepository(conn)

        items: list[tuple[Path, str]] = []
        failed = 0

        try:
            for config in chart_configs:
                chart_path = graphs_dir / config.filename

                if not chart_path.exists():
                    logger.warning(f"Chart not found: {chart_path}")
                    failed += 1
                    continue

                items.append(
                    (
                        chart_path,
                        self._build_chart_caption(config, fred_repo, stock_repo),
                    )
                )

            for combined in combined_configs:
                chart_path = graphs_dir / combined.filename

                if not chart_path.exists():
                    logger.warning(f"Chart not found: {chart_path}")
                    failed += 1
                    continue

                items.append(
                    (chart_path, self._build_combined_caption(combined, fred_repo))
                )

        finally:
            conn.close()

        if not items:
            logger.info(f"FRED charts: sent=0, failed={failed}")
            return failed == 0

        topic_id = self.settings.telegram_chat_fred_id
        if len(items) == 1:
            path, caption = items[0]
            success = client.send_photo(
                path,
                caption=caption,
                topic_id=topic_id,
                disable_notification=True,
            )
        else:
            success = client.send_media_group(
                items,
                topic_id=topic_id,
                disable_notification=True,
            )

        sent = len(items) if success else 0
        if not success:
            failed += len(items)
        logger.info(f"FRED charts: sent={sent}, failed={failed}")
        return sent > 0 or failed == 0

    def _build_chart_caption(
        self,
        config: SeriesPlotConfig,
        fred_repo: FredRepository,
        stock_repo: StockRepository,
    ) -> str:
        date_str: str | None = None
        value: float | None = None
        if config.source == "stock" and config.symbol:
            latest_ts = stock_repo.get_latest_timestamp(config.symbol)
            if latest_ts is not None:
                rows = stock_repo.get_stock_data(config.symbol, days=1)
                if rows:
                    date_str = unix_to_date_str(rows[-1]["date"])
                    value = rows[-1]["close_price"]
        else:
            latest = fred_repo.get_latest_observation(config.series_id)
            if latest:
                date_str, value = latest

        if date_str is None or value is None:
            return config.title

        suffix = config.value_suffix
        daily_close = _DAILY_CLOSE_SERIES
        key = "Last close" if config.series_id in daily_close else "Latest"
        return format_image_caption(
            format_period_heading(config.title, config.frequency, date_str),
            date_str,
            [(key, format_observation_value(value, suffix))],
            next_releases=_caption_next_releases(
                [config.series_id], read_fred_release_calendar(self.settings)
            ),
        )

    def _build_combined_caption(
        self,
        config: FredCombinedPlotConfig,
        fred_repo: FredRepository,
    ) -> str:
        lines: list[tuple[str, str]] = []
        dates: list[str] = []
        series_ids: list[str] = []
        for spec in config.series:
            plot = plot_config_for(spec.series_id)
            series_ids.append(spec.series_id)
            latest = fred_repo.get_latest_observation(spec.series_id)
            if latest is None:
                continue
            date_str, value = latest
            if value is None:
                continue
            dates.append(date_str)
            lines.append(
                (spec.label, format_observation_value(value, plot.value_suffix))
            )
        if not dates:
            return config.title
        return format_image_caption(
            format_combined_heading(
                config.title,
                [plot_config_for(spec.series_id).frequency for spec in config.series],
                max(dates),
            ),
            max(dates),
            lines,
            next_releases=_caption_next_releases(
                series_ids,
                read_fred_release_calendar(self.settings),
                labels={spec.series_id: spec.label for spec in config.series},
            ),
        )

    def send_vix_chart(self) -> bool:
        is_valid, missing = self.settings.validate("chart_telegram")
        if not is_valid:
            logger.error(f"Missing chart Telegram credentials: {missing}")
            return False

        try:
            update_state = read_fred_update_state(self.settings)
            updated_series_ids = (
                set(update_state["updated_series_ids"])
                if update_state is not None
                else set()
            )
            if VIX_SERIES_ID not in updated_series_ids:
                logger.info("VIX chart delivery not due; skipping")
                return True

            return self._send_vix_chart_photo()
        except Exception as e:
            logger.error(f"Error sending VIX chart: {e}", exc_info=True)
            return False

    def send_vix_chart_forced(self) -> bool:
        is_valid, missing = self.settings.validate("chart_telegram")
        if not is_valid:
            logger.error(f"Missing chart Telegram credentials: {missing}")
            return False

        try:
            return self._send_vix_chart_photo()
        except Exception as e:
            logger.error(f"Error sending forced VIX chart: {e}", exc_info=True)
            return False

    def _send_vix_chart_photo(self) -> bool:
        chart_path = get_vix_chart_path(self.settings)
        if not chart_path.exists():
            logger.error(f"VIX chart not found: {chart_path}")
            return False

        session = SessionManager.get_session()

        assert self.settings.telegram_bot_token is not None
        assert self.settings.telegram_chat_id is not None
        client = TelegramClient(
            self.settings.telegram_bot_token,
            self.settings.telegram_chat_id,
            session=session,
        )

        conn = connect(self.settings.db_path)
        try:
            caption = self._build_chart_caption(
                VIX_PLOT_CONFIG, FredRepository(conn), StockRepository(conn)
            )
        finally:
            conn.close()

        return client.send_photo(
            chart_path,
            caption=caption,
            topic_id=self.settings.telegram_chat_chart_id,
            disable_notification=True,
        )

    def send_market_supertrend_chart(self) -> bool:
        is_valid, missing = self.settings.validate("chart_telegram")
        if not is_valid:
            logger.error(f"Missing chart Telegram credentials: {missing}")
            return False

        try:
            session = SessionManager.get_session()

            assert self.settings.telegram_bot_token is not None
            assert self.settings.telegram_chat_id is not None
            client = TelegramClient(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
                session=session,
            )

            chart_path = get_output_path(self.settings)
            if not chart_path.exists():
                logger.error(f"Market chart not found: {chart_path}")
                return False

            chart_service = MarketChartService(self.settings)
            conn = connect(self.settings.db_path)
            try:
                summaries = chart_service.compute_trend_summaries(StockRepository(conn))
            finally:
                conn.close()

            symbols_label = " / ".join(MARKET_CHART_SYMBOLS)
            title = (
                f"Supertrend ({chart_service.config.atr_period}, "
                f"{chart_service.config.factor:.1f}), 1 Year, Daily, "
                f"{symbols_label}"
            )
            summary_dates = [s["date"] for s in summaries.values()]
            date_str = (
                max(summary_dates)
                if summary_dates
                else (self._get_latest_market_chart_date() or "N/A")
            )
            lines: list[tuple[str, str]] = []
            for symbol in MARKET_CHART_SYMBOLS:
                summary = summaries.get(symbol)
                if summary is not None:
                    lines.append(
                        (symbol, f"{summary['emoji']} for {summary['days']} days")
                    )
                else:
                    lines.append((symbol, "n/a"))
            caption = format_image_caption(title, date_str, lines)

            return client.send_photo(
                chart_path,
                caption=caption,
                topic_id=self.settings.telegram_chat_chart_id,
                disable_notification=True,
            )
        except Exception as e:
            logger.error(f"Error sending market chart: {e}", exc_info=True)
            return False

    def send_market_heatmap_chart(self) -> bool:
        from analystbot.services.heatmap_charts import (
            HEATMAP_TITLE,
            read_heatmap_summary,
        )
        from analystbot.services.heatmap_charts import (
            get_output_path as get_heatmap_output_path,
        )
        from analystbot.services.heatmap_charts import (
            get_summary_path as get_heatmap_summary_path,
        )

        is_valid, missing = self.settings.validate("chart_telegram")
        if not is_valid:
            logger.error(f"Missing chart Telegram credentials: {missing}")
            return False

        try:
            session = SessionManager.get_session()

            assert self.settings.telegram_bot_token is not None
            assert self.settings.telegram_chat_id is not None
            client = TelegramClient(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
                session=session,
            )

            chart_path = get_heatmap_output_path(self.settings)
            if not chart_path.exists():
                logger.error(f"Market heatmap not found: {chart_path}")
                return False

            summary = read_heatmap_summary(get_heatmap_summary_path(self.settings))
            if summary is None:
                caption = format_image_caption(
                    format_period_heading(HEATMAP_TITLE, "d", "N/A"), "N/A"
                )
            else:
                lines: list[tuple[str, str]] = [
                    (
                        "Advancers / Decliners",
                        f"{summary.advancers} / {summary.decliners}",
                    ),
                ]
                if summary.median_pct is not None:
                    lines.append(("Median 1D", f"{summary.median_pct:+.2f}%"))
                caption = format_image_caption(
                    format_period_heading(HEATMAP_TITLE, "d", summary.date_str),
                    summary.date_str,
                    lines,
                )

            return client.send_photo(
                chart_path,
                caption=caption,
                topic_id=self.settings.telegram_chat_chart_id,
                disable_notification=True,
            )
        except Exception as e:
            logger.error(f"Error sending market heatmap: {e}", exc_info=True)
            return False

    def _get_latest_market_chart_date(self) -> str | None:
        conn = connect(self.settings.db_path)
        repo = StockRepository(conn)
        try:
            latest_timestamps = [
                ts
                for symbol in MARKET_CHART_SYMBOLS
                if (ts := repo.get_latest_timestamp(symbol)) is not None
            ]
        finally:
            conn.close()

        if not latest_timestamps:
            return None

        latest_ts = max(latest_timestamps)
        return unix_to_date_str(latest_ts)

    def send_rrg_chart(self) -> bool:
        is_valid, missing = self.settings.validate("chart_telegram")
        if not is_valid:
            logger.error(f"Missing chart Telegram credentials: {missing}")
            return False

        try:
            now = datetime.now(resolve_run_timezone(self.settings))

            service = RrgChartService(self.settings)
            conn = connect(self.settings.db_path)
            try:
                repo = StockRepository(conn)
                frame = service.build_weekly_frame(repo)
            finally:
                conn.close()

            decision = evaluate_weekly_decision(self.settings, frame, now)
            if decision.action != "due":
                logger.info(
                    "RRG chart delivery not due (%s); skipping", decision.action
                )
                return True

            chart_path = get_rrg_output_path(self.settings)
            if not chart_path.exists():
                logger.error(f"RRG chart not found: {chart_path}")
                return False

            session = SessionManager.get_session()

            assert self.settings.telegram_bot_token is not None
            assert self.settings.telegram_chat_id is not None
            client = TelegramClient(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
                session=session,
            )

            assert decision.week_ending is not None
            caption = format_image_caption(
                format_rrg_heading(decision.week_ending.isoformat()),
                decision.week_ending.isoformat(),
            )
            success = client.send_photo(
                chart_path,
                caption=caption,
                topic_id=self.settings.telegram_chat_chart_id,
                disable_notification=True,
            )

            if success:
                write_rrg_state(self.settings, decision.week_ending)

            return success
        except Exception as e:
            logger.error(f"Error sending RRG chart: {e}", exc_info=True)
            return False

    def send_rrg_chart_forced(self) -> bool:
        is_valid, missing = self.settings.validate("chart_telegram")
        if not is_valid:
            logger.error(f"Missing chart Telegram credentials: {missing}")
            return False

        try:
            conn = connect(self.settings.db_path)
            try:
                repo = StockRepository(conn)
                week_ending = resolve_test_week_ending(self.settings, repo)
            finally:
                conn.close()
            if week_ending is None:
                logger.error("No weekly data available for RRG chart")
                return False

            chart_path = get_rrg_output_path(self.settings)
            if not chart_path.exists():
                logger.error(f"RRG chart not found: {chart_path}")
                return False

            session = SessionManager.get_session()

            assert self.settings.telegram_bot_token is not None
            assert self.settings.telegram_chat_id is not None
            client = TelegramClient(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
                session=session,
            )

            caption = format_image_caption(
                format_rrg_heading(week_ending.isoformat()),
                week_ending.isoformat(),
            )
            return client.send_photo(
                chart_path,
                caption=caption,
                topic_id=self.settings.telegram_chat_chart_id,
                disable_notification=True,
            )
        except Exception as e:
            logger.error(f"Error sending forced RRG chart: {e}", exc_info=True)
            return False

    def _stage_succeeded(self, succeeded_stages: list[str] | None, *names: str) -> bool:
        if succeeded_stages is None:
            return True
        succeeded = set(succeeded_stages)
        return any(name in succeeded for name in names)

    def _album_supertrend_item(
        self, *, forced: bool, succeeded_stages: list[str] | None
    ) -> tuple[Path, str] | None:
        if not self._stage_succeeded(succeeded_stages, "Market Supertrend Charts"):
            return None
        if not forced and already_sent(self.settings, "supertrend"):
            logger.info("Supertrend already delivered for the latest session; skipping")
            return None
        chart_path = get_output_path(self.settings)
        if not chart_path.exists():
            logger.warning("Market chart not found: %s", chart_path)
            return None
        chart_service = MarketChartService(self.settings)
        conn = connect(self.settings.db_path)
        try:
            summaries = chart_service.compute_trend_summaries(StockRepository(conn))
        finally:
            conn.close()
        symbols_label = " / ".join(MARKET_CHART_SYMBOLS)
        title = (
            f"Supertrend ({chart_service.config.atr_period}, "
            f"{chart_service.config.factor:.1f}), 1 Year, Daily, "
            f"{symbols_label}"
        )
        summary_dates = [s["date"] for s in summaries.values()]
        date_str = (
            max(summary_dates)
            if summary_dates
            else (self._get_latest_market_chart_date() or "N/A")
        )
        lines: list[tuple[str, str]] = []
        for symbol in MARKET_CHART_SYMBOLS:
            summary = summaries.get(symbol)
            if summary is not None:
                lines.append((symbol, f"{summary['emoji']} for {summary['days']} days"))
            else:
                lines.append((symbol, "n/a"))
        return chart_path, format_image_caption(title, date_str, lines)

    def _album_heatmap_item(
        self, *, forced: bool, succeeded_stages: list[str] | None
    ) -> tuple[Path, str] | None:
        from analystbot.services.heatmap_charts import (
            HEATMAP_TITLE,
            read_heatmap_summary,
        )
        from analystbot.services.heatmap_charts import (
            get_output_path as get_heatmap_output_path,
        )
        from analystbot.services.heatmap_charts import (
            get_summary_path as get_heatmap_summary_path,
        )

        if not self._stage_succeeded(succeeded_stages, "Market Heatmap Chart"):
            return None
        if not forced and already_sent(self.settings, "heatmap"):
            logger.info("Heatmap already delivered for the latest session; skipping")
            return None
        chart_path = get_heatmap_output_path(self.settings)
        if not chart_path.exists():
            logger.warning("Market heatmap not found: %s", chart_path)
            return None
        summary = read_heatmap_summary(get_heatmap_summary_path(self.settings))
        if summary is None:
            caption = format_image_caption(
                format_period_heading(HEATMAP_TITLE, "d", "N/A"), "N/A"
            )
        else:
            lines: list[tuple[str, str]] = [
                (
                    "Advancers / Decliners",
                    f"{summary.advancers} / {summary.decliners}",
                ),
            ]
            if summary.median_pct is not None:
                lines.append(("Median 1D", f"{summary.median_pct:+.2f}%"))
            caption = format_image_caption(
                format_period_heading(HEATMAP_TITLE, "d", summary.date_str),
                summary.date_str,
                lines,
            )
        return chart_path, caption

    def _album_vix_item(
        self, *, forced: bool, succeeded_stages: list[str] | None
    ) -> tuple[Path, str] | None:
        if not self._stage_succeeded(succeeded_stages, "FRED Graphs", "VIX Chart"):
            return None
        if not forced:
            update_state = read_fred_update_state(self.settings)
            updated_series_ids = (
                set(update_state["updated_series_ids"])
                if update_state is not None
                else set()
            )
            if VIX_SERIES_ID not in updated_series_ids:
                logger.info("VIX chart delivery not due; skipping")
                return None
        chart_path = get_vix_chart_path(self.settings)
        if not chart_path.exists():
            logger.warning("VIX chart not found: %s", chart_path)
            return None
        conn = connect(self.settings.db_path)
        try:
            caption = self._build_chart_caption(
                VIX_PLOT_CONFIG, FredRepository(conn), StockRepository(conn)
            )
        finally:
            conn.close()
        return chart_path, caption

    def _album_rrg_item(
        self, *, forced: bool, succeeded_stages: list[str] | None
    ) -> tuple[tuple[Path, str], date] | None:
        if not self._stage_succeeded(succeeded_stages, "Sector RRG Chart"):
            return None
        service = RrgChartService(self.settings)
        conn = connect(self.settings.db_path)
        try:
            repo = StockRepository(conn)
            if forced:
                week_ending = resolve_test_week_ending(self.settings, repo)
            else:
                now = datetime.now(resolve_run_timezone(self.settings))
                frame = service.build_weekly_frame(repo)
                decision = evaluate_weekly_decision(self.settings, frame, now)
                if decision.action != "due":
                    logger.info(
                        "RRG chart delivery not due (%s); skipping", decision.action
                    )
                    return None
                week_ending = decision.week_ending
        finally:
            conn.close()
        if week_ending is None:
            logger.warning("No weekly data available for RRG chart")
            return None
        chart_path = get_rrg_output_path(self.settings)
        if not chart_path.exists():
            logger.warning("RRG chart not found: %s", chart_path)
            return None
        caption = format_image_caption(
            format_rrg_heading(week_ending.isoformat()),
            week_ending.isoformat(),
        )
        return (chart_path, caption), week_ending

    def send_chart_album(
        self,
        *,
        forced: bool = False,
        include: list[str] | None = None,
        succeeded_stages: list[str] | None = None,
    ) -> bool:
        wanted = set(include or ("supertrend", "heatmap", "vix", "rrg"))
        try:
            items: list[tuple[Path, str]] = []
            market_items: list[str] = []
            rrg_week: date | None = None

            if "supertrend" in wanted:
                spec = self._album_supertrend_item(
                    forced=forced, succeeded_stages=succeeded_stages
                )
                if spec is not None:
                    items.append(spec)
                    market_items.append("supertrend")
            if "heatmap" in wanted:
                spec = self._album_heatmap_item(
                    forced=forced, succeeded_stages=succeeded_stages
                )
                if spec is not None:
                    items.append(spec)
                    market_items.append("heatmap")
            if "vix" in wanted:
                spec = self._album_vix_item(
                    forced=forced, succeeded_stages=succeeded_stages
                )
                if spec is not None:
                    items.append(spec)
            if "rrg" in wanted:
                rrg_spec = self._album_rrg_item(
                    forced=forced, succeeded_stages=succeeded_stages
                )
                if rrg_spec is not None:
                    spec, rrg_week = rrg_spec
                    items.append(spec)

            if not items:
                logger.info("No chart-topic images to send")
                return True

            is_valid, missing = self.settings.validate("chart_telegram")
            if not is_valid:
                logger.error("Missing chart Telegram credentials: %s", missing)
                return False

            session = SessionManager.get_session()
            assert self.settings.telegram_bot_token is not None
            assert self.settings.telegram_chat_id is not None
            client = TelegramClient(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
                session=session,
            )
            if len(items) == 1:
                path, caption = items[0]
                success = client.send_photo(
                    path,
                    caption=caption,
                    topic_id=self.settings.telegram_chat_chart_id,
                    disable_notification=True,
                )
            else:
                success = client.send_media_group(
                    items,
                    topic_id=self.settings.telegram_chat_chart_id,
                    disable_notification=True,
                )
            if success and not forced:
                for item in market_items:
                    mark_sent(self.settings, item)
                if rrg_week is not None:
                    write_rrg_state(self.settings, rrg_week)
            return success
        except Exception as e:
            logger.error("Error sending chart album: %s", e, exc_info=True)
            return False


def run_vixfix(settings: Settings) -> bool:
    log_script_start(logger, "Telegram Messaging Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_vixfix_watchlist()

        log_script_end(logger, "Telegram Messaging Workflow", start_time, success)
        return success

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "Telegram Messaging Workflow", start_time, False)
        return False


def run_fred_telegram(settings: Settings) -> bool:
    log_script_start(logger, "FRED Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_fred_charts()

        log_script_end(logger, "FRED Telegram Workflow", start_time, success)
        return success

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "FRED Telegram Workflow", start_time, False)
        return False


def run_fred_telegram_forced(settings: Settings, series_ids: list[str]) -> bool:
    log_script_start(logger, "FRED Telegram Workflow (forced)")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_fred_charts_forced(series_ids)

        log_script_end(logger, "FRED Telegram Workflow (forced)", start_time, success)
        return success

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "FRED Telegram Workflow (forced)", start_time, False)
        return False


def run_vix_telegram(settings: Settings) -> bool:
    log_script_start(logger, "VIX Chart Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_vix_chart()

        log_script_end(logger, "VIX Chart Telegram Workflow", start_time, success)
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "VIX Chart Telegram Workflow", start_time, False)
        return False


def run_vix_telegram_forced(settings: Settings) -> bool:
    log_script_start(logger, "VIX Chart Forced Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_vix_chart_forced()

        log_script_end(
            logger, "VIX Chart Forced Telegram Workflow", start_time, success
        )
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "VIX Chart Forced Telegram Workflow", start_time, False)
        return False


def run_market_supertrend_telegram(settings: Settings) -> bool:
    log_script_start(logger, "Market Chart Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_market_supertrend_chart()

        log_script_end(logger, "Market Chart Telegram Workflow", start_time, success)
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "Market Chart Telegram Workflow", start_time, False)
        return False


def run_rrg_telegram(settings: Settings) -> bool:
    log_script_start(logger, "RRG Chart Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_rrg_chart()

        log_script_end(logger, "RRG Chart Telegram Workflow", start_time, success)
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "RRG Chart Telegram Workflow", start_time, False)
        return False


def run_market_heatmap_telegram(settings: Settings) -> bool:
    log_script_start(logger, "Market Heatmap Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_market_heatmap_chart()

        log_script_end(logger, "Market Heatmap Telegram Workflow", start_time, success)
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "Market Heatmap Telegram Workflow", start_time, False)
        return False


def run_rrg_telegram_forced(settings: Settings) -> bool:
    log_script_start(logger, "RRG Chart Forced Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_rrg_chart_forced()

        log_script_end(
            logger, "RRG Chart Forced Telegram Workflow", start_time, success
        )
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "RRG Chart Forced Telegram Workflow", start_time, False)
        return False


def run_delivery(
    settings: Settings,
    succeeded_stages: list[str] | None = None,
) -> bool:
    log_script_start(logger, "Telegram Delivery Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        vixfix_ok = service.send_vixfix_watchlist()
        if succeeded_stages is None or "FRED Graphs" in succeeded_stages:
            fred_ok = service.send_fred_charts()
        else:
            logger.info("Skipping FRED Telegram: FRED Graphs did not succeed")
            fred_ok = True
        album_ok = service.send_chart_album(succeeded_stages=succeeded_stages)
        success = vixfix_ok and fred_ok and album_ok
        log_script_end(logger, "Telegram Delivery Workflow", start_time, success)
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(logger, "Telegram Delivery Workflow", start_time, False)
        return False


def run_chart_album_forced(
    settings: Settings,
    include: list[str] | None = None,
    forced: bool = True,
    succeeded_stages: list[str] | None = None,
) -> bool:
    log_script_start(logger, "Chart Album Forced Telegram Workflow")
    start_time = datetime.now()

    try:
        service = NotificationService(settings)
        success = service.send_chart_album(
            forced=forced,
            include=include,
            succeeded_stages=succeeded_stages,
        )
        log_script_end(
            logger, "Chart Album Forced Telegram Workflow", start_time, success
        )
        return success
    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        log_script_end(
            logger, "Chart Album Forced Telegram Workflow", start_time, False
        )
        return False
