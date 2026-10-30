"""Tests for WindowStats derived fields/predicates and the status badge priority order."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from agy_usage.limits import (
    LimitsCalculator,
    TierConfig,
    WindowStats,
    get_status_badge,
    render_gauge_bar,
    render_remaining_bar,
)
from agy_usage.display import Colors
from agy_usage.models import TokenUsage

NOW = datetime(2026, 10, 18, 15, 0)


def _win(
    tokens=0,
    limit=100,
    cost=0.0,
    cost_limit=None,
    fraction=None,
    refresh_time=None,
    window_name="w",
):
    return WindowStats(
        window_name=window_name,
        start_time=NOW,
        end_time=NOW,
        usage=TokenUsage(prompt_tokens=tokens),
        step_count=1,
        session_count=1,
        limit_tokens=limit,
        estimated_cost=cost,
        refresh_time=refresh_time,
        cost_limit_usd=cost_limit,
        server_remaining_fraction=fraction,
    )


def _healthy():
    return _win(tokens=10)


def _critical():
    return _win(tokens=100)


class TestNextReliefTime(unittest.TestCase):
    def test_follows_refresh_time(self):
        w = _win(refresh_time=NOW + timedelta(hours=1))
        self.assertEqual(w.next_relief_time, NOW + timedelta(hours=1))
        w.refresh_time = None
        self.assertIsNone(w.next_relief_time)

    def test_is_read_only_and_not_a_dataclass_field(self):
        w = _win()
        with self.assertRaises(AttributeError):
            w.next_relief_time = NOW
        self.assertNotIn("next_relief_time", WindowStats.__dataclass_fields__)

    def test_computed_window_exposes_cycle_end(self):
        item = (NOW - timedelta(hours=1), TokenUsage(prompt_tokens=5), 0.0, "s")
        w = LimitsCalculator._compute_cycle_window(
            [item],
            current_time=NOW,
            duration=timedelta(hours=5),
            limit_tokens=100,
            pool="gemini",
            window_label="w",
        )
        self.assertEqual(w.refresh_time, NOW + timedelta(hours=4))
        self.assertEqual(w.next_relief_time, w.refresh_time)


class TestCostDriven(unittest.TestCase):
    def test_edge_cases(self):
        cases = [
            (None, 1.0, False),   # no cost limit
            (0.0, 1.0, False),    # zero limit
            (-1.0, 1.0, False),   # negative limit
            (5.0, 0.0, False),    # no recorded cost
            (5.0, 1.0, True),
            (5.0, 5.0, True),
            (5.0, 9.0, True),     # over the limit still counts
        ]
        for cost_limit, cost, expected in cases:
            with self.subTest(cost_limit=cost_limit, cost=cost):
                self.assertIs(_win(cost=cost, cost_limit=cost_limit)._cost_driven, expected)

    def test_cost_driven_window_uses_cost_for_percentages(self):
        w = _win(tokens=1, limit=1_000, cost=2.5, cost_limit=10.0)
        self.assertEqual(w.pct_used, 25.0)
        self.assertEqual(w.pct_remaining, 75.0)
        self.assertEqual(w.tokens_remaining, 750)
        self.assertFalse(w.is_exceeded)
        self.assertTrue(_win(cost=10.0, cost_limit=10.0).is_exceeded)

    def test_server_fraction_wins_and_is_clamped(self):
        w = _win(cost=9.0, cost_limit=10.0, fraction=1.5)
        self.assertEqual(w.pct_used, 0.0)
        self.assertEqual(w.pct_remaining, 100.0)
        self.assertFalse(w.is_exceeded)
        # is_exceeded reads the raw fraction, not the clamped one.
        self.assertTrue(_win(fraction=-0.5).is_exceeded)
        self.assertEqual(_win(fraction=-0.5).pct_remaining, 0.0)


class TestStatusBadgePriority(unittest.TestCase):
    def setUp(self):
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

    def test_all_critical_reports_gemini_weekly_first(self):
        badge, msg = get_status_badge(
            _critical(), _critical(), _critical(), _critical(), _critical()
        )
        self.assertEqual(badge, "🔴")
        self.assertTrue(msg.startswith("GEMINI WEEKLY LIMIT REACHED"))

    def test_priority_order_when_leading_windows_recover(self):
        kwargs = dict(
            w_5h=_critical(), w_7d=_healthy(), argon_7d=_critical(),
            claude_5h=_critical(), claude_7d=_critical(),
        )
        expected = [
            ("argon_7d", "GEMINI 4 ARGON WEEKLY"),
            ("claude_7d", "CLAUDE & GPT WEEKLY"),
            ("w_5h", "GEMINI 5-HOUR"),
            ("claude_5h", "CLAUDE & GPT 5-HOUR"),
        ]
        for name, prefix in expected:
            with self.subTest(next_critical=name):
                badge, msg = get_status_badge(**kwargs)
                self.assertEqual(badge, "🔴")
                self.assertTrue(msg.startswith(prefix), msg)
                kwargs[name] = _healthy()
        badge, _ = get_status_badge(**kwargs)
        self.assertNotEqual(badge, "🔴")

    def test_message_is_bold_red_when_colors_are_on(self):
        Colors.set_override(True)
        _, msg = get_status_badge(_critical(), _critical())
        self.assertEqual(
            msg,
            Colors.bold(
                Colors.red(
                    "GEMINI WEEKLY LIMIT REACHED (Risk of lockout until weekly cycle reset)"
                )
            ),
        )

    def test_optional_windows_may_be_none(self):
        badge, _ = get_status_badge(_healthy(), _healthy(), None, None, None)
        self.assertEqual(badge, "🟢")

    def test_windows_without_data_never_go_red(self):
        # Over its cost limit on paper, but no tokens and no server fraction: no data.
        no_data = _win(tokens=0, limit=100, cost=5.0, cost_limit=1.0)
        self.assertTrue(no_data.is_exceeded)
        for position in ("argon_7d", "claude_7d", "claude_5h"):
            with self.subTest(position=position):
                badge, _ = get_status_badge(_healthy(), _healthy(), **{position: no_data})
                self.assertNotEqual(badge, "🔴")

    def test_gemini_windows_do_not_need_data_to_go_red(self):
        no_data = _win(tokens=0, limit=100, cost=5.0, cost_limit=1.0)
        self.assertEqual(get_status_badge(_healthy(), no_data)[0], "🔴")
        self.assertEqual(get_status_badge(no_data, _healthy())[0], "🔴")

    def test_server_fraction_counts_as_data(self):
        exhausted = _win(tokens=0, fraction=0.0)
        badge, msg = get_status_badge(_healthy(), _healthy(), claude_5h=exhausted)
        self.assertEqual(badge, "🔴")
        self.assertTrue(msg.startswith("CLAUDE & GPT 5-HOUR"))


class TestTierConfigAndBars(unittest.TestCase):
    def test_tier_config_requires_every_field(self):
        with self.assertRaises(TypeError):
            TierConfig(
                name="x",
                display_name="X",
                five_hour_limit=1,
                weekly_limit=1,
                description="d",
            )

    def test_bars_share_layout_but_not_thresholds(self):
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)
        self.assertEqual(render_gauge_bar(50.0, 10), "[█████░░░░░]")
        self.assertEqual(render_remaining_bar(50.0, 10), "[█████░░░░░]")
        self.assertEqual(render_gauge_bar(130.0, 4), "[████]")
        self.assertEqual(render_remaining_bar(-10.0, 4), "[░░░░]")
        Colors.set_override(True)
        self.assertEqual(render_gauge_bar(85.0, 2), f"[{Colors.red('██')}]")
        self.assertEqual(render_gauge_bar(84.9, 2), f"[{Colors.yellow('██')}]")
        self.assertEqual(render_remaining_bar(15.0, 2), f"[{Colors.red('░░')}]")
        self.assertEqual(render_remaining_bar(15.1, 2), f"[{Colors.yellow('░░')}]")


if __name__ == "__main__":
    unittest.main()
