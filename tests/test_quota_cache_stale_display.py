"""Tests for showing a dated cached server reading when the server is down.

Local session DBs only see this device's usage, so the last
RetrieveUserQuotaSummary response on disk says more about the account than an
empty local estimate. A cache older than the ~60s TTL but younger than
``_QUOTA_CACHE_MAX_AGE_SECONDS`` is used, labeled with its age, and never
reported as live. Expired buckets and too-old caches are still rejected.
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
from agy_usage.limits import LimitsCalculator

_BUCKET_IDS = ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly", "gemini-argon-wtu-weekly")


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


def _no_server(cmd, **kwargs):
    """`ps` output with no language_server in it."""
    return b"  1 /sbin/launchd\n"


class _CacheTestCase(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.mkdtemp(prefix="agy_stale_cache_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        self.cache_path = os.path.join(tmpdir, "agy_usage_quota_cache.json")

    def _write_cache(self, age, reset_in=timedelta(hours=3), remaining=0.35):
        now_utc = datetime.now(timezone.utc)
        reset = (now_utc + reset_in).isoformat()
        payload = {
            "fetched_at": (now_utc - age).isoformat(),
            "user_tier_id": "pro",
            "user_tier_name": "Google AI Pro",
            "buckets": {
                bid: {"bucketId": bid, "remainingFraction": remaining, "resetTime": reset}
                for bid in _BUCKET_IDS
            },
        }
        with open(self.cache_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp)

    def _patched(self, check_output=_no_server):
        """Point the cache at a temp file and keep the real language server out."""
        for patcher in (
            mock.patch.object(limits, "_QUOTA_CACHE_PATH", self.cache_path),
            mock.patch.object(limits.subprocess, "check_output", side_effect=check_output),
            mock.patch.object(limits.urllib.request, "urlopen", side_effect=OSError("unused")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class TestCachedReadingIsUsed(_CacheTestCase):
    def test_eight_minute_old_cache_supplies_fractions_but_is_not_live(self):
        self._write_cache(timedelta(minutes=8, seconds=5))
        self._patched()

        pools = LimitsCalculator.compute_all_pools([], use_live_server=True)

        self.assertFalse(pools.live_synced)
        self.assertEqual(pools.quota_source, "cached")
        self.assertGreaterEqual(pools.cache_age_seconds, 8 * 60)
        self.assertLess(pools.cache_age_seconds, 9 * 60)
        self.assertIn("cached server reading is 8 min old", pools.live_error)
        for w in (pools.gemini_5h, pools.gemini_7d, pools.argon_7d, pools.claude_5h, pools.claude_7d):
            self.assertFalse(w.is_unknown, w.window_name)
            self.assertAlmostEqual(w.server_remaining_fraction, 0.35, msg=w.window_name)
            self.assertAlmostEqual(w.pct_remaining, 35.0, places=3)

    def test_limits_text_labels_the_reading_with_its_age(self):
        self._write_cache(timedelta(minutes=8, seconds=5))
        self._patched()

        out = _capture(cmd_limits, [], _limits_args())

        self.assertIn("Last server reading, 8 min ago (Antigravity server not reachable now)", out)
        self.assertNotIn("Live Antigravity Server", out)
        self.assertNotIn("Local estimate, this device only", out)
        self.assertIn("35%", out)
        self.assertNotIn("UNKNOWN", out)

    def test_limits_json_reports_source_and_age(self):
        self._write_cache(timedelta(minutes=8, seconds=5))
        self._patched()

        payload = json.loads(_capture(cmd_limits, [], _limits_args(json=True)))

        self.assertFalse(payload["live_synced"])
        self.assertEqual(payload["quota_source"], "cached")
        self.assertGreaterEqual(payload["cache_age_seconds"], 8 * 60)
        self.assertIsNotNone(payload["cache_fetched_at"])
        self.assertIn("8 min old", payload["live_error"])
        self.assertFalse(payload["weekly_budget"]["unknown"])
        self.assertAlmostEqual(payload["weekly_budget"]["pct_remaining"], 35.0)

    def test_statusline_shows_cached_percentages_instead_of_question_marks(self):
        self._write_cache(timedelta(minutes=8, seconds=5))
        self._patched()

        out = _capture(cmd_statusline, [], _statusline_args())
        payload = json.loads(_capture(cmd_statusline, [], _statusline_args(json=True)))

        self.assertNotIn("?", out.splitlines()[0])
        self.assertIn("5h: 35%", out)
        self.assertEqual(payload["weekly_pct"], 35)
        self.assertNotEqual(payload["status"], "unknown")


class TestCachedReadingIsRejected(_CacheTestCase):
    def test_cache_past_max_age_is_rejected_and_windows_stay_unknown(self):
        self._write_cache(timedelta(seconds=limits._QUOTA_CACHE_MAX_AGE_SECONDS + 60))
        self._patched()

        errors = []
        self.assertIsNone(limits.fetch_live_quota_summary(errors=errors))
        pools = LimitsCalculator.compute_all_pools([], use_live_server=True)

        self.assertFalse(pools.live_synced)
        self.assertEqual(pools.quota_source, "local")
        self.assertIsNone(pools.cache_age_seconds)
        self.assertIn("cached server reading is", pools.live_error)
        for w in (pools.gemini_5h, pools.gemini_7d, pools.argon_7d, pools.claude_5h, pools.claude_7d):
            self.assertTrue(w.is_unknown, w.window_name)

    def test_cache_whose_cycles_have_reset_is_ignored(self):
        self._write_cache(timedelta(minutes=8), reset_in=-timedelta(minutes=1))
        self._patched()

        pools = LimitsCalculator.compute_all_pools([], use_live_server=True)

        self.assertFalse(pools.live_synced)
        self.assertEqual(pools.quota_source, "local")
        self.assertIn("every cached quota cycle has since reset", pools.live_error)
        for w in (pools.gemini_5h, pools.gemini_7d, pools.argon_7d, pools.claude_5h, pools.claude_7d):
            self.assertIsNone(w.server_remaining_fraction, w.window_name)
            self.assertTrue(w.is_unknown, w.window_name)


class TestLiveResponseWins(_CacheTestCase):
    def test_live_response_overrides_the_cache(self):
        self._write_cache(timedelta(minutes=8), remaining=0.35)

        def check_output(cmd, **kwargs):
            if cmd[0] == "ps":
                return b"  42 /x/language_server --csrf_token abc\n"
            return b"language_ 42 u 7u IPv4 0x1 0t0 TCP 127.0.0.1:56232 (LISTEN)\n"

        reset = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        quota = {
            "response": {
                "groups": [{
                    "buckets": [
                        {"bucketId": bid, "remainingFraction": 0.8, "resetTime": reset}
                        for bid in _BUCKET_IDS
                    ],
                }],
            },
        }

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout=None):
            if req.full_url.endswith("/RetrieveUserQuotaSummary"):
                return _Resp(json.dumps(quota).encode("utf-8"))
            raise OSError("no user status")

        self._patched(check_output)
        with mock.patch.object(limits.urllib.request, "urlopen", side_effect=urlopen):
            pools = LimitsCalculator.compute_all_pools([], use_live_server=True)

        self.assertTrue(pools.live_synced)
        self.assertEqual(pools.quota_source, "live")
        self.assertIsNone(pools.live_error)
        self.assertIsNone(pools.cache_age_seconds)
        self.assertAlmostEqual(pools.gemini_7d.pct_remaining, 80.0, places=3)


if __name__ == "__main__":
    unittest.main()
