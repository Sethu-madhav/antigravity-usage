"""Tests for multi-pool limits and Argon."""

import unittest
from datetime import datetime, timedelta

from agy_usage.aggregator import UsageAggregator
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


class TestMultiPoolLimitsAndArgon(unittest.TestCase):
    def test_classify_model_pool_and_enum_resolution(self):
        from agy_usage.limits import classify_model_pool
        from agy_usage.parser import KNOWN_MODEL_ENUMS, extract_usage_model_enum

        self.assertEqual(classify_model_pool("gemini-4-argon-trial"), "argon")
        self.assertEqual(classify_model_pool("claude-opus-5-5-high"), "claude")
        self.assertEqual(classify_model_pool("gemini-3.8-flash"), "gemini")

        self.assertEqual(KNOWN_MODEL_ENUMS[418], "gemini-4-argon-trial")
        self.assertEqual(KNOWN_MODEL_ENUMS[1418], "gemini-4-argon-trial")
        self.assertEqual(KNOWN_MODEL_ENUMS[1402], "claude-opus-5-5-high")

        # Synthetic UsageMetadata with field 1 = 1418 (0x08, 0x8A, 0x0B)
        raw = bytes([0x08, 0x8A, 0x0B])
        self.assertEqual(extract_usage_model_enum(raw), 1418)

    def test_separate_argon_and_claude_quota_pools(self):
        from datetime import timedelta
        from agy_usage.limits import LimitsCalculator

        ref_time = datetime(2026, 10, 18, 15, 0)
        t_recent = ref_time - timedelta(hours=1)

        session = SessionSummary(
            session_id="s-pools",
            source="cli",
            file_path="/tmp/sp.db",
            title="Pools",
            workspace="ws",
            models=["gemini-4-argon-trial", "gemini-3.8-flash", "claude-opus-5-5-high"],
            start_time=t_recent,
            end_time=t_recent,
            step_count=3,
            usage=TokenUsage(prompt_tokens=30_000_000),
            steps=[
                StepRecord(step_idx=1, timestamp=t_recent, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=5_000_000)),
                StepRecord(step_idx=2, timestamp=t_recent, model="gemini-4-argon-trial", usage=TokenUsage(prompt_tokens=10_000_000)),
                StepRecord(step_idx=3, timestamp=t_recent, model="claude-opus-5-5-high", usage=TokenUsage(prompt_tokens=2_000_000)),
            ],
        )

        pools = LimitsCalculator.compute_all_pools(
            [session],
            tier_name="pro",
            argon_remaining_pct=80.0,  # 10M used = 20% -> 50M calibrated weekly limit
            now=ref_time,
        )

        # Gemini pool should ONLY contain the 5M Gemini 3.8 Flash tokens
        self.assertEqual(pools.gemini_5h.usage.total_tokens, 5_000_000)
        self.assertEqual(pools.gemini_7d.usage.total_tokens, 5_000_000)

        # Argon pool is weekly-only and contains the 10M Argon tokens
        self.assertEqual(pools.argon_7d.usage.total_tokens, 10_000_000)
        self.assertEqual(pools.argon_7d.limit_tokens, 50_000_000)
        self.assertAlmostEqual(pools.argon_7d.pct_remaining, 80.0)

        # Claude partner pool contains the 2M Claude tokens
        self.assertEqual(pools.claude_5h.usage.total_tokens, 2_000_000)
        self.assertEqual(pools.claude_7d.usage.total_tokens, 2_000_000)

    def test_fixed_cycle_5h_reset_and_cost_proportional_limits(self):
        from datetime import timedelta
        from agy_usage.limits import LimitsCalculator

        # Simulate the exact scenario from 2026-10-18 16:05:11:
        # - Gemini Flash session started at 11:04:51 and ended at 11:36:26.
        #   Because 11:04:51 + 5h = 16:04:51 <= 16:05:11, the 5h cycle has ALREADY reset!
        now_dt = datetime(2026, 10, 18, 16, 5, 11)
        s_gemini = SessionSummary(
            session_id="s-gem",
            source="cli",
            file_path="/tmp/sg.db",
            title="Gemini",
            workspace="ws",
            models=["gemini-3.8-flash-n"],
            start_time=datetime(2026, 10, 18, 11, 4, 51),
            end_time=datetime(2026, 10, 18, 11, 36, 26),
            step_count=2,
            usage=TokenUsage(prompt_tokens=196_684, cached_tokens=677_860, output_tokens=14_384),
            steps=[
                StepRecord(
                    step_idx=1,
                    timestamp=datetime(2026, 10, 18, 11, 4, 51),
                    model="gemini-3.8-flash-n",
                    usage=TokenUsage(prompt_tokens=100_000, cached_tokens=100_000, output_tokens=5_000),
                ),
                StepRecord(
                    step_idx=2,
                    timestamp=datetime(2026, 10, 18, 11, 36, 26),
                    model="gemini-3.8-flash-n",
                    usage=TokenUsage(prompt_tokens=96_684, cached_tokens=577_860, output_tokens=9_384),
                ),
            ],
        )
        # Argon session with $3.9063 cost -> ~70.65% remaining (71% rounded)
        s_argon = SessionSummary(
            session_id="s-arg",
            source="cli",
            file_path="/tmp/sa.db",
            title="Argon",
            workspace="ws",
            models=["gemini-4-argon-trial"],
            start_time=datetime(2026, 10, 17, 19, 21, 50),
            end_time=datetime(2026, 10, 18, 16, 4, 0),
            step_count=1,
            usage=TokenUsage(prompt_tokens=860_169, cached_tokens=8_111_130, output_tokens=137_484),
            steps=[
                StepRecord(
                    step_idx=1,
                    timestamp=datetime(2026, 10, 17, 19, 21, 50),
                    model="gemini-4-argon-trial",
                    usage=TokenUsage(prompt_tokens=860_169, cached_tokens=8_111_130, output_tokens=137_484),
                ),
            ],
        )
        pools = LimitsCalculator.compute_all_pools(
            [s_gemini, s_argon],
            tier_name="pro",
            now=now_dt,
            use_live_server=False,
        )
        # Gemini 5h cycle [11:04:51, 16:04:51) has expired -> 0 tokens in active 5h window, 100% remaining
        self.assertEqual(pools.gemini_5h.usage.total_tokens, 0)
        self.assertEqual(round(pools.gemini_5h.pct_remaining), 100)
        # Argon weekly cost ($3.9063 / $13.31) -> 71% remaining
        self.assertEqual(round(pools.argon_7d.pct_remaining), 71)

    def test_gemini_38_flash_grouping(self):
        from agy_usage.models import normalize_model_name
        from agy_usage.parser import KNOWN_MODEL_ENUMS

        for variant in (
            "gemini-3.8-flash",
            "gemini-3.8-flash-control",
            "gemini-3.8-n",
            "gemini-3.8-flash-n",
            "gemini-3.8-flash-high",
        ):
            self.assertEqual(normalize_model_name(variant), "gemini-3.8-flash")

        for enum_id in (318, 1318, 319, 1319, 320, 1320, 322, 1322):
            self.assertEqual(KNOWN_MODEL_ENUMS[enum_id], "gemini-3.8-flash")

        t = datetime(2026, 10, 18, 12, 0, 0)
        s = SessionSummary(
            session_id="s-group",
            source="cli",
            file_path="/tmp/sg.db",
            title="Group Test",
            workspace="ws",
            models=["gemini-3.8-flash", "gemini-3.8-flash-control", "gemini-3.8-n"],
            start_time=t,
            end_time=t,
            step_count=3,
            usage=TokenUsage(prompt_tokens=3000, cached_tokens=0, output_tokens=600),
            steps=[
                StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=1000, output_tokens=200)),
                StepRecord(step_idx=2, timestamp=t, model="gemini-3.8-flash-control", usage=TokenUsage(prompt_tokens=1000, output_tokens=200)),
                StepRecord(step_idx=3, timestamp=t, model="gemini-3.8-n", usage=TokenUsage(prompt_tokens=1000, output_tokens=200)),
            ],
        )
        models = UsageAggregator.aggregate_models([s])
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].model_name, "gemini-3.8-flash")
        self.assertEqual(models[0].request_count, 3)
        self.assertEqual(models[0].usage.total_tokens, 3600)


if __name__ == "__main__":
    unittest.main()
