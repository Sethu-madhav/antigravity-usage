"""Tests for the limits fallback when the Antigravity server cannot be reached.

When the language_server is down and this device has no usage in a quota
window, nothing is known about that window: the quota may have been spent on
another device. `limits` and `statusline` used to print 100% remaining and
HEALTHY in that case. They must now say the window is unknown and why.
"""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agy_usage import limits
from agy_usage.cli import cmd_limits, cmd_statusline
from agy_usage.limits import LimitsCalculator, get_status_badge
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


def _gemini_session(ts):
    usage = TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200)
    return SessionSummary(
        session_id="s-offline",
        source="cli",
        file_path="/tmp/nonexistent-offline.db",
        title="Offline",
        workspace="ws",
        models=["gemini-3.8-flash"],
        start_time=ts,
        end_time=ts,
        step_count=1,
        usage=usage,
        steps=[StepRecord(step_idx=1, timestamp=ts, model="gemini-3.8-flash", usage=usage)],
    )


def _server_down(reason="no running Antigravity language_server found"):
    """Stand-in for fetch_live_quota_summary when the server is unreachable."""

    def fake(use_cache=True, errors=None):
        if errors is not None:
            errors.append(reason)
        return None

    return mock.patch.object(limits, "fetch_live_quota_summary", side_effect=fake)


def _server_up(remaining=0.92):
    reset = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    buckets = {
        bucket_id: {"bucketId": bucket_id, "remainingFraction": remaining, "resetTime": reset}
        for bucket_id in ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly", "gemini-argon-wtu-weekly")
    }
    return mock.patch.object(
        limits, "fetch_live_quota_summary", return_value={"buckets": buckets}
    )


def _capture(fn, *args):
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        fn(*args)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def _limits_args(**overrides):
    values = dict(tier="pro", five_h_limit=None, weekly_limit=None, json=False, csv=False)
    values.update(overrides)
    return argparse.Namespace(**values)


def _statusline_args(**overrides):
    values = dict(
        tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=True,
        tmux=False, json=False, csv=False, format=None, no_color=True,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class TestUnknownWindows(unittest.TestCase):
    def test_server_down_and_no_local_usage_marks_every_window_unknown(self):
        with _server_down():
            pools = LimitsCalculator.compute_all_pools([], use_live_server=True)

        self.assertFalse(pools.live_synced)
        self.assertIn("no running Antigravity language_server", pools.live_error)
        for w in (pools.gemini_5h, pools.gemini_7d, pools.argon_7d, pools.claude_5h, pools.claude_7d):
            self.assertTrue(w.is_unknown, w.window_name)

    def test_local_usage_keeps_its_window_known(self):
        now = datetime.now()
        with _server_down():
            pools = LimitsCalculator.compute_all_pools(
                [_gemini_session(now)], now=now, use_live_server=True
            )

        self.assertFalse(pools.gemini_5h.is_unknown)
        self.assertFalse(pools.gemini_7d.is_unknown)
        self.assertTrue(pools.claude_7d.is_unknown)

    def test_live_server_data_is_never_unknown(self):
        with _server_up():
            pools = LimitsCalculator.compute_all_pools([], use_live_server=True)

        self.assertTrue(pools.live_synced)
        self.assertIsNone(pools.live_error)
        self.assertFalse(pools.gemini_7d.is_unknown)
        self.assertAlmostEqual(pools.gemini_7d.pct_remaining, 92.0, places=3)

    def test_live_lookup_disabled_keeps_local_model(self):
        pools = LimitsCalculator.compute_all_pools([], use_live_server=False)

        self.assertIsNone(pools.live_error)
        self.assertFalse(pools.gemini_7d.is_unknown)

    def test_device_with_no_history_still_asks_the_server(self):
        with _server_up() as fetch:
            pools = LimitsCalculator.compute_all_pools([])

        fetch.assert_called_once()
        self.assertTrue(pools.live_synced)


class TestStatusBadge(unittest.TestCase):
    def test_unknown_windows_are_not_reported_healthy(self):
        with _server_down():
            pools = LimitsCalculator.compute_all_pools([], use_live_server=True)

        badge, msg = get_status_badge(
            pools.gemini_5h, pools.gemini_7d,
            argon_7d=pools.argon_7d, claude_5h=pools.claude_5h, claude_7d=pools.claude_7d,
        )
        self.assertEqual(badge, "⚪")
        self.assertIn("UNKNOWN", msg)
        self.assertNotIn("HEALTHY", msg)


class TestLimitsCommandOffline(unittest.TestCase):
    def test_text_output_shows_unknown_and_reason_instead_of_100_percent(self):
        with _server_down("port 56232: timed out"):
            out = _capture(cmd_limits, [], _limits_args())

        self.assertIn("Antigravity server not reachable", out)
        self.assertIn("port 56232: timed out", out)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("HEALTHY", out)
        self.assertNotIn("100% capacity available", out)
        self.assertNotIn("100.0% exact", out)

    def test_json_reports_null_percentages_and_the_error(self):
        with _server_down():
            payload = json.loads(_capture(cmd_limits, [], _limits_args(json=True)))

        self.assertFalse(payload["live_synced"])
        self.assertIn("language_server", payload["live_error"])
        window = payload["weekly_budget"]
        self.assertTrue(window["unknown"])
        self.assertIsNone(window["pct_remaining"])
        self.assertIsNone(window["pct_used"])
        self.assertIsNone(window["tokens_remaining"])

    def test_csv_leaves_quota_cells_empty(self):
        with _server_down():
            out = _capture(cmd_limits, [], _limits_args(csv=True))

        row = out.splitlines()[1].split(",")
        self.assertEqual(row[0], "5-Hour Sprint")
        self.assertEqual(row[5:8], ["", "", ""])


class TestStatuslineOffline(unittest.TestCase):
    def test_plain_output_shows_question_marks(self):
        with _server_down():
            out = _capture(cmd_statusline, [], _statusline_args())

        self.assertTrue(out.startswith("[UNKNOWN] 5h: ? (unknown) | Wk: ? (unknown)"), out)

    def test_json_reports_unknown_status_and_null_percentages(self):
        with _server_down():
            payload = json.loads(_capture(cmd_statusline, [], _statusline_args(json=True)))

        self.assertEqual(payload["status"], "unknown")
        self.assertIsNone(payload["five_hour_pct"])
        self.assertIsNone(payload["weekly_pct"])
        self.assertEqual(payload["weekly_refresh"], "unknown")

    def test_format_template_substitutes_question_mark(self):
        with _server_down():
            out = _capture(cmd_statusline, [], _statusline_args(format="{5h_pct}/{wk_pct}"))

        self.assertEqual(out.strip(), "?/?")


class TestFetchFailureReasons(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.mkdtemp(prefix="agy_offline_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self.cache_path = os.path.join(tmpdir, "agy_usage_quota_cache.json")

    def _fetch(self, check_output, urlopen=None):
        errors = []
        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", self.cache_path), \
                mock.patch.object(limits.subprocess, "check_output", side_effect=check_output), \
                mock.patch.object(limits.urllib.request, "urlopen", side_effect=urlopen or OSError("unused")):
            result = limits.fetch_live_quota_summary(errors=errors)
        self.assertIsNone(result)
        return errors

    def test_ps_failure_is_reported(self):
        errors = self._fetch(OSError("ps disabled"))
        self.assertEqual(errors, ["could not list processes (OSError)"])

    def test_missing_language_server_is_reported(self):
        errors = self._fetch(lambda *a, **k: b"  1 /sbin/launchd\n")
        self.assertEqual(errors, ["no running Antigravity language_server found"])

    def test_server_without_listening_port_is_reported(self):
        def check_output(cmd, **kwargs):
            if cmd[0] == "ps":
                return b"  42 /x/language_server --csrf_token abc\n"
            return b""

        errors = self._fetch(check_output)
        self.assertEqual(errors, ["language_server pid 42 is not listening on 127.0.0.1"])

    def test_failed_request_names_the_port(self):
        def check_output(cmd, **kwargs):
            if cmd[0] == "ps":
                return b"  42 /x/language_server --csrf_token abc\n"
            return b"language_ 42 u 7u IPv4 0x1 0t0 TCP 127.0.0.1:56232 (LISTEN)\n"

        errors = self._fetch(check_output, urlopen=TimeoutError("timed out"))
        self.assertEqual(errors, ["port 56232: timed out"])

    def test_old_cache_age_is_reported(self):
        fetched_at = datetime.now(timezone.utc) - timedelta(minutes=8, seconds=5)
        with open(self.cache_path, "w", encoding="utf-8") as fp:
            json.dump({"fetched_at": fetched_at.isoformat(), "buckets": {}}, fp)

        errors = self._fetch(lambda *a, **k: b"")
        self.assertEqual(
            errors,
            ["no running Antigravity language_server found", "cached server reading is 8 min old"],
        )


if __name__ == "__main__":
    unittest.main()
