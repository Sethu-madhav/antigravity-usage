"""Tests that each stage of the session parse fails independently.

A failure in one stage (workspace, gen_metadata, title, step reading) must keep
everything the earlier stages already produced, and later stages must still run.
"""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.parser import AntigravityParser, parse_proto_fields

BLOB_TS = 1_751_000_000
STEP_TS = 1_751_234_567


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


def _len_field(num, data):
    return _varint((num << 3) | 2) + _varint(len(data)) + data


def _varint_field(num, value):
    return _varint(num << 3) + _varint(value)


def _gen_metadata_blob(model_name, last_step_idx):
    f20 = _len_field(1, b"last_step_index") + _len_field(2, str(last_step_idx).encode())
    inner = _len_field(19, model_name.encode()) + _len_field(20, f20)
    return _len_field(1, inner)


def _step_metadata():
    ts = _len_field(1, _varint_field(1, STEP_TS))
    usage = _len_field(9, _varint_field(2, 1000) + _varint_field(3, 200))
    return ts + usage


def _write_db(path, gen_blobs=(), title="Diagnose the parser"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
            "step_payload BLOB, metadata BLOB)"
        )
        conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
        ws_blob = _len_field(1, _len_field(1, b"file:///Users/me/proj"))
        ws_blob += _len_field(2, _varint_field(1, BLOB_TS))
        conn.execute("INSERT INTO trajectory_metadata_blob (data) VALUES (?)", (ws_blob,))
        for idx, blob in enumerate(gen_blobs):
            conn.execute(
                "INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                (idx, blob, len(blob)),
            )
        payload = _len_field(19, _len_field(2, title.encode()))
        conn.execute(
            "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (1, 'user', ?, ?)",
            (payload, _step_metadata()),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _raise_on(bad_blob, error):
    def fake(data):
        if data == bad_blob:
            raise error
        return parse_proto_fields(data)

    return fake


class TestParseStageIsolation(unittest.TestCase):
    def test_gen_metadata_failure_keeps_other_stages(self):
        bad = _gen_metadata_blob("never-parsed", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_db(os.path.join(tmp, "sess-gm-fail.db"), [bad])
            with mock.patch(
                "agy_usage.parser.parse_proto_fields",
                side_effect=_raise_on(bad, ValueError("corrupt gen_metadata")),
            ):
                summary, complete = AntigravityParser._parse_session_db_checked(path)

        self.assertTrue(complete)
        self.assertEqual(summary.workspace, "proj")
        self.assertEqual(summary.title, "Diagnose the parser")
        self.assertEqual(summary.step_count, 1)
        self.assertEqual(summary.usage.total_tokens, 1200)
        self.assertEqual(summary.start_time, datetime.fromtimestamp(BLOB_TS))

    def test_models_from_earlier_gen_metadata_rows_survive_a_later_failure(self):
        good = _gen_metadata_blob("model-alpha", 1)
        bad = _gen_metadata_blob("model-beta", 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_db(os.path.join(tmp, "sess-gm-partial.db"), [good, bad])
            with mock.patch(
                "agy_usage.parser.parse_proto_fields",
                side_effect=_raise_on(bad, ValueError("corrupt gen_metadata")),
            ):
                summary, complete = AntigravityParser._parse_session_db_checked(path)

        self.assertTrue(complete)
        # The second pass never ran, so the step falls back to the session default.
        self.assertEqual(summary.models, ["model-alpha"])
        self.assertEqual([s.model for s in summary.steps], ["model-alpha"])

    def test_gen_metadata_model_survives_failure_inside_the_same_row(self):
        """known_models.add happens before the rest of the row is decoded."""
        f20 = _len_field(1, b"last_step_index") + _len_field(2, b"1")
        inner = _len_field(19, b"model-alpha") + _len_field(20, f20)
        blob = _len_field(1, inner)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_db(os.path.join(tmp, "sess-gm-row.db"), [blob])
            with mock.patch(
                "agy_usage.parser.parse_proto_fields",
                side_effect=_raise_on(f20, ValueError("corrupt field 20")),
            ):
                summary, complete = AntigravityParser._parse_session_db_checked(path)

        self.assertTrue(complete)
        self.assertEqual(summary.models, ["model-alpha"])

    def test_execute_failure_during_step_read_keeps_title_and_workspace(self):
        real_connect = sqlite3.connect

        class FailingCursor:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *params):
                if "metadata IS NOT NULL" in sql:
                    raise sqlite3.OperationalError("database is locked")
                return self._inner.execute(sql, *params)

            def __iter__(self):
                return iter(self._inner)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        class FailingConnection:
            def __init__(self, inner):
                self._inner = inner

            def cursor(self):
                return FailingCursor(self._inner.cursor())

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with tempfile.TemporaryDirectory() as tmp:
            path = _write_db(os.path.join(tmp, "sess-step-lock.db"))
            with mock.patch(
                "agy_usage.parser.sqlite3.connect",
                side_effect=lambda *a, **k: FailingConnection(real_connect(*a, **k)),
            ):
                summary, complete = AntigravityParser._parse_session_db_checked(path)

        self.assertIsNotNone(summary)
        self.assertEqual(summary.title, "Diagnose the parser")
        self.assertEqual(summary.workspace, "proj")
        self.assertEqual(summary.step_count, 0)
        self.assertEqual(summary.start_time, datetime.fromtimestamp(BLOB_TS))

    def test_sqlite_error_after_validation_returns_incomplete_partial_summary(self):
        """An escaping sqlite3.DatabaseError after the steps table was validated
        yields the partial summary, flagged incomplete so it is never cached."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_db(os.path.join(tmp, "sess-late-error.db"))
            with mock.patch(
                "agy_usage.parser._fill_missing_step_timestamps",
                side_effect=sqlite3.DatabaseError("database disk image is malformed"),
            ):
                summary, complete = AntigravityParser._parse_session_db_checked(path)

        self.assertFalse(complete)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.title, "Diagnose the parser")
        self.assertEqual(summary.workspace, "proj")
        self.assertEqual(summary.start_time, datetime.fromtimestamp(BLOB_TS))
        self.assertEqual(summary.step_count, 0)

    def test_title_failure_keeps_default_title_and_still_parses_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_db(os.path.join(tmp, "sess-title-fail.db"))
            real = parse_proto_fields

            def fail_on_payload(data):
                # Only the title stage parses the step_payload (field 19 wrapper).
                if data.startswith(_varint((19 << 3) | 2)):
                    raise ValueError("corrupt payload")
                return real(data)

            with mock.patch("agy_usage.parser.parse_proto_fields", side_effect=fail_on_payload):
                summary, complete = AntigravityParser._parse_session_db_checked(path)

        self.assertTrue(complete)
        self.assertEqual(summary.title, "Session sess-tit")
        self.assertEqual(summary.workspace, "proj")
        self.assertEqual(summary.step_count, 1)
        self.assertEqual(summary.usage.total_tokens, 1200)

    def test_missing_steps_table_is_not_a_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nosteps.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE other (a)")
            conn.commit()
            conn.close()
            self.assertEqual(AntigravityParser._parse_session_db_checked(path), (None, False))


if __name__ == "__main__":
    unittest.main()
