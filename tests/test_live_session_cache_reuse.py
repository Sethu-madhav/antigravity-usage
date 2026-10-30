"""`live` loads the session cache once per run and reuses it across ticks."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agy_usage import cache as cache_mod
from agy_usage import cli
from agy_usage.cache import SessionCache
from agy_usage.parser import AntigravityParser


def _varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _step_metadata(prompt: int, output: int) -> bytes:
    ts = bytes([0x08]) + _varint(1_751_234_567)
    usage = bytes([0x10]) + _varint(prompt) + bytes([0x18]) + _varint(output)
    return bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage


def _write_db(path: str, prompt: int = 1000, output: int = 200) -> str:
    """Create a minimal valid session DB with one step."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
            "step_payload BLOB, metadata BLOB)"
        )
        conn.execute(
            "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (1, 'user', '', ?)",
            (_step_metadata(prompt, output),),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _append_step(path: str, prompt: int, output: int) -> None:
    """Add a second step and push the file mtime forward so the fingerprint changes."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO steps (idx, step_type, step_payload, metadata) VALUES (2, 'user', '', ?)",
            (_step_metadata(prompt, output),),
        )
        conn.commit()
    finally:
        conn.close()
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


class _LiveCacheTestBase(unittest.TestCase):
    def setUp(self):
        tmp_cache = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_cache.cleanup)
        tmp_data = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_data.cleanup)
        self.cache_dir = tmp_cache.name
        self.data_dir = tmp_data.name
        env = dict(os.environ)
        env["AGY_USAGE_CACHE_DIR"] = self.cache_dir
        env.pop("AGY_USAGE_NO_CACHE", None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _live_args(self, *extra):
        return cli.parse_args(["--dir", self.data_dir, "live", "--json", "--interval", "1", *extra])

    def _run_live(self, args, sleep_side_effect):
        out = io.StringIO()
        with mock.patch("agy_usage.cli.time.sleep", side_effect=sleep_side_effect), redirect_stdout(out):
            rc = cli.cmd_live(args)
        self.assertEqual(rc, 0)
        return [json.loads(line) for line in out.getvalue().splitlines()]


class TestLiveLoadsCacheOnce(_LiveCacheTestBase):
    def test_cache_loaded_once_across_ticks(self):
        _write_db(os.path.join(self.data_dir, "s1.db"))
        args = self._live_args()
        with mock.patch.object(SessionCache, "load", wraps=SessionCache.load) as load:
            ticks = self._run_live(args, [None, None, KeyboardInterrupt()])
        self.assertEqual(len(ticks), 3)
        self.assertEqual(load.call_count, 1)

    def test_args_carry_no_cache_after_return(self):
        _write_db(os.path.join(self.data_dir, "s1.db"))
        args = self._live_args()
        self._run_live(args, [KeyboardInterrupt()])
        self.assertFalse(hasattr(args, "_session_cache"))

    def test_args_carry_no_cache_after_screen_mode(self):
        args = cli.parse_args(["--dir", self.data_dir, "live", "--interval", "1"])
        with mock.patch("agy_usage.cli.get_sessions", return_value=[]), \
                mock.patch("agy_usage.cli.cmd_overview"), \
                mock.patch("agy_usage.cli.time.sleep", side_effect=KeyboardInterrupt()), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_live(args), 0)
        self.assertFalse(hasattr(args, "_session_cache"))

    def test_args_carry_no_cache_after_error(self):
        args = self._live_args()
        with mock.patch("agy_usage.cli.get_sessions", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                cli.cmd_live(args)
        self.assertFalse(hasattr(args, "_session_cache"))

    def test_get_sessions_sees_the_live_cache(self):
        args = self._live_args()
        seen = []

        def fake_get_sessions(a):
            seen.append(a._session_cache)
            return []

        with mock.patch("agy_usage.cli.get_sessions", side_effect=fake_get_sessions):
            self._run_live(args, [None, KeyboardInterrupt()])
        self.assertEqual(len(seen), 2)
        self.assertIsInstance(seen[0], SessionCache)
        self.assertIs(seen[0], seen[1])

    def test_no_cache_env_still_disables_caching(self):
        db = _write_db(os.path.join(self.data_dir, "s1.db"))
        args = self._live_args()
        with mock.patch.dict(os.environ, {"AGY_USAGE_NO_CACHE": "1"}), \
                mock.patch.object(
                    AntigravityParser,
                    "_parse_session_db_checked",
                    wraps=AntigravityParser._parse_session_db_checked,
                ) as parse:
            self._run_live(args, [None, KeyboardInterrupt()])
        self.assertEqual([c.args[0] for c in parse.call_args_list], [db, db])
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, "sessions.json")))


class TestLivePicksUpChangesThroughPersistentCache(_LiveCacheTestBase):
    def test_new_and_modified_dbs_between_ticks(self):
        s1 = _write_db(os.path.join(self.data_dir, "s1.db"), prompt=1000, output=200)
        s2 = os.path.join(self.data_dir, "s2.db")

        def mutate(_interval):
            mutate.tick += 1
            if mutate.tick == 1:
                _write_db(s2, prompt=3000, output=300)
            elif mutate.tick == 2:
                _append_step(s1, prompt=500, output=100)
            else:
                raise KeyboardInterrupt

        mutate.tick = 0
        args = self._live_args()
        with mock.patch.object(
            AntigravityParser,
            "_parse_session_db_checked",
            wraps=AntigravityParser._parse_session_db_checked,
        ) as parse:
            ticks = self._run_live(args, mutate)

        self.assertEqual([t["summary"]["session_count"] for t in ticks], [1, 2, 2])
        self.assertEqual(
            [t["summary"]["total_usage"]["total_tokens"] for t in ticks],
            [1200, 4500, 5100],
        )
        # tick 1 parses s1, tick 2 only the new s2, tick 3 only the modified s1
        self.assertEqual([c.args[0] for c in parse.call_args_list], [s1, s2, s1])
        with open(os.path.join(self.cache_dir, "sessions.json"), encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        self.assertEqual(sorted(entries), sorted(os.path.abspath(p) for p in (s1, s2)))


class TestScanAllCacheArgument(_LiveCacheTestBase):
    def test_given_cache_is_used_without_loading(self):
        db = _write_db(os.path.join(self.data_dir, "s1.db"))
        c = SessionCache.load()
        with mock.patch.object(SessionCache, "load") as load:
            sessions = AntigravityParser.scan_all(custom_dir=self.data_dir, cache=c)
        load.assert_not_called()
        self.assertEqual(len(sessions), 1)
        self.assertIsNotNone(c.get(db, "custom"))

    def test_use_cache_false_ignores_given_cache(self):
        _write_db(os.path.join(self.data_dir, "s1.db"))
        c = mock.create_autospec(SessionCache, instance=True)
        with mock.patch.object(SessionCache, "load") as load:
            sessions = AntigravityParser.scan_all(custom_dir=self.data_dir, use_cache=False, cache=c)
        load.assert_not_called()
        self.assertEqual(c.mock_calls, [])
        self.assertEqual(len(sessions), 1)


class TestGetSessionsScanAllKwargs(unittest.TestCase):
    def test_without_live_cache_kwargs_are_unchanged(self):
        args = cli.parse_args(["--dir", "/nonexistent", "sessions"])
        with mock.patch.object(AntigravityParser, "scan_all", return_value=[]) as scan_all:
            cli.get_sessions(args)
        scan_all.assert_called_once_with(custom_dir="/nonexistent", source_mode="all")

    def test_with_live_cache_it_is_passed_through(self):
        args = cli.parse_args(["--dir", "/nonexistent", "sessions"])
        c = SessionCache(enabled=False)
        args._session_cache = c
        with mock.patch.object(AntigravityParser, "scan_all", return_value=[]) as scan_all:
            cli.get_sessions(args)
        scan_all.assert_called_once_with(custom_dir="/nonexistent", source_mode="all", cache=c)


class TestCodeHashMemoised(unittest.TestCase):
    def setUp(self):
        cache_mod._compute_code_hash.cache_clear()

    def tearDown(self):
        cache_mod._compute_code_hash.cache_clear()

    def test_hashes_source_files_once(self):
        with mock.patch("agy_usage.cache.open", wraps=open, create=True) as opened:
            first = cache_mod._compute_code_hash()
            second = cache_mod._compute_code_hash()
            third = cache_mod._compute_code_hash()
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(opened.call_count, 3)  # parser.py, models.py, cache.py


if __name__ == "__main__":
    unittest.main()
