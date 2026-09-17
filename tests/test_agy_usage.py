"""Unit tests for Antigravity Model Usage Tracker."""

import unittest
from datetime import date, datetime

from agy_usage.aggregator import UsageAggregator
from agy_usage.display import (
    format_duration,
    format_percent,
    format_tokens,
    render_bar,
    render_sparkline,
    render_table,
    strip_ansi,
)
from agy_usage.exporter import UsageExporter
from agy_usage.heatmap import TerminalHeatmap
from agy_usage.models import DailyUsage, SessionSummary, StepRecord, TokenUsage
from agy_usage.parser import (
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

    def test_formatting_utilities(self):
        self.assertEqual(format_tokens(1_500_000), "1.50M")
        self.assertEqual(format_tokens(45_200), "45.2K")
        self.assertEqual(format_tokens(500), "500")
        self.assertEqual(format_duration(65), "1m 5s")
        self.assertEqual(format_duration(3665), "1h 1m")

    def test_sparkline(self):
        spark = render_sparkline([10, 20, 50, 80, 100])
        self.assertEqual(len(spark), 5)

    def test_table_rendering(self):
        headers = ["Col A", "Col B"]
        rows = [["Value 1", "Value 2"]]
        tbl = render_table(headers, rows)
        self.assertIn("Col A", tbl)
        self.assertIn("Value 1", tbl)

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
        self.assertIsNone(parse_token_amount(None))
        self.assertIsNone(parse_token_amount(""))
        self.assertIsNone(parse_token_amount("invalid"))

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
        self.assertEqual(w_5h.limit_tokens, 50_000_000)
        self.assertEqual(w_7d.limit_tokens, 250_000_000)

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
        self.assertIn("5-HOUR CONSUMED", out)
        self.assertIn("5-HOUR SPRINT QUOTA", out)
        self.assertIn("WEEKLY HARD CAP", out)
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


if __name__ == "__main__":
    unittest.main()
