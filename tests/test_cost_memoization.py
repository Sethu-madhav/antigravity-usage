"""Tests for cost memoization and pricing key cache (OPT-003)."""

from __future__ import annotations

import dataclasses
from datetime import datetime
import unittest
from unittest import mock

from agy_usage import models
from agy_usage.models import (
    SessionSummary,
    StepRecord,
    TokenUsage,
    _resolve_pricing_key,
    price_usage,
)


class TestCostMemoization(unittest.TestCase):
    def setUp(self) -> None:
        _resolve_pricing_key.cache_clear()

    def test_step_record_cost_cached(self) -> None:
        ts = datetime(2026, 10, 20, 14, 30)
        usage = TokenUsage(prompt_tokens=1200, cached_tokens=400, output_tokens=300)
        step = StepRecord(
            step_idx=1,
            timestamp=ts,
            model="gemini-3.8-flash",
            usage=usage,
            duration_ms=450.0,
        )

        cost1 = step.cost
        cost2 = step.cost
        self.assertIs(cost1, cost2)

        fresh_cost = price_usage(usage, step.model, ts.date(), per_request=True)
        self.assertEqual(cost1, fresh_cost)

    def test_session_summary_cost_with_steps_cached(self) -> None:
        ts = datetime(2026, 10, 20, 15, 0)
        steps = [
            StepRecord(
                step_idx=1,
                timestamp=ts,
                model="gemini-3.8-flash",
                usage=TokenUsage(prompt_tokens=1000, cached_tokens=200, output_tokens=150),
            ),
            StepRecord(
                step_idx=2,
                timestamp=ts,
                model="claude-opus-5-5",
                usage=TokenUsage(prompt_tokens=2000, cached_tokens=500, output_tokens=250),
            ),
            StepRecord(
                step_idx=3,
                timestamp=ts,
                model="gemini-3.1-pro",
                usage=TokenUsage(prompt_tokens=3000, cached_tokens=800, output_tokens=350),
            ),
        ]
        session = SessionSummary(
            session_id="session-with-steps",
            source="cli",
            file_path="/path/to/session.json",
            steps=steps,
        )

        with mock.patch("agy_usage.models.price_usage", wraps=models.price_usage) as mock_price:
            # Read session.cost 5 times
            session_costs = [session.cost for _ in range(5)]
            # Read step.cost several times
            for _ in range(3):
                for s in steps:
                    _ = s.cost

            # Assert price_usage was called exactly 3 times (once per step)
            self.assertEqual(mock_price.call_count, 3)

        # Identical cached object across repeated accesses
        for c in session_costs[1:]:
            self.assertIs(c, session_costs[0])

        # Totals equal the sum of per-step costs
        expected_prompt = sum(s.cost.prompt_cost for s in steps)
        expected_cached = sum(s.cost.cached_cost for s in steps)
        expected_output = sum(s.cost.output_cost for s in steps)
        expected_total = sum(s.cost.total for s in steps)

        self.assertAlmostEqual(session.cost.prompt_cost, expected_prompt)
        self.assertAlmostEqual(session.cost.cached_cost, expected_cached)
        self.assertAlmostEqual(session.cost.output_cost, expected_output)
        self.assertAlmostEqual(session.cost.total, expected_total)

    def test_session_summary_cost_without_steps_cached(self) -> None:
        start = datetime(2026, 10, 20, 10, 0)
        usage = TokenUsage(prompt_tokens=5000, cached_tokens=1000, output_tokens=500)
        session = SessionSummary(
            session_id="session-no-steps",
            source="cli",
            file_path="/path/to/session-no-steps.json",
            models=["gemini-3.8-flash"],
            start_time=start,
            usage=usage,
            steps=[],
        )

        expected_cost = price_usage(session.usage, session.primary_model, start.date())

        with mock.patch("agy_usage.models.price_usage", wraps=models.price_usage) as mock_price:
            cost1 = session.cost
            cost2 = session.cost
            self.assertEqual(mock_price.call_count, 1)

        self.assertIs(cost1, cost2)
        self.assertEqual(cost1, expected_cost)

    def test_resolve_pricing_key_and_cache(self) -> None:
        cases = [
            ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
            ("gemini-2.5-flash", "gemini-2.5-flash"),
            ("claude-opus-4-thing", "claude-opus-5-5"),
            ("some-pro-model", "gemini-3.1-pro"),
            ("totally-unknown", "gemini-3.8-flash"),
            ("", "gemini-3.8-flash"),
        ]
        for input_name, expected_key in cases:
            self.assertEqual(_resolve_pricing_key(input_name), expected_key)

        info_before = _resolve_pricing_key.cache_info()
        hits_before = info_before.hits
        # Call a previously queried key again
        res = _resolve_pricing_key("gemini-2.5-flash-lite")
        self.assertEqual(res, "gemini-2.5-flash-lite")
        info_after = _resolve_pricing_key.cache_info()
        self.assertEqual(info_after.hits, hits_before + 1)

    def test_asdict_and_to_dict_after_cost_read(self) -> None:
        ts = datetime(2026, 10, 20, 16, 0)
        usage = TokenUsage(prompt_tokens=800, cached_tokens=100, output_tokens=50)
        step = StepRecord(
            step_idx=1,
            timestamp=ts,
            model="gemini-3.8-flash",
            usage=usage,
            duration_ms=120.0,
        )

        session = SessionSummary(
            session_id="session-dict-test",
            source="cli",
            file_path="/path/to/session-dict.json",
            models=["gemini-3.8-flash"],
            start_time=ts,
            steps=[step],
        )

        # Compute cost before calling asdict / to_dict
        step_cost_before = step.cost
        session_cost_before = session.cost

        # dataclasses.asdict(step) still works after .cost has been read
        step_asdict = dataclasses.asdict(step)
        self.assertEqual(step_asdict["step_idx"], 1)
        self.assertEqual(step_asdict["model"], "gemini-3.8-flash")
        self.assertEqual(step_asdict["duration_ms"], 120.0)
        self.assertEqual(step_asdict["timestamp"], ts)
        self.assertIn("prompt_tokens", step_asdict["usage"])

        # session.to_dict() still works after .cost has been read and to_dict()["cost"] is unchanged
        session_dict = session.to_dict(include_steps=True)
        self.assertEqual(session_dict["cost"], session_cost_before.to_dict())
        self.assertEqual(session_dict["steps"][0]["cost"], step_cost_before.to_dict())


if __name__ == "__main__":
    unittest.main()
