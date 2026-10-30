"""Tests for the refresh countdown, badge mapping and limits CSV shared by `limits` and `statusline`."""

import argparse
import csv
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from agy_usage.cli import _refresh_countdown, cmd_limits, cmd_statusline
from agy_usage.display import Colors
from agy_usage.limits import WindowStats
from agy_usage.models import TokenUsage

NOW = datetime(2026, 10, 29, 12, 0, 0)


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def _window(tokens=0, refresh_in=None, unreachable=False, limit=1_000_000):
    return WindowStats(
        window_name="5h",
        start_time=NOW - timedelta(hours=5),
        end_time=NOW,
        usage=TokenUsage(output_tokens=tokens),
        step_count=1 if tokens else 0,
        session_count=1 if tokens else 0,
        limit_tokens=limit,
        estimated_cost=0.0,
        refresh_time=None if refresh_in is None else NOW + refresh_in,
        server_unreachable=unreachable,
    )


class TestRefreshCountdown(unittest.TestCase):
    def test_unknown_window(self):
        w = _window(unreachable=True, refresh_in=timedelta(hours=1))
        self.assertTrue(w.is_unknown)
        self.assertEqual(_refresh_countdown(w, NOW), "unknown")

    def test_future_refresh_with_usage(self):
        w = _window(tokens=5000, refresh_in=timedelta(hours=3, minutes=30))
        self.assertEqual(_refresh_countdown(w, NOW), "in 3h 30m")

    def test_past_refresh_is_ready(self):
        w = _window(tokens=5000, refresh_in=timedelta(minutes=-5))
        self.assertEqual(_refresh_countdown(w, NOW), "Ready")

    def test_refresh_exactly_now_is_ready(self):
        w = _window(tokens=5000, refresh_in=timedelta(0))
        self.assertEqual(_refresh_countdown(w, NOW), "Ready")

    def test_no_usage_is_ready_even_with_future_refresh(self):
        w = _window(tokens=0, refresh_in=timedelta(hours=2))
        self.assertEqual(w.pct_used, 0.0)
        self.assertEqual(_refresh_countdown(w, NOW), "Ready")

    def test_no_refresh_time_is_ready(self):
        self.assertEqual(_refresh_countdown(_window(tokens=5000), NOW), "Ready")


class _CliCase(unittest.TestCase):
    def setUp(self):
        # Keep compute_all_pools from querying the live language_server.
        for target, kwargs in (
            ("agy_usage.limits.should_use_live_server", dict(return_value=False)),
            ("agy_usage.cli.datetime", dict(new=_FixedDatetime)),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(Colors.set_override, None)
        Colors.set_override(False)

    def _run(self, func, **overrides):
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
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = func([], argparse.Namespace(**values))
        self.assertEqual(rc, 0)
        return buf.getvalue()


class TestStatuslineBadgeMapping(_CliCase):
    CASES = (
        ("🔴", "[ALERT]", "critical", "red"),
        ("🟡", "[WARN]", "warning", "yellow"),
        ("⚪", "[UNKNOWN]", "unknown", "colour244"),
        ("🟢", "[OK]", "healthy", "green"),
    )

    def _run_with_badge(self, badge, **overrides):
        with mock.patch("agy_usage.cli.get_status_badge", return_value=(badge, "msg")):
            return self._run(cmd_statusline, **overrides)

    def test_json_status_and_badge_plain(self):
        for badge, plain, level, _ in self.CASES:
            with self.subTest(badge=badge):
                payload = json.loads(self._run_with_badge(badge, json=True))
                self.assertEqual(payload["badge"], badge)
                self.assertEqual(payload["badge_plain"], plain)
                self.assertEqual(payload["status"], level)

    def test_unexpected_badge_falls_back_to_healthy(self):
        payload = json.loads(self._run_with_badge("?", json=True))
        self.assertEqual(payload["badge"], "🟢")
        self.assertEqual(payload["badge_plain"], "[OK]")
        self.assertEqual(payload["status"], "healthy")

    def test_tmux_colour(self):
        for badge, _, _, tmux_color in self.CASES:
            with self.subTest(badge=badge):
                out = self._run_with_badge(badge, tmux=True)
                self.assertTrue(out.startswith(f"#[fg={tmux_color}]● "), out)

    def test_format_badge_and_status_follow_plain_flag(self):
        for badge, plain, _, _ in self.CASES:
            with self.subTest(badge=badge):
                self.assertEqual(
                    self._run_with_badge(badge, format="{status}|{badge}", plain=True),
                    f"{plain}|{plain}\n",
                )
                # Colours are off in this test, so the plain badge is used either way.
                self.assertEqual(
                    self._run_with_badge(badge, format="{status}|{badge}"),
                    f"{plain}|{plain}\n",
                )

        Colors.set_override(True)
        self.assertEqual(
            self._run_with_badge("🔴", format="{status}|{badge}"),
            "🔴|🔴\n",
        )
        self.assertEqual(
            self._run_with_badge("🔴", format="{status}|{badge}", plain=True),
            "[ALERT]|[ALERT]\n",
        )


class TestLimitsCsvRows(_CliCase):
    def test_header_plus_five_labelled_rows(self):
        out = self._run(cmd_limits, csv=True)
        self.assertEqual(len(out.strip().splitlines()), 6)
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[0][0], "Window")
        self.assertEqual(
            [row[0] for row in rows[1:]],
            [
                "5-Hour Sprint",
                "Weekly Cap",
                "Argon Weekly Cap",
                "Claude & GPT 5-Hour",
                "Claude & GPT Weekly Cap",
            ],
        )
        self.assertTrue(all(len(row) == len(rows[0]) for row in rows))


if __name__ == "__main__":
    unittest.main()
