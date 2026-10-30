"""Tests that each quota window is computed once, in a single accumulation pass."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agy_usage import limits
from agy_usage.limits import LimitsCalculator
from agy_usage.models import SessionSummary, StepRecord, TokenUsage

NOW = datetime(2026, 10, 18, 15, 0)


def _item(ts: datetime, tokens: int, cost: float = 1.0, sid: str = "s1"):
    return (ts, TokenUsage(prompt_tokens=tokens), cost, sid)


def _step(idx: int, ts: datetime, model: str, tokens: int) -> StepRecord:
    return StepRecord(
        step_idx=idx, timestamp=ts, model=model, usage=TokenUsage(prompt_tokens=tokens)
    )


def _session(sid: str, steps) -> SessionSummary:
    return SessionSummary(
        session_id=sid,
        source="cli",
        file_path=f"/tmp/{sid}.db",
        models=list(dict.fromkeys(s.model for s in steps)),
        step_count=len(steps),
        steps=steps,
    )


def _window(items, override=None, duration=timedelta(hours=5), **kwargs):
    return LimitsCalculator._compute_cycle_window(
        items,
        current_time=NOW,
        duration=duration,
        limit_tokens=1_000_000,
        pool="gemini",
        window_label="w",
        override_reset_time=override,
        **kwargs,
    )


class TestCycleWindowAccumulation(unittest.TestCase):
    """The three ways a window can end up: server override, anchored cycle, expired cycle."""

    def test_anchored_cycle_accumulates_items_from_the_anchor(self):
        items = [
            _item(NOW - timedelta(hours=2), 100, 0.5, "a"),
            _item(NOW - timedelta(hours=1), 200, 0.25, "b"),
        ]
        w = _window(items)
        self.assertEqual(w.start_time, NOW - timedelta(hours=2))
        self.assertEqual(w.end_time, NOW + timedelta(hours=3))
        self.assertEqual(w.usage.total_tokens, 300)
        self.assertEqual(w.estimated_cost, 0.75)
        self.assertEqual((w.step_count, w.session_count), (2, 2))
        self.assertEqual(w.earliest_activity, NOW - timedelta(hours=2))
        self.assertEqual(w.latest_activity, NOW - timedelta(hours=1))
        self.assertEqual(w.refresh_time, NOW + timedelta(hours=3))

    def test_expired_cycle_accumulates_nothing_and_keeps_trailing_bounds(self):
        items = [_item(NOW - timedelta(hours=9), 100), _item(NOW - timedelta(hours=8), 200)]
        w = _window(items)
        self.assertEqual(w.start_time, NOW - timedelta(hours=5))
        self.assertEqual(w.end_time, NOW)
        self.assertEqual(w.usage.total_tokens, 0)
        self.assertEqual((w.step_count, w.session_count), (0, 0))
        self.assertIsNone(w.earliest_activity)
        self.assertIsNone(w.refresh_time)

    def test_no_items_accumulates_nothing(self):
        w = _window([])
        self.assertEqual((w.start_time, w.end_time), (NOW - timedelta(hours=5), NOW))
        self.assertEqual(w.usage.total_tokens, 0)
        self.assertIsNone(w.latest_activity)

    def test_override_accumulates_even_without_items(self):
        reset = NOW + timedelta(hours=2)
        w = _window([], override=reset, server_remaining_fraction=0.4)
        self.assertEqual(w.start_time, reset - timedelta(hours=5))
        self.assertEqual(w.end_time, reset)
        self.assertEqual(w.step_count, 0)
        self.assertEqual(w.refresh_time, reset)  # a server fraction below 1.0 is activity

    def test_override_window_ignores_the_item_anchored_cycle(self):
        # Anchored on its own the 09:00 item starts a cycle that expired at 14:00 (empty window).
        # The server's reset at 18:00 puts the cycle start at 13:00 instead, so only the 14:00 item counts.
        items = [_item(NOW - timedelta(hours=6), 100), _item(NOW - timedelta(hours=1), 200)]
        self.assertEqual(_window(items).usage.total_tokens, 200)
        reset = NOW + timedelta(hours=3)
        w = _window(items, override=reset, server_remaining_fraction=0.9)
        self.assertEqual(w.start_time, NOW - timedelta(hours=2))
        self.assertEqual(w.usage.total_tokens, 200)
        self.assertEqual(w.step_count, 1)

    def test_override_in_the_past_falls_back_to_anchored_cycle(self):
        items = [_item(NOW - timedelta(hours=1), 50)]
        w = _window(items, override=NOW - timedelta(minutes=1))
        self.assertEqual(w.start_time, NOW - timedelta(hours=1))
        self.assertEqual(w.usage.total_tokens, 50)

    def test_float_sum_follows_item_order(self):
        costs = [0.1, 0.2, 0.3, 0.7, 1e-9]
        items = [_item(NOW - timedelta(minutes=30 - i), 1, c) for i, c in enumerate(costs)]
        expected = 0.0
        for c in costs:
            expected += c
        self.assertEqual(_window(items).estimated_cost, expected)


class TestCollectPoolItems(unittest.TestCase):
    def setUp(self):
        t = datetime(2026, 10, 18, 12, 0)
        self.sessions = [
            _session("s1", [_step(1, t, "gemini-flash", 1), _step(2, t, "gemini-4-argon", 2)]),
            _session("s2", [_step(3, t, "claude-sonnet-5-5", 3), _step(4, t, "gemini-flash", 4)]),
        ]

    def test_equal_timestamps_keep_session_step_order_for_all_pools(self):
        for pool in (None, "all"):
            with self.subTest(pool=pool):
                items = LimitsCalculator._collect_pool_items(self.sessions, pool=pool)
                self.assertEqual([i[1].prompt_tokens for i in items], [1, 2, 3, 4])
                self.assertEqual([i[3] for i in items], ["s1", "s1", "s2", "s2"])

    def test_single_pool_keeps_relative_order(self):
        items = LimitsCalculator._collect_pool_items(self.sessions, pool="gemini")
        self.assertEqual([i[1].prompt_tokens for i in items], [1, 4])

    def test_unknown_pool_is_empty(self):
        self.assertEqual(LimitsCalculator._collect_pool_items(self.sessions, pool="bogus"), [])

    def test_partition_matches_per_pool_collection(self):
        parts = LimitsCalculator._partition_pool_items(self.sessions)
        for pool in ("gemini", "argon", "claude"):
            self.assertEqual(parts[pool], LimitsCalculator._collect_pool_items(self.sessions, pool=pool))


class TestComputeAllPoolsComputesEachWindowOnce(unittest.TestCase):
    def test_valid_live_buckets_compute_five_windows_not_ten(self):
        """Before this PR this took 11 calls: 6 windows (argon's unused 5-hour one included) + 5 recomputes."""
        sessions = [
            _session("s1", [
                _step(1, NOW - timedelta(hours=1), "gemini-flash", 1_000),
                _step(2, NOW - timedelta(hours=1), "gemini-4-argon", 2_000),
                _step(3, NOW - timedelta(hours=1), "claude-sonnet-5-5", 3_000),
            ])
        ]

        def bucket(bid, hours):
            reset = (NOW + timedelta(hours=hours)).astimezone(timezone.utc)
            return {
                "bucketId": bid,
                "description": f"{bid} desc",
                "remainingFraction": 0.5,
                "resetTime": reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        buckets = {
            bid: bucket(bid, hours)
            for bid, hours in (
                ("gemini-5h", 2),
                ("gemini-weekly", 48),
                ("gemini-argon-wtu-weekly", 48),
                ("3p-5h", 2),
                ("3p-weekly", 48),
            )
        }
        payload = {"fetched_at": "2026-10-18T00:00:00+00:00", "buckets": buckets}
        real = LimitsCalculator._compute_cycle_window
        with mock.patch.object(limits, "fetch_live_quota_summary", return_value=payload), \
                mock.patch.object(
                    LimitsCalculator, "_compute_cycle_window", wraps=real
                ) as spy:
            result = LimitsCalculator.compute_all_pools(
                sessions, tier_name="pro", now=NOW, use_live_server=True
            )

        self.assertEqual(spy.call_count, 5)
        self.assertTrue(result.live_synced)
        for w in (result.gemini_5h, result.gemini_7d, result.argon_7d, result.claude_5h, result.claude_7d):
            self.assertEqual(w.server_remaining_fraction, 0.5)
            self.assertTrue(w.server_description.endswith(" desc"))

    def test_without_live_buckets_still_five_windows(self):
        with mock.patch.object(
            LimitsCalculator, "_compute_cycle_window", wraps=LimitsCalculator._compute_cycle_window
        ) as spy:
            LimitsCalculator.compute_all_pools([], tier_name="pro", now=NOW, use_live_server=False)
        self.assertEqual(spy.call_count, 5)


if __name__ == "__main__":
    unittest.main()
