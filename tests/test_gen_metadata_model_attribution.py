"""Tests for model attribution from gen_metadata rows."""

import os
import sqlite3
import tempfile
import unittest

from agy_usage.aggregator import UsageAggregator
from agy_usage.parser import AntigravityParser


class TestGenMetadataModelAttribution(unittest.TestCase):
    """Regression tests for BUG-010: gen_metadata rows whose model name cannot
    be resolved (empty, M-placeholder, bare enum number) must neither enter
    ``known_models`` nor become attribution anchors — otherwise a phantom
    ``gemini-3.8-flash`` model with zero evidence distorts the model
    distribution and per-model cost. Legitimately named rows (including
    aliased names) keep their previous attribution exactly."""

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

    def _pb_str(self, field_num: int, value: str) -> bytes:
        payload = value.encode("utf-8")
        return self._varint((field_num << 3) | 2) + self._varint(len(payload)) + payload

    def _gen_metadata_row(self, idx, model_name="", last_step_idx=None, model_enum=None):
        """Build a gen_metadata blob: field 1 = {19: model name, 20: {key, val}}."""
        f20 = b""
        if last_step_idx is not None:
            f20 += self._pb_str(1, "last_step_index") + self._pb_str(2, str(last_step_idx))
        if model_enum is not None:
            f20 += self._pb_str(1, "model_enum") + self._pb_str(2, model_enum)
        parts = []
        if model_name:
            parts.append(self._pb_str(19, model_name))
        if f20:
            parts.append(self._varint((20 << 3) | 2) + self._varint(len(f20)) + f20)
        f1 = b"".join(parts)
        return idx, self._varint((1 << 3) | 2) + self._varint(len(f1)) + f1

    def _step_metadata(self, prompt: int, output: int) -> bytes:
        ts = bytes([0x08]) + self._varint(1_751_234_567)
        usage = self._varint(2 << 3) + self._varint(prompt)
        usage += self._varint(3 << 3) + self._varint(output)
        return bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage

    def _write_db(self, path, gen_rows, steps):
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute(
                "CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)"
            )
            for idx, data in gen_rows:
                conn.execute(
                    "INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                    (idx, data, len(data)),
                )
            for idx, metadata in steps:
                conn.execute(
                    "INSERT INTO steps (idx, step_type, step_payload, metadata) "
                    "VALUES (?, 'user', '', ?)",
                    (idx, metadata),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_unresolvable_row_is_neither_known_model_nor_anchor(self):
        # Companion row (enum 298 -> "gemini-3.7-flash", last_step 3) anchors
        # step 4 directly; the unresolvable, name-less row (last_step 7) must
        # not anchor step 8 with a phantom "gemini-3.8-flash" — step 8 falls
        # back to the nearest earlier anchor instead. Total tokens stay intact.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "phantom-anchor.db"),
                gen_rows=[
                    self._gen_metadata_row(1, last_step_idx=3, model_enum="MODEL_PLACEHOLDER_M298"),
                    self._gen_metadata_row(2, last_step_idx=7),  # no name at all
                ],
                steps=[
                    (4, self._step_metadata(1000, 200)),
                    (8, self._step_metadata(2000, 400)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("gemini-3.8-flash", summary.models)
            self.assertEqual(summary.models, ["gemini-3.7-flash"])
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [(4, "gemini-3.7-flash"), (8, "gemini-3.7-flash")],
            )
            self.assertEqual(summary.usage.total_tokens, 3600)
            model_rows = [
                (m.model_name, m.usage.total_tokens)
                for m in UsageAggregator.aggregate_models([summary])
            ]
            self.assertEqual(model_rows, [("gemini-3.7-flash", 3600)])

    def test_unresolvable_row_falls_back_to_evidence_session_default(self):
        # Only an unresolvable row sits before step 6: it must anchor nothing,
        # so step 6 is attributed via the session default derived from the
        # (later) resolvable row "claude-sonnet-5-5" — not via a phantom
        # "gemini-3.8-flash" anchor.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "default-fallback.db"),
                gen_rows=[
                    self._gen_metadata_row(1, last_step_idx=5),  # no name, no enum
                    self._gen_metadata_row(2, model_name="claude-sonnet-5-5", last_step_idx=9),
                ],
                steps=[
                    (6, self._step_metadata(1000, 200)),
                    (10, self._step_metadata(2000, 400)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("gemini-3.8-flash", summary.models)
            self.assertEqual(summary.models, ["claude-sonnet-5-5"])
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [(6, "claude-sonnet-5-5"), (10, "claude-sonnet-5-5")],
            )
            self.assertEqual(summary.usage.total_tokens, 3600)

    def test_alias_named_rows_keep_original_attribution(self):
        # An aliased gen_metadata name ("gemini-3.8-flash-low" -> canonical
        # "gemini-3.8-flash") must still seed known_models and anchor steps
        # exactly as before the fix: step 1 anchored directly by the alias row,
        # step 8 via that same earlier anchor (previously it matched the
        # phantom anchor instead, but with the identical result).
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "alias-anchor.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="gemini-3.8-flash-low", last_step_idx=0),
                    self._gen_metadata_row(2, last_step_idx=7),  # unresolvable
                ],
                steps=[
                    (1, self._step_metadata(500, 100)),
                    (8, self._step_metadata(700, 100)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.models, ["gemini-3.8-flash"])
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [(1, "gemini-3.8-flash"), (8, "gemini-3.8-flash")],
            )
            self.assertEqual(summary.usage.total_tokens, 1400)

    def test_bare_enum_number_is_not_a_model_name(self):
        # "MODEL_PLACEHOLDER_33" (unknown enum 33) used to resolve raw_name to
        # the bogus model "33" and anchor steps with it; it must be treated as
        # unresolvable and skipped.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "bare-enum.db"),
                gen_rows=[
                    self._gen_metadata_row(1, last_step_idx=2, model_enum="MODEL_PLACEHOLDER_33"),
                ],
                steps=[(3, self._step_metadata(900, 0))],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("33", summary.models)
            # Nothing resolvable, so tokens land on the documented session
            # default — never on the fabricated "33" model name.
            self.assertEqual(summary.models, ["gemini-3.8-flash"])
            self.assertEqual(
                [(s.step_idx, s.model, s.usage.total_tokens) for s in summary.steps],
                [(3, "gemini-3.8-flash", 900)],
            )
            self.assertEqual(summary.usage.total_tokens, 900)

    def test_field19_placeholder_is_not_a_model_name(self):
        # A placeholder string recorded in the model-name field (19) must not
        # seed known_models nor corrupt enum_to_model resolution.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "placeholder-name.db"),
                gen_rows=[
                    self._gen_metadata_row(
                        1,
                        model_name="MODEL_PLACEHOLDER_M500",
                        last_step_idx=2,
                        model_enum="MODEL_PLACEHOLDER_M500",
                    ),
                ],
                steps=[(3, self._step_metadata(800, 0))],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("MODEL_PLACEHOLDER_M500", summary.models)
            self.assertEqual(summary.models, ["gemini-3.8-flash"])
            self.assertEqual(summary.usage.total_tokens, 800)

    def test_normalize_model_name_empty_is_unresolvable(self):
        # The contract BUG-010's fix rests on: empty/whitespace-only names
        # normalize to '' (unresolvable), while the legit alias/variant
        # mappings are unchanged.
        from agy_usage.models import normalize_model_name

        self.assertEqual(normalize_model_name(""), "")
        self.assertEqual(normalize_model_name("   "), "")
        self.assertEqual(normalize_model_name("gemini-3.8-flash-n"), "gemini-3.8-flash")
        self.assertEqual(normalize_model_name("claude-5.5"), "claude-5.5")
        self.assertEqual(normalize_model_name("gpt-oss-120b"), "gpt-oss-120b")


if __name__ == "__main__":
    unittest.main()
