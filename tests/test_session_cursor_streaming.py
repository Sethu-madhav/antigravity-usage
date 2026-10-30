"""Tests for streaming cursor consumption when parsing session steps.

Validates that steps are consumed row-by-row lazily rather than loading all
rows into memory upfront with cursor.fetchall(), while maintaining identical
filtering, ordering, error-containment, and connection lifecycle semantics.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.parser import AntigravityParser, parse_usage_proto

_REAL_SQLITE_CONNECT = sqlite3.connect
_REAL_PARSE_USAGE_PROTO = parse_usage_proto


class SpyLog:
    """Per-test record of cursor calls and failure injection configuration."""

    def __init__(
        self,
        fail_execute_pattern=None,
        execute_error=None,
        fail_after=None,
        mid_read_error=None,
    ):
        self.fetchall_calls = []
        self.rows_pulled = 0
        self.close_calls = 0
        self.fail_execute_pattern = fail_execute_pattern
        self.execute_error = execute_error
        self.fail_after = fail_after
        self.mid_read_error = mid_read_error


def make_spy_classes(log: SpyLog):
    """Create SpyConnection and SpyCursor classes linked to the given SpyLog."""

    class SpyCursor(sqlite3.Cursor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.last_sql = ""

        def execute(self, sql, *args, **kwargs):
            self.last_sql = sql
            if (
                log.fail_execute_pattern
                and log.fail_execute_pattern in sql
                and log.execute_error is not None
            ):
                raise log.execute_error
            return super().execute(sql, *args, **kwargs)

        def fetchall(self):
            log.fetchall_calls.append(self.last_sql)
            if (
                "metadata IS NOT NULL" in self.last_sql
                and log.fail_after is not None
                and log.mid_read_error is not None
            ):
                raise log.mid_read_error
            return super().fetchall()

        def __next__(self):
            if "metadata IS NOT NULL" in self.last_sql:
                if (
                    log.fail_after is not None
                    and log.mid_read_error is not None
                    and log.rows_pulled >= log.fail_after
                ):
                    raise log.mid_read_error
                row = super().__next__()
                log.rows_pulled += 1
                return row
            return super().__next__()

        def fetchone(self):
            if "metadata IS NOT NULL" in self.last_sql:
                try:
                    return self.__next__()
                except StopIteration:
                    return None
            return super().fetchone()

    class SpyConnection(sqlite3.Connection):
        def cursor(self, factory=SpyCursor):
            return super().cursor(factory)

        def close(self):
            log.close_calls += 1
            super().close()

    return SpyConnection, SpyCursor


class TestSessionCursorStreaming(unittest.TestCase):
    """Test suite for cursor streaming in AntigravityParser.parse_session_db."""

    @staticmethod
    def _varint(value: int) -> bytes:
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
    def _len_field(cls, num: int, data: bytes) -> bytes:
        return cls._varint((num << 3) | 2) + cls._varint(len(data)) + data

    @classmethod
    def _varint_field(cls, num: int, value: int) -> bytes:
        return cls._varint(num << 3) + cls._varint(value)

    @classmethod
    def _timestamp(cls, seconds: int) -> bytes:
        return cls._varint_field(1, seconds)

    @classmethod
    def _usage(cls, prompt: int, output: int) -> bytes:
        return cls._varint_field(2, prompt) + cls._varint_field(3, output)

    def _write_db(
        self,
        path: str,
        rows: list,
        workspace: str = "file:///Users/me/testproj",
        title: str = "Session Title",
        include_blob_ts: bool = True,
    ) -> str:
        """Create a session DB with workspace blob, optional title payload, and step rows."""
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")
            ws_blob = self._len_field(1, self._len_field(1, workspace.encode("utf-8")))
            if include_blob_ts:
                ws_blob += self._len_field(2, self._timestamp(1_751_000_000))
            conn.execute(
                "INSERT INTO trajectory_metadata_blob (data) VALUES (?)", (ws_blob,)
            )
            for idx, metadata in rows:
                payload = (
                    self._len_field(19, self._len_field(2, title.encode("utf-8")))
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

    def _patch_connect(self, log: SpyLog):
        """Context manager patching sqlite3.connect in the parser to use SpyConnection."""
        SpyConn, _ = make_spy_classes(log)

        def fake_connect(*args, **kwargs):
            kw = dict(kwargs)
            kw["factory"] = SpyConn
            return _REAL_SQLITE_CONNECT(*args, **kw)

        return mock.patch("agy_usage.parser.sqlite3.connect", side_effect=fake_connect)

    def test_steps_ordered_by_idx_regardless_of_insert_order(self):
        """Steps must be returned strictly ordered by steps.idx ascending,
        even when inserted out of order (e.g. 5, 3, 9, 1)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-order.db")
            # Step specs: (idx, prompt, output) -> total = prompt + output
            # idx 1: 100 + 50 = 150
            # idx 3: 300 + 50 = 350
            # idx 5: 500 + 50 = 550
            # idx 9: 900 + 50 = 950
            specs = [(5, 500, 50), (3, 300, 50), (9, 900, 50), (1, 100, 50)]
            rows = []
            for idx, prompt, output in specs:
                meta = self._len_field(
                    1, self._timestamp(1_751_234_000 + idx)
                ) + self._len_field(9, self._usage(prompt, output))
                rows.append((idx, meta))

            self._write_db(path, rows, title="Ordering Test")

            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            step_tuples = [(s.step_idx, s.usage.total_tokens) for s in summary.steps]
            self.assertEqual(
                step_tuples,
                [(1, 150), (3, 350), (5, 550), (9, 950)],
                "Steps must be returned in steps.idx ascending order",
            )
            self.assertEqual(summary.usage.total_tokens, 2000)

    def test_mixed_good_and_bad_rows_equivalent(self):
        """Bad rows (NULL, empty, non-bytes, malformed protobuf, zero-usage, missing
        field 9) must be skipped without dropping prior or subsequent valid steps."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-mixed.db")

            valid_1 = self._len_field(
                1, self._timestamp(1_751_000_010)
            ) + self._len_field(9, self._usage(100, 20))
            valid_6 = self._len_field(
                1, self._timestamp(1_751_000_020)
            ) + self._len_field(9, self._usage(200, 40))
            zero_usage = self._len_field(
                1, self._timestamp(1_751_000_030)
            ) + self._len_field(9, self._usage(0, 0))
            no_field_9 = self._len_field(1, self._timestamp(1_751_000_040))
            valid_10 = self._len_field(
                1, self._timestamp(1_751_000_050)
            ) + self._len_field(9, self._usage(300, 60))

            rows = [
                (1, valid_1),
                (2, None),  # NULL metadata
                (3, b""),  # empty-bytes metadata
                (4, 12345),  # INTEGER stored in BLOB column
                (5, "non-bytes text"),  # TEXT stored in BLOB column
                (6, valid_6),
                (7, b"\x08\x80"),  # malformed protobuf truncated varint
                (8, zero_usage),  # total_tokens == 0
                (9, no_field_9),  # field 9 absent
                (10, valid_10),
            ]

            self._write_db(
                path,
                rows,
                workspace="file:///Users/me/testproj",
                title="Mixed Good Bad Session",
            )

            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.title, "Mixed Good Bad Session")
            self.assertEqual(summary.workspace, "testproj")
            self.assertEqual(
                summary.start_time, datetime.fromtimestamp(1_751_000_000)
            )
            self.assertEqual(summary.step_count, 3)
            self.assertEqual(summary.usage.total_tokens, 720)
            self.assertEqual(summary.usage.prompt_tokens, 600)
            self.assertEqual(summary.usage.output_tokens, 120)

            step_details = [
                (s.step_idx, s.usage.prompt_tokens, s.usage.output_tokens, s.usage.total_tokens)
                for s in summary.steps
            ]
            self.assertEqual(
                step_details,
                [(1, 100, 20, 120), (6, 200, 40, 240), (10, 300, 60, 360)],
            )

    def test_no_steps_rows_returns_session_with_zero_steps(self):
        """A session with an empty steps table or with only NULL metadata rows
        must produce a valid summary with zero steps and zero tokens."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.subTest(scenario="empty_steps_table"):
                path_empty = os.path.join(tmp, "sess-empty.db")
                self._write_db(path_empty, rows=[], title="Empty Steps Session")
                summary_empty = AntigravityParser.parse_session_db(path_empty)
                self.assertIsNotNone(summary_empty)
                self.assertEqual(summary_empty.step_count, 0)
                self.assertEqual(summary_empty.usage.total_tokens, 0)
                self.assertEqual(summary_empty.steps, [])
                self.assertEqual(summary_empty.workspace, "testproj")
                self.assertEqual(
                    summary_empty.start_time, datetime.fromtimestamp(1_751_000_000)
                )

            with self.subTest(scenario="only_null_metadata_rows"):
                path_null = os.path.join(tmp, "sess-null.db")
                self._write_db(
                    path_null,
                    rows=[(1, None), (2, None)],
                    title="Null Metadata Session",
                )
                summary_null = AntigravityParser.parse_session_db(path_null)
                self.assertIsNotNone(summary_null)
                self.assertEqual(summary_null.step_count, 0)
                self.assertEqual(summary_null.usage.total_tokens, 0)
                self.assertEqual(summary_null.steps, [])
                self.assertEqual(summary_null.title, "Null Metadata Session")
                self.assertEqual(summary_null.workspace, "testproj")
                self.assertEqual(
                    summary_null.start_time, datetime.fromtimestamp(1_751_000_000)
                )

    def test_large_session_equivalence(self):
        """A large session (~3000 steps) parsed via streaming must match
        exact step counts, token sums, and spot-checked boundary rows."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-large.db")
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                    "step_payload BLOB, metadata BLOB)"
                )
                conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")
                ws_blob = self._len_field(
                    1, self._len_field(1, b"file:///Users/me/testproj")
                ) + self._len_field(2, self._timestamp(1_751_000_000))
                conn.execute(
                    "INSERT INTO trajectory_metadata_blob (data) VALUES (?)", (ws_blob,)
                )

                rows = []
                for idx in range(1, 3001):
                    prompt = idx * 10
                    output = idx * 2
                    meta = self._len_field(
                        1, self._timestamp(1_751_000_000 + idx)
                    ) + self._len_field(9, self._usage(prompt, output))
                    payload = (
                        self._len_field(19, self._len_field(2, b"Large Session"))
                        if idx == 1
                        else b""
                    )
                    rows.append((idx, "user", payload, meta))

                conn.executemany("INSERT INTO steps VALUES (?, ?, ?, ?)", rows)
                conn.commit()
            finally:
                conn.close()

            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.step_count, 3000)
            # Sum of 12 * idx for idx in 1..3000: 12 * (3000 * 3001) / 2 = 54,018,000
            self.assertEqual(summary.usage.total_tokens, 54_018_000)
            self.assertEqual(summary.usage.prompt_tokens, 45_015_000)
            self.assertEqual(summary.usage.output_tokens, 9_003_000)

            # Spot-check first and last rows
            self.assertEqual(summary.steps[0].step_idx, 1)
            self.assertEqual(summary.steps[0].usage.total_tokens, 12)
            self.assertEqual(summary.steps[-1].step_idx, 3000)
            self.assertEqual(summary.steps[-1].usage.total_tokens, 36_000)

            # Spot-check intermediate rows
            self.assertEqual(summary.steps[499].step_idx, 500)
            self.assertEqual(summary.steps[499].usage.total_tokens, 6_000)
            self.assertEqual(summary.steps[1499].step_idx, 1500)
            self.assertEqual(summary.steps[1499].usage.total_tokens, 18_000)
            self.assertEqual(summary.steps[2499].step_idx, 2500)
            self.assertEqual(summary.steps[2499].usage.total_tokens, 30_000)

    def test_error_at_execute_returns_zero_step_session(self):
        """If cursor.execute fails for the step query (DatabaseError / OperationalError),
        the session is returned with earlier title/workspace/start_time and zero steps."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-exec-err.db")
            valid_meta = self._len_field(
                1, self._timestamp(1_751_000_010)
            ) + self._len_field(9, self._usage(100, 20))
            self._write_db(
                path,
                [(1, valid_meta), (2, valid_meta)],
                workspace="file:///Users/me/testproj",
                title="Execute Error Session",
            )

            for error_cls, err in [
                ("DatabaseError", sqlite3.DatabaseError("database disk image is malformed")),
                ("OperationalError", sqlite3.OperationalError("database is locked")),
            ]:
                with self.subTest(error_type=error_cls):
                    log = SpyLog(
                        fail_execute_pattern="metadata IS NOT NULL",
                        execute_error=err,
                    )
                    with self._patch_connect(log):
                        summary = AntigravityParser.parse_session_db(path)

                    self.assertIsNotNone(summary)
                    self.assertEqual(summary.title, "Execute Error Session")
                    self.assertEqual(summary.workspace, "testproj")
                    self.assertEqual(
                        summary.start_time, datetime.fromtimestamp(1_751_000_000)
                    )
                    self.assertEqual(summary.step_count, 0)
                    self.assertEqual(summary.usage.total_tokens, 0)
                    self.assertEqual(summary.steps, [])

    def test_error_mid_iteration_discards_all_steps(self):
        """A DatabaseError/OperationalError encountered mid-iteration must discard
        all steps (all-or-nothing), returning a summary with zero steps."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-mid-err.db")
            rows = []
            for i in range(1, 6):
                meta = self._len_field(
                    1, self._timestamp(1_751_000_000 + i * 10)
                ) + self._len_field(9, self._usage(100 * i, 20))
                rows.append((i, meta))

            self._write_db(
                path,
                rows,
                workspace="file:///Users/me/testproj",
                title="Mid Iteration Error Session",
            )

            for error_cls, err in [
                ("DatabaseError", sqlite3.DatabaseError("disk I/O error mid-iteration")),
                ("OperationalError", sqlite3.OperationalError("database is locked mid-iteration")),
            ]:
                with self.subTest(error_type=error_cls):
                    # Fail after pulling 3 rows (raising on 4th pull, or on fetchall in old code)
                    log = SpyLog(fail_after=3, mid_read_error=err)
                    with self._patch_connect(log):
                        summary = AntigravityParser.parse_session_db(path)

                    self.assertIsNotNone(summary)
                    self.assertEqual(summary.title, "Mid Iteration Error Session")
                    self.assertEqual(summary.workspace, "testproj")
                    self.assertEqual(
                        summary.start_time, datetime.fromtimestamp(1_751_000_000)
                    )
                    self.assertEqual(
                        summary.step_count,
                        0,
                        "Mid-read error must discard all partially parsed steps",
                    )
                    self.assertEqual(summary.usage.total_tokens, 0)
                    self.assertEqual(summary.steps, [])

    def test_unexpected_exception_in_step_processing_returns_none(self):
        """Any unexpected exception (e.g. RuntimeError) during step processing
        must cause parse_session_db to return None."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-unexpected-err.db")
            rows = []
            for i in range(1, 4):
                meta = self._len_field(
                    1, self._timestamp(1_751_000_000 + i * 10)
                ) + self._len_field(9, self._usage(100 * i, 20))
                rows.append((i, meta))
            self._write_db(path, rows, title="Unexpected Error Session")

            call_count = 0

            def flaky_parse_usage_proto(data):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("unexpected corruption during step processing")
                return _REAL_PARSE_USAGE_PROTO(data)

            with mock.patch(
                "agy_usage.parser.parse_usage_proto",
                side_effect=flaky_parse_usage_proto,
            ):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNone(
                summary,
                "Unexpected exception during step processing must return None",
            )

    def test_connection_closed_on_success_and_on_failures(self):
        """The SQLite connection must be closed exactly once across success,
        execute failure, mid-iteration failure, and unexpected exception."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-close.db")
            rows = []
            for i in range(1, 4):
                meta = self._len_field(
                    1, self._timestamp(1_751_000_000 + i * 10)
                ) + self._len_field(9, self._usage(100 * i, 20))
                rows.append((i, meta))
            self._write_db(path, rows, title="Close Test Session")

            with self.subTest(scenario="success"):
                log = SpyLog()
                with self._patch_connect(log):
                    summary = AntigravityParser.parse_session_db(path)
                self.assertIsNotNone(summary)
                self.assertEqual(log.close_calls, 1)

            with self.subTest(scenario="database_error_at_execute"):
                log = SpyLog(
                    fail_execute_pattern="metadata IS NOT NULL",
                    execute_error=sqlite3.DatabaseError("execute fail"),
                )
                with self._patch_connect(log):
                    summary = AntigravityParser.parse_session_db(path)
                self.assertIsNotNone(summary)
                self.assertEqual(log.close_calls, 1)

            with self.subTest(scenario="mid_iteration_database_error"):
                log = SpyLog(
                    fail_after=1,
                    mid_read_error=sqlite3.DatabaseError("mid-iteration fail"),
                )
                with self._patch_connect(log):
                    summary = AntigravityParser.parse_session_db(path)
                self.assertIsNotNone(summary)
                self.assertEqual(log.close_calls, 1)

            with self.subTest(scenario="unexpected_runtime_error"):
                log = SpyLog()
                call_count = 0

                def flaky_parse_usage(data):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 2:
                        raise RuntimeError("unexpected failure")
                    return _REAL_PARSE_USAGE_PROTO(data)

                with self._patch_connect(log):
                    with mock.patch(
                        "agy_usage.parser.parse_usage_proto",
                        side_effect=flaky_parse_usage,
                    ):
                        summary = AntigravityParser.parse_session_db(path)
                self.assertIsNone(summary)
                self.assertEqual(log.close_calls, 1)

    def test_step_rows_are_not_materialized_with_fetchall(self):
        """LAZY-ONLY: Ensure step query rows are streamed lazily and not
        materialized in bulk via cursor.fetchall()."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-lazy-fetchall.db")
            rows = []
            for i in range(1, 21):
                meta = self._len_field(
                    1, self._timestamp(1_751_000_000 + i * 10)
                ) + self._len_field(9, self._usage(100 * i, 20))
                rows.append((i, meta))
            self._write_db(path, rows, title="Lazy Fetchall Test Session")

            log = SpyLog()
            with self._patch_connect(log):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.step_count, 20)

            step_fetchall_calls = [
                sql for sql in log.fetchall_calls if "metadata IS NOT NULL" in sql
            ]
            self.assertEqual(
                step_fetchall_calls,
                [],
                "Step query rows must be consumed lazily without calling fetchall()",
            )

    def test_step_rows_are_consumed_incrementally(self):
        """LAZY-ONLY: Verify that step rows are consumed one by one on demand,
        so rows_pulled equals [1, 2, ..., N] when each step's usage is parsed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sess-lazy-incremental.db")
            num_steps = 20
            rows = []
            for i in range(1, num_steps + 1):
                meta = self._len_field(
                    1, self._timestamp(1_751_000_000 + i * 10)
                ) + self._len_field(9, self._usage(100 * i, 20))
                rows.append((i, meta))
            self._write_db(path, rows, title="Lazy Incremental Test Session")

            log = SpyLog()
            observed_rows_pulled = []

            def record_rows_pulled(data):
                observed_rows_pulled.append(log.rows_pulled)
                return _REAL_PARSE_USAGE_PROTO(data)

            with self._patch_connect(log):
                with mock.patch(
                    "agy_usage.parser.parse_usage_proto",
                    side_effect=record_rows_pulled,
                ):
                    summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.step_count, num_steps)
            self.assertEqual(
                observed_rows_pulled,
                list(range(1, num_steps + 1)),
                "Each row must be pulled immediately before parsing its usage (lazy row-by-row)",
            )


if __name__ == "__main__":
    unittest.main()
