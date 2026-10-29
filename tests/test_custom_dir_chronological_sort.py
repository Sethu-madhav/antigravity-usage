"""Regression tests for BUG-022: `scan_all(custom_dir=...)` must sort sessions
by start_time like the default path, not by file mtime."""

import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.models import SessionSummary
from agy_usage.parser import AntigravityParser


def _summary(session_id, path, start_time):
    return SessionSummary(
        session_id=session_id, source="custom", file_path=path, start_time=start_time
    )


class TestCustomDirChronologicalSort(unittest.TestCase):
    def test_custom_dir_sorted_by_start_time_not_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = os.path.join(tmp, "old.db")
            new_path = os.path.join(tmp, "new.db")
            for p in (old_path, new_path):
                open(p, "w").close()
            # The older session was touched most recently, so mtime order is the
            # reverse of start_time order.
            os.utime(new_path, (1_000_000, 1_000_000))
            os.utime(old_path, (2_000_000, 2_000_000))
            old = _summary("old", old_path, datetime(2026, 1, 1, 12, 0, 0))
            new = _summary("new", new_path, datetime(2026, 6, 1, 12, 0, 0))

            # scan_directory yields mtime order (old first), as the real one does.
            with mock.patch.object(AntigravityParser, "scan_directory", return_value=[old, new]):
                sessions = AntigravityParser.scan_all(custom_dir=tmp)

        self.assertEqual([s.session_id for s in sessions], ["new", "old"])

    def test_custom_dir_untimed_session_falls_back_to_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            untimed_path = os.path.join(tmp, "untimed.db")
            open(untimed_path, "w").close()
            os.utime(untimed_path, (1_900_000_000, 1_900_000_000))  # newer than timed
            untimed = _summary("untimed", untimed_path, None)
            timed = _summary("timed", os.path.join(tmp, "timed.db"), datetime(2020, 1, 1))

            with mock.patch.object(
                AntigravityParser, "scan_directory", return_value=[timed, untimed]
            ):
                sessions = AntigravityParser.scan_all(custom_dir=tmp)

        self.assertEqual([s.session_id for s in sessions], ["untimed", "timed"])

    def test_custom_dir_untimed_session_with_vanished_file_sorts_oldest(self):
        ghost = _summary("ghost", "/nonexistent/ghost.db", None)
        timed = _summary("timed", "/nonexistent/timed.db", datetime(2026, 1, 1, 12, 0, 0))

        with mock.patch.object(AntigravityParser, "scan_directory", return_value=[ghost, timed]):
            sessions = AntigravityParser.scan_all(custom_dir="/nonexistent")

        self.assertEqual([s.session_id for s in sessions], ["timed", "ghost"])


if __name__ == "__main__":
    unittest.main()
