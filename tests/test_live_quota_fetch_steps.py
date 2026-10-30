"""Unit tests for the discovery, request and cache steps behind fetch_live_quota_summary."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agy_usage import limits


def _ps_line(pid, token="tok", prog="/bin/language_server_macos"):
    return f"{pid} {prog} --foo --csrf_token {token} --bar"


def _lsof(pid, *ports):
    return "COMMAND PID USER\n" + "".join(
        f"language_s {pid} u 9u IPv4 0x1 0t0 TCP 127.0.0.1:{p} (LISTEN)\n" for p in ports
    )


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return io.BytesIO(self._body).read()


class TestFindLanguageServers(unittest.TestCase):
    def _find(self, output):
        notes = []
        with mock.patch.object(limits.subprocess, "check_output", return_value=output.encode()):
            ps_ok, servers = limits._find_language_servers(notes.append)
        return ps_ok, servers, notes

    def test_parses_pid_and_token(self):
        ps_ok, servers, notes = self._find(
            "\n".join(["1 /bin/zsh", _ps_line(42, "abc"), _ps_line(43, "def")])
        )
        self.assertTrue(ps_ok)
        self.assertEqual(servers, [("42", "abc"), ("43", "def")])
        self.assertEqual(notes, [])

    def test_skips_lines_without_name_token_or_value(self):
        ps_ok, servers, _ = self._find(
            "\n".join(
                [
                    "7 /bin/other --csrf_token x",  # not a language_server
                    "8 /bin/language_server --nothing",  # no token flag
                    "9 /bin/language_server --csrf_token",  # flag without value
                    "language_server--csrf_token",  # no command column
                ]
            )
        )
        self.assertTrue(ps_ok)
        self.assertEqual(servers, [])

    def test_ps_failure_reports_class_name_and_not_ok(self):
        notes = []
        with mock.patch.object(limits.subprocess, "check_output", side_effect=OSError("x")):
            ps_ok, servers = limits._find_language_servers(notes.append)
        self.assertFalse(ps_ok)
        self.assertEqual(servers, [])
        self.assertEqual(notes, ["could not list processes (OSError)"])


class TestListeningPorts(unittest.TestCase):
    def test_collects_loopback_ports_only(self):
        out = _lsof(5, 6001, 6002) + "x 5 u TCP *:7000 (LISTEN)\nx 5 u TCP 127.0.0.1:7001 (ESTABLISHED)\n"
        notes = []
        with mock.patch.object(limits.subprocess, "check_output", return_value=out.encode()):
            self.assertEqual(limits._listening_ports("5", notes.append), [6001, 6002])
        self.assertEqual(notes, [])

    def test_no_ports_notes_not_listening(self):
        notes = []
        with mock.patch.object(limits.subprocess, "check_output", return_value=_lsof(5).encode()):
            self.assertEqual(limits._listening_ports("5", notes.append), [])
        self.assertEqual(notes, ["language_server pid 5 is not listening on 127.0.0.1"])

    def test_lsof_failure_returns_none(self):
        notes = []
        with mock.patch.object(limits.subprocess, "check_output", side_effect=TimeoutError()):
            self.assertIsNone(limits._listening_ports("5", notes.append))
        self.assertEqual(notes, ["could not list ports of language_server pid 5 (TimeoutError)"])


class TestPostConnect(unittest.TestCase):
    def test_builds_post_request_and_decodes_json(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen.update(url=req.full_url, method=req.get_method(), data=req.data, timeout=timeout)
            seen["headers"] = dict(req.header_items())
            return _Resp({"ok": 1})

        with mock.patch.object(limits.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = limits._post_connect("http://127.0.0.1:1/svc", "Method", {"X-Test": "1"}, 1.2)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(seen["url"], "http://127.0.0.1:1/svc/Method")
        self.assertEqual((seen["method"], seen["data"], seen["timeout"]), ("POST", b"{}", 1.2))
        self.assertEqual(seen["headers"], {"X-test": "1"})


class TestExtractBuckets(unittest.TestCase):
    def test_flattens_groups_and_skips_buckets_without_id(self):
        data = {
            "response": {
                "groups": [
                    {"buckets": [{"bucketId": "a", "remainingFraction": "0.5", "resetTime": "t"}, {"x": 1}]},
                    {"buckets": [{"bucketId": "b"}]},
                    {},
                ]
            }
        }
        self.assertEqual(
            limits._extract_buckets(data),
            {
                "a": {"bucketId": "a", "displayName": "", "description": "", "window": "",
                      "remainingFraction": 0.5, "resetTime": "t"},
                "b": {"bucketId": "b", "displayName": "", "description": "", "window": "",
                      "remainingFraction": 0.0, "resetTime": None},
            },
        )


class TestFetchOrchestration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="agy_fetch_steps_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        patcher = mock.patch.object(limits, "_QUOTA_CACHE_PATH", os.path.join(tmp, "q.json"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetch(self, ps, lsof, rpc, **kwargs):
        def check_output(cmd, **kw):
            return (ps if cmd[0] == "ps" else lsof[cmd[cmd.index("-p") + 1]]).encode()

        errors = []
        with mock.patch.object(limits.subprocess, "check_output", side_effect=check_output), \
                mock.patch.object(limits.urllib.request, "urlopen", side_effect=rpc):
            return limits.fetch_live_quota_summary(errors=errors, **kwargs), errors

    @staticmethod
    def _rpc(failing_ports=()):
        def rpc(req, timeout=None):
            port = int(req.full_url.split(":")[2].split("/")[0])
            if port in failing_ports:
                raise OSError(f"refused {port}")
            if req.full_url.endswith("RetrieveUserQuotaSummary"):
                return _Resp({"response": {"groups": [{"buckets": [{"bucketId": "gemini-5h", "remainingFraction": 0.3}]}]}})
            return _Resp({"userStatus": {"userTier": {"id": "g1-pro-tier", "name": "Pro"}}})

        return rpc

    def test_failing_port_is_noted_and_next_port_wins(self):
        result, errors = self._fetch(
            _ps_line(10), {"10": _lsof(10, 5002, 5001)}, self._rpc(failing_ports=(5001,))
        )
        self.assertEqual(errors, ["port 5001: refused 5001"])
        self.assertEqual(result["user_tier_id"], "g1-pro-tier")
        self.assertEqual(result["buckets"]["gemini-5h"]["remainingFraction"], 0.3)

    def test_note_order_when_every_step_fails(self):
        ps = "\n".join([_ps_line(10, "a"), _ps_line(11, "b"), _ps_line(12, "c")])
        lsof = {"10": _lsof(10), "11": _lsof(11, 5002, 5001), "12": _lsof(12, 5003)}
        result, errors = self._fetch(ps, lsof, self._rpc(failing_ports=(5001, 5002, 5003)))
        self.assertIsNone(result)
        self.assertEqual(
            errors,
            [
                "language_server pid 10 is not listening on 127.0.0.1",
                "port 5001: refused 5001",
                "port 5002: refused 5002",
                "port 5003: refused 5003",
            ],
        )

    def test_successful_fetch_is_written_to_the_cache(self):
        result, _ = self._fetch(_ps_line(10), {"10": _lsof(10, 5001)}, self._rpc())
        with open(limits._QUOTA_CACHE_PATH, encoding="utf-8") as fp:
            self.assertEqual(json.load(fp), result)


class TestReadQuotaCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="agy_cache_steps_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.path = os.path.join(tmp, "q.json")
        patcher = mock.patch.object(limits, "_QUOTA_CACHE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, age_seconds, buckets=None):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        with open(self.path, "w", encoding="utf-8") as fp:
            json.dump({"fetched_at": stamp, "buckets": {"a": {}} if buckets is None else buckets}, fp)

    def test_missing_file_is_none(self):
        self.assertIsNone(limits._read_quota_cache(lambda _: None))

    def test_fresh_cache_is_returned_as_is(self):
        self._write(5)
        notes = []
        cached = limits._read_quota_cache(notes.append)
        self.assertNotIn("source", cached)
        self.assertEqual(notes, [])

    def test_aged_cache_is_tagged_and_noted(self):
        self._write(3600)
        notes = []
        cached = limits._read_quota_cache(notes.append)
        self.assertEqual(cached["source"], "cached")
        self.assertAlmostEqual(cached["age_seconds"], 3600, delta=5)
        self.assertEqual(notes, ["cached server reading is 60 min old"])

    def test_too_old_or_empty_cache_is_rejected_after_noting_age(self):
        for age, buckets in ((limits._QUOTA_CACHE_MAX_AGE_SECONDS + 60, None), (3600, {})):
            with self.subTest(age=age, buckets=buckets):
                self._write(age, buckets)
                notes = []
                self.assertIsNone(limits._read_quota_cache(notes.append))
                self.assertEqual(len(notes), 1)

    def test_unreadable_cache_is_none(self):
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write("{not json")
        self.assertIsNone(limits._read_quota_cache(lambda _: None))

    def test_write_failure_is_swallowed(self):
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write("a file where a directory should be")
        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", os.path.join(self.path, "q.json")):
            limits._write_quota_cache({"buckets": {}})  # must not raise


class TestParseIso(unittest.TestCase):
    def test_parse_iso_returns_aware_or_naive(self):
        self.assertEqual(
            limits._parse_iso("2026-10-25T00:21:51Z"),
            datetime(2026, 10, 25, 0, 21, 51, tzinfo=timezone.utc),
        )
        self.assertEqual(
            limits._parse_iso(" 2026-10-25T00:21:51+05:30 ").utcoffset(), timedelta(hours=5, minutes=30)
        )
        self.assertEqual(limits._parse_iso("2026-10-25T00:21:51"), datetime(2026, 10, 25, 0, 21, 51))

    def test_parse_iso_rejects_bad_input(self):
        for bad in (None, "", "   ", "garbage", "Z", 123, b"x", [], object()):
            with self.subTest(bad=bad):
                self.assertIsNone(limits._parse_iso(bad))

    def test_fetched_at_normalises_to_utc(self):
        self.assertEqual(
            limits._parse_fetched_at("2026-10-25T00:21:51"),
            datetime(2026, 10, 25, 0, 21, 51, tzinfo=timezone.utc),
        )
        self.assertEqual(
            limits._parse_fetched_at("2026-10-25T05:51:51+05:30"),
            datetime(2026, 10, 25, 0, 21, 51, tzinfo=timezone.utc),
        )
        self.assertIsNone(limits._parse_fetched_at("garbage"))

    def test_local_naive_parse_keeps_its_broad_except_for_overflow(self):
        self.assertIsNone(limits._parse_utc_iso_to_local_naive(""))
        self.assertIsNone(limits._parse_utc_iso_to_local_naive(b"x"))
        got = limits._parse_utc_iso_to_local_naive("2026-10-25T00:21:51Z")
        expected = datetime(2026, 10, 25, 0, 21, 51, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(got, expected)
        # Naive stamps pass through untouched.
        self.assertEqual(
            limits._parse_utc_iso_to_local_naive("2026-10-25T00:21:51"), datetime(2026, 10, 25, 0, 21, 51)
        )
        # Extreme years can overflow the local-time conversion; that must still give None.
        for extreme in ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z"):
            with self.subTest(extreme=extreme):
                try:
                    datetime.fromisoformat(extreme.replace("Z", "+00:00")).astimezone()
                except (OverflowError, OSError, ValueError):
                    self.assertIsNone(limits._parse_utc_iso_to_local_naive(extreme))


if __name__ == "__main__":
    unittest.main()
