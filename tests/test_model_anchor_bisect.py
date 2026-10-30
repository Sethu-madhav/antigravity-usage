"""Tests for model anchor attribution and lookup scaling (OPT-006).

Validates model attribution precedence:
1. Direct hit on step_to_model (last_step_index and last_step_index + 1)
2. Step UsageMetadata field 1 model_enum (if known in enum_to_model)
3. Most recent preceding gen_metadata anchor (bisect / reverse scan)
4. Session default model (alphabetically first known model, else gemini-3.8-flash)
"""

import os
import random
import sqlite3
import tempfile
import time
import unittest

from agy_usage.parser import AntigravityParser


class TestModelAnchorBisect(unittest.TestCase):
    """Regression and scaling tests for gen_metadata model anchor attribution."""

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

    def _step_metadata(self, prompt: int, output: int, model_enum=None) -> bytes:
        """Build step metadata blob: field 1 = timestamp, field 9 = usage proto."""
        ts = bytes([0x08]) + self._varint(1_751_234_567)
        usage = bytearray()
        if model_enum is not None:
            # UsageMetadata field 1 is varint model_enum
            usage += self._varint(1 << 3) + self._varint(model_enum)
        usage += self._varint(2 << 3) + self._varint(prompt)
        usage += self._varint(3 << 3) + self._varint(output)
        usage_bytes = bytes(usage)
        return (
            self._varint((1 << 3) | 2) + self._varint(len(ts)) + ts
            + self._varint((9 << 3) | 2) + self._varint(len(usage_bytes)) + usage_bytes
        )

    def _write_db(self, path, gen_rows, steps):
        """Create a session DB with steps and optional gen_metadata rows."""
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            if gen_rows is not None:
                conn.execute(
                    "CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)"
                )
                conn.executemany(
                    "INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                    [(idx, data, len(data)) for idx, data in gen_rows],
                )
            conn.executemany(
                "INSERT INTO steps (idx, step_type, step_payload, metadata) "
                "VALUES (?, 'user', '', ?)",
                steps,
            )
            conn.commit()
        finally:
            conn.close()
        return path

    @staticmethod
    def _oracle_attribution(gen_rows_spec, step_indices):
        """Reference attribution oracle deliberately simulating the old linear algorithm."""
        known_models = set()
        step_to_model = {}
        for r_idx, m_name, l_idx in sorted(gen_rows_spec, key=lambda r: r[0]):
            if m_name:
                known_models.add(m_name)
                if l_idx >= 0:
                    step_to_model[l_idx] = m_name
                    step_to_model[l_idx + 1] = m_name
        anchors = sorted((k, v) for k, v in step_to_model.items() if v)
        default_model = sorted(list(known_models))[0] if known_models else "gemini-3.8-flash"

        expected = {}
        for idx in sorted(step_indices):
            m = step_to_model.get(idx)
            if not m and anchors:
                for a_idx, a_model in reversed(anchors):
                    if a_idx <= idx:
                        m = a_model
                        break
            if not m:
                m = default_model
            expected[idx] = m
        return expected

    def test_steps_strictly_between_anchors(self):
        """Steps strictly between anchors inherit the nearest preceding anchor's model."""
        # Anchor @10 (indices 10, 11) is claude-sonnet-5-5.
        # Anchor @20 (indices 20, 21) is claude-opus-5-5.
        # Steps 12, 15, 19 fall between 11 and 20 -> get claude-sonnet-5-5.
        # Steps 22, 40 fall after 21 -> get claude-opus-5-5.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "between_anchors.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="claude-sonnet-5-5", last_step_idx=10),
                    self._gen_metadata_row(2, model_name="claude-opus-5-5", last_step_idx=20),
                ],
                steps=[
                    (12, self._step_metadata(100, 20)),
                    (15, self._step_metadata(200, 30)),
                    (19, self._step_metadata(300, 40)),
                    (22, self._step_metadata(400, 50)),
                    (40, self._step_metadata(500, 60)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [
                    (12, "claude-sonnet-5-5"),
                    (15, "claude-sonnet-5-5"),
                    (19, "claude-sonnet-5-5"),
                    (22, "claude-opus-5-5"),
                    (40, "claude-opus-5-5"),
                ],
            )
            self.assertEqual(summary.models, ["claude-opus-5-5", "claude-sonnet-5-5"])
            self.assertEqual(summary.usage.total_tokens, 1700)

    def test_steps_before_first_anchor_fallback_to_session_default(self):
        """Steps before the very first anchor must use the session default, not any anchor."""
        # Anchor @10 has model gemini-3.7-flash. Anchor @30 has model claude-sonnet-5-5.
        # Known models are {claude-sonnet-5-5, gemini-3.7-flash}.
        # Session default is sorted(known_models)[0] = claude-sonnet-5-5.
        # Steps 1 and 2 precede anchor @10, so they must receive the session default
        # (claude-sonnet-5-5) rather than the first anchor's model (gemini-3.7-flash).
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "before_first_anchor.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="gemini-3.7-flash", last_step_idx=10),
                    self._gen_metadata_row(2, model_name="claude-sonnet-5-5", last_step_idx=30),
                ],
                steps=[
                    (1, self._step_metadata(100, 10)),
                    (2, self._step_metadata(200, 20)),
                    (15, self._step_metadata(300, 30)),
                    (35, self._step_metadata(400, 40)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [
                    (1, "claude-sonnet-5-5"),
                    (2, "claude-sonnet-5-5"),
                    (15, "gemini-3.7-flash"),
                    (35, "claude-sonnet-5-5"),
                ],
            )
            self.assertEqual(summary.models, ["claude-sonnet-5-5", "gemini-3.7-flash"])
            self.assertEqual(summary.usage.total_tokens, 1100)

    def test_steps_at_anchor_positions_and_beyond_last_anchor(self):
        """Steps at idx == last, idx == last+1, and beyond last anchor are correctly resolved."""
        # Row 1 sets anchors at 10 and 11 to claude-sonnet-5-5.
        # Row 2 sets anchors at 30 and 31 to claude-opus-5-5.
        # Step 100 is far beyond the last anchor (31) and inherits claude-opus-5-5.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "anchor_positions.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="claude-sonnet-5-5", last_step_idx=10),
                    self._gen_metadata_row(2, model_name="claude-opus-5-5", last_step_idx=30),
                ],
                steps=[
                    (10, self._step_metadata(100, 10)),
                    (11, self._step_metadata(200, 20)),
                    (30, self._step_metadata(300, 30)),
                    (31, self._step_metadata(400, 40)),
                    (100, self._step_metadata(500, 50)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [
                    (10, "claude-sonnet-5-5"),
                    (11, "claude-sonnet-5-5"),
                    (30, "claude-opus-5-5"),
                    (31, "claude-opus-5-5"),
                    (100, "claude-opus-5-5"),
                ],
            )
            self.assertEqual(summary.models, ["claude-opus-5-5", "claude-sonnet-5-5"])
            self.assertEqual(summary.usage.total_tokens, 1650)

    def test_no_anchors_at_all(self):
        """When no valid anchors exist (unresolvable rows or missing table), default is used."""
        steps_data = [
            (1, self._step_metadata(100, 20)),
            (5, self._step_metadata(200, 30)),
            (10, self._step_metadata(300, 40)),
            (20, self._step_metadata(400, 50)),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            # Case A: gen_metadata table has only unresolvable rows
            path_unresolvable = self._write_db(
                os.path.join(tmp, "unresolvable_only.db"),
                gen_rows=[
                    self._gen_metadata_row(1, last_step_idx=5),
                    self._gen_metadata_row(2, model_name="MODEL_PLACEHOLDER_M123", last_step_idx=10),
                    self._gen_metadata_row(3, model_name="42", last_step_idx=15),
                ],
                steps=steps_data,
            )
            summary_a = AntigravityParser.parse_session_db(path_unresolvable)
            self.assertIsNotNone(summary_a)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary_a.steps],
                [
                    (1, "gemini-3.8-flash"),
                    (5, "gemini-3.8-flash"),
                    (10, "gemini-3.8-flash"),
                    (20, "gemini-3.8-flash"),
                ],
            )
            self.assertEqual(summary_a.models, ["gemini-3.8-flash"])
            self.assertEqual(summary_a.usage.total_tokens, 1140)

            # Case B: gen_metadata table is completely absent
            path_no_table = self._write_db(
                os.path.join(tmp, "no_table.db"),
                gen_rows=None,
                steps=steps_data,
            )
            summary_b = AntigravityParser.parse_session_db(path_no_table)
            self.assertIsNotNone(summary_b)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary_b.steps],
                [
                    (1, "gemini-3.8-flash"),
                    (5, "gemini-3.8-flash"),
                    (10, "gemini-3.8-flash"),
                    (20, "gemini-3.8-flash"),
                ],
            )
            self.assertEqual(summary_b.models, ["gemini-3.8-flash"])
            self.assertEqual(summary_b.usage.total_tokens, 1140)

    def test_colliding_overlapping_and_non_monotonic_anchors(self):
        """Later rows overwrite on key collision, and attribution keys on step position."""
        with tempfile.TemporaryDirectory() as tmp:
            # Sub-test 1: Colliding / overlapping anchor keys (later row wins on collision)
            # Row 1 (last=10) sets 10, 11 -> sonnet.
            # Row 2 (last=11) sets 11, 12 -> opus (overwrites key 11).
            path_collide = self._write_db(
                os.path.join(tmp, "collide.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="claude-sonnet-5-5", last_step_idx=10),
                    self._gen_metadata_row(2, model_name="claude-opus-5-5", last_step_idx=11),
                ],
                steps=[
                    (10, self._step_metadata(100, 10)),
                    (11, self._step_metadata(200, 20)),
                    (12, self._step_metadata(300, 30)),
                    (15, self._step_metadata(400, 40)),
                ],
            )
            summary_collide = AntigravityParser.parse_session_db(path_collide)
            self.assertIsNotNone(summary_collide)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary_collide.steps],
                [
                    (10, "claude-sonnet-5-5"),
                    (11, "claude-opus-5-5"),
                    (12, "claude-opus-5-5"),
                    (15, "claude-opus-5-5"),
                ],
            )
            self.assertEqual(summary_collide.models, ["claude-opus-5-5", "claude-sonnet-5-5"])
            self.assertEqual(summary_collide.usage.total_tokens, 1100)

            # Sub-test 2: Non-monotonic last_step_index in row order
            # Row 1 (idx 1): last_step=30 (opus) -> sets 30, 31 to opus
            # Row 2 (idx 2): last_step=5 (sonnet) -> sets 5, 6 to sonnet
            # Step position determines attribution, not the row order in gen_metadata.
            path_nonmono = self._write_db(
                os.path.join(tmp, "non_monotonic.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="claude-opus-5-5", last_step_idx=30),
                    self._gen_metadata_row(2, model_name="claude-sonnet-5-5", last_step_idx=5),
                ],
                steps=[
                    (2, self._step_metadata(100, 10)),
                    (5, self._step_metadata(200, 20)),
                    (6, self._step_metadata(300, 30)),
                    (15, self._step_metadata(400, 40)),
                    (30, self._step_metadata(500, 50)),
                    (31, self._step_metadata(600, 60)),
                    (45, self._step_metadata(700, 70)),
                ],
            )
            summary_nonmono = AntigravityParser.parse_session_db(path_nonmono)
            self.assertIsNotNone(summary_nonmono)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary_nonmono.steps],
                [
                    (2, "claude-opus-5-5"),
                    (5, "claude-sonnet-5-5"),
                    (6, "claude-sonnet-5-5"),
                    (15, "claude-sonnet-5-5"),
                    (30, "claude-opus-5-5"),
                    (31, "claude-opus-5-5"),
                    (45, "claude-opus-5-5"),
                ],
            )
            self.assertEqual(summary_nonmono.models, ["claude-opus-5-5", "claude-sonnet-5-5"])
            self.assertEqual(summary_nonmono.usage.total_tokens, 3080)

    def test_usage_enum_precedence_over_preceding_anchor(self):
        """Step UsageMetadata field 1 model_enum takes precedence over preceding anchor."""
        # Anchors @10 (sonnet) and @30 (opus).
        # Step 15 has usage model_enum=18 ("gemini-3-flash" in KNOWN_MODEL_ENUMS).
        # It must resolve to "gemini-3-flash", not the preceding anchor @10 (sonnet).
        # Neighbouring step 16 has no usage enum and resolves to the preceding anchor (sonnet).
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "usage_enum.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="claude-sonnet-5-5", last_step_idx=10),
                    self._gen_metadata_row(2, model_name="claude-opus-5-5", last_step_idx=30),
                ],
                steps=[
                    (15, self._step_metadata(100, 10, model_enum=18)),
                    (16, self._step_metadata(200, 20)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [
                    (15, "gemini-3-flash"),
                    (16, "claude-sonnet-5-5"),
                ],
            )
            self.assertEqual(
                summary.models,
                ["claude-sonnet-5-5", "gemini-3-flash", "claude-opus-5-5"],
            )
            self.assertEqual(summary.usage.total_tokens, 330)

    def test_differential_random_oracle_comparison(self):
        """Randomized differential test comparing parser output against reference oracle."""
        rng = random.Random(1234)
        pool_models = ["claude-sonnet-5-5", "claude-opus-5-5", "gemini-3.7-flash"]
        with tempfile.TemporaryDirectory() as tmp:
            for db_i in range(6):
                num_anchors = 0 if db_i == 0 else rng.randint(1, 50)
                num_steps = rng.randint(1, 200)

                gen_spec = []
                gen_blobs = []
                for i in range(num_anchors):
                    m_name = rng.choice(pool_models)
                    last_step = rng.randint(0, 150)
                    gen_spec.append((i + 1, m_name, last_step))
                    gen_blobs.append(
                        self._gen_metadata_row(i + 1, model_name=m_name, last_step_idx=last_step)
                    )

                step_indices = sorted(rng.sample(range(1, 350), num_steps))
                steps = [
                    (s_idx, self._step_metadata(100, 20))
                    for s_idx in step_indices
                ]

                db_path = os.path.join(tmp, f"diff_{db_i}.db")
                self._write_db(db_path, gen_blobs, steps)

                expected = self._oracle_attribution(gen_spec, step_indices)
                summary = AntigravityParser.parse_session_db(db_path)
                self.assertIsNotNone(summary)
                actual = {s.step_idx: s.model for s in summary.steps}
                self.assertEqual(
                    actual,
                    expected,
                    f"Mismatch in randomized DB {db_i} (anchors={num_anchors}, steps={num_steps})",
                )

    def test_perf_anchor_lookup_scaling_guard(self):
        """PERF-ONLY: guards OPT-006 by testing anchor lookup scaling under worst-case scan.

        Compares parse_session_db timing across two databases of identical size (N=6000):
        - DB 'scan': all 6000 steps precede every anchor, triggering worst-case linear
          reverse scan O(N*M) across all 12000 anchor entries on unoptimised code.
        - DB 'direct': steps are placed exactly at anchor positions, hitting step_to_model
          directly in O(1) time without ever scanning anchors.

        On unoptimised code, t_scan takes ~20x longer than t_direct and fails
        the assertion `t_scan < 4 * t_direct + 0.05`. Once OPT-006 replaces the linear
        reverse scan with `bisect.bisect_right`, t_scan scales logarithmically O(N log M)
        and passes easily.
        """
        n_rows = 6000
        models = ["claude-opus-5-5", "claude-sonnet-5-5"]
        gen_rows = [
            self._gen_metadata_row(i + 1, models[i % 2], 100000 + 3 * i)
            for i in range(n_rows)
        ]
        steps_scan = [(i + 1, self._step_metadata(100, 50)) for i in range(n_rows)]
        steps_direct = [(100000 + 3 * i, self._step_metadata(100, 50)) for i in range(n_rows)]

        with tempfile.TemporaryDirectory() as tmp:
            p_scan = self._write_db(os.path.join(tmp, "perf_scan.db"), gen_rows, steps_scan)
            p_direct = self._write_db(os.path.join(tmp, "perf_direct.db"), gen_rows, steps_direct)

            # Warm-up / correctness check for scan DB
            scan_summary = AntigravityParser.parse_session_db(p_scan)
            self.assertIsNotNone(scan_summary)
            self.assertEqual(len(scan_summary.steps), n_rows)
            # All steps are before any anchor, so they must receive the session default
            # (the alphabetically first known model, 'claude-opus-5-5')
            self.assertEqual(scan_summary.models, ["claude-opus-5-5", "claude-sonnet-5-5"])
            self.assertEqual(scan_summary.usage.total_tokens, n_rows * 150)
            self.assertTrue(all(s.model == "claude-opus-5-5" for s in scan_summary.steps))

            # Correctness check for direct DB
            direct_summary = AntigravityParser.parse_session_db(p_direct)
            self.assertIsNotNone(direct_summary)
            self.assertEqual(len(direct_summary.steps), n_rows)
            self.assertEqual(direct_summary.usage.total_tokens, n_rows * 150)

            # Benchmark best of 3 runs
            t_scans = []
            for _ in range(3):
                t0 = time.perf_counter()
                AntigravityParser.parse_session_db(p_scan)
                t_scans.append(time.perf_counter() - t0)

            t_directs = []
            for _ in range(3):
                t0 = time.perf_counter()
                AntigravityParser.parse_session_db(p_direct)
                t_directs.append(time.perf_counter() - t0)

            t_scan = min(t_scans)
            t_direct = min(t_directs)

            self.assertLess(
                t_scan,
                4 * t_direct + 0.05,
                f"Scaling guard failed (OPT-006 bisect not yet applied): "
                f"t_scan={t_scan:.4f}s >= 4*t_direct + 0.05 ({4 * t_direct + 0.05:.4f}s; "
                f"t_direct={t_direct:.4f}s, ratio={t_scan / t_direct:.1f}x)",
            )


if __name__ == "__main__":
    unittest.main()
