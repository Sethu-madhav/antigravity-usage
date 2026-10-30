"""Tests for raw numeric CSV cells in `limits --csv` (IMP-008)."""

import argparse
import csv
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

from agy_usage.cli import cmd_limits
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


class TestCsvNumericCells(unittest.TestCase):
    """Verify that limits --csv writes unformatted numbers for percentages and cost."""

    def setUp(self):
        patcher = mock.patch("agy_usage.limits.should_use_live_server", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self):
        t = datetime.now()
        usage = TokenUsage(prompt_tokens=1000, cached_tokens=4000, output_tokens=200)
        return SessionSummary(
            session_id="s-csv-numeric",
            source="cli",
            file_path="/tmp/test_numeric.db",
            title="CSV Numeric Test",
            workspace="ws",
            models=["gemini-3.8-flash"],
            start_time=t,
            end_time=t,
            step_count=1,
            usage=usage,
            steps=[StepRecord(step_idx=1, timestamp=t, model="gemini-3.8-flash", usage=usage)],
        )

    def test_limits_csv_cells_are_raw_numbers(self):
        session = self._session()
        args = argparse.Namespace(
            tier="pro",
            five_h_limit=None,
            weekly_limit=None,
            json=False,
            csv=True,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_limits([session], args)
        self.assertEqual(rc, 0)

        rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
        self.assertGreaterEqual(len(rows), 1)

        for row in rows:
            # Assert no cell contains % or $
            for col_name, val in row.items():
                self.assertNotIn("%", val, f"Column {col_name!r} contains %: {val!r}")
                self.assertNotIn("$", val, f"Column {col_name!r} contains $: {val!r}")

            # For known windows, quota cells and cost parse as numbers and are not empty
            pct_rem = row["Pct Remaining"]
            pct_used = row["Pct Used"]
            tok_rem = row["Tokens Remaining"]
            est_cost = row["Est Cost USD"]

            # Quota cells are empty only when window is unknown
            # In this test with local model, all windows are known
            self.assertNotEqual(pct_rem, "")
            self.assertNotEqual(pct_used, "")
            self.assertNotEqual(tok_rem, "")

            # Verify parsing
            parsed_pct_rem = float(pct_rem)
            parsed_pct_used = float(pct_used)
            parsed_tok_rem = int(tok_rem)
            parsed_est_cost = float(est_cost)

            self.assertIsInstance(parsed_pct_rem, float)
            self.assertIsInstance(parsed_pct_used, float)
            self.assertIsInstance(parsed_tok_rem, int)
            self.assertIsInstance(parsed_est_cost, float)


if __name__ == "__main__":
    unittest.main()
