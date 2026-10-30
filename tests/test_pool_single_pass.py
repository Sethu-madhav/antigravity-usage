"""Tests that compute_all_pools classifies each step once and keeps pool results unchanged (OPT-005)."""

from __future__ import annotations

import collections
import unittest
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import patch

from agy_usage.limits import LimitsCalculator, classify_model_pool
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


def _step(idx: int, ts: datetime, model: str, prompt_tokens: int) -> StepRecord:
    """Create a StepRecord with specified prompt tokens."""
    return StepRecord(
        step_idx=idx,
        timestamp=ts,
        model=model,
        usage=TokenUsage(prompt_tokens=prompt_tokens),
    )


def _session(
    sid: str,
    steps: Optional[List[StepRecord]] = None,
    models: Optional[List[str]] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    prompt_tokens: int = 0,
) -> SessionSummary:
    """Create a SessionSummary with sensible test defaults."""
    step_list = steps or []
    if models is None:
        if step_list:
            seen = set()
            m_list = []
            for st in step_list:
                if st.model not in seen:
                    seen.add(st.model)
                    m_list.append(st.model)
            models = m_list
        else:
            models = []

    st_time = start_time
    if st_time is None and step_list:
        st_time = min(s.timestamp for s in step_list)
    e_time = end_time
    if e_time is None and step_list:
        e_time = max(s.timestamp for s in step_list)

    if step_list:
        total_p = sum(s.usage.prompt_tokens for s in step_list)
        usage = TokenUsage(prompt_tokens=total_p)
    else:
        usage = TokenUsage(prompt_tokens=prompt_tokens)

    return SessionSummary(
        session_id=sid,
        source="cli",
        file_path=f"/tmp/{sid}.db",
        title=sid,
        workspace="ws",
        models=models,
        start_time=st_time,
        end_time=e_time,
        step_count=len(step_list),
        usage=usage,
        steps=step_list,
    )


class TestPoolSinglePass(unittest.TestCase):
    """Test suite verifying compute_all_pools equivalence and single-pass classification."""

    def test_mixed_pools_equivalence(self):
        """Test 1: Mixed pools across sessions yield identical hand-computed window stats."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        s1 = _session(
            "s1",
            steps=[
                _step(1, datetime(2026, 10, 18, 12, 0), "gemini-3.8-flash", 1_000_000),
                _step(2, datetime(2026, 10, 18, 12, 30), "gemini-4-argon", 2_000_000),
                _step(3, datetime(2026, 10, 18, 13, 0), "claude-sonnet-5-5", 3_000_000),
                _step(4, datetime(2026, 10, 18, 13, 30), "gemini-3.8-flash", 4_000_000),
            ],
        )
        s2 = _session(
            "s2",
            steps=[
                _step(5, datetime(2026, 10, 18, 14, 0), "gemini-4-argon", 5_000_000),
                _step(6, datetime(2026, 10, 18, 14, 15), "claude-opus-5-5", 6_000_000),
                _step(7, datetime(2026, 10, 18, 14, 30), "gemini-3.8-flash", 7_000_000),
                _step(8, datetime(2026, 10, 18, 14, 45), "gemini-4-argon", 8_000_000),
                _step(9, datetime(2026, 10, 18, 14, 50), "claude-sonnet-5-5", 9_000_000),
            ],
        )

        pools = LimitsCalculator.compute_all_pools(
            [s1, s2],
            tier_name="pro",
            now=ref_time,
            use_live_server=False,
        )

        # Gemini: steps 1 (1M), 4 (4M), 7 (7M) -> 12M tokens, 3 steps, 2 sessions
        self.assertEqual(pools.gemini_5h.usage.total_tokens, 12_000_000)
        self.assertEqual(pools.gemini_5h.step_count, 3)
        self.assertEqual(pools.gemini_5h.session_count, 2)
        self.assertEqual(pools.gemini_5h.earliest_activity, datetime(2026, 10, 18, 12, 0))
        self.assertEqual(pools.gemini_5h.latest_activity, datetime(2026, 10, 18, 14, 30))

        self.assertEqual(pools.gemini_7d.usage.total_tokens, 12_000_000)
        self.assertEqual(pools.gemini_7d.step_count, 3)
        self.assertEqual(pools.gemini_7d.session_count, 2)
        self.assertEqual(pools.gemini_7d.earliest_activity, datetime(2026, 10, 18, 12, 0))
        self.assertEqual(pools.gemini_7d.latest_activity, datetime(2026, 10, 18, 14, 30))

        # Argon: steps 2 (2M), 5 (5M), 8 (8M) -> 15M tokens, 3 steps, 2 sessions
        self.assertEqual(pools.argon_7d.usage.total_tokens, 15_000_000)
        self.assertEqual(pools.argon_7d.step_count, 3)
        self.assertEqual(pools.argon_7d.session_count, 2)
        self.assertEqual(pools.argon_7d.earliest_activity, datetime(2026, 10, 18, 12, 30))
        self.assertEqual(pools.argon_7d.latest_activity, datetime(2026, 10, 18, 14, 45))

        # Claude: steps 3 (3M), 6 (6M), 9 (9M) -> 18M tokens, 3 steps, 2 sessions
        self.assertEqual(pools.claude_5h.usage.total_tokens, 18_000_000)
        self.assertEqual(pools.claude_5h.step_count, 3)
        self.assertEqual(pools.claude_5h.session_count, 2)
        self.assertEqual(pools.claude_5h.earliest_activity, datetime(2026, 10, 18, 13, 0))
        self.assertEqual(pools.claude_5h.latest_activity, datetime(2026, 10, 18, 14, 50))

        self.assertEqual(pools.claude_7d.usage.total_tokens, 18_000_000)
        self.assertEqual(pools.claude_7d.step_count, 3)
        self.assertEqual(pools.claude_7d.session_count, 2)
        self.assertEqual(pools.claude_7d.earliest_activity, datetime(2026, 10, 18, 13, 0))
        self.assertEqual(pools.claude_7d.latest_activity, datetime(2026, 10, 18, 14, 50))

    def test_mixed_pools_golden_window_fields(self):
        """Window bounds, cost, limits and percentages captured from the pre-optimization code."""
        s1 = _session(
            "s1",
            steps=[
                _step(1, datetime(2026, 10, 18, 12, 0), "gemini-3.8-flash", 1_000_000),
                _step(2, datetime(2026, 10, 18, 12, 30), "gemini-4-argon", 2_000_000),
                _step(3, datetime(2026, 10, 18, 13, 0), "claude-sonnet-5-5", 3_000_000),
                _step(4, datetime(2026, 10, 18, 13, 30), "gemini-3.8-flash", 4_000_000),
            ],
        )
        s2 = _session(
            "s2",
            steps=[
                _step(5, datetime(2026, 10, 18, 14, 0), "gemini-4-argon", 5_000_000),
                _step(6, datetime(2026, 10, 18, 14, 15), "claude-opus-5-5", 6_000_000),
                _step(7, datetime(2026, 10, 18, 14, 30), "gemini-3.8-flash", 7_000_000),
                _step(8, datetime(2026, 10, 18, 14, 45), "gemini-4-argon", 8_000_000),
                _step(9, datetime(2026, 10, 18, 14, 50), "claude-sonnet-5-5", 9_000_000),
            ],
        )
        pools = LimitsCalculator.compute_all_pools(
            [s1, s2], now=datetime(2026, 10, 18, 15, 0), use_live_server=False
        )

        # (window, cost, start, end, limit_tokens, pct_remaining)
        golden = [
            (pools.gemini_5h, 9.0, datetime(2026, 10, 18, 12, 0), datetime(2026, 10, 18, 17, 0), 100_000_000, 88.0),
            (pools.gemini_7d, 9.0, datetime(2026, 10, 18, 12, 0), datetime(2026, 10, 25, 12, 0), 750_000_000, 98.4),
            (pools.argon_7d, 30.0, datetime(2026, 10, 18, 12, 30), datetime(2026, 10, 25, 12, 30), 15_000_000, 0.0),
            (pools.claude_5h, 48.0, datetime(2026, 10, 18, 13, 0), datetime(2026, 10, 18, 18, 0), 3_450_000, 0.0),
            (pools.claude_7d, 48.0, datetime(2026, 10, 18, 13, 0), datetime(2026, 10, 25, 13, 0), 6_910_000, 0.0),
        ]
        for w, cost, start, end, limit_tokens, pct_remaining in golden:
            with self.subTest(window=w.window_name):
                self.assertAlmostEqual(w.estimated_cost, cost)
                self.assertEqual(w.start_time, start)
                self.assertEqual(w.end_time, end)
                self.assertEqual(w.refresh_time, end)
                self.assertEqual(w.limit_tokens, limit_tokens)
                self.assertAlmostEqual(w.pct_remaining, pct_remaining)

    def test_ordering_and_cycle_anchoring(self):
        """Test 2: Chronological sorting and cycle anchoring with expired and active steps."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        # Supplied out of order across and within sessions:
        # Step at 09:00 is 6h before ref_time (>5h), so the [09:00, 14:00) cycle expired.
        # Step at 14:00 anchors the active cycle [14:00, 19:00).
        # Step at 12:00 was in the first cycle [09:00, 14:00).
        # The 7d cycle [09:00, 09:00 + 7d) includes all three steps.
        s1 = _session(
            "s1",
            steps=[
                _step(2, datetime(2026, 10, 18, 14, 0), "gemini-3.8-flash", 3_000_000),
                _step(1, datetime(2026, 10, 18, 9, 0), "gemini-3.8-flash", 1_000_000),
            ],
        )
        s2 = _session(
            "s2",
            steps=[
                _step(3, datetime(2026, 10, 18, 12, 0), "gemini-3.8-flash", 2_000_000),
            ],
        )

        pools = LimitsCalculator.compute_all_pools(
            [s1, s2],
            tier_name="pro",
            now=ref_time,
            use_live_server=False,
        )

        # 5h window: anchored at 14:00, ends at 19:00 (start + 5h)
        self.assertEqual(pools.gemini_5h.start_time, datetime(2026, 10, 18, 14, 0))
        self.assertEqual(pools.gemini_5h.end_time, datetime(2026, 10, 18, 19, 0))
        self.assertEqual(pools.gemini_5h.end_time, pools.gemini_5h.start_time + timedelta(hours=5))
        self.assertEqual(pools.gemini_5h.usage.total_tokens, 3_000_000)
        self.assertEqual(pools.gemini_5h.step_count, 1)
        self.assertEqual(pools.gemini_5h.session_count, 1)
        self.assertEqual(pools.gemini_5h.earliest_activity, datetime(2026, 10, 18, 14, 0))
        self.assertEqual(pools.gemini_5h.latest_activity, datetime(2026, 10, 18, 14, 0))

        # 7d window: anchored at 09:00, ends at 09:00 + 7d, includes all 3 steps (6M tokens)
        self.assertEqual(pools.gemini_7d.start_time, datetime(2026, 10, 18, 9, 0))
        self.assertEqual(pools.gemini_7d.end_time, datetime(2026, 10, 25, 9, 0))
        self.assertEqual(pools.gemini_7d.end_time, pools.gemini_7d.start_time + timedelta(days=7))
        self.assertEqual(pools.gemini_7d.usage.total_tokens, 6_000_000)
        self.assertEqual(pools.gemini_7d.step_count, 3)
        self.assertEqual(pools.gemini_7d.session_count, 2)
        self.assertEqual(pools.gemini_7d.earliest_activity, datetime(2026, 10, 18, 9, 0))
        self.assertEqual(pools.gemini_7d.latest_activity, datetime(2026, 10, 18, 14, 0))

    def test_single_pool_isolation(self):
        """Test 3: Single-pool sessions populate only their pool and leave other pools zeroed."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        cases = [
            ("gemini", "gemini-3.8-flash"),
            ("argon", "gemini-4-argon"),
            ("claude", "claude-sonnet-5-5"),
        ]
        for pool_name, model_name in cases:
            with self.subTest(pool=pool_name):
                s = _session(
                    "s_single",
                    steps=[
                        _step(1, datetime(2026, 10, 18, 14, 0), model_name, 5_000_000),
                    ],
                )
                pools = LimitsCalculator.compute_all_pools(
                    [s],
                    tier_name="pro",
                    now=ref_time,
                    use_live_server=False,
                )

                if pool_name == "gemini":
                    self.assertEqual(pools.gemini_5h.usage.total_tokens, 5_000_000)
                    self.assertEqual(pools.gemini_7d.usage.total_tokens, 5_000_000)
                    for w in (pools.argon_7d, pools.claude_5h, pools.claude_7d):
                        self.assertEqual(w.usage.total_tokens, 0)
                        self.assertEqual(w.step_count, 0)
                        self.assertEqual(w.session_count, 0)
                        self.assertIsNone(w.earliest_activity)
                        self.assertIsNone(w.latest_activity)
                elif pool_name == "argon":
                    self.assertEqual(pools.argon_7d.usage.total_tokens, 5_000_000)
                    for w in (pools.gemini_5h, pools.gemini_7d, pools.claude_5h, pools.claude_7d):
                        self.assertEqual(w.usage.total_tokens, 0)
                        self.assertEqual(w.step_count, 0)
                        self.assertEqual(w.session_count, 0)
                        self.assertIsNone(w.earliest_activity)
                        self.assertIsNone(w.latest_activity)
                else:  # claude
                    self.assertEqual(pools.claude_5h.usage.total_tokens, 5_000_000)
                    self.assertEqual(pools.claude_7d.usage.total_tokens, 5_000_000)
                    for w in (pools.gemini_5h, pools.gemini_7d, pools.argon_7d):
                        self.assertEqual(w.usage.total_tokens, 0)
                        self.assertEqual(w.step_count, 0)
                        self.assertEqual(w.session_count, 0)
                        self.assertIsNone(w.earliest_activity)
                        self.assertIsNone(w.latest_activity)

    def test_empty_input_equivalence(self):
        """Test 4: Empty input yields zero tokens, zero steps, zero sessions, and None activity."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        pools = LimitsCalculator.compute_all_pools([], tier_name="pro", now=ref_time, use_live_server=False)
        for w in (pools.gemini_5h, pools.gemini_7d, pools.argon_7d, pools.claude_5h, pools.claude_7d):
            self.assertEqual(w.usage.total_tokens, 0)
            self.assertEqual(w.step_count, 0)
            self.assertEqual(w.session_count, 0)
            self.assertIsNone(w.earliest_activity)
            self.assertIsNone(w.latest_activity)

    def test_unclassifiable_and_mixed_case_models(self):
        """Test 5: Unclassifiable names default to gemini, while mixed-case claude names route to claude."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        s = _session(
            "s_unclass",
            steps=[
                _step(1, datetime(2026, 10, 18, 14, 0), "", 1_000_000),
                _step(2, datetime(2026, 10, 18, 14, 10), "unknown", 2_000_000),
                _step(3, datetime(2026, 10, 18, 14, 20), "mystery-model-x", 3_000_000),
                _step(4, datetime(2026, 10, 18, 14, 30), "CLAUDE-Opus", 4_000_000),
            ],
        )
        pools = LimitsCalculator.compute_all_pools([s], tier_name="pro", now=ref_time, use_live_server=False)

        # "", "unknown", "mystery-model-x" fall through to gemini
        self.assertEqual(pools.gemini_5h.step_count, 3)
        self.assertEqual(pools.gemini_5h.usage.total_tokens, 6_000_000)
        self.assertEqual(pools.gemini_7d.step_count, 3)
        self.assertEqual(pools.gemini_7d.usage.total_tokens, 6_000_000)

        # "CLAUDE-Opus" matches claude
        self.assertEqual(pools.claude_5h.step_count, 1)
        self.assertEqual(pools.claude_5h.usage.total_tokens, 4_000_000)
        self.assertEqual(pools.claude_7d.step_count, 1)
        self.assertEqual(pools.claude_7d.usage.total_tokens, 4_000_000)

        # Argon has zero
        self.assertEqual(pools.argon_7d.step_count, 0)
        self.assertEqual(pools.argon_7d.usage.total_tokens, 0)

    def test_no_step_sessions_handling(self):
        """Test 6: Sessions without steps use primary_model and start_time/end_time, dropping untimed ones."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        t = datetime(2026, 10, 18, 14, 0)

        # 1. No steps with claude primary model -> claude
        s_claude = _session("s_claude", steps=[], models=["claude-sonnet-5-5"], start_time=t, prompt_tokens=1_000_000)

        # 2. No steps with empty models -> primary_model defaults to "unknown" -> gemini
        s_empty_models = _session("s_empty", steps=[], models=[], start_time=t, prompt_tokens=2_000_000)

        # 3. No steps with only end_time set -> timestamp uses end_time -> gemini
        s_end_time_only = _session(
            "s_end_only",
            steps=[],
            models=["gemini-3.8-flash"],
            start_time=None,
            end_time=t,
            prompt_tokens=3_000_000,
        )

        # 4. No steps with neither start_time nor end_time -> dropped completely
        s_no_times = _session(
            "s_no_times",
            steps=[],
            models=["gemini-3.8-flash"],
            start_time=None,
            end_time=None,
            prompt_tokens=9_000_000,
        )

        pools = LimitsCalculator.compute_all_pools(
            [s_claude, s_empty_models, s_end_time_only, s_no_times],
            tier_name="pro",
            now=ref_time,
            use_live_server=False,
        )

        # Gemini: s_empty_models (2M) + s_end_time_only (3M) = 5M tokens, 2 steps, 2 sessions
        self.assertEqual(pools.gemini_5h.step_count, 2)
        self.assertEqual(pools.gemini_5h.session_count, 2)
        self.assertEqual(pools.gemini_5h.usage.total_tokens, 5_000_000)
        self.assertEqual(pools.gemini_7d.usage.total_tokens, 5_000_000)

        # Claude: s_claude (1M) -> 1 step, 1 session
        self.assertEqual(pools.claude_5h.step_count, 1)
        self.assertEqual(pools.claude_5h.session_count, 1)
        self.assertEqual(pools.claude_5h.usage.total_tokens, 1_000_000)
        self.assertEqual(pools.claude_7d.usage.total_tokens, 1_000_000)

        # Argon: 0
        self.assertEqual(pools.argon_7d.step_count, 0)
        self.assertEqual(pools.argon_7d.usage.total_tokens, 0)

    def test_timezone_aware_timestamps(self):
        """Test 7: Timezone-aware step timestamps are stripped to naive wall-clock datetime."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        ts_aware = datetime(2026, 10, 18, 13, 30, tzinfo=timezone.utc)
        s = _session(
            "s_tz",
            steps=[
                _step(1, ts_aware, "gemini-3.8-flash", 1_500_000),
            ],
        )

        pools = LimitsCalculator.compute_all_pools([s], tier_name="pro", now=ref_time, use_live_server=False)
        self.assertEqual(pools.gemini_5h.usage.total_tokens, 1_500_000)
        self.assertEqual(pools.gemini_5h.step_count, 1)
        self.assertIsNone(pools.gemini_5h.earliest_activity.tzinfo)
        self.assertEqual(pools.gemini_5h.earliest_activity, datetime(2026, 10, 18, 13, 30))
        self.assertEqual(pools.gemini_5h.latest_activity, datetime(2026, 10, 18, 13, 30))

    def test_collect_pool_items_direct_and_compute(self):
        """Test 8: Direct check that LimitsCalculator._collect_pool_items and compute function as expected."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        s1 = _session(
            "s1",
            steps=[
                _step(1, datetime(2026, 10, 18, 14, 0), "claude-sonnet-5-5", 1_000_000),
                _step(2, datetime(2026, 10, 18, 12, 0), "gemini-3.8-flash", 2_000_000),
            ],
        )
        s2 = _session(
            "s2",
            steps=[
                _step(3, datetime(2026, 10, 18, 13, 0), "claude-opus-5-5", 3_000_000),
                _step(4, datetime(2026, 10, 18, 11, 0), "gemini-4-argon", 4_000_000),
            ],
        )
        sessions = [s1, s2]

        # 1. Filtered by pool="claude": step 3 (13:00) then step 1 (14:00)
        claude_items = LimitsCalculator._collect_pool_items(sessions, pool="claude")
        self.assertEqual(len(claude_items), 2)
        # item: (ts, usage, cost_total, session_id)
        self.assertEqual(claude_items[0][0], datetime(2026, 10, 18, 13, 0))
        self.assertEqual(claude_items[0][1].total_tokens, 3_000_000)
        self.assertIsInstance(claude_items[0][2], float)
        self.assertGreater(claude_items[0][2], 0.0)
        self.assertEqual(claude_items[0][3], "s2")

        self.assertEqual(claude_items[1][0], datetime(2026, 10, 18, 14, 0))
        self.assertEqual(claude_items[1][1].total_tokens, 1_000_000)
        self.assertIsInstance(claude_items[1][2], float)
        self.assertGreater(claude_items[1][2], 0.0)
        self.assertEqual(claude_items[1][3], "s1")

        # 2. pool="all" and pool=None: all 4 items sorted chronologically
        for p in ("all", None):
            with self.subTest(pool=p):
                all_items = LimitsCalculator._collect_pool_items(sessions, pool=p)
                self.assertEqual(len(all_items), 4)
                timestamps = [item[0] for item in all_items]
                self.assertEqual(
                    timestamps,
                    [
                        datetime(2026, 10, 18, 11, 0),
                        datetime(2026, 10, 18, 12, 0),
                        datetime(2026, 10, 18, 13, 0),
                        datetime(2026, 10, 18, 14, 0),
                    ],
                )

        # 3. LimitsCalculator.compute for claude returns expected totals
        w_5h, w_7d, tier = LimitsCalculator.compute(sessions, pool="claude", now=ref_time)
        self.assertEqual(w_5h.pool, "claude")
        self.assertEqual(w_5h.usage.total_tokens, 4_000_000)
        self.assertEqual(w_7d.usage.total_tokens, 4_000_000)
        self.assertEqual(tier.name, "pro")

    def test_call_count_all_step_sessions(self):
        """Test 9: Call-count test for N steps across sessions (fails on current 3-pass code)."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        step_models_s1 = ["gemini-3.8-flash", "gemini-4-argon", "claude-sonnet-5-5", "gemini-3.8-flash"]
        step_models_s2 = ["claude-opus-5-5", "gemini-4-argon", "gemini-3.8-flash", "claude-sonnet-5-5"]
        step_models_s3 = ["gemini-4-argon", "gemini-3.8-flash", "claude-sonnet-5-5", "gemini-4-argon"]

        s1 = _session(
            "s1",
            steps=[_step(i, datetime(2026, 10, 18, 12, i), m, 100_000) for i, m in enumerate(step_models_s1, 1)],
        )
        s2 = _session(
            "s2",
            steps=[_step(i + 4, datetime(2026, 10, 18, 13, i), m, 100_000) for i, m in enumerate(step_models_s2, 1)],
        )
        s3 = _session(
            "s3",
            steps=[_step(i + 8, datetime(2026, 10, 18, 14, i), m, 100_000) for i, m in enumerate(step_models_s3, 1)],
        )

        all_models = step_models_s1 + step_models_s2 + step_models_s3
        self.assertEqual(len(all_models), 12)

        with patch("agy_usage.limits.classify_model_pool", wraps=classify_model_pool) as mock_classify:
            LimitsCalculator.compute_all_pools([s1, s2, s3], tier_name="pro", now=ref_time, use_live_server=False)

            self.assertEqual(
                mock_classify.call_count,
                12,
                f"Expected each of the 12 steps to be classified exactly once in a single pass, but got {mock_classify.call_count} calls.",
            )
            actual_models = [c.args[0] for c in mock_classify.call_args_list]
            self.assertEqual(
                collections.Counter(actual_models),
                collections.Counter(all_models),
                "Multiset of classified model names must match the input step models exactly.",
            )

    def test_call_count_mixed_with_no_step_sessions(self):
        """Test 10: Call count and equivalence for mixed input with no-step sessions."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        t = datetime(2026, 10, 18, 14, 0)

        # Session 1: 3 steps
        s1 = _session(
            "s1",
            steps=[
                _step(1, datetime(2026, 10, 18, 12, 0), "gemini-3.8-flash", 1_000_000),
                _step(2, datetime(2026, 10, 18, 12, 30), "gemini-4-argon", 2_000_000),
                _step(3, datetime(2026, 10, 18, 13, 0), "claude-sonnet-5-5", 3_000_000),
            ],
        )
        # Session 2: 2 steps
        s2 = _session(
            "s2",
            steps=[
                _step(4, datetime(2026, 10, 18, 13, 30), "gemini-3.8-flash", 4_000_000),
                _step(5, datetime(2026, 10, 18, 14, 0), "claude-sonnet-5-5", 5_000_000),
            ],
        )
        # Session 3: 0 steps, has start_time, primary model gemini
        s3 = _session(
            "s3",
            steps=[],
            models=["gemini-3.8-flash"],
            start_time=t,
            prompt_tokens=6_000_000,
        )
        # Session 4: 0 steps, has start_time, primary model argon
        s4 = _session(
            "s4",
            steps=[],
            models=["gemini-4-argon"],
            start_time=t,
            prompt_tokens=7_000_000,
        )

        sessions = [s1, s2, s3, s4]
        # 5 steps in sessions with steps + 2 sessions without steps = 7 expected calls
        expected_models = [
            "gemini-3.8-flash",
            "gemini-4-argon",
            "claude-sonnet-5-5",
            "gemini-3.8-flash",
            "claude-sonnet-5-5",
            "gemini-3.8-flash",
            "gemini-4-argon",
        ]

        with patch("agy_usage.limits.classify_model_pool", wraps=classify_model_pool) as mock_classify:
            pools = LimitsCalculator.compute_all_pools(
                sessions,
                tier_name="pro",
                now=ref_time,
                use_live_server=False,
            )

            # Equivalence checks first (pass on current code)
            # Gemini: step 1 (1M) + step 4 (4M) + s3 (6M) = 11M
            self.assertEqual(pools.gemini_5h.usage.total_tokens, 11_000_000)
            self.assertEqual(pools.gemini_7d.usage.total_tokens, 11_000_000)
            # Argon: step 2 (2M) + s4 (7M) = 9M
            self.assertEqual(pools.argon_7d.usage.total_tokens, 9_000_000)
            # Claude: step 3 (3M) + step 5 (5M) = 8M
            self.assertEqual(pools.claude_5h.usage.total_tokens, 8_000_000)
            self.assertEqual(pools.claude_7d.usage.total_tokens, 8_000_000)

            # Call count check (fails on current 3-pass code, passes after single-pass optimization)
            self.assertEqual(
                mock_classify.call_count,
                7,
                f"Expected 5 steps + 2 no-step sessions = 7 classify calls, but got {mock_classify.call_count}.",
            )
            actual_models = [c.args[0] for c in mock_classify.call_args_list]
            self.assertEqual(
                collections.Counter(actual_models),
                collections.Counter(expected_models),
                "Multiset of classified model names must match steps + no-step session primary models.",
            )

    def test_call_count_empty_input(self):
        """Test 11: Empty list yields 0 classify calls."""
        ref_time = datetime(2026, 10, 18, 15, 0)
        with patch("agy_usage.limits.classify_model_pool", wraps=classify_model_pool) as mock_classify:
            LimitsCalculator.compute_all_pools([], tier_name="pro", now=ref_time, use_live_server=False)
            self.assertEqual(
                mock_classify.call_count,
                0,
                f"Expected 0 classify calls for empty session list, but got {mock_classify.call_count}.",
            )


if __name__ == "__main__":
    unittest.main()
