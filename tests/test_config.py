from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analystbot.core.config import Settings, _looks_like_project_root, get_project_root


class SettingsValidateTests(unittest.TestCase):
    def test_missing_explicit_env_file_disables_dotenv_discovery(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict("os.environ", {"APP_ENV_FILE": "/nonexistent"}, clear=True),
            patch("analystbot.core.config.load_dotenv") as load_dotenv,
        ):
            settings = Settings(project_root=Path(temp_dir))

        self.assertEqual(settings.env_path, Path("/nonexistent"))
        load_dotenv.assert_not_called()

    def test_explicit_env_path_takes_precedence_over_environment_override(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict("os.environ", {"APP_ENV_FILE": "/nonexistent"}, clear=True),
            patch("analystbot.core.config.load_dotenv") as load_dotenv,
        ):
            root = Path(temp_dir)
            path = root / "settings.txt"
            path.touch()
            settings = Settings(project_root=root, _loaded=True)
            settings.load(path)

        self.assertEqual(settings.env_path, path)
        load_dotenv.assert_called_once_with(path, override=True)

    def test_market_data_and_telegram_required_when_unscoped(self) -> None:
        settings = Settings(_loaded=True)
        valid, missing = settings.validate()
        self.assertFalse(valid)
        self.assertIn("TASTYTRADE_CLIENT_SECRET", missing)
        self.assertIn("TELEGRAM_BOT_TOKEN", missing)

    def test_fred_and_chart_telegram_are_stage_specific(self) -> None:
        settings = Settings(_loaded=True)
        valid, missing = settings.validate("fred")
        self.assertFalse(valid)
        self.assertEqual(missing, ["FRED_API_KEY"])

        settings.telegram_bot_token = "t"
        settings.telegram_chat_id = "c"
        valid, missing = settings.validate("chart_telegram")
        self.assertFalse(valid)
        self.assertEqual(missing, ["TELEGRAM_CHAT_CHART_ID"])

        settings.telegram_chat_chart_id = "9"
        valid, missing = settings.validate("chart_telegram")
        self.assertTrue(valid)
        self.assertEqual(missing, [])

    def test_fred_telegram_requires_topic(self) -> None:
        settings = Settings(
            _loaded=True,
            telegram_bot_token="t",
            telegram_chat_id="c",
        )
        valid, missing = settings.validate("fred_telegram")
        self.assertFalse(valid)
        self.assertEqual(missing, ["TELEGRAM_CHAT_FRED_ID"])

    def test_indicators_stage_has_no_credential_requirements(self) -> None:
        settings = Settings(_loaded=True)
        valid, missing = settings.validate("indicators")
        self.assertTrue(valid)
        self.assertEqual(missing, [])

    def test_ensure_directories_creates_data_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            root = Path(temp_dir)
            settings.data_dir = root / "data"
            settings.logs_dir = root / "logs"
            settings.ensure_directories()
            self.assertTrue(settings.data_dir.is_dir())
            self.assertTrue(settings.logs_dir.is_dir())

    def test_project_root_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertFalse(_looks_like_project_root(root))
            (root / "pyproject.toml").write_text(
                'name = "analystbot"\n', encoding="utf-8"
            )
            self.assertTrue(_looks_like_project_root(root))
        self.assertTrue(get_project_root().is_dir())


if __name__ == "__main__":
    unittest.main()
