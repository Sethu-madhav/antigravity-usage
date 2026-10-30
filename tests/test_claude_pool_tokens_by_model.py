"""Tests for converting the Claude & GPT pool's USD cap into tokens per model."""

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from unittest import mock

from agy_usage import limits
from agy_usage.cli import cmd_limits
from agy_usage.limits import (
    REFERENCE_TOKEN_MIX,
    TIERS,
    WindowStats,
    claude_budget_left_usd,
    claude_pool_tokens_by_model,
    tokens_for_budget,
)
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


def _window(server_fraction=None, usage=None) -> WindowStats:
    now = datetime(2026, 10, 30, 12, 0)
    return WindowStats(
        window_name="w",
        start_time=now - timedelta(hours=5),
        end_time=now,
        usage=usage or TokenUsage(),
        step_count=0,
        session_count=0,
        limit_tokens=1_000_000,
        estimated_cost=0.0,
        pool="claude",
        server_remaining_fraction=server_fraction,
        server_unreachable=server_fraction is None,
    )


def _claude_session() -> SessionSummary:
    t = datetime.now() - timedelta(minutes=10)
    usage = TokenUsage(prompt_tokens=100_000, cached_tokens=900_000, output_tokens=20_000)
    return SessionSummary(
        session_id="s-claude-mix",
        source="cli",
        file_path="/tmp/claude-mix.db",
        title="Claude mix",
        workspace="ws",
        models=["claude-opus-5-5-high"],
        start_time=t,
        end_time=t,
        step_count=1,
        usage=usage,
        steps=[StepRecord(step_idx=1, timestamp=t, model="claude-opus-5-5-high", usage=usage)],
    )


def _limits_args(**overrides) -> argparse.Namespace:
    base = dict(
        tier="pro",
        five_h_limit=None,
        weekly_limit=None,
        argon_weekly_limit=None,
        argon_remaining_pct=None,
        claude_five_h_limit=None,
        claude_weekly_limit=None,
        json=False,
        csv=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class TestTokensForBudget(unittest.TestCase):
    def test_all_uncached_opus_input(self):
        # Opus non-cached input is $4.00 / 1M tokens.
        self.assertEqual(tokens_for_budget(4.0, "claude-opus-5-5", TokenUsage(prompt_tokens=1)), 1_000_000)

    def test_sonnet_gets_twice_the_opus_tokens_at_any_mix(self):
        for mix in (
            REFERENCE_TOKEN_MIX,
            TokenUsage(prompt_tokens=1),
            TokenUsage(cached_tokens=1),
            TokenUsage(prompt_tokens=373_500, cached_tokens=4_730_000, output_tokens=92_600),
        ):
            by_model = dict(claude_pool_tokens_by_model(4.66, mix))
            self.assertAlmostEqual(by_model["Sonnet"], 2 * by_model["Opus"], delta=2)

    def test_mix_scale_does_not_matter(self):
        small = tokens_for_budget(4.66, "claude-opus-5-5", TokenUsage(1, 9, 1))
        large = tokens_for_budget(4.66, "claude-opus-5-5", TokenUsage(1000, 9000, 1000))
        self.assertAlmostEqual(small, large, delta=1)

    def test_empty_budget_or_mix_is_zero(self):
        self.assertEqual(tokens_for_budget(0.0, "claude-opus-5-5", REFERENCE_TOKEN_MIX), 0)
        self.assertEqual(tokens_for_budget(4.66, "claude-opus-5-5", TokenUsage()), 0)

    def test_labels_cover_the_pool(self):
        labels = [label for label, _ in claude_pool_tokens_by_model(4.66, REFERENCE_TOKEN_MIX)]
        self.assertEqual(labels, ["Opus", "Sonnet", "GPT-OSS"])


class TestClaudeBudgetLeft(unittest.TestCase):
    def test_tighter_window_wins(self):
        tier = TIERS["pro"]
        left = claude_budget_left_usd(tier, _window(0.9), _window(0.25))
        # 5h: 0.9 * $2.33 = $2.097; weekly: 0.25 * $4.66 = $1.165.
        self.assertAlmostEqual(left, 1.165)

    def test_unknown_window_gives_none(self):
        self.assertIsNone(claude_budget_left_usd(TIERS["pro"], _window(), _window(0.5)))


class TestCmdLimitsTokensByModel(unittest.TestCase):
    def _run(self, sessions, **overrides) -> str:
        out = io.StringIO()
        with mock.patch.object(limits, "fetch_live_quota_summary", return_value=None):
            with redirect_stdout(out):
                cmd_limits(sessions, _limits_args(**overrides))
        return out.getvalue()

    def test_json_reports_per_model_weekly_cap(self):
        data = json.loads(self._run([_claude_session()], json=True))
        cap = data["claude_pool"]["weekly_cap_tokens_by_model"]
        self.assertEqual(set(cap), {"claude-opus-5-5", "claude-sonnet-5-5", "gpt-oss-120b"})
        self.assertAlmostEqual(cap["claude-sonnet-5-5"], 2 * cap["claude-opus-5-5"], delta=2)
        left = data["claude_pool"]["tokens_left_by_model"]
        self.assertIsNotNone(left)
        self.assertLessEqual(left["claude-opus-5-5"], cap["claude-opus-5-5"])

    def test_json_left_is_null_when_pool_unknown(self):
        data = json.loads(self._run([], json=True))
        self.assertIsNone(data["claude_pool"]["tokens_left_by_model"])
        self.assertGreater(data["claude_pool"]["weekly_cap_tokens_by_model"]["claude-opus-5-5"], 0)

    def test_text_names_opus_and_sonnet(self):
        out = self._run([_claude_session()])
        self.assertIn("Weekly Cap by Model:", out)
        self.assertIn("Tokens Left by Model:", out)
        self.assertIn("this week's 90.0% cache hit rate", out)
        self.assertNotIn("~6.91M", out)
        self.assertIn("Sonnet", out)


if __name__ == "__main__":
    unittest.main()
