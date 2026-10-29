"""Tests for quota cache freshness."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from agy_usage.models import SessionSummary, StepRecord, TokenUsage


class TestQuotaCacheFreshness(unittest.TestCase):
    """Regression tests for BUG-005: the on-disk quota cache is a fallback,
    not a live feed — a stale cache must be rejected instead of rendered as
    live server data, and expired buckets must not supply quota fractions."""

    def _temp_cache_path(self):
        import os
        import shutil
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="agy_quota_cache_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        return os.path.join(tmpdir, "agy_usage_quota_cache.json")

    def _write_cache(self, path, fetched_at_iso, reset_time_iso, remaining=0.42):
        import json

        payload = {
            "fetched_at": fetched_at_iso,
            "user_tier_id": "pro",
            "user_tier_name": "Google AI Pro",
            "buckets": {
                "gemini-5h": {
                    "bucketId": "gemini-5h",
                    "displayName": "5-Hour Limit",
                    "description": "Gemini 5-hour sprint quota",
                    "window": "5h",
                    "remainingFraction": remaining,
                    "resetTime": reset_time_iso,
                },
            },
        }
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp)
        return payload

    def test_fresh_cache_is_used_when_server_unreachable(self):
        from datetime import timedelta, timezone
        from unittest import mock

        from agy_usage import limits

        cache_path = self._temp_cache_path()
        now_utc = datetime.now(timezone.utc)
        payload = self._write_cache(
            cache_path,
            now_utc.isoformat(),  # fetched right now -> within the ~60s TTL
            (now_utc + timedelta(hours=2)).isoformat(),
        )

        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", cache_path), \
                mock.patch.object(limits.subprocess, "check_output", side_effect=OSError("ps disabled")):
            result = limits.fetch_live_quota_summary()

        self.assertEqual(result, payload)

    def test_stale_cache_is_rejected_and_not_treated_as_live(self):
        from datetime import timedelta, timezone
        from unittest import mock

        from agy_usage import limits

        cache_path = self._temp_cache_path()
        now_utc = datetime.now(timezone.utc)
        self._write_cache(
            cache_path,
            (now_utc - timedelta(seconds=90)).isoformat(),  # older than the ~60s TTL
            (now_utc + timedelta(hours=2)).isoformat(),
        )

        now_local = datetime.now()
        session = SessionSummary(
            session_id="s-cache",
            source="cli",
            file_path="/tmp/nonexistent-cache.db",
            title="Cache",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=now_local,
            end_time=now_local,
            step_count=1,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200),
            steps=[
                StepRecord(step_idx=1, timestamp=now_local, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200))
            ],
        )

        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", cache_path), \
                mock.patch.object(limits.subprocess, "check_output", side_effect=OSError("ps disabled")):
            self.assertIsNone(limits.fetch_live_quota_summary())
            pools = limits.LimitsCalculator.compute_all_pools(
                [session], now=now_local, use_live_server=True
            )

        # Downstream must see the server as not live, not hours-old data as live.
        self.assertFalse(pools.live_synced)
        self.assertIsNone(pools.gemini_5h.server_remaining_fraction)

    def test_bucket_with_past_reset_time_is_ignored(self):
        from datetime import timedelta, timezone

        from agy_usage.limits import LimitsCalculator

        now = datetime.now()
        now_utc = datetime.now(timezone.utc)
        past_reset = (now_utc - timedelta(minutes=5)).isoformat()
        future_reset = (now_utc + timedelta(hours=2)).isoformat()
        items = []

        w_5h, _ = LimitsCalculator._compute_windows_from_items(
            items, current_time=now, limit_5h=100_000_000, limit_7d=750_000_000, pool="gemini"
        )
        expired = LimitsCalculator._apply_bucket_calibration(
            w_5h,
            {
                "bucketId": "gemini-5h",
                "remainingFraction": 0.1,
                "resetTime": past_reset,  # cycle already reset
                "description": "expired cycle",
            },
            now,
            timedelta(hours=5),
            items,
            False,
        )
        self.assertIsNone(expired.server_remaining_fraction)
        self.assertIsNone(expired.server_description)
        self.assertEqual(expired.end_time, now)  # past resetTime not grafted onto the window
        self.assertAlmostEqual(expired.pct_remaining, 100.0)  # old code would report 90% used

        live = LimitsCalculator._apply_bucket_calibration(
            w_5h,
            {
                "bucketId": "gemini-5h",
                "remainingFraction": 0.42,
                "resetTime": future_reset,  # cycle still running
                "description": "live cycle",
            },
            now,
            timedelta(hours=5),
            items,
            False,
        )
        self.assertAlmostEqual(live.server_remaining_fraction, 0.42)
        self.assertEqual(
            live.refresh_time,
            (now_utc + timedelta(hours=2)).astimezone().replace(tzinfo=None),
        )


if __name__ == "__main__":
    unittest.main()
