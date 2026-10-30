"""Tests for IMP-004 truncation hygiene: displayed text is truncated with a
visible "…" marker via ``truncate_ansi`` instead of being hard-sliced, so the
CLI no longer silently drops content mid-word and cell widths stay exact."""

import argparse
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest.mock import patch

from agy_usage.cli import cmd_heatmap, cmd_sessions
from agy_usage.display import Colors, strip_ansi, truncate_ansi
from agy_usage.models import SessionSummary, StepRecord, TokenUsage

LONG_PLAIN = "abcdefghij" * 5  # 50 printable characters


def make_session(**overrides):
    """Build a SessionSummary locally; keyword args override the defaults."""
    end = datetime.now() - timedelta(hours=20)
    fields = dict(
        session_id="abcdef1234567890",
        source="cli",
        file_path="/tmp/session-fixture.jsonl",
        title="T" * 40,
        workspace="W" * 40,
        models=["gemini-3.8-flash"],
        start_time=end - timedelta(minutes=5),
        end_time=end,
        step_count=1,
    )
    fields.update(overrides)
    return SessionSummary(**fields)


def run_cmd(fn, *fn_args):
    """Call a CLI command with captured stdout; return (returncode, output)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*fn_args)
    return rc, buf.getvalue()


def sessions_args():
    return argparse.Namespace(sort="date", limit=10)


def heatmap_details_args():
    return argparse.Namespace(metric="total", weeks=4, details=True)


def force_no_colors():
    """Keep rendering deterministic in piped/CI stdout."""
    Colors.set_override(False)
    return (Colors.set_override, None)


class TestTruncateAnsi(unittest.TestCase):
    """Unit tests for the truncate_ansi helper itself."""

    def test_short_strings_returned_unchanged(self):
        # Well under the cap.
        self.assertEqual(truncate_ansi("short", 14), "short")
        self.assertEqual(truncate_ansi("", 32), "")

        # Exactly at max: unchanged, no ellipsis appended.
        self.assertEqual(truncate_ansi("a" * 14, 14), "a" * 14)
        self.assertEqual(truncate_ansi("x" * 32, 32), "x" * 32)

        # ANSI-wrapped string whose visible length fits is also untouched.
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)
        colored_short = Colors.cyan("abc")
        self.assertEqual(truncate_ansi(colored_short, 14), colored_short)

    def test_long_plain_string_truncates_with_ellipsis(self):
        result = truncate_ansi(LONG_PLAIN, 25)
        self.assertEqual(len(strip_ansi(result)), 25)
        self.assertTrue(result.endswith("…"))
        # All but the final … marker are the original leading characters.
        self.assertEqual(result[:-1], LONG_PLAIN[:24])
        self.assertEqual(result, LONG_PLAIN[:24] + "…")

    def test_long_ansi_string_truncates_with_ellipsis_and_reset(self):
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

        text = Colors.cyan("x" * 40)
        result = truncate_ansi(text, 32)

        self.assertIn(Colors.RESET, result)
        self.assertEqual(len(strip_ansi(result)), 32)
        self.assertTrue(result.endswith("…"))
        # The opening color code survives truncation, so the cell still styles.
        self.assertTrue(result.startswith(Colors.BRIGHT_CYAN))


class TestCmdSessionsTruncation(unittest.TestCase):
    """End-to-end: the sessions table truncates long workspace/title cells
    with an ellipsis and leaves short values untouched."""

    def test_long_values_show_ellipsis(self):
        self.addCleanup(*force_no_colors())

        session = make_session(workspace="W" * 40, title="T" * 40)
        rc, out = run_cmd(cmd_sessions, [session], sessions_args())
        self.assertEqual(rc, 0)

        # 14-wide workspace cell: 13 chars + "…"; 32-wide title: 31 + "…".
        self.assertIn("W" * 13 + "…", out)
        self.assertIn("T" * 31 + "…", out)
        self.assertNotIn("W" * 40, out)
        self.assertNotIn("T" * 40, out)

    def test_short_values_unchanged(self):
        self.addCleanup(*force_no_colors())

        session = make_session(workspace="ws", title="short title")
        rc, out = run_cmd(cmd_sessions, [session], sessions_args())
        self.assertEqual(rc, 0)

        self.assertIn("ws", out)
        self.assertIn("short title", out)
        self.assertNotIn("…", out)


class TestCmdHeatmapDetailsTruncation(unittest.TestCase):
    """End-to-end: heatmap --details truncates the Models cell at 25 visible
    characters with an ellipsis instead of hard-slicing."""

    def test_long_model_name_truncated_in_details_table(self):
        self.addCleanup(*force_no_colors())

        long_model = "a-very-long-model-name-over-25-chars"
        step = StepRecord(
            step_idx=0,
            timestamp=datetime.now() - timedelta(days=1),
            model=long_model,
            usage=TokenUsage(prompt_tokens=500, output_tokens=100),
        )
        session = make_session(
            steps=[step],
            models=[long_model],
            step_count=1,
        )

        with patch(
            "agy_usage.heatmap.shutil.get_terminal_size",
            return_value=os.terminal_size((120, 24)),
        ):
            rc, out = run_cmd(cmd_heatmap, [session], heatmap_details_args())
        self.assertEqual(rc, 0)

        expected = long_model[:24] + "…"
        self.assertIn(expected, out)
        self.assertNotIn(long_model, out)


if __name__ == "__main__":
    unittest.main()
