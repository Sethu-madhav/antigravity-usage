"""BUG-025: adjacent month starts at heatmap columns 0 and 1 must not drop a month."""

import unittest
from datetime import date, timedelta

from agy_usage.display import Colors, strip_ansi
from agy_usage.heatmap import TerminalHeatmap

LABEL_INDENT = 5
COL_WIDTH = 2


def _header(weeks, end_date):
    hm = TerminalHeatmap({}, weeks=weeks, end_date=end_date, term_cols=200)
    return strip_ansi(hm.render().split("\n")[0])


def _month_starts(weeks, end_date):
    """Independently compute (column, name) for every month start in the window."""
    end_sunday = end_date + timedelta(days=6 - end_date.weekday())
    start_monday = end_sunday - timedelta(days=weeks * 7 - 1)
    starts = []
    prev_month = -1
    for w in range(weeks):
        col_date = start_monday + timedelta(days=w * 7)
        if col_date.month != prev_month:
            starts.append((w, TerminalHeatmap.MONTH_NAMES[col_date.month - 1]))
            prev_month = col_date.month
    return starts


def _expected_labels(starts):
    if len(starts) >= 2 and starts[0][0] == 0 and starts[1][0] == 1:
        return starts[1:]
    return starts


class TestHeatmapAdjacentFirstMonths(unittest.TestCase):
    def setUp(self):
        Colors.set_override(False)
        self.addCleanup(Colors.set_override, None)

    def test_adjacent_first_two_columns_drops_leading_partial_month(self):
        header = _header(26, date(2026, 1, 19))

        self.assertEqual(header[:LABEL_INDENT], " " * LABEL_INDENT)
        # Column 0 (Jul, one-column partial month) is dropped.
        self.assertEqual(header[5:7], "  ")
        self.assertNotIn("Jul", header)
        # Aug starts at column 1, Sep at 5, and later months follow exactly.
        for w, name in [(1, "Aug"), (5, "Sep"), (10, "Oct"), (14, "Nov"), (18, "Dec"), (23, "Jan")]:
            offset = LABEL_INDENT + COL_WIDTH * w
            self.assertEqual(header[offset:offset + len(name)], name, f"{name} at column {w}: {header!r}")

    def test_non_adjacent_first_month_preserved(self):
        header = _header(10, date(2026, 10, 28))

        self.assertEqual(header, "     Aug Sep     Oct")
        self.assertEqual(header[5:8], "Aug")

    def test_fuzz_all_end_dates_across_year_no_full_month_dropped_and_exact_alignment(self):
        for weeks in (4, 12, 26, 52):
            for i in range(365):
                end_date = date(2026, 1, 1) + timedelta(days=i)
                with self.subTest(weeks=weeks, end_date=end_date):
                    header = _header(weeks, end_date)
                    expected = _expected_labels(_month_starts(weeks, end_date))

                    prev_end = None
                    for w, name in expected:
                        offset = LABEL_INDENT + COL_WIDTH * w
                        self.assertEqual(header[offset:offset + len(name)], name, header)
                        if prev_end is not None:
                            self.assertGreaterEqual(offset, prev_end, f"overlap in {header!r}")
                        prev_end = offset + len(name)

                    # Nothing but the expected labels is rendered.
                    self.assertEqual(
                        header.split(),
                        [name for _, name in expected],
                        header,
                    )


if __name__ == "__main__":
    unittest.main()
