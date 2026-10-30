"""Tests for computing aggregate_daily once per run (OPT-004)."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from agy_usage import display as _display
from agy_usage.aggregator import UsageAggregator
from agy_usage.cli import (
    cmd_heatmap,
    cmd_live,
    cmd_monthly,
    cmd_overview,
    cmd_weekly,
    main as cli_main,
)
from agy_usage.display import Colors
from agy_usage.exporter import UsageExporter
from agy_usage.heatmap import TerminalHeatmap
from agy_usage.models import (
    SessionSummary,
    StepRecord,
    TokenUsage,
)


class TestAggregateDailyReuse(unittest.TestCase):
    """Test suite verifying single daily aggregation reuse across rollups and exporters."""

    @staticmethod
    def _make_step(
        step_idx: int,
        timestamp: datetime,
        model: str = "gemini-3.8-flash",
        prompt_tokens: int = 1000,
        cached_tokens: int = 200,
        output_tokens: int = 100,
        thought_tokens: int = 0,
    ) -> StepRecord:
        """Create a StepRecord with given token usage and timestamp."""
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
            thought_tokens=thought_tokens,
        )
        return StepRecord(
            step_idx=step_idx,
            timestamp=timestamp,
            model=model,
            usage=usage,
        )

    @staticmethod
    def _make_session(
        session_id: str,
        steps: list[StepRecord] | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        models: list[str] | None = None,
        workspace: str = "ws-test",
        title: str = "Test Session",
        usage: TokenUsage | None = None,
        step_count: int | None = None,
    ) -> SessionSummary:
        """Create a SessionSummary fixture supporting step-based or fallback usage."""
        if steps:
            calculated_usage = TokenUsage()
            for st in steps:
                calculated_usage = calculated_usage.add(st.usage)
            s_models = models or list(dict.fromkeys(st.model for st in steps))
            s_start = start_time or steps[0].timestamp
            s_end = end_time or steps[-1].timestamp
            s_count = step_count if step_count is not None else len(steps)
            return SessionSummary(
                session_id=session_id,
                source="cli",
                file_path=f"/tmp/{session_id}.db",
                title=title,
                workspace=workspace,
                models=s_models,
                start_time=s_start,
                end_time=s_end,
                step_count=s_count,
                usage=usage or calculated_usage,
                steps=steps,
            )
        else:
            return SessionSummary(
                session_id=session_id,
                source="cli",
                file_path=f"/tmp/{session_id}.db",
                title=title,
                workspace=workspace,
                models=models or (["gemini-3.8-flash"] if usage else []),
                start_time=start_time,
                end_time=end_time or start_time,
                step_count=step_count or 0,
                usage=usage or TokenUsage(),
                steps=[],
            )

    def _build_boundary_sessions(self) -> list[SessionSummary]:
        """Build sessions spanning multi-day, ISO-week boundary, month boundary, and fallbacks."""
        # 1. Sun 2026-10-25: ISO-week 43, October
        st_sun = self._make_step(1, datetime(2026, 10, 25, 14, 0), "gemini-3.8-flash", 1000, 400, 150)
        s_sun = self._make_session("sess-sun-w43", steps=[st_sun], title="Sunday W43")

        # 2. Mon 2026-10-26: ISO-week 44, October
        st_mon = self._make_step(1, datetime(2026, 10, 26, 9, 30), "gemini-3.8-flash", 1200, 500, 180)
        s_mon = self._make_session("sess-mon-w44", steps=[st_mon], title="Monday W44")

        # 3. Sat 2026-10-31: ISO-week 44, October (month boundary end)
        st_oct = self._make_step(1, datetime(2026, 10, 31, 22, 15), "gemini-3.8-flash", 800, 300, 100)
        s_oct_end = self._make_session("sess-oct-end", steps=[st_oct], title="Oct 31")

        # 4. Sun 2026-11-01: ISO-week 44, November (month boundary start)
        st_nov = self._make_step(1, datetime(2026, 11, 1, 8, 45), "gemini-3.8-flash", 950, 350, 120)
        s_nov_start = self._make_session("sess-nov-start", steps=[st_nov], title="Nov 1")

        # 5. Session WITHOUT steps (aggregate_daily fallback branch)
        fallback_usage = TokenUsage(prompt_tokens=2500, cached_tokens=1000, output_tokens=300)
        s_no_steps = self._make_session(
            "sess-no-steps",
            steps=[],
            start_time=datetime(2026, 11, 2, 11, 0),
            end_time=datetime(2026, 11, 2, 11, 30),
            models=["gemini-3.8-flash"],
            step_count=2,
            usage=fallback_usage,
            title="Fallback No Steps",
        )

        # 6. Empty-steps session with no start_time
        s_empty_steps = self._make_session(
            "sess-empty-steps",
            steps=[],
            start_time=None,
            step_count=0,
            usage=TokenUsage(),
            models=[],
            title="Empty Steps",
        )

        return [s_sun, s_mon, s_oct_end, s_nov_start, s_no_steps, s_empty_steps]

    def setUp(self) -> None:
        """Preserve color override and create standard baseline test fixtures."""
        self._prev_color_override = _display._COLOR_OVERRIDE
        Colors.set_override(False)

        # Tiny baseline fixture with known deterministic dates and counts
        t1 = datetime(2026, 9, 1, 10, 0)
        t2 = datetime(2026, 9, 8, 14, 0)

        step1_1 = self._make_step(1, t1, "gemini-3.8-flash", prompt_tokens=1000, cached_tokens=4000, output_tokens=200)
        step1_2 = self._make_step(2, t1, "gemini-3.8-flash", prompt_tokens=500, cached_tokens=1000, output_tokens=50)
        s1 = self._make_session("s1", steps=[step1_1, step1_2], workspace="proj1", title="Session 1")

        step2_1 = self._make_step(1, t2, "gemini-3.8-flash", prompt_tokens=500, cached_tokens=1500, output_tokens=100)
        s2 = self._make_session("s2", steps=[step2_1], workspace="proj2", title="Session 2")

        self.fixture_sessions = [s1, s2]

    def tearDown(self) -> None:
        """Restore global color override state."""
        Colors.set_override(self._prev_color_override)

    # -------------------------------------------------------------------------
    # Group A: Call counts
    # -------------------------------------------------------------------------

    def test_to_json_calls_aggregate_daily_once(self) -> None:
        """A.1: UsageExporter.to_json(sessions) calls aggregate_daily exactly once."""
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            result = UsageExporter.to_json(self.fixture_sessions)

        self.assertIsInstance(result, str)
        self.assertEqual(mock_daily.call_count, 1)

    def test_to_json_custom_options_calls_aggregate_daily_once(self) -> None:
        """A.2: UsageExporter.to_json with custom args calls aggregate_daily exactly once."""
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            result = UsageExporter.to_json(
                self.fixture_sessions,
                include_steps=True,
                indent=None,
            )

        self.assertIsInstance(result, str)
        self.assertEqual(mock_daily.call_count, 1)

    def test_export_json_file_calls_aggregate_daily_once(self) -> None:
        """A.3: UsageExporter.export to file calls aggregate_daily exactly once."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = os.path.join(tmp_dir, "exported_usage.json")
            with mock.patch.object(
                UsageAggregator,
                "aggregate_daily",
                wraps=UsageAggregator.aggregate_daily,
            ) as mock_daily:
                UsageExporter.export(
                    self.fixture_sessions,
                    format_type="json",
                    output_path=out_file,
                )

            self.assertTrue(os.path.exists(out_file))
            with open(out_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("summary", data)
            self.assertEqual(mock_daily.call_count, 1)

    def test_aggregate_weekly_with_precomputed_daily_skips_aggregate_daily(self) -> None:
        """A.4: aggregate_weekly(sessions, daily_data=precomputed) skips aggregate_daily."""
        precomputed = UsageAggregator.aggregate_daily(self.fixture_sessions)
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageAggregator.aggregate_weekly(self.fixture_sessions, daily_data=precomputed)

        self.assertEqual(mock_daily.call_count, 0)

    def test_aggregate_monthly_with_precomputed_daily_skips_aggregate_daily(self) -> None:
        """A.4: aggregate_monthly(sessions, daily_data=precomputed) skips aggregate_daily."""
        precomputed = UsageAggregator.aggregate_daily(self.fixture_sessions)
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageAggregator.aggregate_monthly(self.fixture_sessions, daily_data=precomputed)

        self.assertEqual(mock_daily.call_count, 0)

    def test_to_json_with_precomputed_daily_skips_aggregate_daily(self) -> None:
        """A.4: to_json(sessions, daily_data=precomputed) skips aggregate_daily."""
        precomputed = UsageAggregator.aggregate_daily(self.fixture_sessions)
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageExporter.to_json(self.fixture_sessions, daily_data=precomputed)

        self.assertEqual(mock_daily.call_count, 0)

    def test_guard_compute_summary_calls_aggregate_daily_once(self) -> None:
        """A.5 guard: compute_summary(sessions) calls aggregate_daily exactly once."""
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageAggregator.compute_summary(self.fixture_sessions)

        self.assertEqual(mock_daily.call_count, 1)

    def test_guard_compute_summary_with_precomputed_daily_skips_aggregate_daily(self) -> None:
        """A.5 guard: compute_summary(sessions, daily_data=d) does not call aggregate_daily."""
        precomputed = UsageAggregator.aggregate_daily(self.fixture_sessions)
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageAggregator.compute_summary(self.fixture_sessions, daily_data=precomputed)

        self.assertEqual(mock_daily.call_count, 0)

    def test_guard_aggregate_weekly_calls_aggregate_daily_once(self) -> None:
        """A.5 guard: aggregate_weekly(sessions) default signature calls aggregate_daily once."""
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageAggregator.aggregate_weekly(self.fixture_sessions)

        self.assertEqual(mock_daily.call_count, 1)

    def test_guard_aggregate_monthly_calls_aggregate_daily_once(self) -> None:
        """A.5 guard: aggregate_monthly(sessions) default signature calls aggregate_daily once."""
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageAggregator.aggregate_monthly(self.fixture_sessions)

        self.assertEqual(mock_daily.call_count, 1)

    def test_cli_cmd_overview_calls_aggregate_daily_once(self) -> None:
        """A.6: cmd_overview(sessions, args) calls aggregate_daily exactly once."""
        args = argparse.Namespace(
            cli_only=False,
            ide_only=False,
            custom_dir=None,
            model=None,
            workspace=None,
        )
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily, redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cmd_overview(self.fixture_sessions, args)

        self.assertEqual(rc, 0)
        self.assertEqual(mock_daily.call_count, 1)

    def test_cli_cmd_heatmap_calls_aggregate_daily_once(self) -> None:
        """A.6: cmd_heatmap(sessions, args) calls aggregate_daily exactly once."""
        args = argparse.Namespace(
            metric="total",
            weeks=16,
            details=False,
            cli_only=False,
            ide_only=False,
            custom_dir=None,
            model=None,
            workspace=None,
        )
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily, redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cmd_heatmap(self.fixture_sessions, args)

        self.assertEqual(rc, 0)
        self.assertEqual(mock_daily.call_count, 1)

    def test_cli_cmd_weekly_calls_aggregate_daily_once(self) -> None:
        """A.6: cmd_weekly(sessions) calls aggregate_daily exactly once."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily, redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cmd_weekly(self.fixture_sessions)

        self.assertEqual(rc, 0)
        self.assertEqual(mock_daily.call_count, 1)

    def test_cli_cmd_monthly_calls_aggregate_daily_once(self) -> None:
        """A.6: cmd_monthly(sessions) calls aggregate_daily exactly once."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily, redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cmd_monthly(self.fixture_sessions)

        self.assertEqual(rc, 0)
        self.assertEqual(mock_daily.call_count, 1)

    def test_cli_main_json_calls_aggregate_daily_once(self) -> None:
        """A.6: cli.main(['--json']) with patched get_sessions calls aggregate_daily once."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with mock.patch("agy_usage.cli.get_sessions", return_value=self.fixture_sessions), \
             mock.patch.object(
                 UsageAggregator,
                 "aggregate_daily",
                 wraps=UsageAggregator.aggregate_daily,
             ) as mock_daily, \
             redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cli_main(["--json"])

        self.assertEqual(rc, 0)
        self.assertEqual(mock_daily.call_count, 1)

    def test_cli_live_json_calls_aggregate_daily_once_per_tick(self) -> None:
        """A.6: each cmd_live --json tick calls aggregate_daily exactly once."""
        args = argparse.Namespace(csv=False, json=True, interval=1)
        out_buf = io.StringIO()
        with mock.patch("agy_usage.cli.get_sessions", return_value=self.fixture_sessions), \
             mock.patch("agy_usage.cli.time.sleep", side_effect=[None, KeyboardInterrupt]), \
             mock.patch.object(
                 UsageAggregator,
                 "aggregate_daily",
                 wraps=UsageAggregator.aggregate_daily,
             ) as mock_daily, \
             redirect_stdout(out_buf):
            rc = cmd_live(args)

        self.assertEqual(rc, 0)
        self.assertEqual(len(out_buf.getvalue().splitlines()), 2)
        self.assertEqual(mock_daily.call_count, 2)

    # -------------------------------------------------------------------------
    # Group B: Output equivalence
    # -------------------------------------------------------------------------

    def test_to_json_output_matches_assembled_dict_and_literals(self) -> None:
        """B.1: to_json output matches manually assembled dict and hardcoded literals."""
        sessions = self.fixture_sessions
        summary = UsageAggregator.compute_summary(sessions)
        daily = UsageAggregator.aggregate_daily(sessions)
        weekly = UsageAggregator.aggregate_weekly(sessions)
        monthly = UsageAggregator.aggregate_monthly(sessions)
        models = UsageAggregator.aggregate_models(sessions)

        assembled = {
            "summary": {
                "session_count": summary["session_count"],
                "step_count": summary["step_count"],
                "active_days": summary["active_days"],
                "unique_workspaces_count": summary["unique_workspaces_count"],
                "earliest_date": summary["earliest_date"],
                "latest_date": summary["latest_date"],
                "cache_hit_rate_pct": round(summary["cache_hit_rate"], 2),
                "total_usage": summary["total_usage"].to_dict(),
                "total_cost": summary["total_cost"].to_dict(),
            },
            "models": [m.to_dict() for m in models],
            "monthly": [m.to_dict() for m in monthly],
            "weekly": [w.to_dict() for w in weekly],
            "daily": [d.to_dict() for d in sorted(daily.values(), key=lambda x: x.date)],
            "sessions": [s.to_dict(include_steps=False) for s in sessions],
        }

        actual_json = UsageExporter.to_json(sessions)
        actual_data = json.loads(actual_json)

        # 1. Structural equivalence to manually assembled dict
        self.assertEqual(actual_data, assembled)

        # 2. Hardcoded literal assertions on key order/set and KPI values
        expected_keys = ["summary", "models", "monthly", "weekly", "daily", "sessions"]
        self.assertEqual(list(actual_data.keys()), expected_keys)
        self.assertEqual(actual_data["summary"]["session_count"], 2)
        self.assertEqual(actual_data["summary"]["active_days"], 2)
        self.assertEqual(actual_data["summary"]["step_count"], 3)
        self.assertEqual(len(actual_data["daily"]), 2)
        self.assertEqual(len(actual_data["weekly"]), 2)
        self.assertEqual(len(actual_data["monthly"]), 1)
        self.assertEqual(
            [d["date"] for d in actual_data["daily"]],
            ["2026-09-01", "2026-09-08"],
        )

    def test_output_equivalence_with_precomputed_daily_boundaries(self) -> None:
        """B.2: Boundaries and edge cases produce identical output with precomputed daily_data."""
        boundary_sessions = self._build_boundary_sessions()

        # Hard-coded expectations for the boundary fixture
        weekly_fixture = UsageAggregator.aggregate_weekly(boundary_sessions)
        self.assertEqual([(w.year, w.week_num) for w in weekly_fixture], [(2026, 43), (2026, 44), (2026, 45)])
        self.assertEqual([w.step_count for w in weekly_fixture], [1, 3, 2])
        monthly_fixture = UsageAggregator.aggregate_monthly(boundary_sessions)
        self.assertEqual([(m.year, m.month) for m in monthly_fixture], [(2026, 10), (2026, 11)])
        self.assertEqual([m.step_count for m in monthly_fixture], [3, 3])

        # Test both boundary fixture and empty session list
        for test_sessions in (boundary_sessions, []):
            daily = UsageAggregator.aggregate_daily(test_sessions)

            expected_weekly = UsageAggregator.aggregate_weekly(test_sessions)
            actual_weekly = UsageAggregator.aggregate_weekly(test_sessions, daily_data=daily)
            self.assertEqual(actual_weekly, expected_weekly)

            expected_monthly = UsageAggregator.aggregate_monthly(test_sessions)
            actual_monthly = UsageAggregator.aggregate_monthly(test_sessions, daily_data=daily)
            self.assertEqual(actual_monthly, expected_monthly)

            expected_json = UsageExporter.to_json(test_sessions)
            actual_json = UsageExporter.to_json(test_sessions, daily_data=daily)
            self.assertEqual(actual_json, expected_json)

    def test_to_json_empty_sessions_no_crash(self) -> None:
        """B.2: UsageExporter.to_json([]) does not crash and returns valid empty JSON structure."""
        out = UsageExporter.to_json([])
        self.assertIsInstance(out, str)
        data = json.loads(out)
        self.assertEqual(data["sessions"], [])
        self.assertEqual(data["summary"]["session_count"], 0)
        self.assertEqual(data["summary"]["active_days"], 0)
        self.assertEqual(data["daily"], [])
        self.assertEqual(data["weekly"], [])
        self.assertEqual(data["monthly"], [])

    def test_to_json_empty_sessions_calls_aggregate_daily_at_most_once(self) -> None:
        """B.2: UsageExporter.to_json([]) calls aggregate_daily at most once."""
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as mock_daily:
            UsageExporter.to_json([])

        self.assertLessEqual(mock_daily.call_count, 1)

    def test_guard_mutation_safety_compute_summary(self) -> None:
        """B.3 guard: Passing precomputed daily_data to compute_summary does not mutate it."""
        sessions = self.fixture_sessions
        daily = UsageAggregator.aggregate_daily(sessions)
        snapshot_before = {d: v.to_dict() for d, v in daily.items()}

        UsageAggregator.compute_summary(sessions, daily_data=daily)
        snapshot_after = {d: v.to_dict() for d, v in daily.items()}

        self.assertEqual(snapshot_before, snapshot_after)

    def test_mutation_safety_precomputed_daily_data(self) -> None:
        """B.3: Passing precomputed daily_data to weekly/monthly/to_json must not mutate it."""
        sessions = self._build_boundary_sessions()
        daily = UsageAggregator.aggregate_daily(sessions)
        snapshot_before = {d: v.to_dict() for d, v in daily.items()}

        # 1. Weekly aggregation must not mutate daily_data
        UsageAggregator.aggregate_weekly(sessions, daily_data=daily)
        snapshot_after_weekly = {d: v.to_dict() for d, v in daily.items()}
        self.assertEqual(snapshot_before, snapshot_after_weekly)

        # 2. Monthly aggregation must not mutate daily_data
        UsageAggregator.aggregate_monthly(sessions, daily_data=daily)
        snapshot_after_monthly = {d: v.to_dict() for d, v in daily.items()}
        self.assertEqual(snapshot_before, snapshot_after_monthly)

        # 3. Export to JSON must not mutate daily_data
        UsageExporter.to_json(sessions, daily_data=daily)
        snapshot_after_to_json = {d: v.to_dict() for d, v in daily.items()}
        self.assertEqual(snapshot_before, snapshot_after_to_json)

    def test_cmd_heatmap_stdout_matches_manual_render(self) -> None:
        """B.4: cmd_heatmap stdout equals what you get from building it manually."""
        args = argparse.Namespace(metric="total", weeks=16, details=False)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cmd_heatmap(self.fixture_sessions, args)

        self.assertEqual(rc, 0)

        daily_data = UsageAggregator.aggregate_daily(self.fixture_sessions)
        hm = TerminalHeatmap(daily_data, metric="total", weeks=16)
        expected_buf = io.StringIO()
        with redirect_stdout(expected_buf):
            print()
            print(Colors.bold(Colors.cyan(f"── GITHUB-STYLE CONTRIBUTION HEATMAP ({args.metric.upper()} TOKENS) " + "──" * 25)))
            print(hm.render())
            print()

        self.assertEqual(out_buf.getvalue(), expected_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
