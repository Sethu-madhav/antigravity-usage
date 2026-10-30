"""Tests for the live-server seam: should_use_live_server and its auto-detection.

IMP-006: ``LimitsCalculator.compute_all_pools`` queries the local Antigravity
language_server when a session path is a real file (or when there is no local
history at all). These tests exercise that decision directly and prove the
``agy_usage.limits.should_use_live_server`` seam gates it, without depending on
anything that does or does not exist outside tempfile-managed directories.
"""

import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.limits import LimitsCalculator, should_use_live_server
from agy_usage.models import SessionSummary, StepRecord, TokenUsage

_NOW = datetime(2026, 10, 28, 12, 0)


def _session(file_path):
    """Build one synthetic session whose only variable is its file path."""
    usage = TokenUsage(prompt_tokens=1000, cached_tokens=0, output_tokens=100)
    return SessionSummary(
        session_id="s-seam",
        source="cli",
        file_path=file_path,
        title="Live Server Seam Test",
        workspace="ws",
        models=["gemini-3.8-flash"],
        start_time=_NOW,
        end_time=_NOW,
        step_count=1,
        usage=usage,
        steps=[StepRecord(step_idx=1, timestamp=_NOW, model="gemini-3.8-flash", usage=usage)],
    )


class TestLiveServerSeam(unittest.TestCase):

    def _existing_file(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        return path

    def _missing_file(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, tmp)
        return os.path.join(tmp, "never-created.db")

    # --- should_use_live_server itself ---------------------------------

    def test_empty_sessions_ask_the_server(self):
        self.assertTrue(should_use_live_server([]))

    def test_session_without_file_path_does_not_ask(self):
        self.assertFalse(should_use_live_server([_session(None)]))

    def test_missing_session_file_does_not_ask(self):
        self.assertFalse(should_use_live_server([_session(self._missing_file())]))

    def test_existing_session_file_asks_the_server(self):
        self.assertTrue(should_use_live_server([_session(self._existing_file())]))

    # --- compute_all_pools honors an explicit flag ---------------------

    def test_explicit_false_skips_fetch_with_a_real_file(self):
        with mock.patch(
            "agy_usage.limits.fetch_live_quota_summary", return_value=None
        ) as fetch:
            pools = LimitsCalculator.compute_all_pools(
                [_session(self._existing_file())], use_live_server=False, now=_NOW
            )
        fetch.assert_not_called()
        self.assertEqual(pools.quota_source, "local")

    def test_explicit_true_fetches_once_for_empty_and_missing_history(self):
        for sessions in ([], [_session(self._missing_file())]):
            with self.subTest(sessions=sessions), mock.patch(
                "agy_usage.limits.fetch_live_quota_summary", return_value=None
            ) as fetch:
                LimitsCalculator.compute_all_pools(
                    sessions, use_live_server=True, now=_NOW
                )
                fetch.assert_called_once()

    # --- auto-detection through compute_all_pools ----------------------

    def test_auto_detection_fetches_for_existing_session_files(self):
        with mock.patch(
            "agy_usage.limits.fetch_live_quota_summary", return_value=None
        ) as fetch:
            LimitsCalculator.compute_all_pools(
                [_session(self._existing_file())], now=_NOW
            )
        fetch.assert_called_once()

    def test_auto_detection_skips_fetch_for_missing_session_files(self):
        with mock.patch(
            "agy_usage.limits.fetch_live_quota_summary", return_value=None
        ) as fetch:
            LimitsCalculator.compute_all_pools(
                [_session(self._missing_file())], now=_NOW
            )
        fetch.assert_not_called()

    # --- the seam is patchable -----------------------------------------

    def test_seam_patch_gates_fetch(self):
        with mock.patch(
            "agy_usage.limits.should_use_live_server", return_value=False
        ) as seam, mock.patch(
            "agy_usage.limits.fetch_live_quota_summary", return_value=None
        ) as fetch:
            LimitsCalculator.compute_all_pools(
                [_session(self._existing_file())], now=_NOW
            )
        seam.assert_called_once()
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
