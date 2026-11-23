"""Tests for Windows and Antigravity 2.0 compatibility across limits, CLI, and parser."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from agy_usage import cli, display, limits
from agy_usage.parser import AntigravityParser


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return io.BytesIO(self._body).read()


class TestEnvAndDaemonHubDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agy_win_compat_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache_path = os.path.join(self.tmp, "quota_cache.json")
        self.daemon_dir = os.path.join(self.tmp, "daemon")
        os.makedirs(self.daemon_dir, exist_ok=True)

    @staticmethod
    def _ok_rpc(expected_port=None, expected_csrf=None, tier_id="g1-ultra-tier"):
        def rpc(req, timeout=None):
            if expected_port is not None:
                assert f":{expected_port}/" in req.full_url, req.full_url
            if expected_csrf is not None:
                headers = {k.lower(): v for k, v in req.header_items()}
                assert headers.get("x-codeium-csrf-token") == expected_csrf, headers
            if req.full_url.endswith("RetrieveUserQuotaSummary"):
                return _Resp({
                    "response": {
                        "groups": [{
                            "buckets": [
                                {"bucketId": "gemini-5h", "remainingFraction": 0.88},
                                {"bucketId": "gemini-weekly", "remainingFraction": 0.95},
                            ]
                        }]
                    }
                })
            return _Resp({"userStatus": {"userTier": {"id": tier_id, "name": "Google AI Ultra"}}})

        return rpc

    def test_env_endpoint_succeeds_without_spawning_process_tools(self):
        errors = []
        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", self.cache_path), \
                mock.patch.object(limits, "_DAEMON_DIRS", ()), \
                mock.patch.dict(
                    os.environ,
                    {"ANTIGRAVITY_LS_ADDRESS": "localhost:10673", "ANTIGRAVITY_CSRF_TOKEN": "env-tok"},
                ), \
                mock.patch.object(limits.subprocess, "check_output", side_effect=AssertionError("should not run")), \
                mock.patch.object(limits.urllib.request, "urlopen", side_effect=self._ok_rpc(10673, "env-tok")):
            result = limits.fetch_live_quota_summary(errors=errors)

        self.assertIsNotNone(result)
        self.assertEqual(errors, [])
        self.assertEqual(result["user_tier_id"], "g1-ultra-tier")
        self.assertAlmostEqual(result["buckets"]["gemini-5h"]["remainingFraction"], 0.88)

    def test_daemon_hub_json_skips_dead_pid_and_uses_live_hub(self):
        dead_hub = os.path.join(self.daemon_dir, "hub_11111.json")
        live_hub = os.path.join(self.daemon_dir, "hub_22222.json")
        bad_hub = os.path.join(self.daemon_dir, "hub_33333.json")

        with open(dead_hub, "w", encoding="utf-8") as fp:
            json.dump({"pid": 999001, "httpPort": 11111, "csrfToken": "dead-tok"}, fp)
        with open(live_hub, "w", encoding="utf-8") as fp:
            json.dump({"pid": 999002, "httpPort": 22222, "csrfToken": "live-tok"}, fp)
        with open(bad_hub, "w", encoding="utf-8") as fp:
            fp.write("{invalid json")

        # Make dead_hub newer so liveness check must skip it rather than rely on mtime.
        os.utime(live_hub, (1_700_000_000, 1_700_000_000))
        os.utime(dead_hub, (1_700_000_100, 1_700_000_100))

        errors = []
        with mock.patch.object(limits, "_QUOTA_CACHE_PATH", self.cache_path), \
                mock.patch.object(limits, "_DAEMON_DIRS", (self.daemon_dir,)), \
                mock.patch.dict(os.environ, {"ANTIGRAVITY_LS_ADDRESS": "", "ANTIGRAVITY_CSRF_TOKEN": ""}), \
                mock.patch.object(limits, "_is_pid_running", side_effect=lambda pid: pid == 999002), \
                mock.patch.object(limits.subprocess, "check_output", side_effect=AssertionError("should not run")), \
                mock.patch.object(limits.urllib.request, "urlopen", side_effect=self._ok_rpc(22222, "live-tok")):
            result = limits.fetch_live_quota_summary(errors=errors)

        self.assertIsNotNone(result)
        self.assertEqual(errors, [])
        self.assertEqual(result["user_tier_id"], "g1-ultra-tier")


class TestWindowsProcessAndPortDiscovery(unittest.TestCase):
    def test_windows_process_discovery_parses_pid_and_csrf(self):
        ps_out = (
            "19848 \"C:\\Program Files\\Antigravity\\language_server.exe\" --standalone\r\n"
            "20400 \"C:\\Program Files\\Antigravity\\language_server.exe\" --csrf_token win-tok-1 --port 0\r\n"
        )
        notes = []
        with mock.patch.object(limits.sys, "platform", "win32"), \
                mock.patch.object(limits.subprocess, "check_output", return_value=ps_out.encode("utf-8")):
            ok, servers = limits._find_language_servers(notes.append)

        self.assertTrue(ok)
        self.assertEqual(servers, [("20400", "win-tok-1")])
        self.assertEqual(notes, [])

    def test_windows_netstat_filters_loopback_listening_for_pid(self):
        netstat_out = (
            "  Proto  Local Address          Foreign Address        State           PID\r\n"
            "  TCP    127.0.0.1:10673        0.0.0.0:0              LISTENING       20400\r\n"
            "  TCP    127.0.0.1:10674        127.0.0.1:50000        ESTABLISHED     20400\r\n"
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       20400\r\n"
            "  TCP    127.0.0.1:10675        0.0.0.0:0              LISTENING       99999\r\n"
        )
        notes = []
        with mock.patch.object(limits.sys, "platform", "win32"), \
                mock.patch.object(limits.subprocess, "check_output", return_value=netstat_out.encode("utf-8")):
            ports = limits._listening_ports("20400", notes.append)

        self.assertEqual(ports, [10673])
        self.assertEqual(notes, [])

    def test_windows_netstat_no_ports_notes_reason(self):
        notes = []
        with mock.patch.object(limits.sys, "platform", "win32"), \
                mock.patch.object(limits.subprocess, "check_output", return_value=b""):
            ports = limits._listening_ports("20400", notes.append)

        self.assertEqual(ports, [])
        self.assertEqual(notes, ["language_server pid 20400 is not listening on 127.0.0.1"])


class TestWindowsConsoleAndTerminal(unittest.TestCase):
    def test_ensure_utf8_stdio_reconfigures_gbk_stream(self):
        raw = io.BytesIO()
        wrapper = io.TextIOWrapper(raw, encoding="gbk", line_buffering=True)
        with mock.patch("sys.stdout", wrapper):
            cli._ensure_utf8_stdio()
            print("↳ 🟢 ┌─┐ █░")
        wrapper.flush()
        self.assertEqual(wrapper.encoding.lower(), "utf-8")
        self.assertIn("↳ 🟢 ┌─┐ █░", raw.getvalue().decode("utf-8"))

    def test_windows_terminal_enables_truecolor(self):
        with mock.patch("sys.platform", "win32"), \
                mock.patch.dict(os.environ, {"WT_SESSION": "abc-123"}, clear=True):
            self.assertTrue(display.supports_truecolor())


class TestSqliteCursorClosedBeforeConnection(unittest.TestCase):
    def test_mid_iteration_error_releases_db_file_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock_check.db")
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
                    "step_payload BLOB, metadata BLOB)"
                )
                for idx in range(1, 5):
                    conn.execute(
                        "INSERT INTO steps VALUES (?, 'user', '', ?)",
                        (idx, b"\x08\x01"),
                    )
                conn.commit()
            finally:
                conn.close()

            real_connect = sqlite3.connect

            class FailingCursor(sqlite3.Cursor):
                def __next__(self):
                    raise sqlite3.DatabaseError("mid-iteration failure")

            class FailingConn(sqlite3.Connection):
                def cursor(self, factory=FailingCursor):
                    return super().cursor(factory)

            with mock.patch(
                "agy_usage.parser.sqlite3.connect",
                side_effect=lambda *a, **kw: real_connect(*a, **{**kw, "factory": FailingConn}),
            ):
                summary = AntigravityParser.parse_session_db(path)

            self.assertIsNotNone(summary)
            # On Windows this raises PermissionError if the cursor remained unclosed.
            os.unlink(path)
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
