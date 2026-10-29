"""Tests for step timestamp fallbacks."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from agy_usage.models import TokenUsage
from agy_usage.parser import AntigravityParser


class TestTimestampFallbacks(unittest.TestCase):
    """Regression tests for BUG-018: timestamp fallbacks and future skew in quota windows."""

    def test_compute_cycle_window_distant_future_dropped(self):
        """1. _compute_cycle_window: items at now-1h plus one at now+3 days:
        5h window still counts the now-1h usage, and cycle_start is not in the future."""
        from agy_usage.limits import LimitsCalculator

        now = datetime(2026, 10, 29, 12, 0, 0)
        item_past = (now - timedelta(hours=1), TokenUsage(prompt_tokens=50_000, output_tokens=10_000), 0.05, "s-past")
        item_future = (now + timedelta(days=3), TokenUsage(prompt_tokens=100_000, output_tokens=20_000), 0.10, "s-future")

        w = LimitsCalculator._compute_cycle_window(
            [item_past, item_future],
            current_time=now,
            duration=timedelta(hours=5),
            limit_tokens=100_000_000,
            pool="gemini",
            window_label="gemini-5h",
        )

        # Distant future item (now+3d) is dropped entirely
        self.assertEqual(w.usage.total_tokens, 60_000)
        self.assertEqual(w.step_count, 1)
        self.assertEqual(w.session_count, 1)
        # cycle_start is anchored by past item (now-1h), NOT pushed into the future
        self.assertEqual(w.start_time, now - timedelta(hours=1))
        self.assertLessEqual(w.start_time, now)
        self.assertEqual(w.end_time, now - timedelta(hours=1) + timedelta(hours=5))
        self.assertGreater(w.end_time, now)

    def test_compute_cycle_window_clock_skew_tolerance(self):
        """2. _compute_cycle_window: item at now+2min counts (clamped to now) and
        item at now+10min is ignored (> 5min future skew)."""
        from agy_usage.limits import LimitsCalculator

        now = datetime(2026, 10, 29, 12, 0, 0)
        item_near = (now + timedelta(minutes=2), TokenUsage(prompt_tokens=20_000, output_tokens=5_000), 0.02, "s-near")
        item_far = (now + timedelta(minutes=10), TokenUsage(prompt_tokens=30_000, output_tokens=5_000), 0.03, "s-far")

        w = LimitsCalculator._compute_cycle_window(
            [item_near, item_far],
            current_time=now,
            duration=timedelta(hours=5),
            limit_tokens=100_000_000,
            pool="gemini",
            window_label="gemini-5h",
        )

        # now+2min item is clamped to now and counted; now+10min is ignored
        self.assertEqual(w.usage.total_tokens, 25_000)
        self.assertEqual(w.step_count, 1)
        self.assertEqual(w.latest_activity, now)

    def test_fill_missing_step_timestamps_neighbour_inference(self):
        """Unit tests for _fill_missing_step_timestamps helper: preceding, following,
        all-missing with fallback, all-missing without fallback."""
        from agy_usage.parser import _fill_missing_step_timestamps

        t1 = datetime(2026, 10, 15, 10, 0, 0)
        t2 = datetime(2026, 10, 15, 11, 0, 0)
        t_fallback = datetime(2026, 10, 15, 8, 0, 0)

        # Preceding neighbour used when available
        self.assertEqual(
            _fill_missing_step_timestamps([t1, None, None, t2, None]),
            [t1, t1, t1, t2, t2],
        )

        # Following neighbour used when no preceding neighbour exists
        self.assertEqual(
            _fill_missing_step_timestamps([None, None, t1]),
            [t1, t1, t1],
        )

        # Mixed: leading steps use following neighbour, trailing steps use preceding neighbour
        self.assertEqual(
            _fill_missing_step_timestamps([None, t1, None]),
            [t1, t1, t1],
        )

        # All missing with fallback
        self.assertEqual(
            _fill_missing_step_timestamps([None, None, None], fallback=t_fallback),
            [t_fallback, t_fallback, t_fallback],
        )

        # All missing without fallback (returns None so caller drops them)
        self.assertEqual(
            _fill_missing_step_timestamps([None, None], fallback=None),
            [None, None],
        )

        # Empty list
        self.assertEqual(_fill_missing_step_timestamps([]), [])

    def test_parser_step_neighbour_timestamp_inference_session_db(self):
        """3. Parser: a session where step A has a timestamp ~10 days ago and
        step B (later idx) has no timestamp, with DB mtime = now -> step B gets
        step A's timestamp, not mtime (so it is outside the current 5h window)."""
        from agy_usage.limits import LimitsCalculator
        from agy_usage.parser import AntigravityParser

        now = datetime(2026, 10, 29, 12, 0, 0)
        t_step_a = now - timedelta(days=10)

        def varint(value):
            out = bytearray()
            while True:
                b = value & 0x7F
                value >>= 7
                if value:
                    out.append(b | 0x80)
                else:
                    out.append(b)
                    return bytes(out)

        def len_field(num, data):
            return varint((num << 3) | 2) + varint(len(data)) + data

        def varint_field(num, value):
            return varint(num << 3) + varint(value)

        # Step A metadata: field 1 timestamp (10 days ago) + field 9 usage (1000 prompt, 200 output)
        ts_a_bytes = varint_field(1, int(t_step_a.timestamp()))
        usage_a_bytes = varint_field(2, 1000) + varint_field(3, 200)
        meta_a = len_field(1, ts_a_bytes) + len_field(9, usage_a_bytes)

        # Step B metadata: only field 9 usage (2000 prompt, 400 output), NO timestamp
        usage_b_bytes = varint_field(2, 2000) + varint_field(3, 400)
        meta_b = len_field(9, usage_b_bytes)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sess-neighbour-test.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                    "step_payload BLOB, metadata BLOB)"
                )
                conn.execute(
                    "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (?, ?, ?, ?)",
                    (1, "user", b"", meta_a),
                )
                conn.execute(
                    "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (?, ?, ?, ?)",
                    (2, "user", b"", meta_b),
                )
                conn.commit()
            finally:
                conn.close()

            # Set DB mtime to now
            os.utime(db_path, (now.timestamp(), now.timestamp()))

            summary = AntigravityParser.parse_session_db(db_path)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.step_count, 2)
            self.assertEqual(summary.usage.total_tokens, 3600)

            # Step B must inherit Step A's timestamp (~10 days ago), NOT the DB mtime (now)
            step_a, step_b = summary.steps
            self.assertEqual(step_a.timestamp, datetime.fromtimestamp(int(t_step_a.timestamp())))
            self.assertEqual(step_b.timestamp, step_a.timestamp)

            # In limits calculation at 'now', both steps are ~10 days ago, so 0 tokens in 5h window
            pools = LimitsCalculator.compute_all_pools([summary], now=now, use_live_server=False)
            self.assertEqual(pools.gemini_5h.usage.total_tokens, 0)
            self.assertEqual(pools.gemini_5h.step_count, 0)


if __name__ == "__main__":
    unittest.main()
