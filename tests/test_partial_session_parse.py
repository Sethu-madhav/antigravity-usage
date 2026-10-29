"""Tests for partial session parsing."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.parser import AntigravityParser, parse_proto_fields


class TestPartialSessionParse(unittest.TestCase):
    """Regression tests for BUG-008: one transient SQLite error or one corrupt
    step row must not discard the metadata/steps already parsed from a session.
    """

    @staticmethod
    def _varint(value):
        out = bytearray()
        while True:
            b = value & 0x7F
            value >>= 7
            if value:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    @classmethod
    def _len_field(cls, num, data):
        return cls._varint((num << 3) | 2) + cls._varint(len(data)) + data

    @classmethod
    def _varint_field(cls, num, value):
        return cls._varint(num << 3) + cls._varint(value)

    @classmethod
    def _timestamp(cls, seconds):
        return cls._varint_field(1, seconds)

    @classmethod
    def _usage(cls, prompt, output):
        return cls._varint_field(2, prompt) + cls._varint_field(3, output)

    def _write_db(self, path, rows, workspace="file:///Users/me/proj", title=None, include_blob_ts=True):
        """Create a minimal DB with a workspace blob, an optional user prompt,
        and `rows` of (idx, metadata) steps."""
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")
            ws_blob = self._len_field(1, self._len_field(1, workspace.encode()))
            if include_blob_ts:
                ws_blob += self._len_field(2, self._timestamp(1_751_000_000))
            conn.execute("INSERT INTO trajectory_metadata_blob (data) VALUES (?)", (ws_blob,))
            for idx, metadata in rows:
                payload = (
                    self._len_field(19, self._len_field(2, title.encode()))
                    if title and idx == 1
                    else b""
                )
                conn.execute(
                    "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (?, ?, ?, ?)",
                    (idx, "user", payload, metadata),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_corrupt_row_keeps_prior_and_later_steps(self):
        ts = self._timestamp(1_751_234_567)
        meta = self._len_field(1, ts) + self._len_field(9, self._usage(1000, 200))
        bad_blob = b"corrupt-metadata"

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-corrupt.db"),
                [(1, meta), (2, bad_blob), (3, meta)],
                title="Diagnose the parser",
            )
            def flaky(data):
                if data == bad_blob:
                    raise ValueError("corrupt protobuf")
                return parse_proto_fields(data)

            with mock.patch("agy_usage.parser.parse_proto_fields", side_effect=flaky):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary, "corrupt row dropped the whole session")
            self.assertEqual(summary.session_id, "sess-corrupt")
            self.assertEqual(summary.title, "Diagnose the parser")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.step_count, 2)
            self.assertEqual(summary.usage.total_tokens, 2400)
            self.assertEqual([s.step_idx for s in summary.steps], [1, 3])

    def test_lock_during_step_read_returns_partial_summary(self):
        ts = self._timestamp(1_751_234_567)
        meta = self._len_field(1, ts) + self._len_field(9, self._usage(1000, 200))

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-locked.db"),
                [(1, meta)],
                title="Locked session",
            )
            summary = self._parse_with_locked_steps(path)

            self.assertIsNotNone(summary, "a transient lock dropped the whole session")
            self.assertEqual(summary.session_id, "sess-locked")
            self.assertEqual(summary.title, "Locked session")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.step_count, 0)
            self.assertEqual(summary.usage.total_tokens, 0)

    def _parse_with_sql_failure(self, path, marker, error):
        """Parse while any statement containing `marker` raises `error`."""
        real_connect = sqlite3.connect

        class FailingCursor:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *params):
                if marker in sql:
                    raise error
                return self._inner.execute(sql, *params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        class FailingConnection:
            def __init__(self, inner):
                self._inner = inner

            def cursor(self):
                return FailingCursor(self._inner.cursor())

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def fake_connect(*args, **kwargs):
            return FailingConnection(real_connect(*args, **kwargs))

        with mock.patch("agy_usage.parser.sqlite3.connect", side_effect=fake_connect):
            return AntigravityParser.parse_session_db(path)

    def test_lock_during_step_read_returns_partial_summary(self):
        ts = self._timestamp(1_751_234_567)
        meta = self._len_field(1, ts) + self._len_field(9, self._usage(1000, 200))

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-locked.db"),
                [(1, meta)],
                title="Locked session",
            )
            summary = self._parse_with_sql_failure(
                path,
                "metadata IS NOT NULL",
                sqlite3.OperationalError("database is locked"),
            )

            self.assertIsNotNone(summary, "a transient lock dropped the whole session")
            self.assertEqual(summary.session_id, "sess-locked")
            self.assertEqual(summary.title, "Locked session")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.step_count, 0)
            self.assertEqual(summary.usage.total_tokens, 0)

    def test_lock_during_title_query_is_contained(self):
        """Section 3's own guard swallows the lock, so parsing continues: the
        blob start time and the step totals must survive the failed query."""
        meta = self._len_field(1, self._timestamp(1_751_234_567))
        meta += self._len_field(9, self._usage(1000, 200))

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-title-locked.db"),
                [(1, meta)],
                title="Locked title",
            )
            summary = self._parse_with_sql_failure(
                path,
                "step_payload IS NOT NULL",
                sqlite3.OperationalError("database is locked"),
            )

            self.assertIsNotNone(summary)
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.title, "Session sess-tit")
            self.assertEqual(summary.step_count, 1)
            self.assertEqual(summary.usage.total_tokens, 1200)

    def test_corrupt_db_during_step_read_returns_partial_summary(self):
        """A plain sqlite3.DatabaseError (corrupt page) is not an
        OperationalError and must keep the already-parsed metadata too."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-corrupt-db.db"),
                [(1, self._len_field(9, self._usage(1000, 200)))],
                title="Corrupt read",
            )
            summary = self._parse_with_sql_failure(
                path,
                "metadata IS NOT NULL",
                sqlite3.DatabaseError("database disk image is malformed"),
            )

            self.assertIsNotNone(summary, "a corrupt read dropped the session")
            self.assertEqual(summary.title, "Corrupt read")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.step_count, 0)

    def test_empty_steps_table_emits_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(os.path.join(tmp, "sess-empty.db"), [])
            summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.session_id, "sess-empty")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.step_count, 0)
            self.assertEqual(summary.usage.total_tokens, 0)

    def test_unreadable_mtime_skips_untimed_rows(self):
        """If the DB vanishes, steps without any inferrable timestamp are skipped
        instead of crashing the parse (BUG-008/BUG-018); untimed steps with a timed
        neighbour inherit the neighbour timestamp."""
        timed = self._len_field(1, self._timestamp(1_751_234_567))
        timed += self._len_field(9, self._usage(1000, 200))
        untimed = self._len_field(9, self._usage(500, 100))

        # Under BUG-018 neighbour inference, untimed row 2 inherits row 1's timestamp
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-no-mtime-neighbour.db"),
                [(1, timed), (2, untimed)],
            )
            with mock.patch("agy_usage.parser.os.path.getmtime", side_effect=OSError("gone")):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.step_count, 2)
            self.assertEqual(summary.usage.total_tokens, 1800)
            self.assertEqual([s.step_idx for s in summary.steps], [1, 2])
            self.assertEqual(summary.steps[1].timestamp, summary.steps[0].timestamp)

        # Without any inferrable timestamp (no neighbours, no blob ts), untimed rows are skipped
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-no-mtime-isolated.db"),
                [(1, untimed)],
                include_blob_ts=False,
            )
            with mock.patch("agy_usage.parser.os.path.getmtime", side_effect=OSError("gone")):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.step_count, 0)
            self.assertEqual(summary.usage.total_tokens, 0)

    def test_lock_before_validation_returns_none(self):
        """If SQLite fails before the DB is known to be a session, there is no
        partial data worth surfacing - keep the old "skip it" behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-unreadable.db"),
                [(1, self._len_field(9, self._usage(10, 5)))],
            )
            summary = self._parse_with_sql_failure(
                path,
                "sqlite_master",
                sqlite3.OperationalError("database is locked"),
            )
            self.assertIsNone(summary)


if __name__ == "__main__":
    unittest.main()
