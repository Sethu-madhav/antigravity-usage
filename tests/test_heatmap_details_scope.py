"""Tests for IMP-005: heatmap --details scoped to the rendered week window."""

import argparse
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from unittest.mock import patch

from agy_usage.cli import cmd_heatmap
from agy_usage.display import Colors
from agy_usage.heatmap import TerminalHeatmap
from agy_usage.models import DailyUsage, SessionSummary, TokenUsage


def _end_sunday(end: date) -> date:
    """Sunday that ends the week containing `end` (same math as the renderer)."""
    return end + timedelta(days=(6 - end.weekday()))


def _fake_session(username: str = "s1") -> SessionSummary:
    return SessionSummary(
        session_id=username,
        source="cli",
        file_path="/fake/path",
        title="Test Session",
        workspace="ws",
        start_time=datetime(2026, 10, 20),
        end_time=datetime(2026, 10, 20),
        models=["gemini-3.8-flash"],
    )


def _active_day(d: date) -> DailyUsage:
    return DailyUsage(
        date=d,
        usage=TokenUsage(prompt_tokens=1000, cached_tokens=500, output_tokens=300),
    )


def _run_cmd_heatmap(daily):
    """Run cmd_heatmap --details with a patched aggregator; return stdout."""
    args = argparse.Namespace(metric="total", weeks=4, details=True)
    with patch("agy_usage.cli.UsageAggregator.aggregate_daily", return_value=daily), \
         patch("agy_usage.heatmap.shutil.get_terminal_size", return_value=os.terminal_size((120, 24))), \
         io.StringIO() as buf, redirect_stdout(buf):
        cmd_heatmap([_fake_session()], args)
        return buf.getvalue()


class TestHeatmapWindowBounds(unittest.TestCase):
    """TerminalHeatmap exposes public read-only window bounds derived from
    exactly the same math render() uses."""

    def test_window_bounds_match_render_math(self):
        daily = {}
        end = date(2026, 10, 28)  # A Wednesday: end must align to Sunday
        hm = TerminalHeatmap(daily, weeks=4, end_date=end, term_cols=120)
        self.assertEqual(hm.window_start_date, date(2026, 10, 5))
        self.assertEqual(hm.window_end_date, date(2026, 11, 1))
        self.assertEqual(hm.window_start_date.weekday(), 0)  # Monday
        self.assertEqual(hm.window_end_date.weekday(), 6)    # Sunday
        self.assertEqual((hm.window_end_date - hm.window_start_date).days, 4 * 7 - 1)

    def test_window_bounds_sunday_end_date_is_stable(self):
        daily = {}
        end = date(2026, 11, 1)  # Already a Sunday
        hm = TerminalHeatmap(daily, weeks=4, end_date=end, term_cols=120)
        self.assertEqual(hm.window_start_date, date(2026, 10, 5))
        self.assertEqual(hm.window_end_date, date(2026, 11, 1))
        self.assertEqual((hm.window_end_date - hm.window_start_date).days, 4 * 7 - 1)


class TestCmdHeatmapDetailsScoping(unittest.TestCase):
    """cmd_heatmap --details must never list days outside the rendered window."""

    def setUp(self):
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

    def test_in_window_date_for_scoped_details(self):
        end_sunday = _end_sunday(date.today())
        window_start = end_sunday - timedelta(days=4 * 7 - 1)
        in_window = window_start + timedelta(days=7)
        old_day = in_window - timedelta(days=365)
        daily = {in_window: _active_day(in_window), old_day: _active_day(old_day)}

        output = _run_cmd_heatmap(daily)
        self.assertIn(in_window.isoformat(), output)
        self.assertNotIn(old_day.isoformat(), output)
        self.assertIn("Last 4 Weeks", output)

    def test_empty_window_prints_message_instead_of_table(self):
        end_sunday = _end_sunday(date.today())
        window_start = end_sunday - timedelta(days=4 * 7 - 1)
        old_day = window_start - timedelta(days=365)
        daily = {old_day: _active_day(old_day)}

        output = _run_cmd_heatmap(daily)
        self.assertIn("No active days in the last 4 weeks.", output)
        self.assertNotIn("ACTIVE DAYS", output)


if __name__ == "__main__":
    unittest.main()
