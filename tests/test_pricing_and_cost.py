"""Tests for pricing and cost calculation."""

import unittest
from datetime import date, datetime

from agy_usage.aggregator import UsageAggregator
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


class TestPricingAndCostCalculation(unittest.TestCase):
    def test_argon_intro_vs_standard_pricing(self):
        from agy_usage.models import get_model_rates, price_usage
        usage = TokenUsage(prompt_tokens=1_000_000, cached_tokens=10_000_000, output_tokens=500_000)

        # Oct 18, 2026 (introductory pricing: $2.00 / $0.10 / $10.00)
        rates_oct = get_model_rates("gemini-4-argon-trial", on_date=date(2026, 10, 18))
        self.assertEqual(rates_oct, (2.00, 0.10, 10.00))
        cost_oct = price_usage(usage, "gemini-4-argon-trial", on_date=date(2026, 10, 18))
        self.assertAlmostEqual(cost_oct.prompt_cost, 2.00)
        self.assertAlmostEqual(cost_oct.cached_cost, 1.00)
        self.assertAlmostEqual(cost_oct.output_cost, 5.00)
        self.assertAlmostEqual(cost_oct.total, 8.00)
        # 10M cached tokens saved ($2.00 - $0.10) * 10 = $19.00
        self.assertAlmostEqual(cost_oct.cache_savings, 19.00)

        # Dec 15, 2026 (standard pricing: $4.00 / $0.20 / $20.00)
        rates_dec = get_model_rates("gemini-4-argon-trial", on_date=date(2026, 12, 15))
        self.assertEqual(rates_dec, (4.00, 0.20, 20.00))
        cost_dec = price_usage(usage, "gemini-4-argon-trial", on_date=date(2026, 12, 15))
        self.assertAlmostEqual(cost_dec.total, 16.00)

    def test_flash_lite_vs_flash_and_pro_long_context(self):
        from agy_usage.models import get_model_rates, get_rates
        # Longest-key matching prevents gemini-2.5-flash-lite from matching gemini-2.5-flash
        self.assertEqual(get_model_rates("gemini-2.5-flash-lite"), (0.10, 0.01, 0.40))
        self.assertEqual(get_model_rates("gemini-2.5-flash"), (0.30, 0.03, 2.50))

        # Gemini 2.5 Pro <=200k vs >200k context
        self.assertEqual(get_rates("gemini-2.5-pro", prompt_context=150_000).as_tuple(), (1.25, 0.125, 10.00))
        self.assertEqual(get_rates("gemini-2.5-pro", prompt_context=250_000).as_tuple(), (2.50, 0.25, 15.00))

        # Claude Opus 5.5 partner pricing
        self.assertEqual(get_model_rates("claude-opus-5-5-high"), (4.00, 0.20, 20.00))

    def test_multi_model_session_and_summary_cost(self):
        t = datetime(2026, 10, 18, 12, 0)
        s_flash = StepRecord(
            step_idx=1,
            timestamp=t,
            model="gemini-3.8-flash",
            usage=TokenUsage(prompt_tokens=1_000_000, cached_tokens=0, output_tokens=0),
        )
        s_opus = StepRecord(
            step_idx=2,
            timestamp=t,
            model="claude-opus-5-5-high",
            usage=TokenUsage(prompt_tokens=1_000_000, cached_tokens=0, output_tokens=0),
        )
        s_argon = StepRecord(
            step_idx=3,
            timestamp=t,
            model="gemini-4-argon-trial",
            usage=TokenUsage(prompt_tokens=1_000_000, cached_tokens=0, output_tokens=0),
        )
        session = SessionSummary(
            session_id="multi-model",
            source="cli",
            file_path="/tmp/mm.db",
            title="Multi Model",
            workspace="ws",
            models=["claude-opus-5-5-high", "gemini-4-argon-trial", "gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=3,
            usage=s_flash.usage.add(s_opus.usage).add(s_argon.usage),
            steps=[s_flash, s_opus, s_argon],
        )
        # Expected: $0.75 (3.8-flash) + $4.00 (opus-5.5) + $2.00 (argon-trial) = $6.75
        self.assertAlmostEqual(session.cost.total, 6.75)

        summary = UsageAggregator.compute_summary([session])
        self.assertAlmostEqual(summary["total_cost"].total, 6.75)


if __name__ == "__main__":
    unittest.main()
