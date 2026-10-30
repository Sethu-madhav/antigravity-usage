"""The quota cache is written atomically: readers never see a partial file."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from agy_usage import limits

PREVIOUS = {"fetched_at": "2026-10-29T11:00:00+00:00", "buckets": {"old": {"remainingFraction": 0.9}}}
RESULT = {
    "fetched_at": "2026-10-29T12:00:00+00:00",
    "user_tier_id": "g1-pro-tier",
    "user_tier_name": "Pro",
    "buckets": {"gemini-5h": {"bucketId": "gemini-5h", "remainingFraction": 0.25, "resetTime": None}},
}


class TestAtomicQuotaCacheWrite(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="agy_atomic_cache_")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.path = os.path.join(self.tmpdir, "sub", "agy_usage_quota_cache.json")
        patcher = mock.patch.object(limits, "_QUOTA_CACHE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_previous(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump(PREVIOUS, fp, indent=2)

    def _read_text(self):
        with open(self.path, encoding="utf-8") as fp:
            return fp.read()

    def _leftover_files(self):
        return sorted(os.listdir(os.path.dirname(self.path)))

    def test_successful_write_matches_plain_json_dump(self):
        limits._write_quota_cache(RESULT)
        self.assertEqual(self._read_text(), json.dumps(RESULT, indent=2))
        self.assertEqual(json.loads(self._read_text()), RESULT)
        self.assertEqual(self._leftover_files(), [os.path.basename(self.path)])

    def test_overwrites_the_previous_cache(self):
        self._seed_previous()
        limits._write_quota_cache(RESULT)
        self.assertEqual(json.loads(self._read_text()), RESULT)
        self.assertEqual(self._leftover_files(), [os.path.basename(self.path)])

    def test_failure_mid_write_leaves_previous_cache_intact_and_no_temp_files(self):
        self._seed_previous()
        before = self._read_text()

        def partial_dump(obj, fp, **kwargs):
            fp.write('{"fetched_at": "2026-10-29T12:00:00+00:00", "buck')
            raise OSError("disk full")

        with mock.patch("agy_usage.limits.json.dump", side_effect=partial_dump):
            limits._write_quota_cache(RESULT)  # must not raise

        self.assertEqual(self._read_text(), before)
        self.assertEqual(json.loads(before), PREVIOUS)
        self.assertEqual(self._leftover_files(), [os.path.basename(self.path)])

    def test_failure_mid_write_without_previous_cache_creates_nothing(self):
        def partial_dump(obj, fp, **kwargs):
            fp.write("{")
            raise ValueError("boom")

        with mock.patch("agy_usage.limits.json.dump", side_effect=partial_dump):
            limits._write_quota_cache(RESULT)

        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self._leftover_files(), [])

    def test_replace_failure_keeps_old_file_and_cleans_temp_file(self):
        self._seed_previous()
        before = self._read_text()
        with mock.patch("agy_usage.limits.os.replace", side_effect=PermissionError("denied")):
            limits._write_quota_cache(RESULT)  # must not raise

        self.assertEqual(self._read_text(), before)
        self.assertEqual(self._leftover_files(), [os.path.basename(self.path)])

    def test_fetch_still_returns_its_result_when_the_write_fails(self):
        ps = "10 /bin/language_server --csrf_token tok"
        lsof = "COMMAND PID\nlanguage_s 10 u 9u IPv4 0x1 0t0 TCP 127.0.0.1:5001 (LISTEN)\n"

        class Resp:
            def __init__(self, payload):
                self.body = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return self.body

        def urlopen(req, timeout=None):
            if req.full_url.endswith("RetrieveUserQuotaSummary"):
                return Resp({"response": {"groups": [{"buckets": [{"bucketId": "gemini-5h", "remainingFraction": 0.5}]}]}})
            return Resp({"userStatus": {"userTier": {"id": "g1-pro-tier", "name": "Pro"}}})

        errors = []
        with mock.patch.object(
            limits.subprocess, "check_output",
            side_effect=lambda cmd, **kw: (ps if cmd[0] == "ps" else lsof).encode(),
        ), mock.patch.object(limits.urllib.request, "urlopen", side_effect=urlopen), \
                mock.patch("agy_usage.limits.os.replace", side_effect=OSError("read-only fs")):
            result = limits.fetch_live_quota_summary(errors=errors)

        self.assertEqual(errors, [])
        self.assertEqual(result["user_tier_id"], "g1-pro-tier")
        self.assertEqual(result["buckets"]["gemini-5h"]["remainingFraction"], 0.5)
        self.assertEqual(self._leftover_files(), [])

    def test_unwritable_directory_is_swallowed(self):
        blocker = os.path.join(self.tmpdir, "blocker")
        with open(blocker, "w", encoding="utf-8") as fp:
            fp.write("a file, not a directory")
        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", os.path.join(blocker, "q.json")):
            limits._write_quota_cache(RESULT)  # must not raise


if __name__ == "__main__":
    unittest.main()
