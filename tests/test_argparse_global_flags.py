"""Tests for global flags before and after subcommands."""

import unittest


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
        self.assertTrue(self._parse(["--csv", "limits"]).csv)
        self.assertTrue(self._parse(["--json", "statusline"]).json)
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


if __name__ == "__main__":
    unittest.main()
