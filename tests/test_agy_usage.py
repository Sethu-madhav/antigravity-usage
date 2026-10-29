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
        # COLORTERM no longer enables color for piped stdout (BUG-009):
        # even exported by the parent shell (VS Code, Warp, CI), it must not
        # flip enablement — enablement is TTY + NO_COLOR only.
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, {**bare_env, "COLORTERM": "truecolor"}, clear=True):
                self.assertFalse(Colors.enabled())
                self.assertFalse(HeatmapColor.supports_color())

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


class TestPipedOutputColorGating(unittest.TestCase):
    """Regression tests for BUG-009: piped (non-tty) output must never contain
    ANSI escapes, even when COLORTERM is exported by the parent shell (VS Code
    integrated terminals, Warp, CI). Color enablement is gated on
    sys.stdout.isatty() and NO_COLOR only; COLORTERM may exclusively UPGRADE
    color depth (truecolor vs 256-color) and never flips enablement. The
    BUG-004 override switch (Colors.set_override) composes: forced values win
    over tty/env detection."""

    def _hermetic_env(self, **extra):
        """Runner env without inherited color variables, plus explicit extras.

        BUG-004's override-based tests force color via the switch; these tests
        exercise detection, so the inherited COLORTERM/NO_COLOR of the runner
        must be neutralized."""
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(extra)
        return env

    def _daily(self):
        return {
            date(2026, 9, 1): DailyUsage(
                date=date(2026, 9, 1), usage=TokenUsage(prompt_tokens=1000)
            )
        }

    def test_piped_render_has_no_ansi_even_with_colorterm(self):
        """Non-tty render with COLORTERM=truecolor set: zero ANSI escapes —
        the `agy-usage overview > report.txt` repro at render level. The
        depth detector still says truecolor, but enablement gates it out."""
        Colors.set_override(None)
        self.addCleanup(Colors.set_override, None)
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, self._hermetic_env(COLORTERM="truecolor"), clear=True):
                self.assertFalse(Colors.enabled())
                self.assertFalse(HeatmapColor.supports_color())
                self.assertTrue(HeatmapColor.supports_truecolor())  # depth, not enablement
                rendered = TerminalHeatmap(
                    self._daily(), metric="total", weeks=8, end_date=date(2026, 9, 10)
                ).render()
                self.assertNotIn("\033", rendered)
                self.assertNotIn("\033", Colors.bold("plain"))
                self.assertNotIn("\033", format_percent(92.8))

    def test_piped_overview_via_main_writes_no_ansi(self):
        """End-to-end: `agy-usage overview > report.txt` with COLORTERM exported
        (piped StringIO stdout isatty()=False) writes zero escape bytes."""
        self.addCleanup(Colors.set_override, None)
        fake = SessionSummary(
            session_id="abc123def456",
            source="cli",
            file_path="/tmp/fake.db",
            title="Piped repro",
            workspace="/ws/repro",
            models=["gemini-3-pro"],
            start_time=datetime(2026, 10, 26, 10, 0, 0),
            end_time=datetime(2026, 10, 26, 10, 30, 0),
            step_count=4,
            usage=TokenUsage(prompt_tokens=5000, cached_tokens=20000, output_tokens=1000),
        )
        with mock.patch("agy_usage.cli.get_sessions", return_value=[fake]):
            with mock.patch.dict(os.environ, self._hermetic_env(COLORTERM="truecolor"), clear=True):
                with redirect_stdout(io.StringIO()) as captured:
                    result = cli_main(["overview"])
        self.assertEqual(result, 0)
        out = captured.getvalue()
        self.assertIn("ACTIVITY HEATMAP", out)  # real render path reached
        self.assertNotIn("\033", out)

    def test_no_color_env_beats_colorterm_on_a_tty(self):
        """NO_COLOR=1 disables color even when COLORTERM="truecolor" and stdout
        is a (simulated) tty."""
        Colors.set_override(None)
        self.addCleanup(Colors.set_override, None)
        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(
                os.environ, self._hermetic_env(COLORTERM="truecolor", NO_COLOR="1"), clear=True
            ):
                self.assertFalse(Colors.enabled())
                self.assertFalse(HeatmapColor.supports_color())
                rendered = TerminalHeatmap(
                    self._daily(), metric="total", weeks=8, end_date=date(2026, 9, 10)
                ).render()
                self.assertNotIn("\033", rendered)

    def test_colorterm_only_upgrades_depth_never_enablement(self):
        """With color enabled via the BUG-004 switch, COLORTERM=truecolor selects
        the truecolor palette while its absence falls back to 256-color; on a
        piped stdout enablement is False with and without COLORTERM alike."""
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)
        with mock.patch.dict(os.environ, self._hermetic_env(TERM="xterm"), clear=True):
            with mock.patch("sys.platform", "linux"):
                # No COLORTERM: 256-color palette.
                self.assertFalse(HeatmapColor.supports_truecolor())
                self.assertIn("\033[38;5;", HeatmapColor.cell(2))
                # COLORTERM=truecolor: same cell upgraded to 24-bit RGB.
                with mock.patch.dict(os.environ, {"COLORTERM": "truecolor"}):
                    self.assertTrue(HeatmapColor.supports_truecolor())
                    self.assertIn("\033[38;2;", HeatmapColor.cell(2))
                    self.assertTrue(Colors.enabled())  # forced by the switch, not by COLORTERM
        # Enablement on piped stdout is identical with and without COLORTERM.
        Colors.set_override(None)
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, self._hermetic_env(), clear=True):
                self.assertFalse(Colors.enabled())
            with mock.patch.dict(os.environ, self._hermetic_env(COLORTERM="truecolor"), clear=True):
                self.assertFalse(Colors.enabled())
        # ...and on a tty COLORTERM neither helps nor hurts: isatty decides.
        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(os.environ, self._hermetic_env(), clear=True):
                self.assertTrue(Colors.enabled())
            with mock.patch.dict(os.environ, self._hermetic_env(COLORTERM="truecolor"), clear=True):
                self.assertTrue(Colors.enabled())

    def test_bug004_override_switch_outranks_env_and_tty(self):
        """Composition guarantee kept from BUG-004: the forced switch wins over
        tty/env detection in both directions — piped stdout still renders with
        escapes when forced on, and a TTY with COLORTERM stays plain when the
        switch is forced off."""
        self.addCleanup(Colors.set_override, None)
        Colors.set_override(True)
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, self._hermetic_env(TERM="xterm"), clear=True):
                self.assertTrue(Colors.enabled())
                self.assertIn("\033", HeatmapColor.cell(2))
                rendered = TerminalHeatmap(
                    self._daily(), metric="total", weeks=8, end_date=date(2026, 9, 10)
                ).render()
                self.assertIn("\033[38;", rendered)
        Colors.set_override(False)
        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(os.environ, self._hermetic_env(COLORTERM="truecolor"), clear=True):
                self.assertFalse(Colors.enabled())
                rendered = TerminalHeatmap(
                    self._daily(), metric="total", weeks=8, end_date=date(2026, 9, 10)
                ).render()
                self.assertNotIn("\033", rendered)


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

        # Healthy (<60%)
        b, _ = get_status_badge(make_stats(10, 100), make_stats(10, 100))
        self.assertEqual(b, "🟢")
        b, _ = get_status_badge(make_stats(59, 100), make_stats(10, 100))
        self.assertEqual(b, "🟢")

        # Moderate load (>=60% and <80%, BUG-012)
        b, msg = get_status_badge(make_stats(60, 100), make_stats(10, 100))
        self.assertEqual(b, "🟡")
        self.assertIn("MODERATE LOAD", strip_ansi(msg))

        b, msg = get_status_badge(make_stats(75, 100), make_stats(10, 100))
        self.assertEqual(b, "🟡")
        self.assertIn("MODERATE LOAD", strip_ansi(msg))

        # High load (>=80%)
        b, msg = get_status_badge(make_stats(80, 100), make_stats(20, 100))
        self.assertEqual(b, "🟡")
        self.assertIn("HIGH LOAD WARNING", strip_ansi(msg))

        b, msg = get_status_badge(make_stats(85, 100), make_stats(20, 100))
        self.assertEqual(b, "🟡")
        self.assertIn("HIGH LOAD WARNING", strip_ansi(msg))

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

    def test_badge_thresholds_agreement_bug012(self):
        """BUG-012: limits and statusline must use shared threshold constants
        (WARN_MODERATE=60, WARN_HIGH=80, CRIT=100) and agree on health badges."""
        import argparse
        import io
        import json
        import sys
        from agy_usage.cli import cmd_limits, cmd_statusline
        from agy_usage.limits import CRIT, WARN_HIGH, WARN_MODERATE

        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

        # 1. Verify shared constants
        self.assertEqual(WARN_MODERATE, 60.0)
        self.assertEqual(WARN_HIGH, 80.0)
        self.assertEqual(CRIT, 100.0)

        t = datetime.now()

        def make_session(tokens_5h: int) -> SessionSummary:
            # Pro tier 5h limit is 100M tokens
            return SessionSummary(
                session_id="s-bug012",
                source="cli",
                file_path="/tmp/bug012.db",
                title="BUG-012 Badge Agreement",
                workspace="ws",
                models=["gemini-3.8-flash"],
                start_time=t,
                end_time=t,
                step_count=1,
                usage=TokenUsage(prompt_tokens=tokens_5h, cached_tokens=0, output_tokens=0),
                steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=TokenUsage(prompt_tokens=tokens_5h, cached_tokens=0, output_tokens=0))],
            )

        # Case A: 65% used (Moderate load: between 60% and 80%)
        # Previously BUG-012 caused limits to show 🟡 and statusline to show 🟢!
        s_65 = make_session(65_000_000)

        # statusline --json
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            cmd_statusline([s_65], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=True, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        data_sl = json.loads(buf.getvalue())

        # limits --json
        buf_lim = io.StringIO()
        try:
            sys.stdout = buf_lim
            cmd_limits([s_65], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, json=True, csv=False))
        finally:
            sys.stdout = old_stdout
        data_lim = json.loads(buf_lim.getvalue())

        # Both must agree on the 🟡 badge and warning status
        self.assertEqual(data_sl["badge"], "🟡")
        self.assertEqual(data_sl["status"], "warning")
        self.assertEqual(data_lim["status"], "🟡")
        self.assertEqual(data_sl["badge"], data_lim["status"])

        # statusline plain mode must show [WARN], not [OK]
        buf_plain = io.StringIO()
        try:
            sys.stdout = buf_plain
            cmd_statusline([s_65], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=True, tmux=False, json=False, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        out_plain = buf_plain.getvalue()
        self.assertTrue(out_plain.startswith("[WARN]"))
        self.assertNotIn("[OK]", out_plain)

        # statusline colored mode: 5h segment must be yellow, badge 🟡
        buf_ansi = io.StringIO()
        try:
            sys.stdout = buf_ansi
            cmd_statusline([s_65], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=False, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        out_ansi = buf_ansi.getvalue()
        self.assertIn("🟡", out_ansi)
        self.assertIn(Colors.yellow("5h:"), out_ansi)

        # Case B: 20% used (Healthy <60%)
        s_20 = make_session(20_000_000)
        buf_20 = io.StringIO()
        try:
            sys.stdout = buf_20
            cmd_statusline([s_20], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=True, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        data_20 = json.loads(buf_20.getvalue())
        self.assertEqual(data_20["badge"], "🟢")
        self.assertEqual(data_20["status"], "healthy")

        # Case C: 85% used (High load >=80%)
        s_85 = make_session(85_000_000)
        buf_85 = io.StringIO()
        try:
            sys.stdout = buf_85
            cmd_statusline([s_85], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=True, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        data_85 = json.loads(buf_85.getvalue())
        self.assertEqual(data_85["badge"], "🟡")
        self.assertEqual(data_85["status"], "warning")

        # Case D: 100% used (Exceeded / CRIT)
        s_100 = make_session(100_000_000)
        buf_100 = io.StringIO()
        try:
            sys.stdout = buf_100
            cmd_statusline([s_100], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=True, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        data_100 = json.loads(buf_100.getvalue())
        self.assertEqual(data_100["badge"], "🔴")
        self.assertEqual(data_100["status"], "critical")

        # Exact boundary tests: 60.0% (WARN_MODERATE) and 80.0% (WARN_HIGH) through cmd_statusline
        s_60 = make_session(60_000_000)
        buf_60 = io.StringIO()
        try:
            sys.stdout = buf_60
            cmd_statusline([s_60], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=True, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        data_60 = json.loads(buf_60.getvalue())
        self.assertEqual(data_60["badge"], "🟡")
        self.assertEqual(data_60["status"], "warning")

        s_80 = make_session(80_000_000)
        buf_80 = io.StringIO()
        try:
            sys.stdout = buf_80
            cmd_statusline([s_80], argparse.Namespace(tier="pro", five_h_limit=None, weekly_limit=None, short=False, plain=False, tmux=False, json=True, format=None, no_color=False))
        finally:
            sys.stdout = old_stdout
        data_80 = json.loads(buf_80.getvalue())
        self.assertEqual(data_80["badge"], "🟡")
        self.assertEqual(data_80["status"], "warning")


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


class TestPartialSessionParse(unittest.TestCase):
    """Regression tests for BUG-008: one transient SQLite error or one corrupt
    step row must not discard the metadata/steps already parsed from a session.
    """

    @staticmethod
    def _varint(value):
        out = bytearray()
        while True:
            b = value & 0x7F
            value >>= 7
            if value:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    @classmethod
    def _len_field(cls, num, data):
        return cls._varint((num << 3) | 2) + cls._varint(len(data)) + data

    @classmethod
    def _varint_field(cls, num, value):
        return cls._varint(num << 3) + cls._varint(value)

    @classmethod
    def _timestamp(cls, seconds):
        return cls._varint_field(1, seconds)

    @classmethod
    def _usage(cls, prompt, output):
        return cls._varint_field(2, prompt) + cls._varint_field(3, output)

    def _write_db(self, path, rows, workspace="file:///Users/me/proj", title=None):
        """Create a minimal DB with a workspace blob, an optional user prompt,
        and `rows` of (idx, metadata) steps."""
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")
            ws_blob = self._len_field(1, self._len_field(1, workspace.encode()))
            ws_blob += self._len_field(2, self._timestamp(1_751_000_000))
            conn.execute("INSERT INTO trajectory_metadata_blob (data) VALUES (?)", (ws_blob,))
            for idx, metadata in rows:
                payload = (
                    self._len_field(19, self._len_field(2, title.encode()))
                    if title and idx == 1
                    else b""
                )
                conn.execute(
                    "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (?, ?, ?, ?)",
                    (idx, "user", payload, metadata),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_corrupt_row_keeps_prior_and_later_steps(self):
        ts = self._timestamp(1_751_234_567)
        meta = self._len_field(1, ts) + self._len_field(9, self._usage(1000, 200))
        bad_blob = b"corrupt-metadata"

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-corrupt.db"),
                [(1, meta), (2, bad_blob), (3, meta)],
                title="Diagnose the parser",
            )
            def flaky(data):
                if data == bad_blob:
                    raise ValueError("corrupt protobuf")
                return parse_proto_fields(data)

            with mock.patch("agy_usage.parser.parse_proto_fields", side_effect=flaky):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary, "corrupt row dropped the whole session")
            self.assertEqual(summary.session_id, "sess-corrupt")
            self.assertEqual(summary.title, "Diagnose the parser")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.step_count, 2)
            self.assertEqual(summary.usage.total_tokens, 2400)
            self.assertEqual([s.step_idx for s in summary.steps], [1, 3])

    def test_lock_during_step_read_returns_partial_summary(self):
        ts = self._timestamp(1_751_234_567)
        meta = self._len_field(1, ts) + self._len_field(9, self._usage(1000, 200))

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-locked.db"),
                [(1, meta)],
                title="Locked session",
            )
            summary = self._parse_with_locked_steps(path)

            self.assertIsNotNone(summary, "a transient lock dropped the whole session")
            self.assertEqual(summary.session_id, "sess-locked")
            self.assertEqual(summary.title, "Locked session")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.step_count, 0)
            self.assertEqual(summary.usage.total_tokens, 0)

    def _parse_with_sql_failure(self, path, marker, error):
        """Parse while any statement containing `marker` raises `error`."""
        real_connect = sqlite3.connect

        class FailingCursor:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *params):
                if marker in sql:
                    raise error
                return self._inner.execute(sql, *params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        class FailingConnection:
            def __init__(self, inner):
                self._inner = inner

            def cursor(self):
                return FailingCursor(self._inner.cursor())

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def fake_connect(*args, **kwargs):
            return FailingConnection(real_connect(*args, **kwargs))

        with mock.patch("agy_usage.parser.sqlite3.connect", side_effect=fake_connect):
            return AntigravityParser.parse_session_db(path)

    def test_lock_during_step_read_returns_partial_summary(self):
        ts = self._timestamp(1_751_234_567)
        meta = self._len_field(1, ts) + self._len_field(9, self._usage(1000, 200))

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-locked.db"),
                [(1, meta)],
                title="Locked session",
            )
            summary = self._parse_with_sql_failure(
                path,
                "metadata IS NOT NULL",
                sqlite3.OperationalError("database is locked"),
            )

            self.assertIsNotNone(summary, "a transient lock dropped the whole session")
            self.assertEqual(summary.session_id, "sess-locked")
            self.assertEqual(summary.title, "Locked session")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.step_count, 0)
            self.assertEqual(summary.usage.total_tokens, 0)

    def test_lock_during_title_query_is_contained(self):
        """Section 3's own guard swallows the lock, so parsing continues: the
        blob start time and the step totals must survive the failed query."""
        meta = self._len_field(1, self._timestamp(1_751_234_567))
        meta += self._len_field(9, self._usage(1000, 200))

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-title-locked.db"),
                [(1, meta)],
                title="Locked title",
            )
            summary = self._parse_with_sql_failure(
                path,
                "step_payload IS NOT NULL",
                sqlite3.OperationalError("database is locked"),
            )

            self.assertIsNotNone(summary)
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.title, "Session sess-tit")
            self.assertEqual(summary.step_count, 1)
            self.assertEqual(summary.usage.total_tokens, 1200)

    def test_corrupt_db_during_step_read_returns_partial_summary(self):
        """A plain sqlite3.DatabaseError (corrupt page) is not an
        OperationalError and must keep the already-parsed metadata too."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-corrupt-db.db"),
                [(1, self._len_field(9, self._usage(1000, 200)))],
                title="Corrupt read",
            )
            summary = self._parse_with_sql_failure(
                path,
                "metadata IS NOT NULL",
                sqlite3.DatabaseError("database disk image is malformed"),
            )

            self.assertIsNotNone(summary, "a corrupt read dropped the session")
            self.assertEqual(summary.title, "Corrupt read")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.start_time, datetime.fromtimestamp(1_751_000_000))
            self.assertEqual(summary.step_count, 0)

    def test_empty_steps_table_emits_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(os.path.join(tmp, "sess-empty.db"), [])
            summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.session_id, "sess-empty")
            self.assertEqual(summary.workspace, "proj")
            self.assertEqual(summary.step_count, 0)
            self.assertEqual(summary.usage.total_tokens, 0)

    def test_unreadable_mtime_skips_untimed_rows(self):
        """If the DB vanishes, steps without timestamps are skipped instead of
        crashing the parse; timed rows still count."""
        timed = self._len_field(1, self._timestamp(1_751_234_567))
        timed += self._len_field(9, self._usage(1000, 200))
        untimed = self._len_field(9, self._usage(500, 100))

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-no-mtime.db"),
                [(1, timed), (2, untimed)],
            )
            with mock.patch("agy_usage.parser.os.path.getmtime", side_effect=OSError("gone")):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            self.assertEqual(summary.step_count, 1)
            self.assertEqual(summary.usage.total_tokens, 1200)
            self.assertEqual([s.step_idx for s in summary.steps], [1])

    def test_lock_before_validation_returns_none(self):
        """If SQLite fails before the DB is known to be a session, there is no
        partial data worth surfacing - keep the old "skip it" behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "sess-unreadable.db"),
                [(1, self._len_field(9, self._usage(10, 5)))],
            )
            summary = self._parse_with_sql_failure(
                path,
                "sqlite_master",
                sqlite3.OperationalError("database is locked"),
            )
            self.assertIsNone(summary)


class TestGenMetadataModelAttribution(unittest.TestCase):
    """Regression tests for BUG-010: gen_metadata rows whose model name cannot
    be resolved (empty, M-placeholder, bare enum number) must neither enter
    ``known_models`` nor become attribution anchors — otherwise a phantom
    ``gemini-3.8-flash`` model with zero evidence distorts the model
    distribution and per-model cost. Legitimately named rows (including
    aliased names) keep their previous attribution exactly."""

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

    def _pb_str(self, field_num: int, value: str) -> bytes:
        payload = value.encode("utf-8")
        return self._varint((field_num << 3) | 2) + self._varint(len(payload)) + payload

    def _gen_metadata_row(self, idx, model_name="", last_step_idx=None, model_enum=None):
        """Build a gen_metadata blob: field 1 = {19: model name, 20: {key, val}}."""
        f20 = b""
        if last_step_idx is not None:
            f20 += self._pb_str(1, "last_step_index") + self._pb_str(2, str(last_step_idx))
        if model_enum is not None:
            f20 += self._pb_str(1, "model_enum") + self._pb_str(2, model_enum)
        parts = []
        if model_name:
            parts.append(self._pb_str(19, model_name))
        if f20:
            parts.append(self._varint((20 << 3) | 2) + self._varint(len(f20)) + f20)
        f1 = b"".join(parts)
        return idx, self._varint((1 << 3) | 2) + self._varint(len(f1)) + f1

    def _step_metadata(self, prompt: int, output: int) -> bytes:
        ts = bytes([0x08]) + self._varint(1_751_234_567)
        usage = self._varint(2 << 3) + self._varint(prompt)
        usage += self._varint(3 << 3) + self._varint(output)
        return bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage

    def _write_db(self, path, gen_rows, steps):
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                "step_payload BLOB, metadata BLOB)"
            )
            conn.execute(
                "CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB, size INTEGER)"
            )
            for idx, data in gen_rows:
                conn.execute(
                    "INSERT INTO gen_metadata (idx, data, size) VALUES (?, ?, ?)",
                    (idx, data, len(data)),
                )
            for idx, metadata in steps:
                conn.execute(
                    "INSERT INTO steps (idx, step_type, step_payload, metadata) "
                    "VALUES (?, 'user', '', ?)",
                    (idx, metadata),
                )
            conn.commit()
        finally:
            conn.close()
        return path

    def test_unresolvable_row_is_neither_known_model_nor_anchor(self):
        # Companion row (enum 298 -> "gemini-3.7-flash", last_step 3) anchors
        # step 4 directly; the unresolvable, name-less row (last_step 7) must
        # not anchor step 8 with a phantom "gemini-3.8-flash" — step 8 falls
        # back to the nearest earlier anchor instead. Total tokens stay intact.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "phantom-anchor.db"),
                gen_rows=[
                    self._gen_metadata_row(1, last_step_idx=3, model_enum="MODEL_PLACEHOLDER_M298"),
                    self._gen_metadata_row(2, last_step_idx=7),  # no name at all
                ],
                steps=[
                    (4, self._step_metadata(1000, 200)),
                    (8, self._step_metadata(2000, 400)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("gemini-3.8-flash", summary.models)
            self.assertEqual(summary.models, ["gemini-3.7-flash"])
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [(4, "gemini-3.7-flash"), (8, "gemini-3.7-flash")],
            )
            self.assertEqual(summary.usage.total_tokens, 3600)
            model_rows = [
                (m.model_name, m.usage.total_tokens)
                for m in UsageAggregator.aggregate_models([summary])
            ]
            self.assertEqual(model_rows, [("gemini-3.7-flash", 3600)])

    def test_unresolvable_row_falls_back_to_evidence_session_default(self):
        # Only an unresolvable row sits before step 6: it must anchor nothing,
        # so step 6 is attributed via the session default derived from the
        # (later) resolvable row "claude-sonnet-5-5" — not via a phantom
        # "gemini-3.8-flash" anchor.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "default-fallback.db"),
                gen_rows=[
                    self._gen_metadata_row(1, last_step_idx=5),  # no name, no enum
                    self._gen_metadata_row(2, model_name="claude-sonnet-5-5", last_step_idx=9),
                ],
                steps=[
                    (6, self._step_metadata(1000, 200)),
                    (10, self._step_metadata(2000, 400)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("gemini-3.8-flash", summary.models)
            self.assertEqual(summary.models, ["claude-sonnet-5-5"])
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [(6, "claude-sonnet-5-5"), (10, "claude-sonnet-5-5")],
            )
            self.assertEqual(summary.usage.total_tokens, 3600)

    def test_alias_named_rows_keep_original_attribution(self):
        # An aliased gen_metadata name ("gemini-3.8-flash-low" -> canonical
        # "gemini-3.8-flash") must still seed known_models and anchor steps
        # exactly as before the fix: step 1 anchored directly by the alias row,
        # step 8 via that same earlier anchor (previously it matched the
        # phantom anchor instead, but with the identical result).
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "alias-anchor.db"),
                gen_rows=[
                    self._gen_metadata_row(1, model_name="gemini-3.8-flash-low", last_step_idx=0),
                    self._gen_metadata_row(2, last_step_idx=7),  # unresolvable
                ],
                steps=[
                    (1, self._step_metadata(500, 100)),
                    (8, self._step_metadata(700, 100)),
                ],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.models, ["gemini-3.8-flash"])
            self.assertEqual(
                [(s.step_idx, s.model) for s in summary.steps],
                [(1, "gemini-3.8-flash"), (8, "gemini-3.8-flash")],
            )
            self.assertEqual(summary.usage.total_tokens, 1400)

    def test_bare_enum_number_is_not_a_model_name(self):
        # "MODEL_PLACEHOLDER_33" (unknown enum 33) used to resolve raw_name to
        # the bogus model "33" and anchor steps with it; it must be treated as
        # unresolvable and skipped.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "bare-enum.db"),
                gen_rows=[
                    self._gen_metadata_row(1, last_step_idx=2, model_enum="MODEL_PLACEHOLDER_33"),
                ],
                steps=[(3, self._step_metadata(900, 0))],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("33", summary.models)
            # Nothing resolvable, so tokens land on the documented session
            # default — never on the fabricated "33" model name.
            self.assertEqual(summary.models, ["gemini-3.8-flash"])
            self.assertEqual(
                [(s.step_idx, s.model, s.usage.total_tokens) for s in summary.steps],
                [(3, "gemini-3.8-flash", 900)],
            )
            self.assertEqual(summary.usage.total_tokens, 900)

    def test_field19_placeholder_is_not_a_model_name(self):
        # A placeholder string recorded in the model-name field (19) must not
        # seed known_models nor corrupt enum_to_model resolution.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_db(
                os.path.join(tmp, "placeholder-name.db"),
                gen_rows=[
                    self._gen_metadata_row(
                        1,
                        model_name="MODEL_PLACEHOLDER_M500",
                        last_step_idx=2,
                        model_enum="MODEL_PLACEHOLDER_M500",
                    ),
                ],
                steps=[(3, self._step_metadata(800, 0))],
            )
            summary = AntigravityParser.parse_session_db(path)
            self.assertIsNotNone(summary)
            self.assertNotIn("MODEL_PLACEHOLDER_M500", summary.models)
            self.assertEqual(summary.models, ["gemini-3.8-flash"])
            self.assertEqual(summary.usage.total_tokens, 800)

    def test_normalize_model_name_empty_is_unresolvable(self):
        # The contract BUG-010's fix rests on: empty/whitespace-only names
        # normalize to '' (unresolvable), while the legit alias/variant
        # mappings are unchanged.
        from agy_usage.models import normalize_model_name

        self.assertEqual(normalize_model_name(""), "")
        self.assertEqual(normalize_model_name("   "), "")
        self.assertEqual(normalize_model_name("gemini-3.8-flash-n"), "gemini-3.8-flash")
        self.assertEqual(normalize_model_name("claude-5.5"), "claude-5.5")
        self.assertEqual(normalize_model_name("gpt-oss-120b"), "gpt-oss-120b")


class TestVarintBounds(unittest.TestCase):
    """Regression tests for BUG-017: a varint is at most 10 bytes / 64 bits;
    longer runs are corruption and must not decode into astronomically large
    ints that flow into the aggregates."""

    MAX_U64 = (1 << 64) - 1

    def test_ten_byte_varint_still_decodes_to_max_u64(self):
        data = b"\xff" * 9 + b"\x01"
        val, idx = decode_varint(data, 0)
        self.assertEqual(val, self.MAX_U64)
        self.assertEqual(idx, 10)

    def test_tenth_byte_overflow_bits_are_masked_to_64_bits(self):
        val, idx = decode_varint(b"\xff" * 9 + b"\x7f", 0)
        self.assertEqual(val, self.MAX_U64)
        self.assertEqual(idx, 10)

    def test_overlong_varint_is_rejected(self):
        # 10th byte still carries a continuation bit: an 11th byte would be needed.
        with self.assertRaises(ValueError):
            decode_varint(b"\xff" * 10 + b"\x01", 0)
        # Far past the cap: previously decoded to a 2**140-scale int.
        with self.assertRaises(ValueError):
            decode_varint(b"\xff" * 20 + b"\x01", 0)

    def test_cap_is_measured_from_the_start_index(self):
        data = b"\x2a" + b"\xff" * 9 + b"\x01"
        val, idx = decode_varint(data, 1)
        self.assertEqual(val, self.MAX_U64)
        self.assertEqual(idx, 11)

    def test_corrupt_varint_value_stops_field_parsing_keeping_prior_fields(self):
        raw = bytes([0x08, 42]) + bytes([0x10]) + b"\xff" * 12 + bytes([0x18, 7])
        self.assertEqual(parse_proto_fields(raw), [(1, 0, 42)])

    def test_corrupt_tag_and_length_stop_field_parsing(self):
        overlong = b"\xff" * 12
        self.assertEqual(parse_proto_fields(overlong + bytes([0x01])), [])
        self.assertEqual(parse_proto_fields(bytes([0x08, 42, 0x12]) + overlong), [(1, 0, 42)])

    def test_corrupt_usage_blob_cannot_inflate_totals(self):
        # output (field 3) is valid; prompt (field 2) is a 20-byte varint.
        raw = bytes([0x18, 0xC8, 0x01]) + bytes([0x10]) + b"\xff" * 19 + b"\x01"
        usage = parse_usage_proto(raw)
        self.assertEqual(usage.output_tokens, 200)
        self.assertEqual(usage.prompt_tokens, 0)
        self.assertEqual(usage.total_tokens, 200)


def _write_usage_db(path, prompt=1000, output=200):
    """Minimal real session DB with one timed step carrying `prompt`/`output`."""
    def varint(value):
        out = bytearray()
        while True:
            b = value & 0x7F
            value >>= 7
            if value:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    ts = bytes([0x08]) + varint(1_751_234_567)
    usage = bytes([0x10]) + varint(prompt) + bytes([0x18]) + varint(output)
    metadata = bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
            "step_payload BLOB, metadata BLOB)"
        )
        conn.execute(
            "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (1, 'user', '', ?)",
            (metadata,),
        )
        conn.commit()
    finally:
        conn.close()
    return path


class TestScanSurvivesVanishingDb(unittest.TestCase):
    """Regression tests for BUG-016: Antigravity housekeeping can delete a DB
    while a scan is running; mtime lookups in the sort keys must not take the
    whole command down."""

    def test_scan_directory_skips_db_deleted_after_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept = _write_usage_db(os.path.join(tmp, "kept.db"))
            ghost = os.path.join(tmp, "ghost.db")  # globbed, then deleted before sorting
            with mock.patch("agy_usage.parser.glob.glob", return_value=[ghost, kept]):
                sessions = AntigravityParser.scan_directory(tmp, source="cli")

        self.assertEqual([s.session_id for s in sessions], ["kept"])

    def test_scan_all_sort_survives_untimed_session_whose_file_vanished(self):
        ghost = SessionSummary(
            session_id="ghost", source="cli", file_path="/nonexistent/ghost.db", start_time=None
        )
        timed = SessionSummary(
            session_id="timed",
            source="cli",
            file_path="/nonexistent/timed.db",
            start_time=datetime(2026, 1, 1, 12, 0, 0),
        )
        with mock.patch.object(
            AntigravityParser, "get_default_directories", return_value=[("/x", "cli")]
        ), mock.patch.object(AntigravityParser, "scan_directory", return_value=[ghost, timed]):
            sessions = AntigravityParser.scan_all()

        # The vanished file sorts as oldest (mtime 0) rather than raising.
        self.assertEqual([s.session_id for s in sessions], ["timed", "ghost"])


class TestScanDedupeAcrossSources(unittest.TestCase):
    """Regression tests for BUG-015: the same session DB present in both the
    CLI and IDE directories must be counted once, not twice."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cli_dir = os.path.join(self._tmp.name, "cli")
        self.ide_dir = os.path.join(self._tmp.name, "ide")
        os.mkdir(self.cli_dir)
        os.mkdir(self.ide_dir)
        for attr, path in (("DEFAULT_CLI_DIR", self.cli_dir), ("DEFAULT_IDE_DIR", self.ide_dir)):
            patcher = mock.patch.object(AntigravityParser, attr, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _db(self, directory, name, mtime, **usage):
        path = _write_usage_db(os.path.join(directory, name), **usage)
        os.utime(path, (mtime, mtime))
        return path

    def test_session_in_both_dirs_counted_once_keeping_newest_mtime(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000, output=200)
        self._db(self.ide_dir, "shared.db", 1_751_400_000, output=900)

        sessions = AntigravityParser.scan_all()

        self.assertEqual([s.session_id for s in sessions], ["shared"])
        self.assertEqual(sessions[0].source, "ide")
        self.assertEqual(sessions[0].usage.total_tokens, 1900)

    def test_newer_cli_copy_wins_over_stale_ide_copy(self):
        self._db(self.cli_dir, "shared.db", 1_751_400_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)

        sessions = AntigravityParser.scan_all()

        self.assertEqual([(s.session_id, s.source) for s in sessions], [("shared", "cli")])

    def test_equal_mtime_tie_is_deterministic(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)

        for _ in range(3):
            sessions = AntigravityParser.scan_all()
            self.assertEqual([(s.session_id, s.source) for s in sessions], [("shared", "cli")])

    def test_token_total_is_not_double_counted_and_matches_session_count(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)
        self._db(self.cli_dir, "only-cli.db", 1_751_300_000)
        self._db(self.ide_dir, "only-ide.db", 1_751_300_000)

        sessions = AntigravityParser.scan_all()

        self.assertEqual(sorted(s.session_id for s in sessions), ["only-cli", "only-ide", "shared"])
        self.assertEqual(sum(s.usage.total_tokens for s in sessions), 3 * 1200)
        daily = UsageAggregator.aggregate_daily(sessions)
        self.assertEqual(sum(len(d.session_ids) for d in daily.values()), 3)
        self.assertEqual(sum(d.usage.total_tokens for d in daily.values()), 3 * 1200)

    def test_single_source_modes_and_custom_dir_are_unaffected(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)

        cli_only = AntigravityParser.scan_all(source_mode="cli")
        ide_only = AntigravityParser.scan_all(source_mode="ide")
        custom = AntigravityParser.scan_all(custom_dir=self.ide_dir)

        self.assertEqual([s.source for s in cli_only], ["cli"])
        self.assertEqual([s.source for s in ide_only], ["ide"])
        self.assertEqual([s.session_id for s in custom], ["shared"])

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

