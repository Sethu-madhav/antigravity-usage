"""Tests for quota limit calculation."""

import io
import unittest
from datetime import datetime, timedelta

from agy_usage.display import Colors, format_percent, strip_ansi
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


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


if __name__ == "__main__":
    unittest.main()
