"""Heatmap level cut-offs: 3 thresholds separate the 4 non-empty shades.

The level and boundary assertions also pass on the pre-change code (which
returned an unused 4th, 85% threshold), proving the shading is unchanged.
"""

import unittest

from agy_usage.heatmap import TerminalHeatmap


def _level(max_val, val):
    """Map ``val`` to a shade level the way render() does for a given max."""
    hm = TerminalHeatmap({})
    return hm._get_level(val, hm._calculate_thresholds(max_val))


class TestHeatmapLevelThresholds(unittest.TestCase):
    def test_returns_exactly_three_cutoffs(self):
        hm = TerminalHeatmap({})
        for max_val in (1, 2, 3, 7, 87.5, 100, 12345):
            self.assertEqual(len(hm._calculate_thresholds(max_val)), 3, max_val)

    def test_non_positive_max_falls_back_to_one_two_three(self):
        hm = TerminalHeatmap({})
        self.assertEqual(hm._calculate_thresholds(0), [1, 2, 3])
        self.assertEqual(hm._calculate_thresholds(-5), [1, 2, 3])

    def test_band_boundaries_for_max_100(self):
        expected = {0: 0, 14: 1, 15: 2, 34: 2, 35: 3, 64: 3, 65: 4, 100: 4}
        for val, level in expected.items():
            self.assertEqual(_level(100, val), level, val)

    def test_small_maxima_keep_floored_levels(self):
        # Characterised on origin/master: the max(n, ...) floors make the
        # cut-offs [1, 2, 3], so 1 -> 2, 2 -> 3, 3 -> 4.
        cases = {
            1: {0: 0, 1: 2},
            2: {0: 0, 1: 2, 2: 3},
            3: {0: 0, 1: 2, 2: 3, 3: 4},
        }
        for max_val, expected in cases.items():
            for val, level in expected.items():
                self.assertEqual(_level(max_val, val), level, (max_val, val))

    def test_float_cache_efficiency_max_bands_the_same(self):
        # 87.5% max -> cut-offs int(13.125)=13, int(30.625)=30, int(56.875)=56.
        expected = {0.0: 0, 12.9: 1, 13.0: 2, 29.9: 2, 30.0: 3, 55.9: 3, 56.0: 4, 87.5: 4}
        for val, level in expected.items():
            self.assertEqual(_level(87.5, val), level, val)


if __name__ == "__main__":
    unittest.main()
