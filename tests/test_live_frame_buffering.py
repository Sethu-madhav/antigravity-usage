"""Tests for live frame buffering, flicker elimination, and single daily aggregation (OPT-002)."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import unittest
from unittest import mock

from agy_usage.aggregator import UsageAggregator
from agy_usage.cli import cmd_live, cmd_overview, main as cli_main
from agy_usage import display as _display
from agy_usage.display import Colors
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


class _RecordingStdout(io.StringIO):
    """StringIO subclass that records each write call and its first 10 characters."""

    def __init__(self, events_list: list) -> None:
        super().__init__()
        self.events = events_list
        self.raw_writes: list[str] = []

    def write(self, s: str) -> int:
        self.raw_writes.append(s)
        self.events.append(f"write:{s[:10]}")
        return super().write(s)


class TestLiveFrameBuffering(unittest.TestCase):
    """Test frame buffering in live mode and daily data reuse."""

    def _make_session(
        self,
        session_id: str,
        prompt_tokens: int,
        timestamp: datetime | None = None,
        model: str = "gemini-3.8-flash",
    ) -> SessionSummary:
        t = timestamp or datetime(2026, 10, 29, 12, 0, 0)
        u = TokenUsage(
            prompt_tokens=prompt_tokens,
            cached_tokens=200,
            output_tokens=50,
            thought_tokens=10,
        )
        step = StepRecord(
            step_idx=1,
            timestamp=t,
            model=model,
            usage=u,
        )
        return SessionSummary(
            session_id=session_id,
            source="cli",
            file_path=f"/tmp/{session_id}.db",
            title=f"Session {session_id}",
            workspace="ws-test",
            models=[model],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=u,
            steps=[step],
        )

    def test_ordering_and_atomicity_and_single_clear_per_tick(self):
        """Verify that scan happens before write, frame + clear are written atomically,
        there are no clears before the first scan, and exactly one clear per tick occurs."""
        events: list[str] = []
        rec_stdout = _RecordingStdout(events)
        rec_stderr = io.StringIO()

        tick_sessions = [
            [self._make_session("sess-1", 1000)],
            [self._make_session("sess-1", 1000), self._make_session("sess-2", 2000)],
        ]
        tick_idx = 0

        def fake_get_sessions(args):
            nonlocal tick_idx
            events.append("scan")
            s = tick_sessions[min(tick_idx, len(tick_sessions) - 1)]
            tick_idx += 1
            return s

        with mock.patch("agy_usage.cli.get_sessions", side_effect=fake_get_sessions) as mock_scan, \
             mock.patch("agy_usage.cli.time.sleep", side_effect=[None, KeyboardInterrupt()]) as mock_sleep, \
             redirect_stdout(rec_stdout), redirect_stderr(rec_stderr):
            rc = cli_main(["live", "--interval", "1"])

        self.assertEqual(rc, 0)
        self.assertEqual(mock_scan.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 2)

        # 1. Assert there are no clear-screen writes before the first scan
        first_scan_event_idx = events.index("scan")
        writes_before_first_scan = [
            w for w in rec_stdout.raw_writes[:first_scan_event_idx]
        ]
        for w in writes_before_first_scan:
            self.assertNotIn("\033[2J\033[H", w)

        # 2. Find all writes that contain the clear sequence
        clear_writes = [w for w in rec_stdout.raw_writes if "\033[2J\033[H" in w]
        # Exactly one clear sequence per tick (2 ticks -> 2 occurrences)
        self.assertEqual(len(clear_writes), 2)
        total_clear_occurrences = rec_stdout.getvalue().count("\033[2J\033[H")
        self.assertEqual(total_clear_occurrences, 2)

        # 3. Assert clear sequence is written in the SAME write call as the frame body
        for cw in clear_writes:
            self.assertTrue(cw.startswith("\033[2J\033[H"))
            self.assertIn("ANTIGRAVITY LIVE MONITOR", cw)

        # 4. Check ordering of events: scan_1 < write_1 < scan_2 < write_2
        scan_indices = [i for i, ev in enumerate(events) if ev == "scan"]
        clear_event_indices = [
            i for i, ev in enumerate(events) if ev.startswith("write:\033[2J\033[H")
        ]
        self.assertEqual(len(scan_indices), 2)
        self.assertEqual(len(clear_event_indices), 2)
        self.assertLess(scan_indices[0], clear_event_indices[0])
        self.assertLess(clear_event_indices[0], scan_indices[1])
        self.assertLess(scan_indices[1], clear_event_indices[1])

    def test_colors_preserved_when_enabled(self):
        """When color is enabled, frame contains ANSI color codes and override is restored."""
        self.addCleanup(Colors.set_override, None)

        s_tick1 = [self._make_session("sess-color", 1500)]

        # 1. Drive via cli_main with patched is_color_enabled (simulating real TTY stdout)
        out_buf = io.StringIO()
        with mock.patch("agy_usage.display.is_color_enabled", return_value=True), \
             mock.patch("agy_usage.cli.get_sessions", return_value=s_tick1), \
             mock.patch("agy_usage.cli.time.sleep", side_effect=KeyboardInterrupt()), \
             redirect_stdout(out_buf):
            rc = cli_main(["live", "--interval", "1"])

        self.assertEqual(rc, 0)
        output = out_buf.getvalue()
        # Assert the frame contains ANSI color codes such as cyan \033[36m
        self.assertIn("\033[36m", output)
        self.assertIn("\033[2J\033[H", output)

        # 2. Drive cmd_live directly with Colors.set_override(True)
        Colors.set_override(True)
        out_buf_direct = io.StringIO()
        args = argparse.Namespace(
            interval=1,
            json=False,
            csv=False,
            cli_only=False,
            ide_only=False,
            custom_dir=None,
            model=None,
            workspace=None,
        )
        with mock.patch("agy_usage.cli.get_sessions", return_value=s_tick1), \
             mock.patch("agy_usage.cli.time.sleep", side_effect=KeyboardInterrupt()), \
             redirect_stdout(out_buf_direct):
            rc = cmd_live(args)

        self.assertEqual(rc, 0)
        direct_output = out_buf_direct.getvalue()
        self.assertIn("\033[36m", direct_output)
        # After cmd_live returns, the override is restored to its previous value (True here)
        self.assertIs(_display._COLOR_OVERRIDE, True)

    def test_no_color_flag_suppresses_ansi_color_codes(self):
        """With --no-color, the frame contains no ANSI color codes except clear-screen."""
        self.addCleanup(Colors.set_override, None)

        out_buf = io.StringIO()
        s_tick1 = [self._make_session("sess-plain", 1500)]

        with mock.patch("agy_usage.cli.get_sessions", return_value=s_tick1), \
             mock.patch("agy_usage.cli.time.sleep", side_effect=KeyboardInterrupt()), \
             redirect_stdout(out_buf):
            rc = cli_main(["live", "--no-color", "--interval", "1"])

        self.assertEqual(rc, 0)
        output = out_buf.getvalue()
        # Clear screen sequence must be present
        self.assertIn("\033[2J\033[H", output)

        # Remove the clear-screen escape to check that no color escapes (\033[3...) remain
        without_clear = output.replace("\033[2J\033[H", "")
        self.assertNotIn("\033[3", without_clear)
        self.assertNotIn("\033[9", without_clear)

    def test_cmd_live_preserves_and_restores_prior_override_directly(self):
        """Verify cmd_live restores whatever _COLOR_OVERRIDE was before invocation."""
        self.addCleanup(Colors.set_override, None)

        s_tick1 = [self._make_session("sess-direct", 500)]
        args = argparse.Namespace(
            interval=1,
            json=False,
            csv=False,
            cli_only=False,
            ide_only=False,
            custom_dir=None,
            model=None,
            workspace=None,
        )

        for prior in (None, True, False):
            with self.subTest(prior=prior):
                Colors.set_override(prior)
                out_buf = io.StringIO()
                with mock.patch("agy_usage.cli.get_sessions", return_value=s_tick1), \
                     mock.patch("agy_usage.cli.time.sleep", side_effect=KeyboardInterrupt()), \
                     redirect_stdout(out_buf):
                    rc = cmd_live(args)

                self.assertEqual(rc, 0)
                self.assertIs(_display._COLOR_OVERRIDE, prior)

    def test_aggregate_daily_called_once_per_cmd_overview(self):
        """Verify aggregate_daily is invoked exactly once per cmd_overview call."""
        sessions = [
            self._make_session("s1", 1000, datetime(2026, 10, 28, 10, 0)),
            self._make_session("s2", 2000, datetime(2026, 10, 29, 10, 0)),
        ]
        args = argparse.Namespace(
            cli_only=False,
            ide_only=False,
            custom_dir=None,
            model=None,
            workspace=None,
        )

        out_buf = io.StringIO()
        with mock.patch.object(
            UsageAggregator,
            "aggregate_daily",
            wraps=UsageAggregator.aggregate_daily,
        ) as wrapped_aggregate_daily, redirect_stdout(out_buf):
            rc = cmd_overview(sessions, args)

        self.assertEqual(rc, 0)
        self.assertEqual(wrapped_aggregate_daily.call_count, 1)

    def test_compute_summary_with_and_without_daily_data_are_identical(self):
        """compute_summary(sessions) and compute_summary(sessions, daily_data=...) return equal dicts."""
        s1 = self._make_session(
            "s1", 1000, datetime(2026, 10, 25, 9, 0), model="gemini-3.8-flash"
        )
        s2 = self._make_session(
            "s2", 5000, datetime(2026, 10, 26, 14, 0), model="gemini-4-argon"
        )
        s3 = self._make_session(
            "s3", 3000, datetime(2026, 10, 26, 18, 0), model="gemini-3.8-flash"
        )
        sessions = [s1, s2, s3]

        res_without = UsageAggregator.compute_summary(sessions)
        daily = UsageAggregator.aggregate_daily(sessions)
        res_with = UsageAggregator.compute_summary(sessions, daily_data=daily)

        # Compare scalar keys
        scalar_keys = [
            "session_count",
            "step_count",
            "active_days",
            "unique_workspaces_count",
            "earliest_date",
            "latest_date",
            "cache_hit_rate",
        ]
        for k in scalar_keys:
            self.assertEqual(res_without[k], res_with[k], f"Mismatch for key {k}")

        # Compare total_usage and total_cost
        self.assertEqual(res_without["total_usage"], res_with["total_usage"])
        self.assertEqual(
            res_without["total_cost"].to_dict(),
            res_with["total_cost"].to_dict(),
        )

        # Compare models
        models_without = res_without["models"]
        models_with = res_with["models"]
        self.assertEqual(len(models_without), len(models_with))
        for m1, m2 in zip(models_without, models_with):
            self.assertEqual(m1.model_name, m2.model_name)
            self.assertEqual(m1.session_count, m2.session_count)
            self.assertEqual(m1.request_count, m2.request_count)
            self.assertEqual(m1.usage, m2.usage)
            self.assertEqual(m1.cost.to_dict(), m2.cost.to_dict())
            self.assertAlmostEqual(m1.percentage_of_total, m2.percentage_of_total, places=5)

    def test_cmd_overview_empty_sessions_warning_to_stderr_in_live(self):
        """When cmd_overview returns 1 on empty sessions, warning goes to stderr, header still on stdout."""
        args = argparse.Namespace(
            interval=1,
            json=False,
            csv=False,
            cli_only=False,
            ide_only=False,
            custom_dir=None,
            model=None,
            workspace=None,
        )
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        with mock.patch("agy_usage.cli.get_sessions", return_value=[]), \
             mock.patch("agy_usage.cli.time.sleep", side_effect=KeyboardInterrupt()), \
             redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = cmd_live(args)

        self.assertEqual(rc, 0)
        self.assertIn("No Antigravity conversation sessions found.", err_buf.getvalue())
        self.assertIn("\033[2J\033[H", out_buf.getvalue())
        self.assertIn("ANTIGRAVITY LIVE MONITOR", out_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
