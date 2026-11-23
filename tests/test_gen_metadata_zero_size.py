"""Regression tests for gen_metadata rows whose `size` column is 0 or NULL."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from agy_usage.parser import AntigravityParser


class TestGenMetadataZeroSize(unittest.TestCase):
    """Antigravity 2.0 writes populated `gen_metadata.data` blobs with `size = 0`.

    Filtering `WHERE size > 0` silently skipped every such row and caused
    sessions using newer models (e.g. `gemini-4-neon`) to fall back to the
    hardcoded default (`gemini-3.8-flash`).
    """

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
    def _len_field(cls, field_num: int, payload: bytes) -> bytes:
        return cls._varint((field_num << 3) | 2) + cls._varint(len(payload)) + payload

    @classmethod
    def _varint_field(cls, field_num: int, value: int) -> bytes:
        return cls._varint(field_num << 3) + cls._varint(value)

    @classmethod
    def _gen_metadata_blob(cls, model_name: str, last_step_idx: int, model_enum: str) -> bytes:
        inner = bytearray()
        if model_name:
            inner += cls._len_field(19, model_name.encode("utf-8"))
        for k, v in (("last_step_index", str(last_step_idx)), ("model_enum", model_enum)):
            kv = cls._len_field(1, k.encode("utf-8")) + cls._len_field(2, v.encode("utf-8"))
            inner += cls._len_field(20, kv)
        return cls._len_field(1, bytes(inner))

    @classmethod
    def _step_metadata(cls, ts_sec: int, model_enum: int, prompt: int, output: int) -> bytes:
        ts = cls._varint_field(1, ts_sec)
        usage = (
            cls._varint_field(1, model_enum)
            + cls._varint_field(2, prompt)
            + cls._varint_field(3, output)
        )
        return cls._len_field(1, ts) + cls._len_field(9, usage)

    def _build_db(self, path: str, gm_rows, step_rows) -> str:
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)")
            for idx, data, size in gm_rows:
                conn.execute("INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)", (idx, data, size))
            for idx, meta in step_rows:
                conn.execute(
                    "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (?, 'user', '', ?)",
                    (idx, meta),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_zero_and_null_size_gen_metadata_rows_are_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "zero_size_gm.db")
            gm_rows = [
                (0, self._gen_metadata_blob("gemini-4-neon", 0, "MODEL_PLACEHOLDER_M535"), 0),
                (1, self._gen_metadata_blob("gemini-4-neon", 2, "MODEL_PLACEHOLDER_M535"), None),
                (2, b"", 0),  # empty blob must still be skipped safely
            ]
            step_rows = [
                (1, self._step_metadata(1_760_000_001, 1535, 1000, 200)),
                (3, self._step_metadata(1_760_000_005, 1535, 1500, 300)),
            ]
            self._build_db(path, gm_rows, step_rows)

            summary = AntigravityParser.parse_session_db(path)

        self.assertIsNotNone(summary)
        self.assertEqual(summary.models, ["gemini-4-neon"])
        self.assertEqual([s.model for s in summary.steps], ["gemini-4-neon", "gemini-4-neon"])

    def test_known_enum_fallback_resolves_neon_without_gen_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "enum_only_neon.db")
            step_rows = [
                (1, self._step_metadata(1_760_000_010, 1535, 2000, 400)),
            ]
            self._build_db(path, [], step_rows)

            summary = AntigravityParser.parse_session_db(path)

        self.assertIsNotNone(summary)
        self.assertEqual(summary.models, ["gemini-4-neon"])
        self.assertEqual(summary.steps[0].model, "gemini-4-neon")


if __name__ == "__main__":
    unittest.main()
