"""Tests for the TokenUsage model."""

import unittest

from agy_usage.models import TokenUsage


class TestTokenUsageModel(unittest.TestCase):
    def test_token_math(self):
        u1 = TokenUsage(prompt_tokens=100, cached_tokens=900, output_tokens=50, thought_tokens=20, candidate_tokens=30)
        self.assertEqual(u1.total_prompt_tokens, 1000)
        self.assertEqual(u1.total_tokens, 1050)
        self.assertEqual(u1.billable_tokens, 150)
        self.assertAlmostEqual(u1.cache_hit_rate, 90.0)

        u2 = TokenUsage(prompt_tokens=200, cached_tokens=300, output_tokens=100)
        added = u1.add(u2)
        self.assertEqual(added.prompt_tokens, 300)
        self.assertEqual(added.cached_tokens, 1200)
        self.assertEqual(added.output_tokens, 150)
        self.assertEqual(added.total_tokens, 1650)


if __name__ == "__main__":
    unittest.main()
