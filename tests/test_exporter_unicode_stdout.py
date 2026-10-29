"""Tests for exporting non-ASCII text to a non-UTF-8 stdout."""

import io
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.exporter import UsageExporter
from agy_usage.models import SessionSummary, TokenUsage


class TestExporterUnicodeStdout(unittest.TestCase):
    """BUG-020: stdout export must not crash on non-console-encodable characters."""

    TITLE = "Launch 🚀 日本語"
    WORKSPACE = "/work/プロジェクト🚀"

    def _session(self):
        return SessionSummary(
            session_id="s-uni",
            source="cli",
            file_path="/tmp/s-uni.db",
            title=self.TITLE,
            workspace=self.WORKSPACE,
            models=["m1"],
            start_time=datetime(2026, 9, 1, 12, 0),
            usage=TokenUsage(prompt_tokens=10, cached_tokens=90, output_tokens=5),
        )

    @staticmethod
    def _cp1252_wrapper():
        raw = io.BytesIO()
        wrapper = io.TextIOWrapper(raw, encoding="cp1252", line_buffering=True)
        return raw, wrapper

    def test_csv_export_to_cp1252_stdout_reconfigures_to_utf8(self):
        raw, wrapper = self._cp1252_wrapper()
        with mock.patch("sys.stdout", wrapper):
            UsageExporter.export([self._session()], format_type="csv")
        wrapper.flush()
        self.assertEqual(wrapper.encoding.lower(), "utf-8")
        self.assertEqual(wrapper.errors, "replace")
        text = raw.getvalue().decode("utf-8")
        self.assertIn(self.TITLE, text)
        self.assertIn(self.WORKSPACE, text)
        self.assertTrue(text.endswith("\n"))

    def test_json_export_to_cp1252_stdout_reconfigures_to_utf8(self):
        import json

        raw, wrapper = self._cp1252_wrapper()
        with mock.patch("sys.stdout", wrapper):
            UsageExporter.export([self._session()], format_type="json", output_path="-")
        wrapper.flush()
        self.assertEqual(wrapper.encoding.lower(), "utf-8")
        self.assertEqual(wrapper.errors, "replace")
        data = json.loads(raw.getvalue().decode("utf-8"))
        self.assertEqual(data["sessions"][0]["title"], self.TITLE)
        self.assertEqual(data["sessions"][0]["workspace"], self.WORKSPACE)

    def test_export_falls_back_when_reconfigure_unavailable_or_unsupported(self):
        class NoReconfigure:
            encoding = "cp1252"

            def __init__(self):
                self.buf = bytearray()

            def write(self, s):
                self.buf.extend(s.encode("cp1252"))
                return len(s)

        class RaisingReconfigure(NoReconfigure):
            def reconfigure(self, **kwargs):
                raise io.UnsupportedOperation("reconfigure unsupported")

        class MissingEncodingAttr(NoReconfigure):
            encoding = None

        for cls in (NoReconfigure, RaisingReconfigure, MissingEncodingAttr):
            with self.subTest(stream=cls.__name__):
                stream = cls()
                with mock.patch("sys.stdout", stream):
                    UsageExporter.export([self._session()], format_type="csv")
                text = bytes(stream.buf).decode("cp1252")
                self.assertTrue(text.endswith("\n"))
                self.assertIn("s-uni,cli,", text)
                self.assertIn("Launch ? ???", text)
                self.assertNotIn("🚀", text)
                self.assertNotIn("日本語", text)

    def test_export_to_file_and_stringio_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            UsageExporter.export([self._session()], format_type="csv", output_path=path)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn(self.TITLE, content)
            self.assertIn(self.WORKSPACE, content)

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            UsageExporter.export([self._session()], format_type="csv")
        out = buf.getvalue()
        self.assertIn(self.TITLE, out)
        self.assertIn(self.WORKSPACE, out)
        self.assertTrue(out.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
