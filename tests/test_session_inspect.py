"""Tests for session inspection."""

import io
import unittest
from datetime import datetime

from agy_usage.models import SessionSummary, StepRecord, TokenUsage


class TestSessionInspect(unittest.TestCase):
    def setUp(self):
        import io
        import sys
        self.io = io
        self.sys = sys
        t = datetime(2026, 9, 1, 12, 0)
        self.session = SessionSummary(
            session_id="test-session-12345",
            source="cli",
            file_path="/tmp/test.db",
            title="Inspect Test",
            workspace="test-ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=2,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200, thought_tokens=50, candidate_tokens=150),
            steps=[
                StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=500, cached_tokens=2000, output_tokens=100)),
                StepRecord(step_idx=2, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=500, cached_tokens=2000, output_tokens=100)),
            ],
        )

    def test_default_inspect_shows_totals_only(self):
        from agy_usage.cli import cmd_session_inspect
        buf = self.io.StringIO()
        old_stdout = self.sys.stdout
        try:
            self.sys.stdout = buf
            cmd_session_inspect([self.session], session_id_arg="current")
        finally:
            self.sys.stdout = old_stdout

        out = buf.getvalue()
        self.assertIn("SESSION TOTALS", out)
        self.assertIn("TOTAL PROCESSED", out)
        self.assertIn("CACHE SAVINGS", out)
        self.assertIn("BILLABLE CONSUMED", out)
        self.assertIn("ESTIMATED COST", out)
        self.assertIn("TOKEN USAGE BREAKDOWN", out)
        self.assertIn("Cost", out)
        # Verify progression row is removed
        self.assertNotIn("Progression:", out)
        self.assertNotIn("TURN-BY-TURN TOKEN LOG", out)

    def test_inspect_with_details_shows_turn_log(self):
        from agy_usage.cli import cmd_session_inspect
        buf = self.io.StringIO()
        old_stdout = self.sys.stdout
        try:
            self.sys.stdout = buf
            cmd_session_inspect([self.session], session_id_arg="current", show_details=True)
        finally:
            self.sys.stdout = old_stdout

        out = buf.getvalue()
        self.assertIn("SESSION TOTALS", out)
        self.assertIn("TURN-BY-TURN TOKEN LOG", out)
        self.assertIn("#1", out)
        self.assertIn("#2", out)
        self.assertNotIn("Progression:", out)

    def test_calculate_cost_and_format(self):
        from agy_usage.models import calculate_cost
        from agy_usage.display import format_cost
        # 1M prompt @ $0.75, 1M cache @ $0.075, 1M output @ $3.75
        p_c, c_c, o_c, tot = calculate_cost(1_000_000, 1_000_000, 1_000_000, "gemini-3.8-flash")
        self.assertAlmostEqual(p_c, 0.75)
        self.assertAlmostEqual(c_c, 0.075)
        self.assertAlmostEqual(o_c, 3.75)
        self.assertAlmostEqual(tot, 4.575)

        self.assertEqual(format_cost(4.575), "$4.58")
        self.assertEqual(format_cost(0.005), "$0.005")
        self.assertEqual(format_cost(0.0), "$0.00")


if __name__ == "__main__":
    unittest.main()
