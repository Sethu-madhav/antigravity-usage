"""Tests for clamping the heatmap week count."""

import io
import os
import unittest
from datetime import datetime

from agy_usage.display import Colors
from agy_usage.heatmap import TerminalHeatmap
from agy_usage.models import SessionSummary


class TestHeatmapWeeksClamping(unittest.TestCase):
    """Regression tests for BUG-014: -w flag accepted out-of-range values
    (e.g. -w 500 when help specifies 'up to 52') rendering hundreds of columns
    that wrap into terminal garbage. Weeks must be clamped to [4, 52] both at
    CLI parse time and in TerminalHeatmap, and a terminal-width warning must be
    emitted when the heatmap exceeds the available terminal columns."""

    def test_parse_args_clamps_weeks_to_supported_range(self):
        from agy_usage.cli import parse_args

        # Over 52 weeks clamped to 52
        args = parse_args(["heatmap", "-w", "500"])
        self.assertEqual(args.weeks, 52)

        args = parse_args(["heatmap", "--weeks", "100"])
        self.assertEqual(args.weeks, 52)

        # Under 4 weeks clamped to 4
        args = parse_args(["heatmap", "-w", "1"])
        self.assertEqual(args.weeks, 4)

        args = parse_args(["heatmap", "-w", "0"])
        self.assertEqual(args.weeks, 4)

        args = parse_args(["heatmap", "-w", "-10"])
        self.assertEqual(args.weeks, 4)

        # Within bounds preserved
        args = parse_args(["heatmap", "-w", "4"])
        self.assertEqual(args.weeks, 4)

        args = parse_args(["heatmap", "-w", "26"])
        self.assertEqual(args.weeks, 26)

        args = parse_args(["heatmap", "-w", "52"])
        self.assertEqual(args.weeks, 52)

        # Default remains 26
        args = parse_args(["heatmap"])
        self.assertEqual(args.weeks, 26)

    def test_terminal_heatmap_clamps_weeks_to_supported_range(self):
        daily = {}
        # Explicit out-of-range values clamped to [4, 52]
        hm = TerminalHeatmap(daily, weeks=500)
        self.assertEqual(hm.num_weeks, 52)

        hm = TerminalHeatmap(daily, weeks=1)
        self.assertEqual(hm.num_weeks, 4)

        hm = TerminalHeatmap(daily, weeks=0)
        self.assertEqual(hm.num_weeks, 4)

        hm = TerminalHeatmap(daily, weeks=-5)
        self.assertEqual(hm.num_weeks, 4)

        hm = TerminalHeatmap(daily, weeks=52)
        self.assertEqual(hm.num_weeks, 52)

        hm = TerminalHeatmap(daily, weeks=16)
        self.assertEqual(hm.num_weeks, 16)

        # Default when weeks is None adapts to term_cols up to 26
        hm_default = TerminalHeatmap(daily, weeks=None, term_cols=120)
        self.assertEqual(hm_default.num_weeks, 26)

    def test_terminal_heatmap_required_cols_and_narrow_check(self):
        daily = {}
        # 26 weeks requires 26 * 2 + 8 = 60 columns
        hm_26 = TerminalHeatmap(daily, weeks=26, term_cols=80)
        self.assertEqual(hm_26.required_cols, 60)
        self.assertFalse(hm_26.is_terminal_narrow)

        # In a 50-column terminal, 60 columns wraps
        hm_narrow = TerminalHeatmap(daily, weeks=26, term_cols=50)
        self.assertEqual(hm_narrow.required_cols, 60)
        self.assertTrue(hm_narrow.is_terminal_narrow)

        # 52 weeks requires 52 * 2 + 8 = 112 columns; in 80 cols it wraps
        hm_52 = TerminalHeatmap(daily, weeks=52, term_cols=80)
        self.assertEqual(hm_52.required_cols, 112)
        self.assertTrue(hm_52.is_terminal_narrow)

        # In 120-column terminal, 52 weeks fits
        hm_wide = TerminalHeatmap(daily, weeks=52, term_cols=120)
        self.assertFalse(hm_wide.is_terminal_narrow)

    def test_cmd_heatmap_warns_on_narrow_terminal(self):
        import io
        from unittest.mock import patch
        from agy_usage.cli import cmd_heatmap, parse_args
        from agy_usage.models import SessionSummary

        fake_session = SessionSummary(
            session_id="s1",
            source="cli",
            file_path="/fake/path",
            title="Test Session",
            workspace="ws",
            start_time=datetime(2026, 10, 20),
            end_time=datetime(2026, 10, 20),
            models=["gemini-3.8-flash"],
        )
        fake_session.usage.prompt_tokens = 500

        # When terminal width is 80 and weeks is 52 (needs 112 columns), warn on stderr
        args = parse_args(["heatmap", "-w", "52"])
        with patch("agy_usage.heatmap.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))), \
             patch("sys.stdout", new_callable=io.StringIO) as fake_out, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            cmd_heatmap([fake_session], args)
            output = fake_err.getvalue()
            self.assertIn("Warning: Terminal width (80) is narrower than heatmap (112 columns); output may wrap.", output)
            self.assertNotIn("Warning: Terminal width", fake_out.getvalue())

        # When terminal width is 120 and weeks is 26 (needs 60 columns), do not warn
        args_wide = parse_args(["heatmap", "-w", "26"])
        with patch("agy_usage.heatmap.shutil.get_terminal_size", return_value=os.terminal_size((120, 24))), \
             patch("sys.stdout", new_callable=io.StringIO) as fake_out, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            cmd_heatmap([fake_session], args_wide)
            output = fake_err.getvalue()
            self.assertNotIn("Warning: Terminal width", output)
            self.assertNotIn("Warning: Terminal width", fake_out.getvalue())

        # With --no-color, warning on stderr contains no ANSI escape codes
        args_no_color = parse_args(["heatmap", "-w", "52", "--no-color"])
        with patch("agy_usage.heatmap.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))), \
             patch("sys.stdout", new_callable=io.StringIO) as fake_out, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            Colors.set_override(False)
            self.addCleanup(Colors.set_override, None)
            cmd_heatmap([fake_session], args_no_color)
            output = fake_err.getvalue()
            warn_line = [ln for ln in output.splitlines() if "Warning: Terminal width" in ln][0]
            self.assertNotIn("\033", warn_line)
            self.assertIn("Warning: Terminal width (80) is narrower than heatmap (112 columns); output may wrap.", warn_line)
            self.assertNotIn("Warning: Terminal width", fake_out.getvalue())


if __name__ == "__main__":
    unittest.main()
