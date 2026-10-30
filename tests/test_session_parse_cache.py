"""Tests for session parse caching (OPT-001)."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.cache import SessionCache, summary_from_dict, summary_to_dict
from agy_usage.models import SessionSummary, StepRecord, TokenUsage
from agy_usage.parser import AntigravityParser


def _varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _write_db(path: str, prompt: int = 1000, output: int = 200, step_idx: int = 1) -> str:
    """Create a minimal valid session DB with one step."""
    ts = bytes([0x08]) + _varint(1_751_234_567)
    usage = bytes([0x10]) + _varint(prompt) + bytes([0x18]) + _varint(output)
    metadata = bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
            "step_payload BLOB, metadata BLOB)"
        )
        conn.execute(
            "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (?, 'user', '', ?)",
            (step_idx, metadata),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _append_step_row(path: str, prompt: int = 500, output: int = 100, step_idx: int = 2) -> None:
    """Append a step row to an existing session DB."""
    ts = bytes([0x08]) + _varint(1_751_234_999)
    usage = bytes([0x10]) + _varint(prompt) + bytes([0x18]) + _varint(output)
    metadata = bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (?, 'user', '', ?)",
            (step_idx, metadata),
        )
        conn.commit()
    finally:
        conn.close()


class TestSessionParseCache(unittest.TestCase):
    """Test suite for SessionCache and session parse caching."""

    def setUp(self):
        self._tmp_cache = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_cache.cleanup)
        env = dict(os.environ)
        env["AGY_USAGE_CACHE_DIR"] = self._tmp_cache.name
        env.pop("AGY_USAGE_NO_CACHE", None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_summary_round_trip(self):
        """Round-trip: summary_from_dict(summary_to_dict(s)) equals s field by field."""
        usage = TokenUsage(
            prompt_tokens=1000,
            cached_tokens=500,
            output_tokens=300,
            thought_tokens=100,
            candidate_tokens=200,
        )
        step1 = StepRecord(
            step_idx=1,
            timestamp=datetime(2026, 5, 10, 14, 30, 0),
            model="gemini-3.8-flash",
            usage=TokenUsage(
                prompt_tokens=600,
                cached_tokens=300,
                output_tokens=150,
                thought_tokens=50,
                candidate_tokens=100,
            ),
            duration_ms=123.45,
        )
        step2 = StepRecord(
            step_idx=2,
            timestamp=datetime(2026, 5, 10, 14, 31, 0),
            model="gemini-3.1-pro",
            usage=TokenUsage(
                prompt_tokens=400,
                cached_tokens=200,
                output_tokens=150,
                thought_tokens=50,
                candidate_tokens=100,
            ),
            duration_ms=67.89,
        )
        s = SessionSummary(
            session_id="test-session-1234",
            source="custom",
            file_path="/path/to/test.db",
            title="Test Session",
            workspace="my_project",
            models=["gemini-3.8-flash", "gemini-3.1-pro"],
            start_time=datetime(2026, 5, 10, 14, 30, 0),
            end_time=datetime(2026, 5, 10, 14, 31, 0),
            step_count=2,
            usage=usage,
            steps=[step1, step2],
        )
        d = summary_to_dict(s)
        s_rt = summary_from_dict(d)
        self.assertEqual(s, s_rt)
        self.assertEqual(s.models, s_rt.models)
        self.assertEqual(s.cost.to_dict(), s_rt.cost.to_dict())

        # Also test None times
        s_none = SessionSummary(
            session_id="sess-none",
            source="cli",
            file_path="/path/to/none.db",
            start_time=None,
            end_time=None,
            models=["gemini-3.8-flash"],
        )
        d_none = summary_to_dict(s_none)
        s_none_rt = summary_from_dict(d_none)
        self.assertEqual(s_none, s_none_rt)
        self.assertIsNone(s_none_rt.start_time)
        self.assertIsNone(s_none_rt.end_time)
        self.assertEqual(s_none.cost.to_dict(), s_none_rt.cost.to_dict())

    def test_scan_all_uses_cache_on_second_call(self):
        """Second scan_all(custom_dir=tmp) call does NOT call _parse_session_db_checked."""
        with tempfile.TemporaryDirectory() as tmp_data:
            _write_db(os.path.join(tmp_data, "s1.db"), prompt=1000, output=200)
            _write_db(os.path.join(tmp_data, "s2.db"), prompt=2000, output=400)

            # First scan
            res1 = AntigravityParser.scan_all(custom_dir=tmp_data)
            self.assertEqual(len(res1), 2)

            # Second scan with wraps spy
            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as spy:
                res2 = AntigravityParser.scan_all(custom_dir=tmp_data)
                self.assertEqual(spy.call_count, 0)

            self.assertEqual(len(res2), 2)
            self.assertEqual([s.session_id for s in res1], [s.session_id for s in res2])
            self.assertEqual(
                [s.usage.total_tokens for s in res1],
                [s.usage.total_tokens for s in res2],
            )

    def test_db_change_triggers_reparse_of_changed_file_only(self):
        """Changing the db triggers a reparse of that file only."""
        with tempfile.TemporaryDirectory() as tmp_data:
            p1 = _write_db(os.path.join(tmp_data, "s1.db"), prompt=1000, output=200)
            p2 = _write_db(os.path.join(tmp_data, "s2.db"), prompt=2000, output=400)

            AntigravityParser.scan_all(custom_dir=tmp_data)

            # Modify s1.db: append a step row
            _append_step_row(p1, prompt=500, output=100, step_idx=2)

            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as spy:
                res = AntigravityParser.scan_all(custom_dir=tmp_data)
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(spy.call_args[0][0], p1)

            # Modify s2.db via utime
            stat_before = os.stat(p2)
            new_mtime = stat_before.st_mtime_ns + 10_000_000_000
            os.utime(p2, ns=(new_mtime, new_mtime))
            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as spy:
                res = AntigravityParser.scan_all(custom_dir=tmp_data)
                self.assertEqual(spy.call_count, 1)
                self.assertEqual(spy.call_args[0][0], p2)

    def test_wal_file_change_triggers_reparse(self):
        """Creating or growing a <name>.db-wal file triggers a reparse."""
        with tempfile.TemporaryDirectory() as tmp_data:
            p = _write_db(os.path.join(tmp_data, "s.db"), prompt=1000, output=200)
            AntigravityParser.scan_all(custom_dir=tmp_data)

            # Create wal file
            wal_path = p + "-wal"
            with open(wal_path, "wb") as f:
                f.write(b"wal_initial_data")

            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as spy:
                AntigravityParser.scan_all(custom_dir=tmp_data)
                self.assertEqual(spy.call_count, 1)

            # Warm cache with wal
            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as spy:
                AntigravityParser.scan_all(custom_dir=tmp_data)
                self.assertEqual(spy.call_count, 0)

            # Grow wal file
            time.sleep(0.01)
            with open(wal_path, "ab") as f:
                f.write(b"more_wal_bytes")

            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as spy:
                AntigravityParser.scan_all(custom_dir=tmp_data)
                self.assertEqual(spy.call_count, 1)

    def test_partial_parse_is_not_stored(self):
        """A partial parse (complete=False) is NOT stored in the cache."""
        with tempfile.TemporaryDirectory() as tmp_data:
            p = _write_db(os.path.join(tmp_data, "s.db"), prompt=1000, output=200)

            summary, _ = AntigravityParser._parse_session_db_checked(p)
            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                return_value=(summary, False),
            ):
                AntigravityParser.scan_all(custom_dir=tmp_data)

            # The cache file should not contain s.db
            cache = SessionCache.load()
            self.assertIsNone(cache.get(p, "custom"))

            # Next scan should call _parse_session_db_checked
            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as spy:
                AntigravityParser.scan_all(custom_dir=tmp_data)
                self.assertEqual(spy.call_count, 1)

    def test_invalid_cache_files_give_empty_cache(self):
        """Corrupt cache file contents, wrong format, and different code hash give clean empty cache."""
        cache_file = os.path.join(self._tmp_cache.name, "sessions.json")

        # Corrupt JSON
        with open(cache_file, "w", encoding="utf-8") as f:
            f.write("{not json")
        cache = SessionCache.load()
        self.assertEqual(cache._entries, {})

        # Wrong format version
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"format": 999, "code": "abc", "tz": [], "entries": {}}, f)
        cache = SessionCache.load()
        self.assertEqual(cache._entries, {})

        # Different code hash
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump({"format": 1, "code": "different_hash", "tz": [], "entries": {}}, f)
        cache = SessionCache.load()
        self.assertEqual(cache._entries, {})

    def test_no_cache_env_var(self):
        """AGY_USAGE_NO_CACHE=1 gives no cache file written and parse called every time."""
        with mock.patch.dict(os.environ, {"AGY_USAGE_NO_CACHE": "1"}):
            with tempfile.TemporaryDirectory() as tmp_data:
                _write_db(os.path.join(tmp_data, "s.db"))
                with mock.patch.object(
                    AntigravityParser,
                    "_parse_session_db_checked",
                    wraps=AntigravityParser._parse_session_db_checked,
                ) as spy:
                    AntigravityParser.scan_all(custom_dir=tmp_data)
                    self.assertEqual(spy.call_count, 1)

                    AntigravityParser.scan_all(custom_dir=tmp_data)
                    self.assertEqual(spy.call_count, 2)

                cache_file = os.path.join(self._tmp_cache.name, "sessions.json")
                self.assertFalse(os.path.exists(cache_file))

    def test_scan_all_use_cache_false(self):
        """scan_all(..., use_cache=False) never writes a cache file."""
        with tempfile.TemporaryDirectory() as tmp_data:
            _write_db(os.path.join(tmp_data, "s.db"))
            AntigravityParser.scan_all(custom_dir=tmp_data, use_cache=False)
            cache_file = os.path.join(self._tmp_cache.name, "sessions.json")
            self.assertFalse(os.path.exists(cache_file))

    def test_deleted_db_pruned_on_save(self):
        """Entries for deleted db files are pruned on save."""
        with tempfile.TemporaryDirectory() as tmp_data:
            p1 = _write_db(os.path.join(tmp_data, "s1.db"))
            p2 = _write_db(os.path.join(tmp_data, "s2.db"))
            AntigravityParser.scan_all(custom_dir=tmp_data)

            cache = SessionCache.load()
            self.assertIn(os.path.abspath(p1), cache._entries)
            self.assertIn(os.path.abspath(p2), cache._entries)

            # Remove s1.db
            os.remove(p1)

            # Saving should prune s1.db
            cache.save()

            cache2 = SessionCache.load()
            self.assertNotIn(os.path.abspath(p1), cache2._entries)
            self.assertIn(os.path.abspath(p2), cache2._entries)

    def test_get_returns_fresh_objects_and_applies_source(self):
        """get() returns distinct objects on two calls and applies caller's source."""
        with tempfile.TemporaryDirectory() as tmp_data:
            p = _write_db(os.path.join(tmp_data, "s.db"))
            AntigravityParser.scan_all(custom_dir=tmp_data)

            cache = SessionCache.load()
            s1 = cache.get(p, "cli")
            s2 = cache.get(p, "custom")

            self.assertIsNotNone(s1)
            self.assertIsNotNone(s2)
            self.assertIsNot(s1, s2)
            self.assertEqual(s1.source, "cli")
            self.assertEqual(s2.source, "custom")
            self.assertIsNot(s1.usage, s2.usage)
            self.assertIsNot(s1.steps, s2.steps)


if __name__ == "__main__":
    unittest.main()
