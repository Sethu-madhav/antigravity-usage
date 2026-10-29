"""Tests that colors are off for piped output."""

import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from unittest import mock

from agy_usage.cli import main as cli_main
from agy_usage.display import Colors, format_percent
from agy_usage.heatmap import HeatmapColor, TerminalHeatmap
from agy_usage.models import DailyUsage, SessionSummary, TokenUsage


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


if __name__ == "__main__":
    unittest.main()
