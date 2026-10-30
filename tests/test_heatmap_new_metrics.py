"""Tests for the cache-efficiency and hour-of-day heatmap metrics (FEAT-008)."""

import contextlib
import io
import unittest
from datetime import date, datetime
from unittest import mock

from agy_usage.aggregator import UsageAggregator
from agy_usage.cli import cmd_heatmap, parse_args
from agy_usage.display import strip_ansi
from agy_usage.heatmap import HeatmapColor, HourOfDayHeatmap, TerminalHeatmap
from agy_usage.models import DailyUsage, SessionSummary, StepRecord, TokenUsage

END = date(2026, 9, 10)  # a Thursday


def _daily(d, **kw):
    return DailyUsage(date=d, usage=TokenUsage(**kw))


def _step(idx, ts, **kw):
    return StepRecord(step_idx=idx, timestamp=ts, model="gemini-3-pro", usage=TokenUsage(**kw))


def _session(sid, steps=None, start_time=None, usage=None):
    return SessionSummary(
        session_id=sid, source="cli", file_path="/x", steps=steps or [],
        start_time=start_time, usage=usage or TokenUsage(),
    )


class TestCacheEfficiency(unittest.TestCase):
    def _hm(self, daily):
        return TerminalHeatmap(daily, metric="cache-efficiency", weeks=8, end_date=END, term_cols=80)

    def test_metric_value_reuses_cache_hit_rate(self):
        hm = self._hm({})
        usage = TokenUsage(prompt_tokens=100, cached_tokens=300, output_tokens=999)
        self.assertAlmostEqual(hm._get_metric_val(usage), 75.0)
        self.assertEqual(hm._get_metric_val(usage), usage.cache_hit_rate)

    def test_no_input_does_not_divide_by_zero(self):
        hm = self._hm({date(2026, 9, 1): _daily(date(2026, 9, 1), output_tokens=500)})
        self.assertEqual(hm._get_metric_val(TokenUsage(output_tokens=500)), 0.0)
        out = strip_ansi(hm.render())
        self.assertIn("0.0% Cache Efficiency across 0 active days", out)

    def test_summary_is_overall_ratio_not_sum_of_percentages(self):
        daily = {
            date(2026, 9, 1): _daily(date(2026, 9, 1), prompt_tokens=100, cached_tokens=0),
            date(2026, 9, 2): _daily(date(2026, 9, 2), prompt_tokens=100, cached_tokens=900),
        }
        out = strip_ansi(self._hm(daily).render())
        # overall = 900 / 1100 = 81.8%; a sum of daily percentages would be 90%.
        self.assertIn("81.8% Cache Efficiency across 2 active days in last 8 weeks", out)
        self.assertNotIn("Tokens", out.splitlines()[-1])

    def test_zero_percent_day_with_input_counts_as_active(self):
        daily = {date(2026, 9, 1): _daily(date(2026, 9, 1), prompt_tokens=100)}
        out = strip_ansi(self._hm(daily).render())
        self.assertIn("0.0% Cache Efficiency across 1 active days", out)

    def test_days_outside_window_are_ignored(self):
        daily = {date(2025, 1, 1): _daily(date(2025, 1, 1), prompt_tokens=10, cached_tokens=90)}
        out = strip_ansi(self._hm(daily).render())
        self.assertIn("0.0% Cache Efficiency across 0 active days", out)

    def test_existing_metrics_unchanged(self):
        daily = {date(2026, 9, 1): _daily(date(2026, 9, 1), prompt_tokens=1000, cached_tokens=5000, output_tokens=7)}
        hm = TerminalHeatmap(daily, metric="cached", weeks=8, end_date=END, term_cols=80)
        self.assertIn("5,000 Cached Tokens across 1 active days", strip_ansi(hm.render()))
        hm = TerminalHeatmap(daily, metric="total", weeks=8, end_date=END, term_cols=80)
        self.assertIn("6,007 Total Tokens across 1 active days", strip_ansi(hm.render()))


class TestAggregateHourly(unittest.TestCase):
    def test_buckets_by_step_date_and_hour(self):
        s = _session("a", steps=[
            _step(0, datetime(2026, 9, 1, 9, 5), prompt_tokens=10),
            _step(1, datetime(2026, 9, 1, 9, 55), prompt_tokens=5, output_tokens=5),
            _step(2, datetime(2026, 9, 1, 23, 59), cached_tokens=7),
            _step(3, datetime(2026, 9, 2, 0, 0), output_tokens=3),
        ])
        hourly = UsageAggregator.aggregate_hourly([s])
        self.assertEqual(hourly[(date(2026, 9, 1), 9)].total_tokens, 20)
        self.assertEqual(hourly[(date(2026, 9, 1), 23)].total_tokens, 7)
        self.assertEqual(hourly[(date(2026, 9, 2), 0)].total_tokens, 3)
        self.assertEqual(len(hourly), 3)

    def test_session_without_steps_falls_back_to_start_time(self):
        s = _session("b", start_time=datetime(2026, 9, 3, 14, 30), usage=TokenUsage(prompt_tokens=42))
        self.assertEqual(UsageAggregator.aggregate_hourly([s])[(date(2026, 9, 3), 14)].total_tokens, 42)

    def test_session_without_steps_or_start_time_is_skipped(self):
        self.assertEqual(UsageAggregator.aggregate_hourly([_session("c", usage=TokenUsage(prompt_tokens=1))]), {})

    def test_hourly_totals_match_daily_totals(self):
        sessions = [
            _session("a", steps=[_step(0, datetime(2026, 9, 1, 1), prompt_tokens=10),
                                 _step(1, datetime(2026, 9, 1, 22), cached_tokens=20)]),
            _session("b", start_time=datetime(2026, 9, 1, 12), usage=TokenUsage(output_tokens=5)),
        ]
        daily = UsageAggregator.aggregate_daily(sessions)[date(2026, 9, 1)].usage.total_tokens
        hourly = sum(u.total_tokens for u in UsageAggregator.aggregate_hourly(sessions).values())
        self.assertEqual(daily, hourly)


class TestHourOfDayHeatmap(unittest.TestCase):
    def _render(self, hourly, **kw):
        hm = HourOfDayHeatmap(hourly, weeks=kw.pop("weeks", 8), end_date=END, term_cols=80)
        return strip_ansi(hm.render())

    def test_grid_shape_and_header(self):
        lines = self._render({}).splitlines()
        self.assertEqual(lines[0], "     " + "".join(f"{h:02d}    " for h in range(0, 24, 3)).rstrip())
        rows = lines[1:8]
        self.assertEqual([r[:3].strip() for r in rows], ["Mon", "", "Wed", "", "Fri", "", "Sun"])
        for r in rows:
            self.assertEqual(len(r), 4 + 24 * 2)  # label + space + 24 two-char cells

    def test_places_activity_at_weekday_and_hour(self):
        d = date(2026, 9, 9)  # Wednesday
        self.assertEqual(d.weekday(), 2)
        with mock.patch.object(HeatmapColor, "supports_color", return_value=False):
            out = self._render({(d, 14): TokenUsage(prompt_tokens=100)})
        rows = out.splitlines()[1:8]
        # no-color fallback glyphs: "·" empty, "█" top level
        cells = rows[2][4:]
        self.assertEqual(cells[14 * 2], "█")
        self.assertEqual(cells.count("█"), 1)
        self.assertNotIn("█", "".join(r[4:] for i, r in enumerate(rows) if i != 2))

    def test_aggregates_same_weekday_hour_across_weeks(self):
        hourly = {
            (date(2026, 9, 7), 9): TokenUsage(prompt_tokens=300),   # Monday
            (date(2026, 8, 31), 9): TokenUsage(prompt_tokens=200),  # previous Monday
            (date(2026, 9, 8), 20): TokenUsage(prompt_tokens=10),   # Tuesday
        }
        out = self._render(hourly)
        last = out.splitlines()[-1]
        self.assertIn("510 Tokens in last 8 weeks", last)
        self.assertIn("busiest hour 09:00", last)

    def test_window_excludes_old_and_future_data(self):
        hourly = {
            (date(2025, 1, 6), 9): TokenUsage(prompt_tokens=1000),   # before window
            (date(2026, 9, 11), 9): TokenUsage(prompt_tokens=1000),  # after end_date
            (date(2026, 9, 10), 9): TokenUsage(prompt_tokens=5),
        }
        self.assertIn("5 Tokens in last 8 weeks", self._render(hourly).splitlines()[-1])

    def test_empty_data_has_no_busiest_hour(self):
        last = self._render({}).splitlines()[-1]
        self.assertIn("0 Tokens in last 8 weeks", last)
        self.assertNotIn("busiest", last)

    def test_required_cols_and_narrow_terminal(self):
        self.assertEqual(HourOfDayHeatmap({}, term_cols=200).required_cols, 56)
        self.assertTrue(HourOfDayHeatmap({}, term_cols=50).is_terminal_narrow)
        self.assertFalse(HourOfDayHeatmap({}, term_cols=56).is_terminal_narrow)


class TestMetricCli(unittest.TestCase):
    def test_new_metrics_accepted_and_existing_kept(self):
        for metric in ("total", "cached", "input", "output", "thoughts", "cache-efficiency", "hour-of-day"):
            with self.subTest(metric=metric):
                self.assertEqual(parse_args(["heatmap", "--metric", metric]).metric, metric)

    def test_unknown_metric_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["heatmap", "--metric", "bogus"])

    def test_help_mentions_new_metrics(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            parse_args(["heatmap", "--help"])
        self.assertIn("cache-efficiency", buf.getvalue())
        self.assertIn("hour-of-day", buf.getvalue())

    def _run(self, metric):
        sessions = [_session("a", steps=[
            _step(0, datetime(2026, 9, 9, 14, 0), prompt_tokens=100, cached_tokens=300),
        ])]
        args = parse_args(["heatmap", "--metric", metric, "--no-color"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("agy_usage.heatmap.date") as mdate, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            mdate.today.return_value = END
            self.assertEqual(cmd_heatmap(sessions, args), 0)
        return strip_ansi(out.getvalue())

    def test_cmd_heatmap_cache_efficiency_title_and_summary(self):
        out = self._run("cache-efficiency")
        self.assertIn("(CACHE EFFICIENCY)", out)
        self.assertNotIn("TOKENS)", out)
        self.assertIn("75.0% Cache Efficiency", out)

    def test_cmd_heatmap_hour_of_day_title_and_summary(self):
        out = self._run("hour-of-day")
        self.assertIn("(TOKENS BY HOUR OF DAY)", out)
        self.assertIn("400 Tokens", out)
        self.assertIn("busiest hour 14:00", out)

    def test_cmd_heatmap_existing_title_unchanged(self):
        self.assertIn("(CACHED TOKENS)", self._run("cached"))


if __name__ == "__main__":
    unittest.main()
