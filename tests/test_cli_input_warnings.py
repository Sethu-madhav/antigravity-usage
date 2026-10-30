"""Tests for CLI input validation, warnings on silently-ignored inputs, and exclusivity."""

import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest import mock

from agy_usage import limits
from agy_usage.cli import cmd_limits, cmd_statusline, main as cli_main, parse_args
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


def _mock_session() -> SessionSummary:
    t = datetime.now()
    usage = TokenUsage(
        prompt_tokens=10_000,
        cached_tokens=40_000,
        output_tokens=2_000,
        thought_tokens=500,
        candidate_tokens=1_500,
    )
    return SessionSummary(
        session_id="s-warning-test",
        source="cli",
        file_path="/tmp/nonexistent-warning-test.db",
        title="Warning Test Session",
        workspace="test-ws",
        models=["gemini-3.8-flash"],
        start_time=t,
        end_time=t,
        step_count=1,
        usage=usage,
        steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=usage)],
    )


class TestDataSourceExclusivity(unittest.TestCase):
    """Test data-source flag exclusivity (--dir, --cli-only, --ide-only)."""

    def _assert_exclusivity_error(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                parse_args(argv)
        self.assertEqual(cm.exception.code, 2, f"Expected exit code 2 for {argv}")
        err_val = err.getvalue()
        self.assertIn("error: argument", err_val)
        self.assertIn("not allowed with argument", err_val)

    def test_dir_and_cli_only_exclusivity(self):
        # Both before subcommand
        self._assert_exclusivity_error(["--dir", "/custom/path", "--cli-only"])
        self._assert_exclusivity_error(["--dir", "/custom/path", "--cli-only", "sessions"])
        # Both after subcommand
        self._assert_exclusivity_error(["sessions", "--dir", "/custom/path", "--cli-only"])
        # One before, one after
        self._assert_exclusivity_error(["--dir", "/custom/path", "sessions", "--cli-only"])
        self._assert_exclusivity_error(["--cli-only", "sessions", "--dir", "/custom/path"])

    def test_dir_and_ide_only_exclusivity(self):
        # Both before subcommand
        self._assert_exclusivity_error(["--dir", "/custom/path", "--ide-only"])
        self._assert_exclusivity_error(["--dir", "/custom/path", "--ide-only", "sessions"])
        # Both after subcommand
        self._assert_exclusivity_error(["sessions", "--dir", "/custom/path", "--ide-only"])
        # One before, one after
        self._assert_exclusivity_error(["--dir", "/custom/path", "sessions", "--ide-only"])
        self._assert_exclusivity_error(["--ide-only", "sessions", "--dir", "/custom/path"])

    def test_cli_only_and_ide_only_exclusivity(self):
        # Both before subcommand
        self._assert_exclusivity_error(["--cli-only", "--ide-only"])
        self._assert_exclusivity_error(["--cli-only", "--ide-only", "sessions"])
        # Both after subcommand
        self._assert_exclusivity_error(["sessions", "--cli-only", "--ide-only"])
        # One before, one after
        self._assert_exclusivity_error(["--cli-only", "sessions", "--ide-only"])
        self._assert_exclusivity_error(["--ide-only", "sessions", "--cli-only"])


class TestInvalidLimitArgumentWarnings(unittest.TestCase):
    """Test warnings on unparseable custom limit values in limits and statusline."""

    def _run_cli(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        session = _mock_session()
        with mock.patch("agy_usage.cli.get_sessions", return_value=[session]):
            with mock.patch.object(limits, "fetch_live_quota_summary", return_value=None):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_limits_warns_and_falls_back_on_invalid_values(self):
        test_cases = [
            ("--5h-limit", "5OM", "five_hour_window"),
            ("--weekly-limit", "bogus", "weekly_budget"),
            ("--argon-weekly-limit", "-5M", "argon_weekly_budget"),
            ("--claude-5h-limit", "nan", None),
            ("--claude-weekly-limit", "bad", None),
        ]
        for flag, invalid_val, _ in test_cases:
            with self.subTest(flag=flag, val=invalid_val):
                rc, out, err = self._run_cli(["limits", flag, invalid_val, "--json"])
                self.assertEqual(rc, 0)
                expected_warning = (
                    f"Warning: ignoring invalid {flag} value {invalid_val!r}; falling back to tier default."
                )
                self.assertIn(expected_warning, err)
                # stdout must remain clean JSON
                data = json.loads(out)
                self.assertEqual(data["tier"], "pro")

    def test_statusline_warns_and_falls_back_on_invalid_values(self):
        test_cases = [
            ("--5h-limit", "5OM"),
            ("--weekly-limit", "bogus"),
            ("--argon-weekly-limit", "-5M"),
            ("--claude-5h-limit", "nan"),
            ("--claude-weekly-limit", "bad"),
        ]
        for flag, invalid_val in test_cases:
            with self.subTest(flag=flag, val=invalid_val):
                rc, out, err = self._run_cli(["statusline", flag, invalid_val, "--json"])
                self.assertEqual(rc, 0)
                expected_warning = (
                    f"Warning: ignoring invalid {flag} value {invalid_val!r}; falling back to tier default."
                )
                self.assertIn(expected_warning, err)
                data = json.loads(out)
                self.assertEqual(data["tier"], "pro")
                self.assertEqual(data["five_hour_limit_tokens"], 100_000_000)

    def test_multiple_invalid_limits_warn_each(self):
        rc, out, err = self._run_cli([
            "limits",
            "--5h-limit", "5OM",
            "--weekly-limit", "bogus",
            "--argon-weekly-limit", "-5M",
            "--claude-5h-limit", "nan",
            "--json",
        ])
        self.assertEqual(rc, 0)
        self.assertIn("Warning: ignoring invalid --5h-limit value '5OM'", err)
        self.assertIn("Warning: ignoring invalid --weekly-limit value 'bogus'", err)
        self.assertIn("Warning: ignoring invalid --argon-weekly-limit value '-5M'", err)
        self.assertIn("Warning: ignoring invalid --claude-5h-limit value 'nan'", err)
        data = json.loads(out)
        self.assertEqual(data["tier"], "pro")

    def test_valid_limits_emit_no_warning(self):
        rc, out, err = self._run_cli(["limits", "--5h-limit", "50M", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        data = json.loads(out)
        self.assertEqual(data["five_hour_window"]["limit_tokens"], 50_000_000)


class TestJsonCsvPrecedenceWarning(unittest.TestCase):
    """Test warning when both --json and --csv flags are provided."""

    def _run_cli(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        session = _mock_session()
        with mock.patch("agy_usage.cli.get_sessions", return_value=[session]):
            with mock.patch.object(limits, "fetch_live_quota_summary", return_value=None):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_both_json_and_csv_on_limits(self):
        for argv in (["limits", "--json", "--csv"], ["limits", "--csv", "--json"], ["--json", "--csv", "limits"]):
            with self.subTest(argv=argv):
                rc, out, err = self._run_cli(argv)
                self.assertEqual(rc, 0)
                self.assertIn("Warning: both --json and --csv specified; using --json.", err)
                data = json.loads(out)
                self.assertIn("five_hour_window", data)

    def test_both_json_and_csv_on_statusline(self):
        for argv in (["statusline", "--json", "--csv"], ["statusline", "--csv", "--json"], ["--json", "--csv", "statusline"]):
            with self.subTest(argv=argv):
                rc, out, err = self._run_cli(argv)
                self.assertEqual(rc, 0)
                self.assertIn("Warning: both --json and --csv specified; using --json.", err)
                data = json.loads(out)
                self.assertIn("five_hour_pct", data)

    def test_both_json_and_csv_on_sessions(self):
        for argv in (["sessions", "--json", "--csv"], ["sessions", "--csv", "--json"], ["--json", "--csv", "sessions"]):
            with self.subTest(argv=argv):
                rc, out, err = self._run_cli(argv)
                self.assertEqual(rc, 0)
                self.assertIn("Warning: both --json and --csv specified; using --json.", err)
                data = json.loads(out)
                self.assertIn("sessions", data)

    def test_cmd_limits_direct_call_warns_on_both(self):
        out = io.StringIO()
        err = io.StringIO()
        args = argparse.Namespace(
            tier="pro",
            five_h_limit=None,
            weekly_limit=None,
            argon_weekly_limit=None,
            argon_remaining_pct=None,
            claude_five_h_limit=None,
            claude_weekly_limit=None,
            json=True,
            csv=True,
        )
        with mock.patch.object(limits, "fetch_live_quota_summary", return_value=None):
            with redirect_stdout(out), redirect_stderr(err):
                cmd_limits([_mock_session()], args)
        self.assertIn("Warning: both --json and --csv specified; using --json.", err.getvalue())
        data = json.loads(out.getvalue())
        self.assertIn("tier", data)

    def test_cmd_statusline_direct_call_warns_on_both(self):
        out = io.StringIO()
        err = io.StringIO()
        args = argparse.Namespace(
            tier="pro",
            five_h_limit=None,
            weekly_limit=None,
            argon_weekly_limit=None,
            argon_remaining_pct=None,
            claude_five_h_limit=None,
            claude_weekly_limit=None,
            short=False,
            plain=False,
            tmux=False,
            json=True,
            csv=True,
            format=None,
            no_color=False,
        )
        with mock.patch.object(limits, "fetch_live_quota_summary", return_value=None):
            with redirect_stdout(out), redirect_stderr(err):
                cmd_statusline([_mock_session()], args)
        self.assertIn("Warning: both --json and --csv specified; using --json.", err.getvalue())
        data = json.loads(out.getvalue())
        self.assertIn("tier", data)


class TestStatuslineTmuxIgnoredFlagsWarnings(unittest.TestCase):
    """Test warnings when statusline --tmux ignores --short or --plain."""

    def _run_cli(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        session = _mock_session()
        with mock.patch("agy_usage.cli.get_sessions", return_value=[session]):
            with mock.patch.object(limits, "fetch_live_quota_summary", return_value=None):
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_tmux_warns_ignoring_short(self):
        rc, out, err = self._run_cli(["statusline", "--tmux", "--short"])
        self.assertEqual(rc, 0)
        self.assertIn("Warning: --tmux ignores --short.", err)
        self.assertIn("#[fg=", out)

    def test_tmux_warns_ignoring_plain(self):
        rc, out, err = self._run_cli(["statusline", "--tmux", "--plain"])
        self.assertEqual(rc, 0)
        self.assertIn("Warning: --tmux ignores --plain.", err)
        self.assertIn("#[fg=", out)

    def test_tmux_warns_ignoring_both_short_and_plain(self):
        rc, out, err = self._run_cli(["statusline", "--tmux", "--short", "--plain"])
        self.assertEqual(rc, 0)
        self.assertIn("Warning: --tmux ignores --short, --plain.", err)
        self.assertIn("#[fg=", out)

    def test_tmux_without_ignored_flags_emits_no_warning(self):
        rc, out, err = self._run_cli(["statusline", "--tmux"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("#[fg=", out)

    def test_format_warns_ignoring_tmux_and_short(self):
        rc, out, err = self._run_cli(["statusline", "--format", "{tier}", "--tmux", "--short"])
        self.assertEqual(rc, 0)
        self.assertIn("Warning: --format ignores --tmux, --short.", err)
        self.assertEqual(out.strip(), "pro")


if __name__ == "__main__":
    unittest.main()
