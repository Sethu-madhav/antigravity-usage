"""Unit tests for IMP-003: Color detection hygiene.

Verifies:
- is_color_enabled() and supports_truecolor() in agy_usage.display
- TERM=dumb disables color enablement and truecolor detection
- Colors.set_override(True) still overrides TERM=dumb
- Under multiplexers (TMUX, screen, tmux), macOS blanket truecolor is withheld
  unless COLORTERM or TERM explicitly advertises truecolor/24bit/direct
- is_color_enabled() and supports_truecolor() serve as the single source of truth
  for Colors and HeatmapColor
"""

import os
import sys
import unittest
from unittest import mock

from agy_usage.display import Colors, is_color_enabled, supports_truecolor
from agy_usage.heatmap import HeatmapColor


class TestColorDetectionHygiene(unittest.TestCase):
    def setUp(self):
        Colors.set_override(None)
        self.addCleanup(Colors.set_override, None)

    def _hermetic_env(self, **extra):
        """Hermetic environment containing only PATH and provided variables."""
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(extra)
        return env

    def test_term_dumb_disables_color_enablement(self):
        """TERM=dumb disables is_color_enabled(), Colors.enabled(), and HeatmapColor.supports_color()."""
        with mock.patch("sys.stdout.isatty", return_value=True):
            for dumb_val in ("dumb", "DUMB", " dumb ", "DuMb"):
                with mock.patch.dict(os.environ, self._hermetic_env(TERM=dumb_val), clear=True):
                    self.assertFalse(is_color_enabled(), f"Failed for TERM={dumb_val!r}")
                    self.assertFalse(Colors.enabled(), f"Failed for TERM={dumb_val!r}")
                    self.assertFalse(HeatmapColor.supports_color(), f"Failed for TERM={dumb_val!r}")

    def test_colors_override_wins_over_term_dumb(self):
        """Colors.set_override(True) forces color on even when TERM=dumb (BUG-004 contract)."""
        Colors.set_override(True)
        with mock.patch("sys.stdout.isatty", return_value=False):
            with mock.patch.dict(os.environ, self._hermetic_env(TERM="dumb"), clear=True):
                self.assertTrue(is_color_enabled())
                self.assertTrue(Colors.enabled())
                self.assertTrue(HeatmapColor.supports_color())

        Colors.set_override(False)
        with mock.patch("sys.stdout.isatty", return_value=True):
            with mock.patch.dict(os.environ, self._hermetic_env(TERM="xterm-256color"), clear=True):
                self.assertFalse(is_color_enabled())
                self.assertFalse(Colors.enabled())
                self.assertFalse(HeatmapColor.supports_color())

    def test_term_dumb_disables_truecolor_even_on_macos_and_with_colorterm(self):
        """TERM=dumb forces supports_truecolor() to False regardless of OS or COLORTERM."""
        with mock.patch("sys.platform", "darwin"):
            with mock.patch.dict(os.environ, self._hermetic_env(TERM="dumb", COLORTERM="truecolor"), clear=True):
                self.assertFalse(supports_truecolor())
                self.assertFalse(Colors.supports_truecolor())
                self.assertFalse(HeatmapColor.supports_truecolor())

    def test_macos_multiplexer_withholds_blanket_truecolor(self):
        """Under tmux/screen without truecolor advert, macOS falls back to 256-color palette."""
        Colors.set_override(True)  # Enable color so cell() uses depth probe

        # 1. Under TMUX without truecolor COLORTERM
        with mock.patch("sys.platform", "darwin"):
            with mock.patch.dict(
                os.environ,
                self._hermetic_env(TMUX="/tmp/tmux-1000/default,123,0", TERM="screen-256color"),
                clear=True,
            ):
                self.assertFalse(supports_truecolor())
                self.assertFalse(Colors.supports_truecolor())
                self.assertFalse(HeatmapColor.supports_truecolor())
                cell_render = HeatmapColor.cell(2)
                self.assertIn("\033[38;5;", cell_render)
                self.assertNotIn("\033[38;2;", cell_render)

            # 2. Under TERM=screen without COLORTERM
            with mock.patch.dict(os.environ, self._hermetic_env(TERM="screen"), clear=True):
                self.assertFalse(supports_truecolor())
                self.assertFalse(HeatmapColor.supports_truecolor())
                self.assertIn("\033[38;5;", HeatmapColor.cell(2))

            # 3. Under TERM=tmux-256color without COLORTERM
            with mock.patch.dict(os.environ, self._hermetic_env(TERM="tmux-256color"), clear=True):
                self.assertFalse(supports_truecolor())
                self.assertFalse(HeatmapColor.supports_truecolor())
                self.assertIn("\033[38;5;", HeatmapColor.cell(2))

            # 4. Under tmux WITH COLORTERM=truecolor -> TrueColor granted
            with mock.patch.dict(
                os.environ,
                self._hermetic_env(TMUX="/tmp/tmux-1000/default,123,0", TERM="tmux-256color", COLORTERM="truecolor"),
                clear=True,
            ):
                self.assertTrue(supports_truecolor())
                self.assertTrue(Colors.supports_truecolor())
                self.assertTrue(HeatmapColor.supports_truecolor())
                cell_render = HeatmapColor.cell(2)
                self.assertIn("\033[38;2;", cell_render)
                self.assertNotIn("\033[38;5;", cell_render)

            # 5. Under tmux WITH COLORTERM=24bit -> TrueColor granted
            with mock.patch.dict(
                os.environ,
                self._hermetic_env(TMUX="/tmp/tmux-1000/default,123,0", TERM="tmux-256color", COLORTERM="24bit"),
                clear=True,
            ):
                self.assertTrue(supports_truecolor())

            # 6. Under tmux WITH TERM containing direct -> TrueColor granted
            with mock.patch.dict(
                os.environ,
                self._hermetic_env(TMUX="/tmp/tmux-1000/default,123,0", TERM="tmux-direct"),
                clear=True,
            ):
                self.assertTrue(supports_truecolor())

    def test_macos_outside_multiplexer_has_truecolor(self):
        """Outside multiplexers on macOS, truecolor is enabled when TERM is not dumb."""
        with mock.patch("sys.platform", "darwin"):
            with mock.patch.dict(os.environ, self._hermetic_env(TERM="xterm-256color"), clear=True):
                self.assertTrue(supports_truecolor())
                self.assertTrue(Colors.supports_truecolor())
                self.assertTrue(HeatmapColor.supports_truecolor())

    def test_single_source_of_truth_wiring(self):
        """Colors and HeatmapColor delegate directly to is_color_enabled and supports_truecolor."""
        with mock.patch("agy_usage.heatmap.is_color_enabled", return_value=True) as mock_enabled_heatmap:
            self.assertTrue(HeatmapColor.supports_color())
            mock_enabled_heatmap.assert_called_once()

        with mock.patch("agy_usage.display.is_color_enabled", return_value=True) as mock_enabled_display:
            self.assertTrue(Colors.enabled())
            mock_enabled_display.assert_called_once()

        with mock.patch("agy_usage.heatmap.supports_truecolor", return_value=True) as mock_tc_heatmap:
            self.assertTrue(HeatmapColor.supports_truecolor())
            mock_tc_heatmap.assert_called_once()

        with mock.patch("agy_usage.display.supports_truecolor", return_value=True) as mock_tc_display:
            self.assertTrue(Colors.supports_truecolor())
            mock_tc_display.assert_called_once()


if __name__ == "__main__":
    unittest.main()
