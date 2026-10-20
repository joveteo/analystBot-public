from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from analystbot.core.config import Settings
from analystbot.db.date_utils import date_str_to_unix
from analystbot.services.heatmap_charts import (
    AXES_BOUNDS,
    COMPANY_GROUPS,
    COMPANY_SECTORS,
    GICS_SECTORS,
    GRAY_RGB,
    GREEN_RGB,
    HEATMAP_OUTPUT_FILENAME,
    INDUSTRY_HEADER_DEFAULT,
    RED_RGB,
    SECTOR_HEADER_DEFAULT,
    SECTOR_LABELS,
    HeatmapChartConfig,
    HeatmapChartService,
    HeatmapTile,
    Rect,
    build_heatmap_tiles,
    color_for_pct,
    fitted_fontsize,
    header_height,
    inset_rect,
    latest_two_closes,
    layout_heatmap,
    pct_change_1d,
    split_header,
    squarify,
)
from analystbot.services.market_data import TOP_100_COMPANIES


class SectorMapTests(unittest.TestCase):
    def test_company_groups_cover_the_top_100_universe(self) -> None:
        self.assertEqual(set(COMPANY_GROUPS), set(TOP_100_COMPANIES))
        self.assertEqual(set(COMPANY_SECTORS), set(TOP_100_COMPANIES))
        self.assertIn("GOOGL", COMPANY_GROUPS)
        self.assertNotIn("GOOG", COMPANY_GROUPS)
        self.assertTrue(set(COMPANY_SECTORS.values()) <= GICS_SECTORS)
        self.assertTrue(
            all(industry.strip() for _, industry in COMPANY_GROUPS.values())
        )
        self.assertEqual(set(SECTOR_LABELS), GICS_SECTORS)


class PctChangeTests(unittest.TestCase):
    def test_close_to_close_percent(self) -> None:
        self.assertAlmostEqual(pct_change_1d(100.0, 103.0), 3.0)
        self.assertAlmostEqual(pct_change_1d(100.0, 97.0), -3.0)
        self.assertAlmostEqual(pct_change_1d(100.0, 100.0), 0.0)

    def test_undefined_when_previous_close_is_zero_or_non_finite(self) -> None:
        self.assertIsNone(pct_change_1d(0.0, 1.0))
        self.assertIsNone(pct_change_1d(float("nan"), 1.0))
        self.assertIsNone(pct_change_1d(100.0, float("inf")))


class ColorScaleTests(unittest.TestCase):
    def test_zero_is_gray_and_clip_saturates(self) -> None:
        self.assertEqual(color_for_pct(0.0, clip=3.0), GRAY_RGB)
        self._assert_rgb(color_for_pct(3.0, clip=3.0), GREEN_RGB)
        self._assert_rgb(color_for_pct(99.0, clip=3.0), GREEN_RGB)
        self._assert_rgb(color_for_pct(-3.0, clip=3.0), RED_RGB)
        self._assert_rgb(color_for_pct(-99.0, clip=3.0), RED_RGB)

    def _assert_rgb(
        self,
        actual: tuple[float, float, float],
        expected: tuple[float, float, float],
    ) -> None:
        for got, want in zip(actual, expected, strict=True):
            self.assertAlmostEqual(got, want)

    def test_interpolates_between_integer_legend_stops(self) -> None:
        low = color_for_pct(1.0, clip=3.0)
        high = color_for_pct(2.0, clip=3.0)
        mid = color_for_pct(1.5, clip=3.0)
        for got, want in zip(
            mid,
            tuple((a + b) / 2.0 for a, b in zip(low, high, strict=True)),
            strict=True,
        ):
            self.assertAlmostEqual(got, want)

    def test_zero_is_dark_enough_for_white_labels(self) -> None:
        gray = color_for_pct(0.0, clip=3.0)
        luminance = 0.299 * gray[0] + 0.587 * gray[1] + 0.114 * gray[2]
        self.assertLess(luminance, 0.35)


class SquarifyTests(unittest.TestCase):
    def test_areas_match_weight_share(self) -> None:
        placed = dict(squarify([("A", 4.0), ("B", 1.0)], Rect(0.0, 0.0, 10.0, 10.0)))
        self.assertAlmostEqual(placed["A"].area, 80.0, places=6)
        self.assertAlmostEqual(placed["B"].area, 20.0, places=6)
        self.assertAlmostEqual(
            placed["A"].area + placed["B"].area,
            100.0,
            places=6,
        )

    def test_empty_or_non_positive_weights_yield_nothing(self) -> None:
        self.assertEqual(squarify([], Rect(0.0, 0.0, 1.0, 1.0)), [])
        self.assertEqual(
            squarify([("A", 0.0), ("B", -1.0)], Rect(0.0, 0.0, 1.0, 1.0)), []
        )


class InsetTests(unittest.TestCase):
    def test_inset_shrinks_every_side(self) -> None:
        shrunk = inset_rect(Rect(0.0, 0.0, 10.0, 4.0), 1.0)
        self.assertAlmostEqual(shrunk.x, 1.0)
        self.assertAlmostEqual(shrunk.y, 1.0)
        self.assertAlmostEqual(shrunk.width, 8.0)
        self.assertAlmostEqual(shrunk.height, 2.0)


class FittedFontTests(unittest.TestCase):
    _AXES = (20.0 * AXES_BOUNDS[2], 11.4 * AXES_BOUNDS[3])

    def _size(self, width: float, height: float, text: str = "AAPL") -> float:
        return fitted_fontsize(
            text,
            Rect(0.0, 0.0, width, height),
            axes_inches=self._AXES,
            max_size=44.0,
            width_ratio=0.80,
            height_ratio=0.36,
        )

    def test_larger_tiles_get_larger_ticker_type(self) -> None:
        small = self._size(0.04, 0.04)
        medium = self._size(0.10, 0.10)
        large = self._size(0.22, 0.22)
        self.assertGreater(medium, small)
        self.assertGreater(large, medium)
        self.assertGreater(large, small * 2.5)
        self.assertLess(small, 14.0)
        self.assertGreater(large, 28.0)

    def test_longer_tickers_shrink_to_fit_the_same_tile(self) -> None:
        short = self._size(0.06, 0.14, "V")
        long = self._size(0.06, 0.14, "BRK.B")
        self.assertGreater(short, long)


def _rects_overlap(left: Rect, right: Rect, eps: float = 1e-9) -> bool:
    return (
        left.x < right.right - eps
        and left.right > right.x + eps
        and left.y < right.top - eps
        and left.top > right.y + eps
    )


class HeaderBandTests(unittest.TestCase):
    def test_split_header_reserves_top_band(self) -> None:
        header, body = split_header(Rect(0.0, 0.0, 1.0, 1.0), 0.05, minimum=0.01)
        self.assertAlmostEqual(header.height, 0.05)
        self.assertAlmostEqual(header.y, 0.95)
        self.assertAlmostEqual(body.height, 0.95)
        self.assertAlmostEqual(body.top, header.y)
        self.assertFalse(_rects_overlap(header, body))

    def test_header_height_drops_when_rect_is_too_short(self) -> None:
        self.assertEqual(
            header_height(Rect(0.0, 0.0, 1.0, 0.04), 0.03, minimum=0.02),
            0.0,
        )


class NestedLayoutTests(unittest.TestCase):
    def test_stocks_stay_inside_industry_and_below_headers(self) -> None:
        tiles = [
            HeatmapTile("AAPL", "Technology", "Consumer Electronics", 400.0, 1.0, 1),
            HeatmapTile(
                "MSFT", "Technology", "Software - Infrastructure", 100.0, -0.5, 1
            ),
            HeatmapTile("JPM", "Financials", "Banks - Diversified", 100.0, 0.2, 1),
        ]
        layout = layout_heatmap(
            tiles,
            Rect(0.0, 0.0, 1.0, 1.0),
            sector_header=SECTOR_HEADER_DEFAULT,
            industry_header=INDUSTRY_HEADER_DEFAULT,
            sector_gap=0.0,
            industry_gap=0.0,
        )
        by_sector = dict(layout.sectors)
        self.assertAlmostEqual(
            by_sector["Technology"].area / by_sector["Financials"].area,
            5.0,
            places=5,
        )
        industries = {
            (sector, industry): rect for sector, industry, rect in layout.industries
        }
        self.assertIn(("Technology", "Consumer Electronics"), industries)
        self.assertIn(("Technology", "Software - Infrastructure"), industries)
        self.assertTrue(layout.sector_headers)
        self.assertTrue(layout.industry_headers)
        for tile, rect in layout.tiles:
            parent = by_sector[tile.sector]
            industry = industries[(tile.sector, tile.industry)]
            self.assertGreaterEqual(rect.x, industry.x - 1e-9)
            self.assertLessEqual(rect.right, industry.right + 1e-9)
            self.assertGreaterEqual(rect.y, industry.y - 1e-9)
            self.assertLessEqual(rect.top, industry.top + 1e-9)
            self.assertGreaterEqual(industry.x, parent.x - 1e-9)
            self.assertLessEqual(industry.right, parent.right + 1e-9)
            for _, header in layout.sector_headers:
                self.assertFalse(_rects_overlap(rect, header))
            for _, _, header in layout.industry_headers:
                self.assertFalse(_rects_overlap(rect, header))


class TileFilterTests(unittest.TestCase):
    def test_drops_missing_cap_and_missing_ohlc(self) -> None:
        tiles = build_heatmap_tiles(
            ["AAPL", "MSFT", "NVDA"],
            groups={
                "AAPL": ("Technology", "Consumer Electronics"),
                "MSFT": ("Technology", "Software - Infrastructure"),
                "NVDA": ("Technology", "Semiconductors"),
            },
            market_caps={"AAPL": 100.0, "MSFT": 0.0},
            closes={
                "AAPL": (100.0, 101.0, 1),
                "NVDA": (10.0, 11.0, 1),
            },
        )
        self.assertEqual([tile.symbol for tile in tiles], ["AAPL"])
        self.assertAlmostEqual(tiles[0].pct_1d, 1.0)
        self.assertEqual(tiles[0].industry, "Consumer Electronics")

    def test_latest_two_closes_uses_last_valid_pair(self) -> None:
        rows = [
            {"date": 1, "close_price": 10.0},
            {"date": 2, "close_price": None},
            {"date": 3, "close_price": 11.0},
            {"date": 4, "close_price": 12.0},
        ]
        self.assertEqual(latest_two_closes(rows), (11.0, 12.0, 4))
        self.assertIsNone(latest_two_closes([{"date": 1, "close_price": 10.0}]))


class HeatmapRenderTests(unittest.TestCase):
    def test_generate_chart_writes_dark_png_and_summary(self) -> None:
        class FakeRepo:
            def get_stock_data(self, symbol: str, days: int = 5):
                prev = date_str_to_unix("2026-01-02")
                last = date_str_to_unix("2026-01-05")
                closes = {"AAPL": 103.0, "JPM": 97.0}
                return [
                    {"date": prev, "close_price": 100.0},
                    {"date": last, "close_price": closes[symbol]},
                ]

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.data_dir = Path(temp_dir)
            config = HeatmapChartConfig(symbols=("AAPL", "JPM"))
            service = HeatmapChartService(settings, config=config)
            self.assertTrue(
                service.generate_chart(
                    FakeRepo(),
                    {"AAPL": 400.0, "JPM": 100.0},
                )
            )
            png = Path(temp_dir) / "chart_graphs" / HEATMAP_OUTPUT_FILENAME
            self.assertTrue(png.is_file())
            self.assertGreater(png.stat().st_size, 1000)
            summary_path = (
                Path(temp_dir) / "chart_graphs" / "market_heatmap_top100_d1.json"
            )
            self.assertTrue(summary_path.is_file())

            import matplotlib.image as mpimg

            image = mpimg.imread(png)
            corner = image[8, 8, :3]
            self.assertLess(float(corner.mean()), 0.25)


class HeatmapRunTests(unittest.TestCase):
    def test_run_fails_without_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.db_path = Path(temp_dir) / "missing.db"
            from analystbot.services.heatmap_charts import run as heatmap_run

            self.assertFalse(heatmap_run(settings))

    def test_run_fails_without_market_data_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(_loaded=True)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            settings.db_path.write_bytes(b"")
            from analystbot.services.heatmap_charts import run as heatmap_run

            self.assertFalse(heatmap_run(settings))

    def test_run_uses_mocked_market_caps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Settings(
                _loaded=True,
                tastytrade_client_secret="secret",
                tastytrade_refresh_token="token",
            )
            settings.data_dir = Path(temp_dir)
            settings.db_path = Path(temp_dir) / "live_stocks.db"
            settings.db_path.write_bytes(b"")
            client = MagicMock()
            client.get_market_caps.return_value = {"AAPL": 1.0}
            from analystbot.services import heatmap_charts

            with (
                patch.object(
                    heatmap_charts.TastytradeClient,
                    "from_settings",
                    return_value=client,
                ),
                patch("analystbot.services.heatmap_charts.connect") as connect,
                patch.object(
                    heatmap_charts.HeatmapChartService,
                    "generate_chart",
                    return_value=True,
                ) as generate,
            ):
                connect.return_value = MagicMock()
                self.assertTrue(heatmap_charts.run(settings))
            generate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
