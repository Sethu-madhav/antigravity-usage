import contextlib
import io
import re
import unittest

from agy_usage.cli import parse_args


def _help_output():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            parse_args(["--help"])
        except SystemExit as exc:
            code = exc.code
    return code, buf.getvalue()


def _help_examples():
    return _help_output()[1].split("Examples:", 1)[1]


def _subcommand_names():
    """Subcommands as listed in the `{a,b,...}` usage choices of --help."""
    usage = _help_output()[1]
    match = re.search(r"\{([a-z,]+)\}", usage)
    return match.group(1).split(",")


class HelpEpilogTests(unittest.TestCase):
    def test_help_exits_zero(self):
        code, out = _help_output()
        self.assertEqual(code, 0)
        self.assertIn("Examples:", out)

    def test_new_examples_present(self):
        tail = _help_examples()
        self.assertIn("agy-usage statusline", tail)
        self.assertIn("agy-usage live", tail)
        self.assertIn("agy-usage --no-color", tail)

    def test_every_subcommand_has_an_example(self):
        tail = _help_examples()
        names = _subcommand_names()
        self.assertTrue(names)
        for name in names:
            with self.subTest(subcommand=name):
                if name == "overview":
                    # Default command is documented as the bare invocation.
                    self.assertRegex(tail, r"agy-usage {2,}\S")
                elif name == "session":
                    self.assertIn("agy-usage session <id>", tail)
                else:
                    self.assertIn(f"agy-usage {name}", tail)


if __name__ == "__main__":
    unittest.main()
