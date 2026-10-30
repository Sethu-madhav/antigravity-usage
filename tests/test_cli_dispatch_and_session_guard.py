"""Tests for subcommand dispatch, the shared empty-sessions guard and the `sessions --sort` keys."""

from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from agy_usage.cli import main as cli_main
from agy_usage.display import Colors
from agy_usage.models import SessionSummary, TokenUsage

NOW = datetime(2026, 10, 29, 12, 0, 0)
NO_SESSIONS = "No Antigravity conversation sessions found.\n"


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class _CliTestCase(unittest.TestCase):
    def setUp(self):
        # Keep limits/statusline from querying the live language_server.
        patcher = mock.patch("agy_usage.limits.should_use_live_server", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("agy_usage.cli.datetime", _FixedDatetime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(Colors.set_override, None)

    def _run(self, argv, sessions):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("agy_usage.cli.get_sessions", return_value=sessions):
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestEmptySessionsGuard(_CliTestCase):
    def test_every_session_command_warns_on_stderr_and_exits_1(self):
        cases = [
            [],
            ["overview"],
            ["heatmap"],
            ["sessions"],
            ["current"],
            ["session", "x"],
            ["weekly"],
            ["monthly"],
            ["models"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                rc, out, err = self._run(["--no-color"] + argv, [])
                self.assertEqual(rc, 1)
                self.assertEqual(out, "")
                self.assertEqual(err, NO_SESSIONS)


class TestDispatchRouting(_CliTestCase):
    SESSIONS = [object()]

    def _route(self, cmd_name, argv, rc=42):
        with mock.patch(f"agy_usage.cli.{cmd_name}", mock.Mock(return_value=rc)) as cmd:
            got, out, err = self._run(argv, self.SESSIONS)
        self.assertEqual(got, rc)
        self.assertEqual((out, err), ("", ""))
        return cmd

    def test_no_subcommand_and_overview_go_to_overview(self):
        for argv in ([], ["overview"]):
            with self.subTest(argv=argv):
                cmd = self._route("cmd_overview", argv, rc=11)
                cmd.assert_called_once()
                self.assertIs(cmd.call_args.args[0], self.SESSIONS)

    def test_heatmap_and_sessions_receive_sessions_and_args(self):
        for name, sub, rc in (("cmd_heatmap", "heatmap", 12), ("cmd_sessions", "sessions", 13)):
            with self.subTest(sub=sub):
                cmd = self._route(name, [sub], rc=rc)
                cmd.assert_called_once()
                sessions, args = cmd.call_args.args
                self.assertIs(sessions, self.SESSIONS)
                self.assertEqual(args.subcommand, sub)

    def test_weekly_monthly_models_receive_only_sessions(self):
        for name, sub, rc in (
            ("cmd_weekly", "weekly", 14),
            ("cmd_monthly", "monthly", 15),
            ("cmd_models", "models", 16),
        ):
            with self.subTest(sub=sub):
                cmd = self._route(name, [sub], rc=rc)
                cmd.assert_called_once_with(self.SESSIONS)

    def test_current_inspects_current_session(self):
        cmd = self._route("cmd_session_inspect", ["current"], rc=17)
        cmd.assert_called_once_with(self.SESSIONS, "current", turns_limit=None, show_details=False)

    def test_session_passes_id_turns_and_details(self):
        cmd = self._route("cmd_session_inspect", ["session", "abc", "-n", "5", "--details"], rc=18)
        cmd.assert_called_once_with(self.SESSIONS, "abc", turns_limit=5, show_details=True)

    def test_unknown_subcommand_falls_back_to_overview(self):
        ns = argparse.Namespace(subcommand="bogus", no_color=False, json=False, csv=False)
        with mock.patch("agy_usage.cli.parse_args", return_value=ns):
            with mock.patch("agy_usage.cli.cmd_overview", mock.Mock(return_value=19)) as cmd:
                with mock.patch("agy_usage.cli.get_sessions", return_value=self.SESSIONS):
                    self.assertEqual(cli_main([]), 19)
        cmd.assert_called_once_with(self.SESSIONS, ns)


class TestSessionsSortKeys(_CliTestCase):
    def _session(self, sid, tokens, cached, steps, duration, start):
        end = start + timedelta(seconds=duration) if start else None
        return SessionSummary(
            session_id=f"{sid * 8}-0000",
            source="cli",
            file_path=f"/tmp/{sid}.db",
            title=f"title {sid}",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=start,
            end_time=end,
            step_count=steps,
            usage=TokenUsage(prompt_tokens=tokens - cached, cached_tokens=cached),
        )

    def setUp(self):
        super().setUp()
        base = NOW - timedelta(days=10)
        # D has no start_time, so it sorts last by date (key 0) and by duration (0s).
        self.sessions = [
            self._session("a", 100, 5, 2, 900, base + timedelta(days=2)),
            self._session("b", 300, 1, 9, 10, base + timedelta(days=1)),
            self._session("c", 200, 9, 5, 500, base + timedelta(days=3)),
            self._session("d", 250, 3, 1, 0, None),
        ]

    def _order(self, argv):
        rc, out, err = self._run(["--no-color", "sessions"] + argv, self.sessions)
        self.assertEqual((rc, err), (0, ""))
        found = [(out.index(s.short_id), s.short_id[0]) for s in self.sessions]
        return "".join(letter for _, letter in sorted(found))

    def test_each_sort_key_orders_descending(self):
        expected = {
            "tokens": "bdca",
            "cached": "cadb",
            "steps": "bcad",
            "duration": "acbd",
            "date": "cabd",
        }
        for key, order in expected.items():
            with self.subTest(sort=key):
                self.assertEqual(self._order(["--sort", key]), order)

    def test_default_sort_is_date(self):
        self.assertEqual(self._order([]), "cabd")

    def test_ties_keep_input_order(self):
        for s in self.sessions:
            s.step_count = 3
        self.assertEqual(self._order(["--sort", "steps"]), "abcd")


class TestColorOverrideGetter(unittest.TestCase):
    def setUp(self):
        self.addCleanup(Colors.set_override, Colors.get_override())

    def test_get_override_returns_last_value_set(self):
        for value in (True, False, None):
            with self.subTest(value=value):
                Colors.set_override(value)
                self.assertIs(Colors.get_override(), value)


if __name__ == "__main__":
    unittest.main()
