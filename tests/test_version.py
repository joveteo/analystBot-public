from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from analystbot import __version__
from analystbot.api.http_client import RetryPolicy
from analystbot.api.tastytrade import TastytradeConfig

ROOT = Path(__file__).resolve().parents[1]


def _product_user_agent(version: str) -> str:
    major, minor, *_rest = version.split(".")
    return f"analystbot/{major}.{minor}"


class VersionSitesTest(unittest.TestCase):
    def test_pyproject_matches_package_version(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            data = tomllib.load(handle)
        self.assertEqual(data["project"]["version"], __version__)

    def test_user_agents_use_major_minor(self) -> None:
        expected = _product_user_agent(__version__)
        self.assertEqual(TastytradeConfig.DEFAULT_USER_AGENT, expected)
        self.assertEqual(RetryPolicy().default_headers["User-Agent"], expected)
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn(f"TASTYTRADE_USER_AGENT={expected}", env_example)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"defaults to `{expected}`", readme)


class LazyPackageImportTests(unittest.TestCase):
    def test_api_getattr_loads_http_helpers(self) -> None:
        import analystbot.api as api

        self.assertIs(api.RetryPolicy, RetryPolicy)
        with self.assertRaises(AttributeError):
            api.__getattr__("not_an_export")

    def test_services_getattr_loads_named_module(self) -> None:
        import analystbot.services as services

        services.__dict__.pop("notifications", None)
        loaded = services.__getattr__("notifications")
        self.assertTrue(hasattr(loaded, "run_delivery"))
        self.assertIs(services.notifications, loaded)
        with self.assertRaises(AttributeError):
            services.__getattr__("not_an_export")


if __name__ == "__main__":
    unittest.main()
