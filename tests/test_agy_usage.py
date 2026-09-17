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
        self.assertIn("TOKEN USAGE BREAKDOWN", out)
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


if __name__ == "__main__":
    unittest.main()
