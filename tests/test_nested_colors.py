"""Tests for nested color calls."""

import unittest

from agy_usage.display import Colors, render_table, strip_ansi
from agy_usage.heatmap import HeatmapColor


class TestNestedColors(unittest.TestCase):
    """Regression tests for BUG-021: nested color calls and RESET handling."""

    def setUp(self):
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

    def test_direct_nesting_single_reset(self):
        out = Colors.bold(Colors.green("x"))
        self.assertEqual(out, f"{Colors.BOLD}{Colors.BRIGHT_GREEN}x{Colors.RESET}")
        self.assertNotIn(Colors.RESET + Colors.RESET, out)

    def test_three_level_nesting_single_reset(self):
        out = Colors.bold(Colors.color(Colors.cyan("x"), Colors.UNDERLINE))
        self.assertEqual(out.count(Colors.RESET), 1)
        self.assertTrue(out.endswith("x" + Colors.RESET))
        self.assertTrue(out.startswith(Colors.BOLD + Colors.UNDERLINE))

    def test_mid_string_nesting_reasserts_outer(self):
        out = Colors.bold(f"prefix {Colors.green('mid')} suffix")
        self.assertEqual(
            out,
            f"{Colors.BOLD}prefix {Colors.BRIGHT_GREEN}mid{Colors.RESET}{Colors.BOLD} suffix{Colors.RESET}",
        )

    def test_sibling_inner_colors(self):
        out = Colors.bold(f"{Colors.green('a')} / {Colors.yellow('b')}")
        self.assertEqual(
            out,
            f"{Colors.BOLD}{Colors.BRIGHT_GREEN}a{Colors.RESET}{Colors.BOLD} / "
            f"{Colors.BRIGHT_YELLOW}b{Colors.RESET}",
        )

    def test_disabled_returns_plain_text(self):
        Colors.set_override(False)
        out = Colors.bold(f"{Colors.green('a')} / {Colors.yellow('b')}")
        self.assertEqual(out, "a / b")
        self.assertNotIn("\033", out)

    def test_heatmap_wrap_matches_colors_color(self):
        inner = HeatmapColor.wrap("x", HeatmapColor.CYAN)
        self.assertEqual(
            HeatmapColor.wrap(inner, HeatmapColor.BOLD),
            f"{HeatmapColor.BOLD}{HeatmapColor.CYAN}x{HeatmapColor.RESET}",
        )

    def test_width_alignment_preserved(self):
        from agy_usage.display import render_kpi_card

        nested = Colors.bold(f"{Colors.green('ab')} / {Colors.yellow('cd')}")
        self.assertEqual(strip_ansi(nested), "ab / cd")
        card = render_kpi_card("T", nested, Colors.bold(Colors.green("ok")), width=26)
        for line in card.split("\n"):
            self.assertEqual(len(strip_ansi(line)), 26)
        table = render_table(["H"], [[nested]])
        widths = {len(strip_ansi(l)) for l in table.split("\n") if l}
        self.assertEqual(len(widths), 1)


if __name__ == "__main__":
    unittest.main()
