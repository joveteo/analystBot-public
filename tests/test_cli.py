from __future__ import annotations

import io
import unittest
from argparse import ArgumentTypeError
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from analystbot.__main__ import (
    _get_pipeline_stages,
    _parse_args,
    _parse_symbols_arg,
    main,
)


class ParseSymbolsTests(unittest.TestCase):
    def test_uppercases_and_deduplicates(self) -> None:
        self.assertEqual(_parse_symbols_arg("aapl, MSFT, aapl"), ["AAPL", "MSFT"])

    def test_rejects_invalid_symbol(self) -> None:
        with self.assertRaises(ArgumentTypeError):
            _parse_symbols_arg("AAPL,BAD!")

    def test_rejects_empty_list(self) -> None:
        with self.assertRaises(ArgumentTypeError):
            _parse_symbols_arg(" , , ")


class FlagComboTests(unittest.TestCase):
    def test_symbols_require_rebuild(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parse_args(["--symbols", "AAPL"])

    def test_force_flags_cannot_combine_with_rebuild(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parse_args(["--rebuild", "--supertrend"])

    def test_help_exits_zero(self) -> None:
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            _parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)


class PipelineStageSelectionTests(unittest.TestCase):
    def test_default_run_uses_daily_stages(self) -> None:
        names = [stage.name for stage in _get_pipeline_stages(_parse_args([]))]
        self.assertIn("Database Update", names)
        self.assertIn("Telegram Delivery", names)
        self.assertNotIn("FRED Telegram", names)

    def test_rebuild_scopes_to_symbols(self) -> None:
        args = _parse_args(["--rebuild", "--symbols", "AAPL,MSFT"])
        names = [stage.name for stage in _get_pipeline_stages(args)]
        self.assertIn("Database Rebuild", names)
        self.assertIn("Indicator Calculation", names)
        self.assertNotIn("FRED Update", names)
        self.assertNotIn("Telegram Delivery", names)

    def test_force_chart_flags_build_album_stages(self) -> None:
        args = _parse_args(["--supertrend", "--heatmap", "--rrg"])
        names = [stage.name for stage in _get_pipeline_stages(args)]
        self.assertEqual(
            names,
            [
                "Market Supertrend Charts",
                "Market Heatmap Chart",
                "Sector RRG Chart",
                "Chart Telegram",
            ],
        )


class MainEntryTests(unittest.TestCase):
    def test_successful_pipeline_returns_zero(self) -> None:
        with (
            patch("analystbot.__main__.Settings") as settings_cls,
            patch("analystbot.__main__.run_pipeline", return_value=True) as run,
        ):
            settings_cls.return_value = MagicMock()
            self.assertEqual(main([]), 0)
        run.assert_called_once()

    def test_failed_pipeline_returns_one(self) -> None:
        with (
            patch("analystbot.__main__.Settings"),
            patch("analystbot.__main__.run_pipeline", return_value=False),
        ):
            self.assertEqual(main(["--rebuild"]), 1)

    def test_logs_rebuild_and_force_flag_names(self) -> None:
        with (
            patch("analystbot.__main__.Settings"),
            patch("analystbot.__main__.run_pipeline", return_value=True),
        ):
            self.assertEqual(main(["--rebuild", "--symbols", "AAPL"]), 0)
            self.assertEqual(main(["--supertrend", "--vix", "--rrg", "--gdp"]), 0)

    def test_unexpected_error_returns_one(self) -> None:
        with (
            patch("analystbot.__main__.Settings"),
            patch(
                "analystbot.__main__.run_pipeline",
                side_effect=RuntimeError("boom"),
            ),
        ):
            self.assertEqual(main(["--vix"]), 1)


if __name__ == "__main__":
    unittest.main()
