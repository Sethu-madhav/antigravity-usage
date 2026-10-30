"""Tests for the argparse flags shared across subcommands (defined once in cli.py)."""

import contextlib
import io
import os
import re
import unittest
from unittest import mock

from agy_usage.cli import parse_args

SUBCOMMANDS = (
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
)

LIMIT_ARGV = [
    "--tier", "ultra",
    "--5h-limit", "50M",
    "--weekly-limit", "1B",
    "--argon-weekly-limit", "25M",
    "--argon-remaining-pct", "40",
    "--claude-5h-limit", "3M",
    "--claude-weekly-limit", "7M",
]

LIMIT_DESTS = (
    "tier",
    "five_h_limit",
    "weekly_limit",
    "argon_weekly_limit",
    "argon_remaining_pct",
    "claude_five_h_limit",
    "claude_weekly_limit",
)


def _help_text(sub):
    buf = io.StringIO()
    with mock.patch.dict(os.environ, {"COLUMNS": "100"}):
        with contextlib.redirect_stdout(buf):
            with contextlib.suppress(SystemExit):
                parse_args([sub, "--help"])
    return buf.getvalue()


def _limit_option_strings(sub):
    """Long option strings of the limit flags, in the order --help lists them."""
    found = re.findall(r"^\s+((?:-[-\w]+(?:, | )?)+)", _help_text(sub), re.MULTILINE)
    options = [opt for line in found for opt in re.findall(r"--[\w-]+", line)]
    return [
        o for o in options
        if o in ("--tier", "--5h-limit", "--five-hour-limit", "--weekly-limit",
                 "--argon-weekly-limit", "--argon-remaining-pct",
                 "--claude-5h-limit", "--claude-weekly-limit")
    ]


class SharedGlobalFlagTests(unittest.TestCase):
    def test_flags_parse_before_and_after_every_subcommand(self):
        cases = (
            (["--json"], "json", True),
            (["--csv"], "csv", True),
            (["--no-color"], "no_color", True),
            (["--dir", "/x"], "custom_dir", "/x"),
            (["--cli-only"], "cli_only", True),
            (["--ide-only"], "ide_only", True),
        )
        for sub in SUBCOMMANDS:
            for flag, dest, expected in cases:
                with self.subTest(sub=sub, flag=flag[0], position="before"):
                    self.assertEqual(getattr(parse_args(flag + [sub]), dest), expected)
                with self.subTest(sub=sub, flag=flag[0], position="after"):
                    self.assertEqual(getattr(parse_args([sub] + flag), dest), expected)

    def test_root_value_not_clobbered_by_subparser(self):
        self.assertTrue(parse_args(["--json", "limits"]).json)
        self.assertEqual(parse_args(["--dir", "/x", "sessions"]).custom_dir, "/x")
        args = parse_args(["--json", "--csv", "--no-color", "statusline"])
        self.assertTrue(args.json and args.csv and args.no_color)

    def test_root_defaults_when_no_flags_given(self):
        for argv in ([], ["sessions"], ["limits"]):
            with self.subTest(argv=argv):
                args = parse_args(argv)
                self.assertIsNone(args.custom_dir)
                self.assertIs(args.json, False)
                self.assertIs(args.csv, False)
                self.assertIs(args.no_color, False)
                self.assertIs(args.cli_only, False)
                self.assertIs(args.ide_only, False)

    def test_data_source_flags_stay_mutually_exclusive(self):
        for argv in (
            ["--cli-only", "--ide-only"],
            ["sessions", "--cli-only", "--ide-only"],
            ["--dir", "/x", "sessions", "--cli-only"],
        ):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        parse_args(argv)
                self.assertEqual(cm.exception.code, 2)


class SharedLimitFlagTests(unittest.TestCase):
    def test_limits_and_statusline_accept_identical_limit_flags(self):
        limits = vars(parse_args(["limits"] + LIMIT_ARGV))
        statusline = vars(parse_args(["statusline"] + LIMIT_ARGV))
        for dest in LIMIT_DESTS:
            with self.subTest(dest=dest):
                self.assertEqual(limits[dest], statusline[dest])
        self.assertEqual(limits["tier"], "ultra")
        self.assertEqual(limits["five_h_limit"], "50M")
        self.assertEqual(limits["argon_remaining_pct"], 40.0)

    def test_five_hour_limit_alias_and_defaults(self):
        for sub in ("limits", "statusline"):
            with self.subTest(sub=sub):
                self.assertEqual(
                    parse_args([sub, "--five-hour-limit", "60M"]).five_h_limit, "60M"
                )
                args = parse_args([sub])
                for dest in LIMIT_DESTS:
                    self.assertIsNone(getattr(args, dest))

    def test_limit_flags_listed_in_same_order_in_help(self):
        limits = _limit_option_strings("limits")
        statusline = _limit_option_strings("statusline")
        self.assertEqual(len(limits), 8)
        self.assertEqual(limits, statusline)


if __name__ == "__main__":
    unittest.main()
