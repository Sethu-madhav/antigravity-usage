"""Tests for parsing a session database."""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from agy_usage.parser import AntigravityParser


class TestParseSessionDB(unittest.TestCase):
    """Regression tests for BUG-007: the SQLite filename must be percent-quoted
    into the URI, the DB must open read-only, and there must be no silent
    read-write fallback."""

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

    def _write_session_db(self, path):
        """Create a minimal real session DB: one step whose `metadata`
        protobuf carries a Timestamp (field 1) and UsageMetadata (field 9)
        with prompt=1000, output=200."""
        ts = bytes([0x08]) + self._varint(1_751_234_567)
        usage = bytes([0x10, 0xE8, 0x07, 0x18, 0xC8, 0x01])  # {2:1000, 3:200}
        metadata = bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute(
                "INSERT INTO steps (idx, step_type, step_payload, metadata) "
                "VALUES (1, 'user', '', ?)",
                (metadata,),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_hash_and_question_mark_paths_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            for filename in ("sess#ion.db", "sess?ion.db"):
                path = self._write_session_db(os.path.join(tmp, filename))
                summary = AntigravityParser.parse_session_db(path)
                self.assertIsNotNone(summary, f"parse failed for path: {path}")
                self.assertEqual(summary.session_id, filename.replace(".db", ""))
                self.assertEqual(summary.usage.total_tokens, 1200)

    def test_percent_path_parses_via_quoted_uri_single_connect(self):
        # '%41' percent-decodes to 'A': the old unquoted URI resolved to a
        # phantom 'sessAion.db' and only ever parsed via the silent read-write
        # fallback. The quoted URI (%25) must hit the real file with exactly
        # one read-only connect attempt.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_session_db(os.path.join(tmp, "sess%41ion.db"))
            with mock.patch("sqlite3.connect", wraps=sqlite3.connect) as m:
                summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.usage.total_tokens, 1200)
            self.assertEqual(m.call_count, 1)  # no read-write fallback attempt
            uri = m.call_args.args[0]
            self.assertEqual(m.call_args.kwargs.get("uri"), True)
            self.assertTrue(uri.startswith("file:"))
            self.assertIn("sess%2541ion.db", uri)
            self.assertIn("?mode=ro", uri)

    def test_vanished_db_returns_none_and_creates_no_file(self):
        # os.path.isfile() answered "exists" but the DB is gone by connect
        # time: must return None, must not create the file (read-only mode)
        # and must not retry via the old read-write fallback (TOCTOU).
        with tempfile.TemporaryDirectory() as tmp:
            ghost = os.path.join(tmp, "ghost.db")
            with mock.patch("agy_usage.parser.os.path.isfile", return_value=True):
                self.assertIsNone(AntigravityParser.parse_session_db(ghost))
            self.assertFalse(os.path.exists(ghost))
            self.assertFalse(os.path.exists(ghost + "-journal"))
            self.assertFalse(os.path.exists(ghost + "-wal"))


if __name__ == "__main__":
    unittest.main()
