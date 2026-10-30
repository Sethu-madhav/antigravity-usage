"""Tests for the 7-day token sparkline in `statusline --short` (FEAT-004)."""

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from agy_usage.aggregator import UsageAggregator
from agy_usage.cli import cmd_statusline
from agy_usage.display import Colors, render_sparkline, strip_ansi
from agy_usage.models import SessionSummary, StepRecord, TokenUsage

NOW = datetime(2026, 10, 29, 12, 0, 0)
BLOCKS = set("▂▃▄▅▆▇█")


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


class TestStatuslineSparkline(unittest.TestCase):
    def setUp(self):
        # Keep compute_all_pools from querying the live language_server.
        patcher = mock.patch("agy_usage.limits.should_use_live_server", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("agy_usage.cli.datetime", _FixedDatetime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(Colors.set_override, None)
        Colors.set_override(False)

    def _session(self, sid, days_ago, tokens):
        t = NOW - timedelta(days=days_ago)
        usage = TokenUsage(output_tokens=tokens)
        return SessionSummary(
            session_id=sid,
            source="cli",
            file_path=f"/tmp/{sid}.db",
            title=sid,
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=usage,
            steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=usage)],
        )

    def _sessions(self, per_day):
        """per_day maps days_ago -> tokens."""
        return [self._session(f"s{d}", d, tok) for d, tok in per_day.items()]

    def _args(self, **overrides):
        values = dict(
            tier="pro",
            five_h_limit=None,
            weekly_limit=None,
            short=True,
            plain=False,
            tmux=False,
            json=False,
            csv=False,
            format=None,
            no_color=False,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def _run(self, sessions, **overrides):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_statusline(sessions, self._args(**overrides))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_seven_day_window_oldest_to_newest_with_zero_days(self):
        # Today=700, 3 days ago=300, 6 days ago=100; the rest are zero days.
        out = self._run(self._sessions({0: 700, 3: 300, 6: 100}), plain=True)
        self.assertTrue(out.rstrip("\n").endswith("▂  ▄  █"), out)

    def test_usage_older_than_seven_days_is_excluded(self):
        # An 8-day-old spike must neither appear nor change the scaling.
        out = self._run(self._sessions({0: 700, 3: 300, 6: 100, 7: 70_000, 8: 90_000}), plain=True)
        self.assertTrue(out.rstrip("\n").endswith("▂  ▄  █"), out)

    def test_single_active_day(self):
        out = self._run(self._sessions({0: 500}), plain=True)
        self.assertTrue(out.rstrip("\n").endswith("      █"), out)

    def test_flat_usage_renders_mid_bars(self):
        out = self._run(self._sessions({d: 100 for d in range(7)}), plain=True)
        self.assertTrue(out.rstrip("\n").endswith("▄" * 7), out)

    def test_empty_data_has_no_sparkline_and_does_not_crash(self):
        out = self._run([], plain=True)
        self.assertFalse(BLOCKS & set(out), out)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("5h:", out)

    def test_only_stale_data_has_no_sparkline(self):
        out = self._run(self._sessions({10: 1000, 20: 5000}), plain=True)
        self.assertFalse(BLOCKS & set(out), out)
        self.assertRegex(out, r"^\[(OK|WARN|ALERT|UNKNOWN)\] 5h:\S+ Wk:\S+ \$\S+\n$")

    def test_plain_line_keeps_existing_prefix(self):
        out = self._run(self._sessions({0: 700, 3: 300}), plain=True)
        # Old short format (badge, 5h, Wk, cost) followed by exactly 7 sparkline cells.
        self.assertRegex(out, r"^\[(OK|WARN|ALERT|UNKNOWN)\] 5h:\S+ Wk:\S+ \$\S+ .{7}\n$")

    def test_color_enabled_wraps_sparkline_in_ansi(self):
        Colors.set_override(True)
        out = self._run(self._sessions({0: 700, 3: 300, 6: 100}))
        self.assertIn("\033[", out)
        self.assertIn("▂  ▄  █", strip_ansi(out))

    def test_color_disabled_has_no_ansi(self):
        for overrides in ({}, {"no_color": True}, {"plain": True}):
            with self.subTest(overrides=overrides):
                out = self._run(self._sessions({0: 700, 3: 300}), **overrides)
                self.assertNotIn("\033", out)

    def test_color_enabled_plain_flag_stays_uncolored(self):
        Colors.set_override(True)
        out = self._run(self._sessions({0: 700, 3: 300}), plain=True)
        self.assertNotIn("\033", out)
        self.assertTrue(BLOCKS & set(out))

    def test_non_short_output_has_no_sparkline(self):
        for overrides in ({"plain": True}, {}):
            with self.subTest(overrides=overrides):
                out = self._run(self._sessions({0: 700, 3: 300}), short=False, **overrides)
                self.assertFalse(BLOCKS & set(out), out)

    def test_json_and_csv_unchanged_by_short(self):
        sessions = self._sessions({0: 700, 3: 300})
        for fmt in ("json", "csv"):
            with self.subTest(fmt=fmt):
                a = self._run(sessions, short=True, **{fmt: True})
                b = self._run(sessions, short=False, **{fmt: True})
                self.assertEqual(a, b)
                self.assertFalse(BLOCKS & set(a), a)
        self.assertNotIn("sparkline", json.loads(self._run(sessions, json=True)))

    def test_single_aggregate_daily_pass(self):
        with mock.patch(
            "agy_usage.cli.UsageAggregator.aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as spy:
            self._run(self._sessions({0: 700, 3: 300}), plain=True)
        self.assertEqual(spy.call_count, 1)

    def test_render_sparkline_edge_cases(self):
        self.assertEqual(render_sparkline([]), "")
        self.assertEqual(render_sparkline([5]), "▄")
        self.assertEqual(render_sparkline([0, 0, 0]), "▄▄▄")
        self.assertEqual(render_sparkline([0, 700]), " █")


if __name__ == "__main__":
    unittest.main()
