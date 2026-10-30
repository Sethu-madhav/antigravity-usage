"""Tests for OPT-007: hoist re.compile in strip_ansi and truncate_ansi to module-level _ANSI_RE.

Pin byte-identical behaviour across all edge cases (empty, plain, SGR, truecolor,
256-color, private mode, lone ESC, wide CJK, emoji, combining accents, etc.), verify
that agy_usage.display._ANSI_RE is the single compiled pattern used across the module,
and confirm no per-call recompilation occurs.
"""

import re
import unittest
from unittest.mock import MagicMock, patch

from agy_usage import display
from agy_usage.display import Colors, render_table, strip_ansi, truncate_ansi

# Golden values captured from the ORIGINAL (pre-optimization) implementation.
# CORPUS: name -> input; GOLDEN_STRIP: name -> strip_ansi(input);
# GOLDEN_TRUNC: (name, max_visible) -> truncate_ansi(input, max_visible)
CORPUS = {
    'empty': '',
    'plain': 'hello world',
    'red': '\x1b[31mred\x1b[0m',
    'bold_green': '\x1b[1;32mbold green\x1b[0m tail',
    'nested': '\x1b[1m a\x1b[31mb\x1b[0m\x1b[1m c \x1b[0m',
    'truecolor': '\x1b[38;2;255;100;0mORANGE\x1b[0m',
    'c256': '\x1b[38;5;208mx256\x1b[0m',
    'private_param': '\x1b[?25lhidden\x1b[?25h',
    'two_char_esc': 'a\x1bMb\x1b7c',
    'lone_esc': 'abc\x1b',
    'unterminated_csi': 'ab\x1b[31',
    'wide_cjk': '日本語のテキスト',
    'emoji': 'ok ✅ done 🚀',
    'accents': 'café naïve é',
    'colored_wide': '\x1b[36m日本語のテキスト\x1b[0m',
    'adjacent': '\x1b[1m\x1b[31m\x1b[4mX\x1b[0m\x1b[0m',
    'ellipsis_in': 'a…b',
    'newline': 'line1\n\x1b[32mline2\x1b[0m\tx',
}

GOLDEN_STRIP = {
    'empty': '',
    'plain': 'hello world',
    'red': 'red',
    'bold_green': 'bold green tail',
    'nested': ' ab c ',
    'truecolor': 'ORANGE',
    'c256': 'x256',
    'private_param': 'hidden',
    'two_char_esc': 'ab\x1b7c',
    'lone_esc': 'abc\x1b',
    'unterminated_csi': 'ab\x1b[31',
    'wide_cjk': '日本語のテキスト',
    'emoji': 'ok ✅ done 🚀',
    'accents': 'café naïve é',
    'colored_wide': '日本語のテキスト',
    'adjacent': 'X',
    'ellipsis_in': 'a…b',
    'newline': 'line1\nline2\tx',
}

GOLDEN_TRUNC = {
    ('empty', -1): '',
    ('empty', 0): '',
    ('empty', 1): '',
    ('empty', 2): '',
    ('empty', 3): '',
    ('empty', 5): '',
    ('empty', 10): '',
    ('plain', -1): '',
    ('plain', 0): '',
    ('plain', 1): '…',
    ('plain', 2): 'h…',
    ('plain', 3): 'he…',
    ('plain', 5): 'hell…',
    ('plain', 11): 'hello world',
    ('plain', 12): 'hello world',
    ('plain', 10): 'hello wor…',
    ('red', -1): '',
    ('red', 0): '',
    ('red', 1): '…',
    ('red', 2): '\x1b[31mr\x1b[0m…',
    ('red', 3): '\x1b[31mred\x1b[0m',
    ('red', 5): '\x1b[31mred\x1b[0m',
    ('red', 4): '\x1b[31mred\x1b[0m',
    ('red', 10): '\x1b[31mred\x1b[0m',
    ('bold_green', -1): '',
    ('bold_green', 0): '',
    ('bold_green', 1): '…',
    ('bold_green', 2): '\x1b[1;32mb\x1b[0m…',
    ('bold_green', 3): '\x1b[1;32mbo\x1b[0m…',
    ('bold_green', 5): '\x1b[1;32mbold\x1b[0m…',
    ('bold_green', 15): '\x1b[1;32mbold green\x1b[0m tail',
    ('bold_green', 16): '\x1b[1;32mbold green\x1b[0m tail',
    ('bold_green', 10): '\x1b[1;32mbold gree\x1b[0m…',
    ('nested', -1): '',
    ('nested', 0): '',
    ('nested', 1): '…',
    ('nested', 2): '\x1b[1m \x1b[0m…',
    ('nested', 3): '\x1b[1m a\x1b[0m…',
    ('nested', 5): '\x1b[1m a\x1b[31mb\x1b[0m\x1b[1m \x1b[0m…',
    ('nested', 6): '\x1b[1m a\x1b[31mb\x1b[0m\x1b[1m c \x1b[0m',
    ('nested', 7): '\x1b[1m a\x1b[31mb\x1b[0m\x1b[1m c \x1b[0m',
    ('nested', 10): '\x1b[1m a\x1b[31mb\x1b[0m\x1b[1m c \x1b[0m',
    ('truecolor', -1): '',
    ('truecolor', 0): '',
    ('truecolor', 1): '…',
    ('truecolor', 2): '\x1b[38;2;255;100;0mO\x1b[0m…',
    ('truecolor', 3): '\x1b[38;2;255;100;0mOR\x1b[0m…',
    ('truecolor', 5): '\x1b[38;2;255;100;0mORAN\x1b[0m…',
    ('truecolor', 6): '\x1b[38;2;255;100;0mORANGE\x1b[0m',
    ('truecolor', 7): '\x1b[38;2;255;100;0mORANGE\x1b[0m',
    ('truecolor', 10): '\x1b[38;2;255;100;0mORANGE\x1b[0m',
    ('c256', -1): '',
    ('c256', 0): '',
    ('c256', 1): '…',
    ('c256', 2): '\x1b[38;5;208mx\x1b[0m…',
    ('c256', 3): '\x1b[38;5;208mx2\x1b[0m…',
    ('c256', 5): '\x1b[38;5;208mx256\x1b[0m',
    ('c256', 4): '\x1b[38;5;208mx256\x1b[0m',
    ('c256', 10): '\x1b[38;5;208mx256\x1b[0m',
    ('private_param', -1): '',
    ('private_param', 0): '',
    ('private_param', 1): '…',
    ('private_param', 2): '\x1b[?25lh\x1b[0m…',
    ('private_param', 3): '\x1b[?25lhi\x1b[0m…',
    ('private_param', 5): '\x1b[?25lhidd\x1b[0m…',
    ('private_param', 6): '\x1b[?25lhidden\x1b[?25h',
    ('private_param', 7): '\x1b[?25lhidden\x1b[?25h',
    ('private_param', 10): '\x1b[?25lhidden\x1b[?25h',
    ('two_char_esc', -1): '',
    ('two_char_esc', 0): '',
    ('two_char_esc', 1): '…',
    ('two_char_esc', 2): 'a…',
    ('two_char_esc', 3): 'a\x1bMb\x1b[0m…',
    ('two_char_esc', 5): 'a\x1bMb\x1b7c',
    ('two_char_esc', 6): 'a\x1bMb\x1b7c',
    ('two_char_esc', 10): 'a\x1bMb\x1b7c',
    ('lone_esc', -1): '',
    ('lone_esc', 0): '',
    ('lone_esc', 1): '…',
    ('lone_esc', 2): 'a…',
    ('lone_esc', 3): 'ab…',
    ('lone_esc', 5): 'abc\x1b',
    ('lone_esc', 4): 'abc\x1b',
    ('lone_esc', 10): 'abc\x1b',
    ('unterminated_csi', -1): '',
    ('unterminated_csi', 0): '',
    ('unterminated_csi', 1): '…',
    ('unterminated_csi', 2): 'a…',
    ('unterminated_csi', 3): 'ab…',
    ('unterminated_csi', 5): 'ab\x1b[…',
    ('unterminated_csi', 6): 'ab\x1b[31',
    ('unterminated_csi', 7): 'ab\x1b[31',
    ('unterminated_csi', 10): 'ab\x1b[31',
    ('wide_cjk', -1): '',
    ('wide_cjk', 0): '',
    ('wide_cjk', 1): '…',
    ('wide_cjk', 2): '日…',
    ('wide_cjk', 3): '日本…',
    ('wide_cjk', 5): '日本語の…',
    ('wide_cjk', 8): '日本語のテキスト',
    ('wide_cjk', 9): '日本語のテキスト',
    ('wide_cjk', 10): '日本語のテキスト',
    ('emoji', -1): '',
    ('emoji', 0): '',
    ('emoji', 1): '…',
    ('emoji', 2): 'o…',
    ('emoji', 3): 'ok…',
    ('emoji', 5): 'ok ✅…',
    ('emoji', 11): 'ok ✅ done 🚀',
    ('emoji', 12): 'ok ✅ done 🚀',
    ('emoji', 10): 'ok ✅ done…',
    ('accents', -1): '',
    ('accents', 0): '',
    ('accents', 1): '…',
    ('accents', 2): 'c…',
    ('accents', 3): 'ca…',
    ('accents', 5): 'café…',
    ('accents', 13): 'café naïve é',
    ('accents', 14): 'café naïve é',
    ('accents', 10): 'café naïv…',
    ('colored_wide', -1): '',
    ('colored_wide', 0): '',
    ('colored_wide', 1): '…',
    ('colored_wide', 2): '\x1b[36m日\x1b[0m…',
    ('colored_wide', 3): '\x1b[36m日本\x1b[0m…',
    ('colored_wide', 5): '\x1b[36m日本語の\x1b[0m…',
    ('colored_wide', 8): '\x1b[36m日本語のテキスト\x1b[0m',
    ('colored_wide', 9): '\x1b[36m日本語のテキスト\x1b[0m',
    ('colored_wide', 10): '\x1b[36m日本語のテキスト\x1b[0m',
    ('adjacent', -1): '',
    ('adjacent', 0): '',
    ('adjacent', 1): '\x1b[1m\x1b[31m\x1b[4mX\x1b[0m\x1b[0m',
    ('adjacent', 2): '\x1b[1m\x1b[31m\x1b[4mX\x1b[0m\x1b[0m',
    ('adjacent', 3): '\x1b[1m\x1b[31m\x1b[4mX\x1b[0m\x1b[0m',
    ('adjacent', 5): '\x1b[1m\x1b[31m\x1b[4mX\x1b[0m\x1b[0m',
    ('adjacent', 10): '\x1b[1m\x1b[31m\x1b[4mX\x1b[0m\x1b[0m',
    ('ellipsis_in', -1): '',
    ('ellipsis_in', 0): '',
    ('ellipsis_in', 1): '…',
    ('ellipsis_in', 2): 'a…',
    ('ellipsis_in', 3): 'a…b',
    ('ellipsis_in', 5): 'a…b',
    ('ellipsis_in', 4): 'a…b',
    ('ellipsis_in', 10): 'a…b',
    ('newline', -1): '',
    ('newline', 0): '',
    ('newline', 1): '…',
    ('newline', 2): 'l…',
    ('newline', 3): 'li…',
    ('newline', 5): 'line…',
    ('newline', 13): 'line1\n\x1b[32mline2\x1b[0m\tx',
    ('newline', 14): 'line1\n\x1b[32mline2\x1b[0m\tx',
    ('newline', 10): 'line1\n\x1b[32mlin\x1b[0m…',
}


class TestAnsiEquivalenceGolden(unittest.TestCase):
    """Equivalence tests pinning strip_ansi and truncate_ansi to golden values."""

    def test_strip_ansi_matches_golden(self):
        """strip_ansi matches golden output across all corpus test cases."""
        for name, text in CORPUS.items():
            with self.subTest(case=name):
                self.assertEqual(strip_ansi(text), GOLDEN_STRIP[name])

    def test_truncate_ansi_matches_golden(self):
        """truncate_ansi matches golden output across all corpus test cases and boundary widths."""
        for (name, max_visible), expected in GOLDEN_TRUNC.items():
            with self.subTest(case=name, max_visible=max_visible):
                self.assertEqual(truncate_ansi(CORPUS[name], max_visible), expected)

    def test_render_table_with_truncated_ansi_has_equal_visible_width(self):
        """render_table containing colored and truncate_ansi cells preserves consistent line widths."""
        Colors.set_override(True)
        self.addCleanup(Colors.set_override, None)

        long_colored = Colors.red("ImportantHeaderDataExceedingLimit" * 3)
        truncated_cell = truncate_ansi(long_colored, 12)
        short_cell = Colors.green("OK")

        table = render_table(
            ["Service", "Status"],
            [
                [truncated_cell, short_cell],
                [Colors.cyan("MetricsCollector"), Colors.yellow("Pending")],
                [Colors.dim("Gateway"), Colors.bold("Ready")],
            ],
        )
        lines = [line for line in table.split("\n") if line]
        self.assertTrue(len(lines) > 0)
        widths = [len(strip_ansi(line)) for line in lines]
        self.assertEqual(len(set(widths)), 1, f"Mismatched rendered table line widths: {widths}")


class TestSharedAnsiRegexPattern(unittest.TestCase):
    """Tests for the hoisted module-level compiled pattern agy_usage.display._ANSI_RE."""

    def test_ansi_re_attribute_and_type(self):
        """_ANSI_RE exists at module level in agy_usage.display and is a compiled re.Pattern."""
        self.assertTrue(
            hasattr(display, "_ANSI_RE"),
            "agy_usage.display must define module-level compiled pattern _ANSI_RE",
        )
        self.assertIsInstance(display._ANSI_RE, re.Pattern)

    def test_ansi_re_pattern_string(self):
        """_ANSI_RE pattern string exactly matches the original regex source."""
        self.assertTrue(
            hasattr(display, "_ANSI_RE"),
            "agy_usage.display must define module-level compiled pattern _ANSI_RE",
        )
        expected_pattern = r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
        self.assertEqual(display._ANSI_RE.pattern, expected_pattern)

    def test_ansi_re_sub_matches_strip_ansi_on_corpus(self):
        """_ANSI_RE.sub('', s) matches strip_ansi(s) across every corpus input."""
        self.assertTrue(
            hasattr(display, "_ANSI_RE"),
            "agy_usage.display must define module-level compiled pattern _ANSI_RE",
        )
        for name, text in CORPUS.items():
            with self.subTest(case=name):
                self.assertEqual(display._ANSI_RE.sub("", text), strip_ansi(text))


class TestNoRecompilePerCall(unittest.TestCase):
    """Tests ensuring no re.compile occurs on strip_ansi/truncate_ansi invocations."""

    def test_no_recompile_on_strip_and_truncate_calls(self):
        """strip_ansi and truncate_ansi do not call re.compile when invoked."""
        with patch("re.compile", side_effect=re.compile) as mock_compile:
            for _ in range(1000):
                for name, raw in CORPUS.items():
                    strip_ansi(raw)
                    truncate_ansi(raw, 5)
                # Input that forces the truncation path
                long_colored = "\x1b[31m" + "x" * 50 + "\x1b[0m"
                truncate_ansi(long_colored, 5)

            self.assertEqual(
                mock_compile.call_count,
                0,
                f"re.compile was called {mock_compile.call_count} times; expected 0 (shared compiled pattern)",
            )

    def test_functions_use_shared_ansi_re_object(self):
        """strip_ansi and truncate_ansi delegate directly to the shared _ANSI_RE object."""
        self.assertTrue(
            hasattr(display, "_ANSI_RE"),
            "agy_usage.display must define module-level compiled pattern _ANSI_RE",
        )
        mock_pattern = MagicMock(wraps=display._ANSI_RE)
        with patch.object(display, "_ANSI_RE", mock_pattern):
            strip_ansi("\x1b[31mx\x1b[0m")
            self.assertTrue(mock_pattern.sub.called, "strip_ansi did not call _ANSI_RE.sub")

            mock_pattern.reset_mock()
            truncate_ansi("\x1b[31m" + "x" * 20 + "\x1b[0m", 5)
            self.assertTrue(mock_pattern.match.called, "truncate_ansi did not call _ANSI_RE.match")
            self.assertTrue(
                mock_pattern.sub.called,
                "truncate_ansi did not call _ANSI_RE.sub (via initial strip_ansi length check)",
            )


if __name__ == "__main__":
    unittest.main()
