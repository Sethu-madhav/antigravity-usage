"""Tests for the shared heatmap legend/styles and render_table padding (PR 15 dedup)."""

import unittest
from datetime import date
from unittest import mock

import agy_usage.display as display
from agy_usage.display import Colors, render_table, strip_ansi
from agy_usage.heatmap import HeatmapColor, HourOfDayHeatmap, TerminalHeatmap
from agy_usage.models import DailyUsage, TokenUsage

END = date(2026, 10, 29)  # a Thursday


def _force_color(test, on, truecolor=False):
    Colors.set_override(on)
    test.addCleanup(Colors.set_override, None)
    patcher = mock.patch.object(HeatmapColor, "supports_truecolor", return_value=truecolor)
    patcher.start()
    test.addCleanup(patcher.stop)


def _terminal_heatmap():
    daily = {
        END: DailyUsage(date=END, usage=TokenUsage(prompt_tokens=500, cached_tokens=1500)),
        date(2026, 10, 20): DailyUsage(date=date(2026, 10, 20), usage=TokenUsage(prompt_tokens=10)),
    }
    return TerminalHeatmap(daily, metric="total", weeks=8, end_date=END, term_cols=80)


def _hour_heatmap():
    hourly = {(date(2026, 10, 27), 14): TokenUsage(prompt_tokens=5000, cached_tokens=2000)}
    return HourOfDayHeatmap(hourly, weeks=8, end_date=END, term_cols=80)


def _expected_legend():
    cells = "".join(HeatmapColor.cell(lvl, "■") + " " for lvl in range(5))
    return Colors.dim("Less ") + cells + Colors.dim("More")


class TestHeatmapReusesColors(unittest.TestCase):
    def test_duplicate_style_api_removed(self):
        for name in ("wrap", "DIM", "BOLD", "CYAN", "GRAY", "RESET"):
            self.assertFalse(hasattr(HeatmapColor, name), name)

    def test_active_days_count_uses_plain_cyan(self):
        _force_color(self, True)
        last = _terminal_heatmap().render().splitlines()[-1]
        self.assertIn("across \033[36m2\033[0m active days", last)
        self.assertNotIn("\033[96m", last)

    def test_busiest_hour_uses_plain_cyan(self):
        _force_color(self, True)
        last = _hour_heatmap().render().splitlines()[-1]
        self.assertIn("busiest hour \033[36m14:00\033[0m", last)


class TestSharedLegend(unittest.TestCase):
    def _check(self, on, truecolor=False):
        _force_color(self, on, truecolor)
        legend = _expected_legend()
        for hm in (_terminal_heatmap(), _hour_heatmap()):
            last = hm.render().splitlines()[-1]
            self.assertTrue(last.startswith(f" {legend}    "), repr(last))

    def test_legend_color_off(self):
        self._check(False)
        self.assertEqual(strip_ansi(_expected_legend()), "Less · ░ ▒ ▓ █ More")

    def test_legend_256_color(self):
        self._check(True, truecolor=False)

    def test_legend_truecolor(self):
        self._check(True, truecolor=True)


class TestCellClamping(unittest.TestCase):
    def _assert_clamped(self):
        for lvl in (-3, -1):
            self.assertEqual(HeatmapColor.cell(lvl), HeatmapColor.cell(0))
        for lvl in (5, 7):
            self.assertEqual(HeatmapColor.cell(lvl), HeatmapColor.cell(4))

    def test_clamp_color_off(self):
        _force_color(self, False)
        self._assert_clamped()
        self.assertEqual(HeatmapColor.cell(-1), "·")
        self.assertEqual(HeatmapColor.cell(9), "█")

    def test_clamp_256_color(self):
        _force_color(self, True, truecolor=False)
        self._assert_clamped()
        self.assertEqual(HeatmapColor.cell(9, "x"), "\033[38;5;46mx\033[0m")

    def test_clamp_truecolor(self):
        _force_color(self, True, truecolor=True)
        self._assert_clamped()
        r, g, b = HeatmapColor.RGB_LEVELS[0]
        self.assertEqual(HeatmapColor.cell(-2, "x"), f"\033[38;2;{r};{g};{b}mx\033[0m")


class TestTablePadding(unittest.TestCase):
    def setUp(self):
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

    def _rows(self):
        # Visible widths 1..6 in a 7-wide column exercise odd and even padding.
        return [
            [Colors.green("a" * k), Colors.bold(f"{Colors.cyan('b')}{'c' * k}"), "d" * k]
            for k in range(1, 7)
        ] + [["e" * 7, "f" * 8, "g" * 7]]

    def test_equal_visible_widths_for_each_alignment(self):
        for align in ("left", "right", "center"):
            with self.subTest(align=align):
                out = render_table(["Hdr", "Head", "H"], self._rows(), [align] * 3, title="T")
                widths = {len(strip_ansi(line)) for line in out.splitlines()[1:]}
                self.assertEqual(len(widths), 1, out)

    def test_center_header_keeps_str_center_split(self):
        # 2-char header in a 7-wide column: str.center gives 3 left / 2 right.
        out = render_table(["HH"], [["c" * 7]], ["center"])
        header = strip_ansi(out.splitlines()[1])
        self.assertEqual(header, "│ " + "HH".center(7) + " │")

    def test_strip_ansi_once_per_data_cell(self):
        rows = self._rows()
        with mock.patch.object(display, "strip_ansi", wraps=display.strip_ansi) as spy:
            render_table(["Hdr", "Head", "H"], rows, ["center"] * 3)
        self.assertEqual(spy.call_count, sum(len(r) for r in rows))


if __name__ == "__main__":
    unittest.main()
