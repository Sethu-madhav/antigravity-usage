"""Tests for the heatmap and display helpers."""

import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from unittest import mock

from agy_usage import display as display_mod
from agy_usage.cli import main as cli_main
from agy_usage.display import (
    Colors,
    format_duration,
    format_duration_verbose,
    format_percent,
    format_tokens,
    render_sparkline,
    render_table,
    strip_ansi,
)
from agy_usage.exporter import UsageExporter
from agy_usage.heatmap import HeatmapColor, TerminalHeatmap
from agy_usage.models import DailyUsage, SessionSummary, TokenUsage


class TestHeatmapAndDisplay(unittest.TestCase):
    def test_heatmap_rendering(self):
        daily = {
            date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(cached_tokens=5000, prompt_tokens=1000)),
        }
        hm = TerminalHeatmap(daily, metric="total", weeks=8, end_date=date(2026, 9, 10))
        rendered = hm.render()
        self.assertIn("Mon", rendered)
        self.assertIn("Wed", rendered)
        self.assertIn("Fri", rendered)
        self.assertIn("Less", rendered)
        self.assertIn("More", rendered)

    def test_month_labels_align_to_week_columns(self):
        """BUG-006: month labels start exactly at their week column and advance
        enough columns for their width, so they never drift right or overlap."""
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        weeks = 52
        hm = TerminalHeatmap(daily, metric="total", weeks=weeks, end_date=date(2026, 10, 28))
        # Colors are disabled, so line 0 of the render is the plain month header.
        header = hm.render().splitlines()[0]
        self.assertNotIn("\033", header)

        # Independently derive which week column each month label is placed on.
        end_sunday = date(2026, 10, 28) + timedelta(days=6 - date(2026, 10, 28).weekday())
        start_monday = end_sunday - timedelta(days=weeks * 7 - 1)
        placed = []  # (week_column, month_name)
        prev_month = -1
        for w in range(weeks):
            col_date = start_monday + timedelta(days=7 * w)
            if col_date.month != prev_month:
                placed.append((w, TerminalHeatmap.MONTH_NAMES[col_date.month - 1]))
                prev_month = col_date.month

        # Multi-month window with non-uniform month boundaries (Monday of a week
        # still in the previous month) so any drift would show up.
        self.assertGreater(len(placed), 10)

        # Every label starts exactly at char offset 5 + 2*w of its own column.
        for w, name in placed:
            self.assertEqual(header[5 + 2 * w:5 + 2 * w + len(name)], name)

        # Over consecutive labels, each previous label clears exactly its own
        # ceil(len/2) columns and nothing more: no cumulative drift.
        for (w1, n1), (w2, _n2) in zip(placed, placed[1:]):
            cols_skip = max(1, -(-len(n1) // 2))
            segment = header[5 + 2 * w1:5 + 2 * w2]
            self.assertEqual(segment, n1.ljust(cols_skip * 2) + " " * 2 * (w2 - w1 - cols_skip))

    def test_month_labels_pinned_window_layout(self):
        """BUG-006: exact header for a pinned window (labels on weeks 0/2/6)."""
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        hm = TerminalHeatmap(daily, metric="total", weeks=10, end_date=date(2026, 10, 28))
        header = hm.render().splitlines()[0]

        # 5-space indent, then week columns of 2 chars: "Aug" on col 0, "Sep" on
        # col 2, "Oct" on col 6; each label owns ceil(len/2) columns, the rest blank.
        self.assertEqual(header, "     Aug Sep     Oct")

    def test_no_color_kill_switch_disables_heatmap_escapes(self):
        """BUG-004: the shared --no-color switch must strip every ANSI escape, not just Colors."""
        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

        self.assertFalse(HeatmapColor.supports_color())
        self.assertNotIn("\033", HeatmapColor.cell(4))
        rendered = TerminalHeatmap(daily, metric="total", weeks=8, end_date=date(2026, 9, 10)).render()
        self.assertNotIn("\033", rendered)
        # Falls back to plain density symbols instead of colored cells.
        self.assertIn("·", rendered)
        self.assertIn("█", rendered)
        self.assertIn("Less", rendered)
        self.assertIn("More", rendered)

    def test_color_force_on_enables_heatmap_escapes(self):
        """The same switch can force colors on even when stdout is not a TTY."""
        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

        self.assertTrue(HeatmapColor.supports_color())
        self.assertIn("\033[38;", HeatmapColor.cell(2))
        rendered = TerminalHeatmap(daily, metric="total", weeks=8, end_date=date(2026, 9, 10)).render()
        self.assertIn("\033[38;", rendered)

    def test_color_auto_detect_paths(self):
        """BUG-004 (review): the auto-detection path itself is covered deterministically."""
        Colors.set_override(None)
        self.addCleanup(Colors.set_override, None)
        # Hermetic env: no COLORTERM / NO_COLOR inherited from the runner.
        bare_env = {"PATH": os.environ.get("PATH", "")}

        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(os.environ, bare_env, clear=True):
                self.assertTrue(Colors.enabled())
                self.assertTrue(HeatmapColor.supports_color())
        # TTY off + no COLORTERM → disabled.
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, bare_env, clear=True):
                self.assertFalse(Colors.enabled())
        # NO_COLOR wins even on a TTY.
        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(os.environ, {**bare_env, "NO_COLOR": "1"}, clear=True):
                self.assertFalse(Colors.enabled())
        # COLORTERM no longer enables color for piped stdout (BUG-009):
        # even exported by the parent shell (VS Code, Warp, CI), it must not
        # flip enablement — enablement is TTY + NO_COLOR only.
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, {**bare_env, "COLORTERM": "truecolor"}, clear=True):
                self.assertFalse(Colors.enabled())
                self.assertFalse(HeatmapColor.supports_color())

    def test_main_no_color_wires_global_switch(self):
        """BUG-004: cli.main() applies --no-color to the shared switch and cmd_heatmap
        renders an escape-free heatmap (integration, real render not the early return)."""
        self.addCleanup(Colors.set_override, None)

        fake = SessionSummary(
            session_id="abc123def456",
            source="cli",
            file_path="/tmp/fake.db",
            title="Repro session",
            workspace="/ws/repro",
            models=["gemini-3-pro"],
            start_time=datetime(2026, 10, 26, 10, 0, 0),
            end_time=datetime(2026, 10, 26, 10, 30, 0),
            step_count=4,
            usage=TokenUsage(prompt_tokens=5000, cached_tokens=20000, output_tokens=1000),
        )

        with mock.patch("agy_usage.cli.get_sessions", return_value=[fake]):
            with redirect_stdout(io.StringIO()) as captured:
                cli_main(["heatmap", "--no-color"])
        out = captured.getvalue()
        self.assertIn("GITHUB-STYLE CONTRIBUTION HEATMAP", out)  # render path reached
        self.assertNotIn("\033", out)

        # Rather than inheriting the switch from a previous call, main() resets it.
        Colors.set_override(False)
        with mock.patch("agy_usage.cli.get_sessions", return_value=[fake]):
            with redirect_stdout(io.StringIO()):
                cli_main(["heatmap"])
        self.assertIsNone(display_mod._COLOR_OVERRIDE)  # reset without the flag

    def test_formatting_utilities(self):
        self.assertEqual(format_tokens(1_500_000), "1.50M")
        self.assertEqual(format_tokens(45_200), "45.2K")
        self.assertEqual(format_tokens(500), "500")
        self.assertEqual(format_duration(65), "1m 5s")
        self.assertEqual(format_duration(3665), "1h 1m")
        self.assertEqual(format_duration(576000), "6d 16h")
        self.assertEqual(format_duration_verbose(576000), "6 days, 16 hours")
        self.assertEqual(format_duration_verbose(3660), "1 hour, 1 minute")
        self.assertEqual(format_duration_verbose(0), "0 minutes")

    def test_format_percent_remaining_semantics_unchanged(self):
        # BUG-011 regression guard: *remaining* callers keep today's thresholds
        # (green >=75, yellow >=40, gray below) — default and explicit kwarg.
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)
        self.assertEqual(format_percent(92.0), Colors.green("92.0%"))
        self.assertEqual(format_percent(100.0), Colors.green("100.0%"))
        self.assertEqual(format_percent(75.0), Colors.green("75.0%"))
        self.assertEqual(format_percent(74.9), Colors.yellow("74.9%"))
        self.assertEqual(format_percent(40.0), Colors.yellow("40.0%"))
        self.assertEqual(format_percent(39.9), Colors.gray("39.9%"))
        self.assertEqual(format_percent(92.0, high_is_good=True), Colors.green("92.0%"))

    def test_format_percent_used_semantics_inverted(self):
        # BUG-011: for *used* quota a high fraction is bad (warn/danger color),
        # and a low fraction is healthy (green) — the inverse reading.
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)
        self.assertEqual(format_percent(92.0, high_is_good=False), Colors.red("92.0%"))
        self.assertEqual(format_percent(100.0, high_is_good=False), Colors.red("100.0%"))
        self.assertEqual(format_percent(75.0, high_is_good=False), Colors.red("75.0%"))
        self.assertEqual(format_percent(74.9, high_is_good=False), Colors.yellow("74.9%"))
        self.assertEqual(format_percent(40.0, high_is_good=False), Colors.yellow("40.0%"))
        self.assertEqual(format_percent(39.9, high_is_good=False), Colors.green("39.9%"))
        self.assertEqual(format_percent(30.0, high_is_good=False), Colors.green("30.0%"))
        # The bug itself: 92.0% used must render a warn/danger color, never green.

    def test_sparkline(self):
        spark = render_sparkline([10, 20, 50, 80, 100])
        self.assertEqual(len(spark), 5)

    def test_table_rendering(self):
        from agy_usage.display import render_kpi_card, strip_ansi

        headers = ["Col A", "Col B"]
        rows = [["Value 1", "Value 2"]]
        tbl = render_table(headers, rows)
        self.assertIn("Col A", tbl)
        self.assertIn("Value 1", tbl)

        colored_sub = "\033[92m92.8%\033[0m prompt hit rate ($225.73 saved)"
        card = render_kpi_card("CACHE SAVINGS", "\033[1m\033[92m355.71M\033[0m", colored_sub, width=26)
        for line in card.split("\n"):
            self.assertEqual(len(strip_ansi(line)), 26)

    def test_exporter_json_and_csv(self):
        t = datetime(2026, 9, 1, 12, 0)
        s = SessionSummary(
            session_id="s1",
            source="cli",
            file_path="/tmp/s1.db",
            title="Test",
            workspace="ws",
            models=["m1"],
            start_time=t,
            usage=TokenUsage(prompt_tokens=10, cached_tokens=90, output_tokens=5),
        )
        json_out = UsageExporter.to_json([s])
        self.assertIn('"session_count": 1', json_out)
        self.assertIn('"cached_tokens": 90', json_out)

        csv_out = UsageExporter.to_csv([s])
        self.assertIn("Session ID,Source,Workspace", csv_out)
        self.assertIn("s1,cli,ws", csv_out)


if __name__ == "__main__":
    unittest.main()
