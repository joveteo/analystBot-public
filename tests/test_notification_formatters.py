from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from analystbot.services.notifications.formatters import (
    MessageGenerator,
    TableFormatter,
    format_image_caption,
    format_next_release_date,
)
from analystbot.services.period_headings import (
    format_combined_heading,
    format_observation_value,
    format_period_heading,
)


class FormatImageCaptionTests(unittest.TestCase):
    def test_title_and_date_only(self) -> None:
        caption = format_image_caption("VIX Chart", "2026-07-10")
        self.assertEqual(caption, "VIX Chart\nDate: 2026-07-10")

    def test_observation_value_uses_thousands_separator(self) -> None:
        self.assertEqual(format_observation_value(159000.0, ""), "159,000")
        self.assertEqual(format_observation_value(4.1, "%"), "4.10%")

    def test_period_heading_joins_title_frequency_and_date(self) -> None:
        self.assertEqual(
            format_period_heading("Real GDP Percent Change", "q", "2026-04-01"),
            "Real GDP Percent Change, Quarterly, 2026-04-01",
        )
        self.assertEqual(
            format_period_heading("Top 100 Market Heatmap", "d", "2026-01-05"),
            "Top 100 Market Heatmap, Daily, 2026-01-05",
        )

    def test_combined_heading_joins_mixed_frequencies(self) -> None:
        self.assertEqual(
            format_combined_heading(
                "Real PCE and Consumer Inflation (CPI vs PCE)",
                ["q", "m", "m"],
                "2026-07-01",
            ),
            "Real PCE and Consumer Inflation (CPI vs PCE), "
            "Quarterly / Monthly, 2026-07-01",
        )

    def test_includes_metric_lines(self) -> None:
        caption = format_image_caption(
            "UNRATE",
            "2026-06-01",
            [("Latest", "4.10%"), ("Note", "monthly")],
        )
        self.assertEqual(
            caption,
            "UNRATE\nDate: 2026-06-01\nLatest: 4.10%\nNote: monthly",
        )

    def test_description_and_notes_come_after_title(self) -> None:
        caption = format_image_caption(
            "Real GDP Percent Change, Quarterly, 2026-04-01",
            "2026-04-01",
            [("Latest", "0.52%")],
            description="Real Gross Domestic Product, percent change",
            next_releases=[(None, "2026-10-30")],
            notes=["Inflation-adjusted output. Up = expanding economy."],
            as_of=date(2026, 10, 2),
        )
        self.assertEqual(
            caption,
            "Real GDP Percent Change, Quarterly, 2026-04-01\n"
            "Real Gross Domestic Product, percent change\n"
            "Inflation-adjusted output. Up = expanding economy.\n"
            "Next release date: 2026-10-30, 28 Days\n"
            "Latest: 0.52%",
        )

    def test_omits_date_line_when_title_already_includes_it(self) -> None:
        caption = format_image_caption(
            "CPI Percent Change from Year Ago, Monthly, 2026-08-01",
            "2026-08-01",
            [("Latest", "3.35%")],
        )
        self.assertEqual(
            caption,
            "CPI Percent Change from Year Ago, Monthly, 2026-08-01\nLatest: 3.35%",
        )
        self.assertNotIn("Date:", caption)

    def test_labeled_next_releases_when_dates_differ(self) -> None:
        caption = format_image_caption(
            "Real PCE and Consumer Inflation (CPI vs PCE)",
            "2026-08-01",
            [("CPI", "2.90%")],
            next_releases=[
                ("CPI", "2026-10-14"),
                ("PCE Price Index", "2026-10-29"),
            ],
            as_of=date(2026, 10, 2),
        )
        self.assertIn("Next release date (CPI): 2026-10-14, 12 Days", caption)
        self.assertIn(
            "Next release date (PCE Price Index): 2026-10-29, 27 Days", caption
        )
        self.assertNotIn("Next release date: 2026-10-14", caption)

    def test_next_release_date_includes_days_until(self) -> None:
        self.assertEqual(
            format_next_release_date("2026-10-29", as_of=date(2026, 10, 2)),
            "2026-10-29, 27 Days",
        )
        self.assertEqual(
            format_next_release_date("2026-10-03", as_of=date(2026, 10, 2)),
            "2026-10-03, 1 Day",
        )
        caption = format_image_caption(
            "Real GDP Percent Change",
            "2026-04-01",
            next_releases=[(None, "2026-10-29")],
            as_of=date(2026, 10, 2),
        )
        self.assertIn("Next release date: 2026-10-29, 27 Days", caption)


class TableFormatterTests(unittest.TestCase):
    def test_symbol_width_uses_widest_symbol(self) -> None:
        data = [{"symbol": "A"}, {"symbol": "GOOGL"}]
        self.assertEqual(TableFormatter.get_symbol_width(data), 6)

    def test_symbol_width_respects_header_minimum(self) -> None:
        data = [{"symbol": "A"}]
        self.assertEqual(
            TableFormatter.get_symbol_width(data),
            len(TableFormatter.SYMBOL_HEADER),
        )

    def test_format_number_fits_within_limit(self) -> None:
        self.assertEqual(TableFormatter.format_number(12.34, 5), "12.34")
        self.assertEqual(TableFormatter.format_number(123.4, 5), "123.4")
        self.assertEqual(TableFormatter.format_number(1234.5, 5), "1234 ")
        self.assertEqual(TableFormatter.format_number(99999.9, 5), "99999")

    def test_format_sma_percentage_handles_missing_value(self) -> None:
        self.assertEqual(
            TableFormatter.format_sma_percentage(None),
            "—".ljust(TableFormatter.SMA_PERCENT_MAX_CHARS),
        )

    def test_indicator_and_sma_percentage_use_one_decimal_place(self) -> None:
        self.assertEqual(TableFormatter.format_indicator(12.34), "12.3 ")
        self.assertEqual(TableFormatter.format_sma_percentage(-12.34), "-12.3% ")

    def test_table_columns_are_left_aligned_with_consistent_spacing(self) -> None:
        header = TableFormatter.format_header(6, "B022")
        row = MessageGenerator._format_table_row(
            {
                "symbol": "SPY",
                "close_price": 99.12,
                "btd_22": -1.23,
                "pct_from_sma_200": 4.26,
            },
            "btd_22",
            6,
        )

        self.assertEqual(header, "Symbol Last  B022  SMA%   ")
        self.assertEqual(row, "SPY    99.12 -1.2  4.3%   ")
        self.assertEqual(len(row), len(header))


class MessageGeneratorTests(unittest.TestCase):
    def test_empty_buy_message(self) -> None:
        with patch.object(
            MessageGenerator,
            "_get_header",
            return_value="💡 <b>William's VixFix Indicator (2026-07-11)</b>",
        ):
            chunks = MessageGenerator.generate_buy_message([])

        self.assertEqual(
            chunks,
            [
                "💡 <b>William's VixFix Indicator (2026-07-11)</b>\n"
                "\n"
                "🟢 <b>Buy Tip</b>\n"
                "<i>No tickers found.</i>"
            ],
        )
        self.assertEqual(chunks[0].count("Buy Tip"), 1)
        self.assertNotIn("significantly dipped", chunks[0])
        self.assertNotIn("<pre>", chunks[0])

    def test_empty_sell_message(self) -> None:
        chunks = MessageGenerator.generate_sell_message([])
        self.assertEqual(
            chunks,
            ["🔴 <b>Sell Tip</b>\n<i>No tickers found.</i>"],
        )
        self.assertEqual(chunks[0].count("Sell Tip"), 1)
        self.assertNotIn("overbought", chunks[0])

    def test_buy_message_includes_table_rows(self) -> None:
        data = [
            {
                "symbol": "SPY",
                "close_price": 500.12,
                "btd_22": 12.34,
                "pct_from_sma_200": 4.26,
            },
            {
                "symbol": "QQQ",
                "close_price": 400.5,
                "btd_22": 8.1,
                "pct_from_sma_200": None,
            },
        ]
        with patch.object(
            MessageGenerator,
            "_get_header",
            return_value="💡 <b>William's VixFix Indicator (2026-07-11)</b>",
        ):
            chunks = MessageGenerator.generate_buy_message(data)

        self.assertEqual(len(chunks), 1)
        self.assertIn("<pre>", chunks[0])
        self.assertIn("SPY", chunks[0])
        self.assertIn("QQQ", chunks[0])
        self.assertIn("B022", chunks[0])
        self.assertIn("SMA%", chunks[0])
        self.assertIn("12.3", chunks[0])
        self.assertNotIn("12.34", chunks[0])
        self.assertIn("4.3%", chunks[0])
        self.assertNotIn("4.26", chunks[0])
        self.assertIn("—", chunks[0])

    def test_sell_message_includes_sma_percentage(self) -> None:
        data = [
            {
                "symbol": "SPY",
                "close_price": 500.12,
                "str_22": 3.1,
                "pct_from_sma_200": -12.34,
            }
        ]

        chunks = MessageGenerator.generate_sell_message(data)

        self.assertEqual(len(chunks), 1)
        self.assertIn("S022", chunks[0])
        self.assertIn("SMA%", chunks[0])
        self.assertIn("-12.3%", chunks[0])
        self.assertNotIn("-12.34", chunks[0])

    def test_chunking_splits_long_tables(self) -> None:
        header_parts = ["Header"]
        table_header = "Symbol Last  B022 "
        rows = [f"ROW{i} " + ("x" * 40) for i in range(3)]
        chunks = MessageGenerator._chunk_message_with_table(
            header_parts,
            rows,
            table_header,
            max_length=90,
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("Header"))
        self.assertIn("<pre>", chunks[0])
        self.assertIn("<pre>", chunks[1])


if __name__ == "__main__":
    unittest.main()
