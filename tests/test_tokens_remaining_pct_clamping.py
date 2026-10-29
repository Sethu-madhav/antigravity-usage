"""Tests for tokens_remaining and percent clamping."""

import unittest
from datetime import datetime

from agy_usage.models import TokenUsage


class TestBug013TokensRemainingAndPctClamping(unittest.TestCase):
    """Regression tests for BUG-013: tokens_remaining must derive from remaining

    percentage when server fraction or cost model is active, tokens_remaining
    must be 0 when limit is exceeded or limit <= 0, and pct_used must clamp to 100.0.
    """

    def test_server_remaining_fraction_exhausted(self):
        # When server_remaining_fraction is 0.0 (100% used), tokens_remaining must be 0
        # instead of incorrectly reporting the entire limit as remaining.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="gemini-weekly",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=0),
            step_count=1,
            session_count=1,
            limit_tokens=2_750_000_000,
            estimated_cost=0.0,
            server_remaining_fraction=0.0,
        )
        self.assertEqual(w.pct_used, 100.0)
        self.assertEqual(w.pct_remaining, 0.0)
        self.assertTrue(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 0)

    def test_server_remaining_fraction_partial(self):
        # When server fraction indicates partial remaining quota (e.g. 25%),
        # tokens_remaining must derive from that fraction.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="gemini-5h",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=5_000_000),
            step_count=1,
            session_count=1,
            limit_tokens=100_000_000,
            estimated_cost=0.0,
            server_remaining_fraction=0.25,
        )
        self.assertEqual(w.pct_used, 75.0)
        self.assertEqual(w.pct_remaining, 25.0)
        self.assertFalse(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 25_000_000)

    def test_cost_limit_exceeded(self):
        # When estimated cost meets or exceeds cost_limit_usd, tokens_remaining must be 0.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="claude-weekly",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=5_000_000),
            step_count=1,
            session_count=1,
            limit_tokens=50_000_000,
            estimated_cost=25.0,
            cost_limit_usd=20.0,
        )
        self.assertEqual(w.pct_used, 100.0)
        self.assertEqual(w.pct_remaining, 0.0)
        self.assertTrue(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 0)

    def test_cost_limit_partial(self):
        # When cost limit is active and partially consumed, tokens_remaining derives from pct_remaining.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="claude-5h",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=5_000_000),
            step_count=1,
            session_count=1,
            limit_tokens=50_000_000,
            estimated_cost=10.0,
            cost_limit_usd=20.0,
        )
        self.assertEqual(w.pct_used, 50.0)
        self.assertEqual(w.pct_remaining, 50.0)
        self.assertFalse(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 25_000_000)

    def test_token_pct_and_remaining_clamping(self):
        # Token usage exceeding limit_tokens must clamp pct_used to 100.0 and tokens_remaining to 0.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="gemini-5h",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=1_500_000),
            step_count=1,
            session_count=1,
            limit_tokens=1_000_000,
            estimated_cost=0.0,
        )
        self.assertEqual(w.pct_used, 100.0)
        self.assertEqual(w.pct_remaining, 0.0)
        self.assertTrue(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 0)

    def test_zero_limit_tokens(self):
        # Window with zero limit_tokens must return 0 tokens_remaining.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="no-limit",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=100),
            step_count=1,
            session_count=1,
            limit_tokens=0,
            estimated_cost=0.0,
        )
        self.assertEqual(w.pct_used, 100.0)
        self.assertEqual(w.pct_remaining, 0.0)
        self.assertTrue(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 0)


    def test_token_model_partial_usage(self):
        # Pure token model with partial usage must retain exact integer difference.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="gemini-5h",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=400_000),
            step_count=1,
            session_count=1,
            limit_tokens=1_000_000,
            estimated_cost=0.0,
        )
        self.assertEqual(w.pct_used, 40.0)
        self.assertEqual(w.pct_remaining, 60.0)
        self.assertFalse(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 600_000)

    def test_cost_limit_with_zero_estimated_cost_falls_through_to_tokens(self):
        # When cost_limit_usd is set but estimated_cost is 0.0, token usage drives calculation.
        from datetime import datetime
        from agy_usage.limits import WindowStats
        from agy_usage.models import TokenUsage

        now = datetime.now()
        w = WindowStats(
            window_name="claude-5h",
            start_time=now,
            end_time=now,
            usage=TokenUsage(prompt_tokens=300_000),
            step_count=1,
            session_count=1,
            limit_tokens=1_000_000,
            estimated_cost=0.0,
            cost_limit_usd=20.0,
        )
        self.assertEqual(w.pct_used, 30.0)
        self.assertEqual(w.pct_remaining, 70.0)
        self.assertFalse(w.is_exceeded)
        self.assertEqual(w.tokens_remaining, 700_000)


if __name__ == "__main__":
    unittest.main()
