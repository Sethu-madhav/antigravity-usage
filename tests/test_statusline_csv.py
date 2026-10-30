"""Tests for `statusline --csv`."""

import argparse
import csv
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

from agy_usage.cli import cmd_statusline, main as cli_main
from agy_usage.models import SessionSummary, StepRecord, TokenUsage

FIELDS = [
    "status",
    "badge",
    "tier",
    "five_hour_pct",
    "five_hour_refresh",
    "five_hour_used_tokens",
    "five_hour_limit_tokens",
    "weekly_pct",
    "weekly_refresh",
    "weekly_used_tokens",
    "weekly_limit_tokens",
    "argon_weekly_pct",
    "argon_weekly_refresh",
    "argon_weekly_used_tokens",
    "argon_weekly_limit_tokens",
    "today_tokens",
    "today_tokens_str",
    "today_cost_usd",
    "today_cost_str",
]


class TestStatuslineCsv(unittest.TestCase):
    """Regression tests for BUG-024: `statusline --csv` (before or after the
    subcommand) used to be silently ignored and print the one-line status."""

    def setUp(self):
        # IMP-006: keep `compute_all_pools` (reached through cmd_statusline with
        # the fake path /tmp/ssc.db) from querying the live language_server.
        patcher = mock.patch("agy_usage.limits.should_use_live_server", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self):
        t = datetime.now()
        usage = TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200)
        return SessionSummary(
            session_id="s-status-csv",
            source="cli",
            file_path="/tmp/ssc.db",
            title="Statusline CSV Test",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=usage,
            steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=usage)],
        )

    def _args(self, **overrides):
        values = dict(
            tier="pro",
            five_h_limit=None,
            weekly_limit=None,
            short=False,
            plain=False,
            tmux=False,
            json=False,
            csv=False,
            format=None,
            no_color=False,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def _capture_cmd(self, **overrides):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_statusline([self._session()], self._args(**overrides))
        return buf.getvalue()

    def _run_main(self, argv):
        buf = io.StringIO()
        with mock.patch("agy_usage.cli.get_sessions", return_value=[self._session()]):
            with redirect_stdout(buf):
                rc = cli_main(argv)
        return rc, buf.getvalue()

    def _parse_csv(self, text):
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_csv_matches_json_payload(self):
        csv_row = self._parse_csv(self._capture_cmd(csv=True))
        payload = json.loads(self._capture_cmd(json=True))

        self.assertEqual(list(csv_row.keys()), FIELDS)
        self.assertEqual(list(payload.keys()), FIELDS)
        for field in FIELDS:
            self.assertEqual(csv_row[field], str(payload[field]), field)

    def test_csv_has_header_and_one_data_row(self):
        out = self._capture_cmd(csv=True)
        self.assertEqual(len(out.strip().splitlines()), 2)
        self.assertEqual(out.splitlines()[0], ",".join(FIELDS))

    def test_main_csv_after_and_before_subcommand(self):
        for argv in (["statusline", "--csv"], ["--csv", "statusline"]):
            with self.subTest(argv=argv):
                rc, out = self._run_main(argv)
                self.assertEqual(rc, 0)
                self.assertEqual(len(out.strip().splitlines()), 2)
                self.assertEqual(self._parse_csv(out).keys(), set(FIELDS))
                self.assertNotIn("\033", out)
                for fragment in ("5h:", "Wk:", "Today:"):
                    self.assertNotIn(fragment, out)

    def test_csv_respects_tier_and_custom_limit(self):
        _, out = self._run_main(["statusline", "--csv", "--tier", "ultra"])
        self.assertEqual(self._parse_csv(out)["tier"].lower(), "ultra")

        _, out = self._run_main(["statusline", "--csv", "--5h-limit", "50M"])
        self.assertEqual(self._parse_csv(out)["five_hour_limit_tokens"], str(50_000_000))


if __name__ == "__main__":
    unittest.main()
