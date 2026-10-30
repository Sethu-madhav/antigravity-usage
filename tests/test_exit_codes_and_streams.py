"""Tests for IMP-001: Exit codes and stream discipline."""

from __future__ import annotations

import io
import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch

from agy_usage.cli import (
    cmd_models,
    cmd_monthly,
    cmd_session_inspect,
    cmd_weekly,
    main as cli_main,
)
from agy_usage.display import Colors
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


class TestExitCodesAndStreams(unittest.TestCase):
    """Verify exit codes (0 on success, 1 on not found/no data) and stream separation (stdout vs stderr)."""

    def setUp(self):
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

        t = datetime(2026, 9, 1, 12, 0)
        self.sample_session = SessionSummary(
            session_id="test-session-12345678",
            source="cli",
            file_path="/tmp/test.db",
            title="Inspect Test",
            workspace="test-ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=2,
            usage=TokenUsage(
                prompt_tokens=1000,
                cached_tokens=4000,
                output_tokens=200,
                thought_tokens=50,
                candidate_tokens=150,
            ),
            steps=[
                StepRecord(
                    step_idx=1,
                    timestamp=t,
                    model="gemini-3.8-flash",
                    usage=TokenUsage(prompt_tokens=500, cached_tokens=2000, output_tokens=100),
                ),
                StepRecord(
                    step_idx=2,
                    timestamp=t,
                    model="gemini-3.8-flash",
                    usage=TokenUsage(prompt_tokens=500, cached_tokens=2000, output_tokens=100),
                ),
            ],
        )

    def _run_main(self, args: list[str], mock_sessions: list[SessionSummary] | None = None) -> tuple[int, str, str]:
        sessions = [] if mock_sessions is None else mock_sessions
        with patch("agy_usage.cli.get_sessions", return_value=sessions), \
             patch("agy_usage.heatmap.shutil.get_terminal_size", return_value=os.terminal_size((120, 24))), \
             patch("sys.stdout", new_callable=io.StringIO) as fake_out, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            ret = cli_main(args)
            return ret, fake_out.getvalue(), fake_err.getvalue()

    def test_empty_sessions_exit_code_and_stderr(self):
        """When get_sessions returns [], subcommands return 1, write error to stderr, and leave stdout empty."""
        subcommands = [
            [],
            ["overview"],
            ["heatmap"],
            ["sessions"],
            ["current"],
            ["session", "abc"],
            ["weekly"],
            ["monthly"],
            ["models"],
        ]
        for cmd in subcommands:
            with self.subTest(cmd=cmd):
                code, out, err = self._run_main(cmd, mock_sessions=[])
                self.assertEqual(code, 1, f"Expected return code 1 for command: {cmd}")
                self.assertEqual(out, "", f"Expected empty stdout for command: {cmd}")
                self.assertIn(
                    "No Antigravity conversation sessions found.",
                    err,
                    f"Expected 'No Antigravity conversation sessions found.' in stderr for command: {cmd}",
                )

    def test_session_inspect_not_found_exit_code_and_stderr(self):
        """When session_id is not found, return 1, write error + hint to stderr, and leave stdout empty."""
        code, out, err = self._run_main(["session", "nonexistent"], mock_sessions=[self.sample_session])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Session 'nonexistent' not found.", err)
        self.assertIn("Run 'agy-usage sessions' to see all available session IDs.", err)

    def test_empty_aggregation_exit_code_and_stderr(self):
        """When sessions are non-empty but aggregation returns [], return 1 and write to stderr."""
        # Session with no timestamps and no steps -> daily/weekly/monthly aggregations will be empty
        session_no_ts = SessionSummary(
            session_id="no-ts-session",
            source="cli",
            file_path="/tmp/no_ts.db",
            title="No TS",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=None,
            end_time=None,
            step_count=0,
            usage=TokenUsage(),
            steps=[],
        )

        with patch("sys.stdout", new_callable=io.StringIO) as fake_out, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            code = cmd_weekly([session_no_ts])
            self.assertEqual(code, 1)
            self.assertEqual(fake_out.getvalue(), "")
            self.assertIn("No weekly usage data found.", fake_err.getvalue())

        with patch("sys.stdout", new_callable=io.StringIO) as fake_out, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            code = cmd_monthly([session_no_ts])
            self.assertEqual(code, 1)
            self.assertEqual(fake_out.getvalue(), "")
            self.assertIn("No monthly usage data found.", fake_err.getvalue())

        # For models, when aggregate_models returns []
        with patch("agy_usage.aggregator.UsageAggregator.aggregate_models", return_value=[]), \
             patch("sys.stdout", new_callable=io.StringIO) as fake_out, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            code = cmd_models([self.sample_session])
            self.assertEqual(code, 1)
            self.assertEqual(fake_out.getvalue(), "")
            self.assertIn("No model usage data found.", fake_err.getvalue())

    def test_global_json_and_csv_exports_with_empty_sessions(self):
        """--json and --csv global exports with empty sessions still return 0 and write to stdout with empty stderr."""
        code, out, err = self._run_main(["--json"], mock_sessions=[])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        data = json.loads(out)
        self.assertIn("sessions", data)
        self.assertEqual(data["sessions"], [])

        code, out, err = self._run_main(["--csv"], mock_sessions=[])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("Session ID", out)

    def test_successful_commands_exit_code_and_stdout(self):
        """When sessions are present, commands return 0 and write results to stdout."""
        commands = [
            [],
            ["overview"],
            ["heatmap"],
            ["sessions"],
            ["current"],
            ["session", "test-session"],
            ["weekly"],
            ["monthly"],
            ["models"],
            ["limits"],
            ["statusline"],
        ]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                code, out, err = self._run_main(cmd, mock_sessions=[self.sample_session])
                self.assertEqual(code, 0, f"Expected return code 0 for command: {cmd}")
                self.assertNotEqual(out, "", f"Expected non-empty stdout for command: {cmd}")
                self.assertEqual(err, "", f"Expected empty stderr for command: {cmd}")


if __name__ == "__main__":
    unittest.main()
