"""Tests for live mode with --json and --csv."""

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

from agy_usage.cli import main as cli_main
from agy_usage.models import SessionSummary, TokenUsage


class TestLiveJsonAndCsvSemantics(unittest.TestCase):
    """Regression tests for BUG-019: `live` with `--json` (before or after the
    subcommand) must stream one compact JSON object per tick (NDJSON) with no
    ANSI screen-clear escapes or banner noise, while `live` with `--csv` is
    rejected with exit code 2 to stderr."""

    def _make_session(self, session_id: str, prompt_tokens: int) -> SessionSummary:
        t = datetime(2026, 10, 29, 12, 0, 0)
        return SessionSummary(
            session_id=session_id,
            source="cli",
            file_path=f"/tmp/{session_id}.db",
            title=f"Session {session_id}",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=TokenUsage(prompt_tokens=prompt_tokens, cached_tokens=200, output_tokens=50),
        )

    def test_live_json_streams_ndjson_per_tick_for_both_flag_positions(self):
        import json
        from contextlib import redirect_stderr

        s_tick1 = [self._make_session("sess-1", 1000)]
        s_tick2 = [self._make_session("sess-1", 1000), self._make_session("sess-2", 2500)]

        for argv in (
            ["live", "--json", "--interval", "2"],
            ["--json", "live", "--interval", "2"],
        ):
            with self.subTest(argv=argv):
                out_buf = io.StringIO()
                err_buf = io.StringIO()
                with mock.patch(
                    "agy_usage.cli.get_sessions",
                    side_effect=[s_tick1, s_tick2],
                ) as mock_get_sessions, mock.patch(
                    "agy_usage.cli.time.sleep",
                    side_effect=[None, KeyboardInterrupt()],
                ) as mock_sleep, redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = cli_main(argv)

                self.assertEqual(rc, 0)
                self.assertEqual(err_buf.getvalue(), "")
                # Must scan sessions once per tick inside cmd_live (no wasted pre-scan in main)
                self.assertEqual(mock_get_sessions.call_count, 2)
                self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [2, 2])

                raw_out = out_buf.getvalue()
                self.assertNotIn("\033", raw_out)
                self.assertNotIn("Starting Antigravity Usage live monitor", raw_out)
                self.assertNotIn("ANTIGRAVITY LIVE MONITOR", raw_out)
                self.assertNotIn("Live monitor stopped.", raw_out)

                lines = raw_out.splitlines()
                self.assertEqual(len(lines), 2)
                tick1_obj = json.loads(lines[0])
                tick2_obj = json.loads(lines[1])
                self.assertEqual(tick1_obj["summary"]["session_count"], 1)
                self.assertEqual(tick1_obj["summary"]["total_usage"]["total_tokens"], 1250)
                self.assertEqual(tick2_obj["summary"]["session_count"], 2)
                self.assertEqual(tick2_obj["summary"]["total_usage"]["total_tokens"], 4000)

    def test_live_csv_rejected_with_exit_code_2(self):
        from contextlib import redirect_stderr

        for argv in (
            ["live", "--csv"],
            ["--csv", "live"],
            ["live", "--json", "--csv"],
        ):
            with self.subTest(argv=argv):
                out_buf = io.StringIO()
                err_buf = io.StringIO()
                with mock.patch("agy_usage.cli.get_sessions") as mock_get_sessions, \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = cli_main(argv)

                self.assertEqual(rc, 2)
                self.assertEqual(out_buf.getvalue(), "")
                self.assertIn("--csv is not supported by 'live'", err_buf.getvalue())
                mock_get_sessions.assert_not_called()

    def test_live_interactive_mode_clears_screen_and_renders_overview(self):
        s_tick1 = [self._make_session("sess-interactive", 1000)]
        out_buf = io.StringIO()
        with mock.patch("agy_usage.cli.get_sessions", return_value=s_tick1) as mock_get_sessions, \
             mock.patch("agy_usage.cli.time.sleep", side_effect=KeyboardInterrupt()) as mock_sleep, \
             redirect_stdout(out_buf):
            rc = cli_main(["live", "--interval", "0"])

        self.assertEqual(rc, 0)
        # Interval 0 clamped to 1s; get_sessions called only once on tick 1
        self.assertEqual(mock_get_sessions.call_count, 1)
        mock_sleep.assert_called_once_with(1)
        out = out_buf.getvalue()
        self.assertIn("Starting Antigravity Usage live monitor (refreshing every 1s).", out)
        self.assertIn("\033[2J\033[H", out)
        self.assertIn("ANTIGRAVITY LIVE MONITOR", out)
        self.assertIn("Live monitor stopped.", out)


if __name__ == "__main__":
    unittest.main()
