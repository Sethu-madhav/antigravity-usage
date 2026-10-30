"""Tests for tier auto-detection from the server's ``user_tier_id`` (FEAT-003).

Without ``--tier``, ``limits`` and ``statusline`` read the tier the live
Antigravity server reports. An explicit ``--tier`` always wins, and when the
server is unreachable or its tier id is missing or unknown the old default
(pro) applies silently, with no "auto-detected" line.
"""

import argparse
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agy_usage import limits
from agy_usage.cli import cmd_limits, cmd_statusline, parse_args
from agy_usage.limits import LimitsCalculator, tier_key_from_server_id


def _buckets(remaining=0.9):
    reset = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    return {
        bucket_id: {"bucketId": bucket_id, "remainingFraction": remaining, "resetTime": reset}
        for bucket_id in ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly", "gemini-argon-wtu-weekly")
    }


def _server(tier_id, **extra):
    data = {"buckets": _buckets(), "user_tier_id": tier_id, "user_tier_name": "ignored"}
    data.update(extra)
    return mock.patch.object(limits, "fetch_live_quota_summary", return_value=data)


def _server_down():
    def fake(use_cache=True, errors=None):
        if errors is not None:
            errors.append("no running Antigravity language_server found")
        return None

    return mock.patch.object(limits, "fetch_live_quota_summary", side_effect=fake)


def _capture(fn, *args):
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        fn(*args)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


def _limits_args(**overrides):
    values = dict(tier=None, five_h_limit=None, weekly_limit=None, json=False, csv=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class TestTierKeyMapping(unittest.TestCase):
    def test_observed_pro_id(self):
        self.assertEqual(tier_key_from_server_id("g1-pro-tier"), "pro")

    def test_ultra_and_free_ids(self):
        self.assertEqual(tier_key_from_server_id("g1-ultra-tier"), "ultra")
        self.assertEqual(tier_key_from_server_id("free-tier"), "free")

    def test_case_insensitive(self):
        self.assertEqual(tier_key_from_server_id("G1-ULTRA-TIER"), "ultra")

    def test_unknown_missing_and_ambiguous_ids_return_none(self):
        for value in (None, "", 7, "standard-tier", "g1-professional-tier", "pro-ultra-tier"):
            self.assertIsNone(tier_key_from_server_id(value), value)


class TestComputeAllPoolsTier(unittest.TestCase):
    def test_auto_uses_server_tier(self):
        with _server("g1-ultra-tier"):
            pools = LimitsCalculator.compute_all_pools([], tier_name=None, use_live_server=True)
        self.assertEqual(pools.tier.name, "ultra")
        self.assertEqual(pools.tier_source, "auto")

    def test_explicit_tier_wins_over_server(self):
        with _server("g1-ultra-tier"):
            pools = LimitsCalculator.compute_all_pools([], tier_name="free", use_live_server=True)
        self.assertEqual(pools.tier.name, "free")
        self.assertEqual(pools.tier_source, "explicit")

    def test_explicit_pro_still_wins_over_server(self):
        with _server("g1-ultra-tier"):
            pools = LimitsCalculator.compute_all_pools([], tier_name="pro", use_live_server=True)
        self.assertEqual(pools.tier.name, "pro")
        self.assertEqual(pools.tier_source, "explicit")

    def test_unreachable_server_falls_back_to_pro(self):
        with _server_down():
            pools = LimitsCalculator.compute_all_pools([], tier_name=None, use_live_server=True)
        self.assertEqual(pools.tier.name, "pro")
        self.assertEqual(pools.tier_source, "default")

    def test_live_server_not_consulted_falls_back_to_pro(self):
        pools = LimitsCalculator.compute_all_pools([], tier_name=None, use_live_server=False)
        self.assertEqual(pools.tier.name, "pro")
        self.assertEqual(pools.tier_source, "default")

    def test_missing_or_unknown_tier_id_falls_back_to_pro(self):
        for tier_id in (None, "something-new"):
            with _server(tier_id):
                pools = LimitsCalculator.compute_all_pools([], tier_name=None, use_live_server=True)
            self.assertEqual(pools.tier.name, "pro", tier_id)
            self.assertEqual(pools.tier_source, "default", tier_id)

    def test_stale_cached_reading_is_not_used_for_tier(self):
        # Server unreachable now: the last cached reading is only a quota estimate.
        with _server("g1-ultra-tier", source="cached", age_seconds=600.0,
                     fetched_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()):
            pools = LimitsCalculator.compute_all_pools([], tier_name=None, use_live_server=True)
        self.assertEqual(pools.quota_source, "cached")
        self.assertEqual(pools.tier.name, "pro")
        self.assertEqual(pools.tier_source, "default")


class TestCliOutput(unittest.TestCase):
    def test_tier_flag_defaults_to_none(self):
        self.assertIsNone(parse_args(["limits"]).tier)
        self.assertIsNone(parse_args(["statusline"]).tier)
        self.assertEqual(parse_args(["limits", "--tier", "free"]).tier, "free")

    def test_limits_prints_auto_detected_line(self):
        with _server("g1-ultra-tier"):
            out = _capture(cmd_limits, [], _limits_args())
        self.assertIn("auto-detected (Ultra)", out)
        self.assertIn("Google AI Ultra", out)

    def test_limits_explicit_tier_has_no_auto_line(self):
        with _server("g1-ultra-tier"):
            out = _capture(cmd_limits, [], _limits_args(tier="free"))
        self.assertNotIn("auto-detected", out)
        self.assertIn("Google AI Free", out)

    def test_limits_server_down_has_no_auto_line_and_uses_pro(self):
        with _server_down():
            out = _capture(cmd_limits, [], _limits_args())
        self.assertNotIn("auto-detected", out)
        self.assertIn("Google AI Pro", out)

    def test_limits_unknown_tier_id_has_no_auto_line(self):
        with _server("something-new"):
            out = _capture(cmd_limits, [], _limits_args())
        self.assertNotIn("auto-detected", out)
        self.assertIn("Google AI Pro", out)

    def test_statusline_json_uses_detected_tier(self):
        args = argparse.Namespace(
            tier=None, five_h_limit=None, weekly_limit=None, short=False, plain=False,
            tmux=False, json=True, csv=False, format=None, no_color=True,
        )
        with _server("g1-ultra-tier"):
            out = _capture(cmd_statusline, [], args)
        self.assertEqual(json.loads(out)["tier"], "ultra")


if __name__ == "__main__":
    unittest.main()
