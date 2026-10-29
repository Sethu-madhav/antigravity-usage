"""Tests for scanning session directories."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.aggregator import UsageAggregator
from agy_usage.models import SessionSummary
from agy_usage.parser import AntigravityParser


def _write_usage_db(path, prompt=1000, output=200):
    """Minimal real session DB with one timed step carrying `prompt`/`output`."""
    def varint(value):
        out = bytearray()
        while True:
            b = value & 0x7F
            value >>= 7
            if value:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    ts = bytes([0x08]) + varint(1_751_234_567)
    usage = bytes([0x10]) + varint(prompt) + bytes([0x18]) + varint(output)
    metadata = bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
            "step_payload BLOB, metadata BLOB)"
        )
        conn.execute(
            "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (1, 'user', '', ?)",
            (metadata,),
        )
        conn.commit()
    finally:
        conn.close()
    return path


class TestScanSurvivesVanishingDb(unittest.TestCase):
    """Regression tests for BUG-016: Antigravity housekeeping can delete a DB
    while a scan is running; mtime lookups in the sort keys must not take the
    whole command down."""

    def test_scan_directory_skips_db_deleted_after_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept = _write_usage_db(os.path.join(tmp, "kept.db"))
            ghost = os.path.join(tmp, "ghost.db")  # globbed, then deleted before sorting
            with mock.patch("agy_usage.parser.glob.glob", return_value=[ghost, kept]):
                sessions = AntigravityParser.scan_directory(tmp, source="cli")

        self.assertEqual([s.session_id for s in sessions], ["kept"])

    def test_scan_all_sort_survives_untimed_session_whose_file_vanished(self):
        ghost = SessionSummary(
            session_id="ghost", source="cli", file_path="/nonexistent/ghost.db", start_time=None
        )
        timed = SessionSummary(
            session_id="timed",
            source="cli",
            file_path="/nonexistent/timed.db",
            start_time=datetime(2026, 1, 1, 12, 0, 0),
        )
        with mock.patch.object(
            AntigravityParser, "get_default_directories", return_value=[("/x", "cli")]
        ), mock.patch.object(AntigravityParser, "scan_directory", return_value=[ghost, timed]):
            sessions = AntigravityParser.scan_all()

        # The vanished file sorts as oldest (mtime 0) rather than raising.
        self.assertEqual([s.session_id for s in sessions], ["timed", "ghost"])


class TestScanDedupeAcrossSources(unittest.TestCase):
    """Regression tests for BUG-015: the same session DB present in both the
    CLI and IDE directories must be counted once, not twice."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cli_dir = os.path.join(self._tmp.name, "cli")
        self.ide_dir = os.path.join(self._tmp.name, "ide")
        os.mkdir(self.cli_dir)
        os.mkdir(self.ide_dir)
        for attr, path in (("DEFAULT_CLI_DIR", self.cli_dir), ("DEFAULT_IDE_DIR", self.ide_dir)):
            patcher = mock.patch.object(AntigravityParser, attr, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _db(self, directory, name, mtime, **usage):
        path = _write_usage_db(os.path.join(directory, name), **usage)
        os.utime(path, (mtime, mtime))
        return path

    def test_session_in_both_dirs_counted_once_keeping_newest_mtime(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000, output=200)
        self._db(self.ide_dir, "shared.db", 1_751_400_000, output=900)

        sessions = AntigravityParser.scan_all()

        self.assertEqual([s.session_id for s in sessions], ["shared"])
        self.assertEqual(sessions[0].source, "ide")
        self.assertEqual(sessions[0].usage.total_tokens, 1900)

    def test_newer_cli_copy_wins_over_stale_ide_copy(self):
        self._db(self.cli_dir, "shared.db", 1_751_400_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)

        sessions = AntigravityParser.scan_all()

        self.assertEqual([(s.session_id, s.source) for s in sessions], [("shared", "cli")])

    def test_equal_mtime_tie_is_deterministic(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)

        for _ in range(3):
            sessions = AntigravityParser.scan_all()
            self.assertEqual([(s.session_id, s.source) for s in sessions], [("shared", "cli")])

    def test_token_total_is_not_double_counted_and_matches_session_count(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)
        self._db(self.cli_dir, "only-cli.db", 1_751_300_000)
        self._db(self.ide_dir, "only-ide.db", 1_751_300_000)

        sessions = AntigravityParser.scan_all()

        self.assertEqual(sorted(s.session_id for s in sessions), ["only-cli", "only-ide", "shared"])
        self.assertEqual(sum(s.usage.total_tokens for s in sessions), 3 * 1200)
        daily = UsageAggregator.aggregate_daily(sessions)
        self.assertEqual(sum(len(d.session_ids) for d in daily.values()), 3)
        self.assertEqual(sum(d.usage.total_tokens for d in daily.values()), 3 * 1200)

    def test_single_source_modes_and_custom_dir_are_unaffected(self):
        self._db(self.cli_dir, "shared.db", 1_751_300_000)
        self._db(self.ide_dir, "shared.db", 1_751_300_000)

        cli_only = AntigravityParser.scan_all(source_mode="cli")
        ide_only = AntigravityParser.scan_all(source_mode="ide")
        custom = AntigravityParser.scan_all(custom_dir=self.ide_dir)

        self.assertEqual([s.source for s in cli_only], ["cli"])
        self.assertEqual([s.source for s in ide_only], ["ide"])
        self.assertEqual([s.session_id for s in custom], ["shared"])


if __name__ == "__main__":
    unittest.main()
