"""Unit tests for Antigravity Model Usage Tracker."""

import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from unittest import mock

from agy_usage import display as display_mod
from agy_usage.aggregator import UsageAggregator
from agy_usage.cli import main as cli_main
from agy_usage.display import (
    Colors,
    format_duration,
    format_duration_verbose,
    format_percent,
    format_tokens,
    render_bar,
    render_sparkline,
    render_table,
    strip_ansi,
)
from agy_usage.exporter import UsageExporter
from agy_usage.heatmap import HeatmapColor, TerminalHeatmap
from agy_usage.models import DailyUsage, SessionSummary, StepRecord, TokenUsage
from agy_usage.parser import (
    AntigravityParser,
    decode_varint,
    parse_proto_fields,
    parse_timestamp_proto,
    parse_usage_proto,
)


class TestProtobufDecoding(unittest.TestCase):
    def test_varint_decoding(self):
        # 1-byte varint: 0 -> 0, 1 -> 1, 127 -> 127
        val, idx = decode_varint(b"\x00", 0)
        self.assertEqual(val, 0)
        self.assertEqual(idx, 1)

        val, idx = decode_varint(b"\x01", 0)
        self.assertEqual(val, 1)

        # 2-byte varint: 300 = 0xAC 0x02
        val, idx = decode_varint(b"\xac\x02", 0)
        self.assertEqual(val, 300)
        self.assertEqual(idx, 2)

    def test_proto_fields_parsing(self):
        # field 1: varint 42 (tag = (1 << 3) | 0 = 8 = 0x08)
        # field 2: string 'test' (tag = (2 << 3) | 2 = 18 = 0x12, len=4)
        raw = bytes([0x08, 42, 0x12, 4]) + b"test"
        fields = parse_proto_fields(raw)
        self.assertEqual(len(fields), 2)
        self.assertEqual(fields[0], (1, 0, 42))
        self.assertEqual(fields[1], (2, 2, b"test"))

    def test_usage_proto_parsing(self):
        # Construct synthetic UsageMetadata:
        # field 2 (prompt): 1000 -> 0x10, 0xe8, 0x07
        # field 3 (output): 200 -> 0x18, 0xc8, 0x01
        # field 5 (cached): 500 -> 0x28, 0xf4, 0x03
        raw = bytes([0x10, 0xE8, 0x07, 0x18, 0xC8, 0x01, 0x28, 0xF4, 0x03])
        usage = parse_usage_proto(raw)
        self.assertEqual(usage.prompt_tokens, 1000)
        self.assertEqual(usage.output_tokens, 200)
        self.assertEqual(usage.cached_tokens, 500)
        self.assertEqual(usage.total_tokens, 1700)
        self.assertAlmostEqual(usage.cache_hit_rate, 33.33, places=1)


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


class TestAggregations(unittest.TestCase):
    def setUp(self):
        t1 = datetime(2026, 9, 1, 10, 0)
        t2 = datetime(2026, 9, 8, 14, 0)
        self.s1 = SessionSummary(
            session_id="session-001",
            source="cli",
            file_path="/tmp/s1.db",
            title="Session 1",
            workspace="proj1",
            models=["gemini-3.8-flash"],
            start_time=t1,
            end_time=t1,
            step_count=2,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200),
            steps=[
                StepRecord(step_idx=2, timestamp=t1, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200))
            ]
        )
        self.s2 = SessionSummary(
            session_id="session-002",
            source="cli",
            file_path="/tmp/s2.db",
            title="Session 2",
            workspace="proj2",
            models=["gemini-3.7-flash"],
            start_time=t2,
            end_time=t2,
            step_count=1,
            usage=TokenUsage(prompt_tokens=500, cached_tokens=1500, output_tokens=100),
            steps=[
                StepRecord(step_idx=2, timestamp=t2, model="gemini-3.7-flash", usage=TokenUsage(prompt_tokens=500, cached_tokens=1500, output_tokens=100))
            ]
        )

    def test_daily_aggregation(self):
        daily = UsageAggregator.aggregate_daily([self.s1, self.s2])
        self.assertEqual(len(daily), 2)
        d1 = date(2026, 9, 1)
        d2 = date(2026, 9, 8)
        self.assertIn(d1, daily)
        self.assertIn(d2, daily)
        self.assertEqual(daily[d1].usage.cached_tokens, 4000)
        self.assertEqual(daily[d2].usage.cached_tokens, 1500)

    def test_weekly_aggregation(self):
        weekly = UsageAggregator.aggregate_weekly([self.s1, self.s2])
        self.assertEqual(len(weekly), 2)

    def test_monthly_aggregation(self):
        monthly = UsageAggregator.aggregate_monthly([self.s1, self.s2])
        self.assertEqual(len(monthly), 1)
        self.assertEqual(monthly[0].label, "2026-09")
        self.assertEqual(monthly[0].usage.total_tokens, 7300)

    def test_model_aggregation(self):
        models = UsageAggregator.aggregate_models([self.s1, self.s2])
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0].model_name, "gemini-3.8-flash")
        self.assertEqual(models[1].model_name, "gemini-3.7-flash")


class TestHeatmapAndDisplay(unittest.TestCase):
    def test_heatmap_rendering(self):
        daily = {
            date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(cached_tokens=5000, prompt_tokens=1000)),
        }
        hm = TerminalHeatmap(daily, metric="total", weeks=8, end_date=date(2026, 9, 10))
        rendered = hm.render()
        self.assertIn("Mon", rendered)
        self.assertIn("Wed", rendered)
        self.assertIn("Fri", rendered)
        self.assertIn("Less", rendered)
        self.assertIn("More", rendered)

    def test_month_labels_align_to_week_columns(self):
        """BUG-006: month labels start exactly at their week column and advance
        enough columns for their width, so they never drift right or overlap."""
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        weeks = 52
        hm = TerminalHeatmap(daily, metric="total", weeks=weeks, end_date=date(2026, 10, 28))
        # Colors are disabled, so line 0 of the render is the plain month header.
        header = hm.render().splitlines()[0]
        self.assertNotIn("\033", header)

        # Independently derive which week column each month label is placed on.
        end_sunday = date(2026, 10, 28) + timedelta(days=6 - date(2026, 10, 28).weekday())
        start_monday = end_sunday - timedelta(days=weeks * 7 - 1)
        placed = []  # (week_column, month_name)
        prev_month = -1
        for w in range(weeks):
            col_date = start_monday + timedelta(days=7 * w)
            if col_date.month != prev_month:
                placed.append((w, TerminalHeatmap.MONTH_NAMES[col_date.month - 1]))
                prev_month = col_date.month

        # Multi-month window with non-uniform month boundaries (Monday of a week
        # still in the previous month) so any drift would show up.
        self.assertGreater(len(placed), 10)

        # Every label starts exactly at char offset 5 + 2*w of its own column.
        for w, name in placed:
            self.assertEqual(header[5 + 2 * w:5 + 2 * w + len(name)], name)

        # Over consecutive labels, each previous label clears exactly its own
        # ceil(len/2) columns and nothing more: no cumulative drift.
        for (w1, n1), (w2, _n2) in zip(placed, placed[1:]):
            cols_skip = max(1, -(-len(n1) // 2))
            segment = header[5 + 2 * w1:5 + 2 * w2]
            self.assertEqual(segment, n1.ljust(cols_skip * 2) + " " * 2 * (w2 - w1 - cols_skip))

    def test_month_labels_pinned_window_layout(self):
        """BUG-006: exact header for a pinned window (labels on weeks 0/2/6)."""
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        hm = TerminalHeatmap(daily, metric="total", weeks=10, end_date=date(2026, 10, 28))
        header = hm.render().splitlines()[0]

        # 5-space indent, then week columns of 2 chars: "Aug" on col 0, "Sep" on
        # col 2, "Oct" on col 6; each label owns ceil(len/2) columns, the rest blank.
        self.assertEqual(header, "     Aug Sep     Oct")

    def test_no_color_kill_switch_disables_heatmap_escapes(self):
        """BUG-004: the shared --no-color switch must strip every ANSI escape, not just Colors."""
        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

        self.assertFalse(HeatmapColor.supports_color())
        self.assertNotIn("\033", HeatmapColor.cell(4))
        rendered = TerminalHeatmap(daily, metric="total", weeks=8, end_date=date(2026, 9, 10)).render()
        self.assertNotIn("\033", rendered)
        # Falls back to plain density symbols instead of colored cells.
        self.assertIn("·", rendered)
        self.assertIn("█", rendered)
        self.assertIn("Less", rendered)
        self.assertIn("More", rendered)

    def test_color_force_on_enables_heatmap_escapes(self):
        """The same switch can force colors on even when stdout is not a TTY."""
        daily = {date(2026, 9, 1): DailyUsage(date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000))}
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

        self.assertTrue(HeatmapColor.supports_color())
        self.assertIn("\033[38;", HeatmapColor.cell(2))
        rendered = TerminalHeatmap(daily, metric="total", weeks=8, end_date=date(2026, 9, 10)).render()
        self.assertIn("\033[38;", rendered)

    def test_color_auto_detect_paths(self):
        """BUG-004 (review): the auto-detection path itself is covered deterministically."""
        Colors.set_override(None)
        self.addCleanup(Colors.set_override, None)
        # Hermetic env: no COLORTERM / NO_COLOR inherited from the runner.
        bare_env = {"PATH": os.environ.get("PATH", "")}

        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(os.environ, bare_env, clear=True):
                self.assertTrue(Colors.enabled())
                self.assertTrue(HeatmapColor.supports_color())
        # TTY off + no COLORTERM → disabled.
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, bare_env, clear=True):
                self.assertFalse(Colors.enabled())
        # NO_COLOR wins even on a TTY.
        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(os.environ, {**bare_env, "NO_COLOR": "1"}, clear=True):
                self.assertFalse(Colors.enabled())
        # COLORTERM still upgrades piped stdout (pre-BUG-009 behavior preserved).
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, {**bare_env, "COLORTERM": "truecolor"}, clear=True):
                self.assertTrue(Colors.enabled())

    def test_main_no_color_wires_global_switch(self):
        """BUG-004: cli.main() applies --no-color to the shared switch and cmd_heatmap
        renders an escape-free heatmap (integration, real render not the early return)."""
        self.addCleanup(Colors.set_override, None)

        fake = SessionSummary(
            session_id="abc123def456",
            source="cli",
            file_path="/tmp/fake.db",
            title="Repro session",
            workspace="/ws/repro",
            models=["gemini-3-pro"],
            start_time=datetime(2026, 10, 26, 10, 0, 0),
            end_time=datetime(2026, 10, 26, 10, 30, 0),
            step_count=4,
            usage=TokenUsage(prompt_tokens=5000, cached_tokens=20000, output_tokens=1000),
        )

        with mock.patch("agy_usage.cli.get_sessions", return_value=[fake]):
            with redirect_stdout(io.StringIO()) as captured:
                cli_main(["heatmap", "--no-color"])
        out = captured.getvalue()
        self.assertIn("GITHUB-STYLE CONTRIBUTION HEATMAP", out)  # render path reached
        self.assertNotIn("\033", out)

        # Rather than inheriting the switch from a previous call, main() resets it.
        Colors.set_override(False)
        with mock.patch("agy_usage.cli.get_sessions", return_value=[fake]):
            with redirect_stdout(io.StringIO()):
                cli_main(["heatmap"])
        self.assertIsNone(display_mod._COLOR_OVERRIDE)  # reset without the flag

    def test_formatting_utilities(self):
        self.assertEqual(format_tokens(1_500_000), "1.50M")
        self.assertEqual(format_tokens(45_200), "45.2K")
        self.assertEqual(format_tokens(500), "500")
        self.assertEqual(format_duration(65), "1m 5s")
        self.assertEqual(format_duration(3665), "1h 1m")
        self.assertEqual(format_duration(576000), "6d 16h")
        self.assertEqual(format_duration_verbose(576000), "6 days, 16 hours")
        self.assertEqual(format_duration_verbose(3660), "1 hour, 1 minute")
        self.assertEqual(format_duration_verbose(0), "0 minutes")

    def test_format_percent_remaining_semantics_unchanged(self):
        # BUG-011 regression guard: *remaining* callers keep today's thresholds
        # (green >=75, yellow >=40, gray below) — default and explicit kwarg.
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)
        self.assertEqual(format_percent(92.0), Colors.green("92.0%"))
        self.assertEqual(format_percent(100.0), Colors.green("100.0%"))
        self.assertEqual(format_percent(75.0), Colors.green("75.0%"))
        self.assertEqual(format_percent(74.9), Colors.yellow("74.9%"))
        self.assertEqual(format_percent(40.0), Colors.yellow("40.0%"))
        self.assertEqual(format_percent(39.9), Colors.gray("39.9%"))
        self.assertEqual(format_percent(92.0, high_is_good=True), Colors.green("92.0%"))

    def test_format_percent_used_semantics_inverted(self):
        # BUG-011: for *used* quota a high fraction is bad (warn/danger color),
        # and a low fraction is healthy (green) — the inverse reading.
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)
        self.assertEqual(format_percent(92.0, high_is_good=False), Colors.red("92.0%"))
        self.assertEqual(format_percent(100.0, high_is_good=False), Colors.red("100.0%"))
        self.assertEqual(format_percent(75.0, high_is_good=False), Colors.red("75.0%"))
        self.assertEqual(format_percent(74.9, high_is_good=False), Colors.yellow("74.9%"))
        self.assertEqual(format_percent(40.0, high_is_good=False), Colors.yellow("40.0%"))
        self.assertEqual(format_percent(39.9, high_is_good=False), Colors.green("39.9%"))
        self.assertEqual(format_percent(30.0, high_is_good=False), Colors.green("30.0%"))
        # The bug itself: 92.0% used must render a warn/danger color, never green.

    def test_sparkline(self):
        spark = render_sparkline([10, 20, 50, 80, 100])
        self.assertEqual(len(spark), 5)

    def test_table_rendering(self):
        from agy_usage.display import render_kpi_card, strip_ansi

        headers = ["Col A", "Col B"]
        rows = [["Value 1", "Value 2"]]
        tbl = render_table(headers, rows)
        self.assertIn("Col A", tbl)
        self.assertIn("Value 1", tbl)

        colored_sub = "\033[92m92.8%\033[0m prompt hit rate ($225.73 saved)"
        card = render_kpi_card("CACHE SAVINGS", "\033[1m\033[92m355.71M\033[0m", colored_sub, width=26)
        for line in card.split("\n"):
            self.assertEqual(len(strip_ansi(line)), 26)

    def test_exporter_json_and_csv(self):
        t = datetime(2026, 9, 1, 12, 0)
        s = SessionSummary(
            session_id="s1",
            source="cli",
            file_path="/tmp/s1.db",
            title="Test",
            workspace="ws",
            models=["m1"],
            start_time=t,
            usage=TokenUsage(prompt_tokens=10, cached_tokens=90, output_tokens=5),
        )
        json_out = UsageExporter.to_json([s])
        self.assertIn('"session_count": 1', json_out)
        self.assertIn('"cached_tokens": 90', json_out)

        csv_out = UsageExporter.to_csv([s])
        self.assertIn("Session ID,Source,Workspace", csv_out)
        self.assertIn("s1,cli,ws", csv_out)


class TestSessionInspect(unittest.TestCase):
    def setUp(self):
        import io
        import sys
        self.io = io
        self.sys = sys
        t = datetime(2026, 9, 1, 12, 0)
        self.session = SessionSummary(
            session_id="test-session-12345",
            source="cli",
            file_path="/tmp/test.db",
            title="Inspect Test",
            workspace="test-ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=2,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200, thought_tokens=50, candidate_tokens=150),
            steps=[
                StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=500, cached_tokens=2000, output_tokens=100)),
                StepRecord(step_idx=2, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=500, cached_tokens=2000, output_tokens=100)),
            ],
        )

    def test_default_inspect_shows_totals_only(self):
        from agy_usage.cli import cmd_session_inspect
        buf = self.io.StringIO()
        old_stdout = self.sys.stdout
        try:
            self.sys.stdout = buf
            cmd_session_inspect([self.session], session_id_arg="current")
        finally:
            self.sys.stdout = old_stdout

        out = buf.getvalue()
        self.assertIn("SESSION TOTALS", out)
        self.assertIn("TOTAL PROCESSED", out)
        self.assertIn("CACHE SAVINGS", out)
        self.assertIn("BILLABLE CONSUMED", out)
        self.assertIn("ESTIMATED COST", out)
        self.assertIn("TOKEN USAGE BREAKDOWN", out)
        self.assertIn("Cost", out)
        # Verify progression row is removed
        self.assertNotIn("Progression:", out)
        self.assertNotIn("TURN-BY-TURN TOKEN LOG", out)

    def test_inspect_with_details_shows_turn_log(self):
        from agy_usage.cli import cmd_session_inspect
        buf = self.io.StringIO()
        old_stdout = self.sys.stdout
        try:
            self.sys.stdout = buf
            cmd_session_inspect([self.session], session_id_arg="current", show_details=True)
        finally:
            self.sys.stdout = old_stdout

        out = buf.getvalue()
        self.assertIn("SESSION TOTALS", out)
        self.assertIn("TURN-BY-TURN TOKEN LOG", out)
        self.assertIn("#1", out)
        self.assertIn("#2", out)
        self.assertNotIn("Progression:", out)

    def test_calculate_cost_and_format(self):
        from agy_usage.models import calculate_cost
        from agy_usage.display import format_cost
        # 1M prompt @ $0.75, 1M cache @ $0.075, 1M output @ $3.75
        p_c, c_c, o_c, tot = calculate_cost(1_000_000, 1_000_000, 1_000_000, "gemini-3.8-flash")
        self.assertAlmostEqual(p_c, 0.75)
        self.assertAlmostEqual(c_c, 0.075)
        self.assertAlmostEqual(o_c, 3.75)
        self.assertAlmostEqual(tot, 4.575)

        self.assertEqual(format_cost(4.575), "$4.58")
        self.assertEqual(format_cost(0.005), "$0.005")
        self.assertEqual(format_cost(0.0), "$0.00")


class TestLimits(unittest.TestCase):
    def test_parse_token_amount(self):
        from agy_usage.limits import parse_token_amount
        self.assertEqual(parse_token_amount("50M"), 50_000_000)
        self.assertEqual(parse_token_amount("1B"), 1_000_000_000)
        self.assertEqual(parse_token_amount("250k"), 250_000)
        self.assertEqual(parse_token_amount("1.5M"), 1_500_000)
        self.assertEqual(parse_token_amount("0.5M"), 500_000)
        self.assertEqual(parse_token_amount(" 100k "), 100_000)
        self.assertIsNone(parse_token_amount(None))
        self.assertIsNone(parse_token_amount(""))
        self.assertIsNone(parse_token_amount("invalid"))
        # BUG-003: non-finite values must not crash or be accepted
        self.assertIsNone(parse_token_amount("inf"))
        self.assertIsNone(parse_token_amount("-inf"))    # exercises OverflowError path
        self.assertIsNone(parse_token_amount("INF"))
        self.assertIsNone(parse_token_amount("nan"))
        self.assertIsNone(parse_token_amount("1e309"))    # float() -> inf
        self.assertIsNone(parse_token_amount("1e308M"))   # multiplier overflow
        # BUG-003: negatives and zero are not valid token amounts
        self.assertIsNone(parse_token_amount("-5M"))
        self.assertIsNone(parse_token_amount("-1"))
        self.assertIsNone(parse_token_amount("0"))
        self.assertIsNone(parse_token_amount("0K"))

    def test_render_gauge_bar(self):
        from agy_usage.limits import render_gauge_bar
        from agy_usage.display import strip_ansi
        bar_low = render_gauge_bar(20.0, width=10)
        self.assertIn("██", strip_ansi(bar_low))
        self.assertEqual(len(strip_ansi(bar_low)), 12)  # 10 chars + [ ]

        bar_mid = render_gauge_bar(70.0, width=10)
        self.assertIn("███████", strip_ansi(bar_mid))

        bar_high = render_gauge_bar(95.0, width=10)
        self.assertIn("██████████", strip_ansi(bar_high))

    def test_get_status_badge(self):
        from agy_usage.limits import WindowStats, get_status_badge
        now = datetime.now()
        
        def make_stats(tokens, limit):
            return WindowStats(
                window_name="test",
                start_time=now,
                end_time=now,
                usage=TokenUsage(prompt_tokens=tokens),
                step_count=1,
                session_count=1,
                limit_tokens=limit,
                estimated_cost=0.0,
            )

        # Healthy
        b, _ = get_status_badge(make_stats(10, 100), make_stats(10, 100))
        self.assertEqual(b, "🟢")

        # High load (>80%)
        b, _ = get_status_badge(make_stats(85, 100), make_stats(20, 100))
        self.assertEqual(b, "🟡")

        # 5h exceeded (>=100%)
        b, _ = get_status_badge(make_stats(100, 100), make_stats(20, 100))
        self.assertEqual(b, "🔴")

        # Weekly exceeded (>=100%)
        b, _ = get_status_badge(make_stats(20, 100), make_stats(100, 100))
        self.assertEqual(b, "🔴")

    def test_limits_calculator(self):
        from datetime import timedelta
        from agy_usage.limits import LimitsCalculator

        ref_time = datetime(2026, 9, 17, 12, 0)
        t_recent = ref_time - timedelta(hours=2)      # in 5h and 7d
        t_yesterday = ref_time - timedelta(days=2)   # outside 5h, inside 7d
        t_old = ref_time - timedelta(days=10)        # outside 7d

        s_recent = SessionSummary(
            session_id="s-recent",
            source="cli",
            file_path="/tmp/sr.db",
            title="Recent",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t_recent,
            end_time=t_recent,
            step_count=1,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200),
            steps=[StepRecord(step_idx=1, timestamp=t_recent, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200))]
        )
        s_mid = SessionSummary(
            session_id="s-mid",
            source="cli",
            file_path="/tmp/sm.db",
            title="Mid",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t_yesterday,
            end_time=t_yesterday,
            step_count=1,
            usage=TokenUsage(prompt_tokens=2000, cached_tokens=8000, output_tokens=400),
            steps=[StepRecord(step_idx=1, timestamp=t_yesterday, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=2000, cached_tokens=8000, output_tokens=400))]
        )
        s_old = SessionSummary(
            session_id="s-old",
            source="cli",
            file_path="/tmp/so.db",
            title="Old",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t_old,
            end_time=t_old,
            step_count=1,
            usage=TokenUsage(prompt_tokens=5000, cached_tokens=10000, output_tokens=1000),
            steps=[StepRecord(step_idx=1, timestamp=t_old, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=5000, cached_tokens=10000, output_tokens=1000))]
        )

        w_5h, w_7d, tier = LimitsCalculator.compute([s_recent, s_mid, s_old], tier_name="pro", now=ref_time)
        # 5h should only include s_recent (total: 5200 tokens)
        self.assertEqual(w_5h.usage.total_tokens, 5200)
        self.assertEqual(w_5h.step_count, 1)
        self.assertEqual(w_5h.session_count, 1)

        # 7d should include s_recent + s_mid (5200 + 10400 = 15600 tokens)
        self.assertEqual(w_7d.usage.total_tokens, 15600)
        self.assertEqual(w_7d.step_count, 2)
        self.assertEqual(w_7d.session_count, 2)

        # Pro limits
        self.assertEqual(w_5h.limit_tokens, 100_000_000)
        self.assertEqual(w_7d.limit_tokens, 750_000_000)
        self.assertIsNotNone(w_7d.refresh_time)
        self.assertEqual(w_7d.refresh_time, t_yesterday + timedelta(days=7))

    def test_cmd_limits_rendering_and_exports(self):
        import argparse
        import io
        import json
        import sys
        from agy_usage.cli import cmd_limits

        t = datetime.now()
        session = SessionSummary(
            session_id="s-limits",
            source="cli",
            file_path="/tmp/sl.db",
            title="Limits Test",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200),
            steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200))]
        )

        # Terminal render
        args = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, json=False, csv=False)
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            cmd_limits([session], args)
        finally:
            sys.stdout = old_stdout

        out = buf.getvalue()
        self.assertIn("ANTIGRAVITY QUOTA & RATE LIMITS MONITOR", out)
        self.assertIn("5-HOUR REMAINING", out)
        self.assertIn("WEEKLY REMAINING", out)
        self.assertIn("ANTIGRAVITY TIER LIMITS", out)

        # JSON export
        args_json = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, json=True, csv=False)
        buf_json = io.StringIO()
        try:
            sys.stdout = buf_json
            cmd_limits([session], args_json)
        finally:
            sys.stdout = old_stdout

        data = json.loads(buf_json.getvalue())
        self.assertIn("five_hour_window", data)
        self.assertIn("weekly_budget", data)
        self.assertEqual(data["tier"], "pro")

        # CSV export
        args_csv = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, json=False, csv=True)
        buf_csv = io.StringIO()
        try:
            sys.stdout = buf_csv
            cmd_limits([session], args_csv)
        finally:
            sys.stdout = old_stdout

        csv_out = buf_csv.getvalue()
        self.assertIn("5-Hour Sprint", csv_out)
        self.assertIn("Weekly Cap", csv_out)

    def test_cmd_limits_used_percent_uses_inverted_semantics(self):
        """BUG-011: in the limits gauge rows, '(x.x% used)' must color high usage
        warn/danger instead of green, while the '(x.x% exact' remaining fragment
        keeps the unchanged thresholds."""
        import argparse
        import io
        import re
        import sys
        from agy_usage.cli import cmd_limits

        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

        t = datetime.now()
        # 81M tokens against the pro-tier 100M 5h / 750M weekly Gemini caps:
        # the 5h window sits at 81.0% used (previously rendered green — the bug),
        # the weekly window at 10.8% used (healthy green under inverted semantics).
        session = SessionSummary(
            session_id="s-usage-bug011",
            source="cli",
            file_path="/tmp/bug011-nonexistent.db",
            title="BUG-011 Used Percent Color",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=TokenUsage(prompt_tokens=80_000_000, cached_tokens=0, output_tokens=1_000_000),
            steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=80_000_000, cached_tokens=0, output_tokens=1_000_000))],
        )
        args = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, json=False, csv=False)
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            cmd_limits([session], args)
        finally:
            sys.stdout = old_stdout

        out = buf.getvalue()
        gauge_lines = [ln for ln in out.splitlines() if "exact |" in ln]
        self.assertEqual(len(gauge_lines), 5)  # gemini weekly/5h, claude weekly/5h, argon weekly

        any_high_used_seen = False
        for ln in gauge_lines:
            plain = strip_ansi(ln)
            rem_val = float(re.search(r"\((\d+\.\d)% exact", plain).group(1))
            used_val = float(re.search(r"\| (\d+\.\d)% used\)", plain).group(1))
            self.assertAlmostEqual(rem_val + used_val, 100.0, delta=0.1)
            # The used fragment must match the inverted path, wrapped before " used)".
            used_fragment = format_percent(used_val, high_is_good=False) + " used)"
            self.assertIn(used_fragment, ln)
            if used_val >= 75.0:
                any_high_used_seen = True
                self.assertNotIn(Colors.green(f"{used_val:.1f}%"), ln)
        self.assertTrue(any_high_used_seen)

        # High usage: Gemini 5h at 81.0% used renders red (was green before the fix).
        self.assertTrue(any(Colors.red("81.0%") in ln for ln in gauge_lines))
        self.assertFalse(any(Colors.green("81.0%") in ln for ln in gauge_lines))
        # Low usage: Gemini weekly at 10.8% used stays green (healthy).
        self.assertTrue(any(Colors.green("10.8%") + " used)" in ln for ln in gauge_lines))
        # Remaining fragments are unchanged: weekly 89.2% green, 5h 19.0% gray.
        self.assertTrue(any(Colors.green("89.2%") + " exact |" in ln for ln in gauge_lines))
        self.assertTrue(any(Colors.gray("19.0%") + " exact |" in ln for ln in gauge_lines))

    def test_cmd_statusline_formatting_and_exports(self):
        import argparse
        import io
        import json
        import sys
        from agy_usage.cli import cmd_statusline

        t = datetime.now()
        session = SessionSummary(
            session_id="s-status",
            source="cli",
            file_path="/tmp/ss.db",
            title="Statusline Test",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200),
            steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200))]
        )

        old_stdout = sys.stdout

        # 1. Default ANSI
        args_def = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=False, format=None, no_color=False)
        buf = io.StringIO()
        try:
            sys.stdout = buf
            cmd_statusline([session], args_def)
        finally:
            sys.stdout = old_stdout
        out = buf.getvalue().strip()
        self.assertIn("5h:", out)
        self.assertIn("Wk:", out)
        self.assertIn("Today:", out)

        # 2. Short
        args_short = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=True, plain=False, tmux=False, json=False, format=None, no_color=False)
        buf_s = io.StringIO()
        try:
            sys.stdout = buf_s
            cmd_statusline([session], args_short)
        finally:
            sys.stdout = old_stdout
        out_s = buf_s.getvalue().strip()
        self.assertIn("5h:", out_s)
        self.assertIn("Wk:", out_s)

        # 3. Plain
        args_plain = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=True, tmux=False, json=False, format=None, no_color=False)
        buf_p = io.StringIO()
        try:
            sys.stdout = buf_p
            cmd_statusline([session], args_plain)
        finally:
            sys.stdout = old_stdout
        out_p = buf_p.getvalue().strip()
        self.assertIn("[OK]", out_p)
        self.assertIn("5h:", out_p)

        # 4. Tmux
        args_tmux = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=True, json=False, format=None, no_color=False)
        buf_t = io.StringIO()
        try:
            sys.stdout = buf_t
            cmd_statusline([session], args_tmux)
        finally:
            sys.stdout = old_stdout
        out_t = buf_t.getvalue().strip()
        self.assertIn("#[fg=", out_t)
        self.assertIn("5h:", out_t)

        # 5. JSON
        args_json = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=True, format=None, no_color=False)
        buf_j = io.StringIO()
        try:
            sys.stdout = buf_j
            cmd_statusline([session], args_json)
        finally:
            sys.stdout = old_stdout
        data = json.loads(buf_j.getvalue())
        self.assertEqual(data["status"], "healthy")
        self.assertIn("five_hour_pct", data)
        self.assertIn("weekly_pct", data)
        self.assertIn("today_cost_usd", data)

        # 6. Format template
        args_fmt = argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=False, format="{status} 5h:{5h_pct}% | Wk:{wk_pct}%", no_color=False)
        buf_f = io.StringIO()
        try:
            sys.stdout = buf_f
            cmd_statusline([session], args_fmt)
        finally:
            sys.stdout = old_stdout
        out_f = buf_f.getvalue().strip()
        self.assertIn("5h:100%", out_f)
        self.assertIn("Wk:100%", out_f)


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


class TestQuotaCacheFreshness(unittest.TestCase):
    """Regression tests for BUG-005: the on-disk quota cache is a fallback,
    not a live feed — a stale cache must be rejected instead of rendered as
    live server data, and expired buckets must not supply quota fractions."""

    def _temp_cache_path(self):
        import os
        import shutil
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="agy_quota_cache_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        return os.path.join(tmpdir, "agy_usage_quota_cache.json")

    def _write_cache(self, path, fetched_at_iso, reset_time_iso, remaining=0.42):
        import json

        payload = {
            "fetched_at": fetched_at_iso,
            "user_tier_id": "pro",
            "user_tier_name": "Google AI Pro",
            "buckets": {
                "gemini-5h": {
                    "bucketId": "gemini-5h",
                    "displayName": "5-Hour Limit",
                    "description": "Gemini 5-hour sprint quota",
                    "window": "5h",
                    "remainingFraction": remaining,
                    "resetTime": reset_time_iso,
                },
            },
        }
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp)
        return payload

    def test_fresh_cache_is_used_when_server_unreachable(self):
        from datetime import timedelta, timezone
        from unittest import mock

        from agy_usage import limits

        cache_path = self._temp_cache_path()
        now_utc = datetime.now(timezone.utc)
        payload = self._write_cache(
            cache_path,
            now_utc.isoformat(),  # fetched right now -> within the ~60s TTL
            (now_utc + timedelta(hours=2)).isoformat(),
        )

        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", cache_path), \
                mock.patch.object(limits.subprocess, "check_output", side_effect=OSError("ps disabled")):
            result = limits.fetch_live_quota_summary()

        self.assertEqual(result, payload)

    def test_stale_cache_is_rejected_and_not_treated_as_live(self):
        from datetime import timedelta, timezone
        from unittest import mock

        from agy_usage import limits

        cache_path = self._temp_cache_path()
        now_utc = datetime.now(timezone.utc)
        self._write_cache(
            cache_path,
            (now_utc - timedelta(seconds=90)).isoformat(),  # older than the ~60s TTL
            (now_utc + timedelta(hours=2)).isoformat(),
        )

        now_local = datetime.now()
        session = SessionSummary(
            session_id="s-cache",
            source="cli",
            file_path="/tmp/nonexistent-cache.db",
            title="Cache",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=now_local,
            end_time=now_local,
            step_count=1,
            usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200),
            steps=[
                StepRecord(step_idx=1, timestamp=now_local, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200))
            ],
        )

        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", cache_path), \
                mock.patch.object(limits.subprocess, "check_output", side_effect=OSError("ps disabled")):
            self.assertIsNone(limits.fetch_live_quota_summary())
            pools = limits.LimitsCalculator.compute_all_pools(
                [session], now=now_local, use_live_server=True
            )

        # Downstream must see the server as not live, not hours-old data as live.
        self.assertFalse(pools.live_synced)
        self.assertIsNone(pools.gemini_5h.server_remaining_fraction)

    def test_bucket_with_past_reset_time_is_ignored(self):
        from datetime import timedelta, timezone

        from agy_usage.limits import LimitsCalculator

        now = datetime.now()
        now_utc = datetime.now(timezone.utc)
        past_reset = (now_utc - timedelta(minutes=5)).isoformat()
        future_reset = (now_utc + timedelta(hours=2)).isoformat()
        items = []

        w_5h, _ = LimitsCalculator._compute_windows_from_items(
            items, current_time=now, limit_5h=100_000_000, limit_7d=750_000_000, pool="gemini"
        )
        expired = LimitsCalculator._apply_bucket_calibration(
            w_5h,
            {
                "bucketId": "gemini-5h",
                "remainingFraction": 0.1,
                "resetTime": past_reset,  # cycle already reset
                "description": "expired cycle",
            },
            now,
            timedelta(hours=5),
            items,
            False,
        )
        self.assertIsNone(expired.server_remaining_fraction)
        self.assertIsNone(expired.server_description)
        self.assertEqual(expired.end_time, now)  # past resetTime not grafted onto the window
        self.assertAlmostEqual(expired.pct_remaining, 100.0)  # old code would report 90% used

        live = LimitsCalculator._apply_bucket_calibration(
            w_5h,
            {
                "bucketId": "gemini-5h",
                "remainingFraction": 0.42,
                "resetTime": future_reset,  # cycle still running
                "description": "live cycle",
            },
            now,
            timedelta(hours=5),
            items,
            False,
        )
        self.assertAlmostEqual(live.server_remaining_fraction, 0.42)
        self.assertEqual(
            live.refresh_time,
            (now_utc + timedelta(hours=2)).astimezone().replace(tzinfo=None),
        )


class TestArgparseGlobalFlags(unittest.TestCase):
    """Regression tests for BUG-001/BUG-002: shared flags (--json, --csv,
    --no-color, --dir, --cli-only, --ide-only) must be accepted both before
    and after the subcommand, and explicit root-level values must survive
    subparser namespace merging."""

    def _parse(self, argv):
        from agy_usage.cli import parse_args

        return parse_args(argv)

    def test_flags_after_subcommand_accepted(self):
        args = self._parse(["sessions", "--no-color"])
        self.assertTrue(args.no_color)

        args = self._parse(["heatmap", "--json"])
        self.assertTrue(args.json)

        args = self._parse(["session", "abc123", "--csv"])
        self.assertTrue(args.csv)
        self.assertEqual(args.session_id, "abc123")

    def test_flags_before_subcommand_survive(self):
        # BUG-002: subparser defaults must not clobber root-level values.
        self.assertTrue(self._parse(["--json", "limits"]).json)
        self.assertTrue(self._parse(["--csv", "statusline"]).csv)

    def test_data_source_flags_both_positions(self):
        args = self._parse(["--dir", "/x", "sessions"])
        self.assertEqual(args.custom_dir, "/x")

        args = self._parse(["sessions", "--dir", "/y"])
        self.assertEqual(args.custom_dir, "/y")

        args = self._parse(["sessions", "--cli-only"])
        self.assertTrue(args.cli_only)

    def test_defaults_still_exist_when_unset(self):
        args = self._parse(["sessions"])
        self.assertFalse(args.json)
        self.assertFalse(args.csv)
        self.assertFalse(args.no_color)
        self.assertIsNone(args.custom_dir)
        self.assertFalse(args.cli_only)
        self.assertFalse(args.ide_only)

    def test_all_subcommands_accept_shared_flags(self):
        for sub in (
            "overview",
            "heatmap",
            "sessions",
            "current",
            "session",
            "weekly",
            "monthly",
            "models",
            "live",
            "limits",
            "statusline",
        ):
            args = self._parse([sub, "--json"])
            self.assertTrue(args.json, f"--json rejected after subcommand {sub}")


class TestParseSessionDB(unittest.TestCase):
    """Regression tests for BUG-007: the SQLite filename must be percent-quoted
    into the URI, the DB must open read-only, and there must be no silent
    read-write fallback."""

    @staticmethod
    def _varint(value: int) -> bytes:
        out = bytearray()
        while True:
            b = value & 0x7F
            value >>= 7
            if value:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def _write_session_db(self, path):
        """Create a minimal real session DB: one step whose `metadata`
        protobuf carries a Timestamp (field 1) and UsageMetadata (field 9)
        with prompt=1000, output=200."""
        ts = bytes([0x08]) + self._varint(1_751_234_567)
        usage = bytes([0x10, 0xE8, 0x07, 0x18, 0xC8, 0x01])  # {2:1000, 3:200}
        metadata = bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute(
                "INSERT INTO steps (idx, step_type, step_payload, metadata) "
                "VALUES (1, 'user', '', ?)",
                (metadata,),
            )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_hash_and_question_mark_paths_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            for filename in ("sess#ion.db", "sess?ion.db"):
                path = self._write_session_db(os.path.join(tmp, filename))
                summary = AntigravityParser.parse_session_db(path)
                self.assertIsNotNone(summary, f"parse failed for path: {path}")
                self.assertEqual(summary.session_id, filename.replace(".db", ""))
                self.assertEqual(summary.usage.total_tokens, 1200)

    def test_percent_path_parses_via_quoted_uri_single_connect(self):
        # '%41' percent-decodes to 'A': the old unquoted URI resolved to a
        # phantom 'sessAion.db' and only ever parsed via the silent read-write
        # fallback. The quoted URI (%25) must hit the real file with exactly
        # one read-only connect attempt.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_session_db(os.path.join(tmp, "sess%41ion.db"))
            with mock.patch("sqlite3.connect", wraps=sqlite3.connect) as m:
                summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.usage.total_tokens, 1200)
            self.assertEqual(m.call_count, 1)  # no read-write fallback attempt
            uri = m.call_args.args[0]
            self.assertEqual(m.call_args.kwargs.get("uri"), True)
            self.assertTrue(uri.startswith("file:"))
            self.assertIn("sess%2541ion.db", uri)
            self.assertIn("?mode=ro", uri)

    def test_vanished_db_returns_none_and_creates_no_file(self):
        # os.path.isfile() answered "exists" but the DB is gone by connect
        # time: must return None, must not create the file (read-only mode)
        # and must not retry via the old read-write fallback (TOCTOU).
        with tempfile.TemporaryDirectory() as tmp:
            ghost = os.path.join(tmp, "ghost.db")
            with mock.patch("agy_usage.parser.os.path.isfile", return_value=True):
                self.assertIsNone(AntigravityParser.parse_session_db(ghost))
            self.assertFalse(os.path.exists(ghost))
            self.assertFalse(os.path.exists(ghost + "-journal"))
            self.assertFalse(os.path.exists(ghost + "-wal"))


if __name__ == "__main__":
    unittest.main()

