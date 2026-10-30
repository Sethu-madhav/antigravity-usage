"""Tests that a scan stats each session database once, with unchanged results."""

import os
import sqlite3
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from unittest import mock

from agy_usage import cache as cache_mod
from agy_usage.cache import SessionCache, file_fingerprint
from agy_usage.models import SessionSummary
from agy_usage.parser import AntigravityParser


def _varint(value):
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _write_db(path, mtime=None, output=200, timed=True):
    """Session DB with one step; `timed=False` leaves the session without a start_time."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type TEXT, "
            "step_payload BLOB, metadata BLOB)"
        )
        if timed:
            ts = bytes([0x08]) + _varint(1_751_234_567)
            usage = bytes([0x10]) + _varint(1000) + bytes([0x18]) + _varint(output)
            metadata = bytes([0x0A, len(ts)]) + ts + bytes([0x4A, len(usage)]) + usage
            conn.execute(
                "INSERT INTO steps (idx, step_type, step_payload, metadata) "
                "VALUES (1, 'user', '', ?)",
                (metadata,),
            )
        conn.commit()
    finally:
        conn.close()
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _new_cache(tmp):
    return SessionCache(
        cache_file=os.path.join(tmp, "cache", "sessions.json"),
        enabled=True,
        code_hash="test",
        tz_info=[],
    )


def _count_db_stats(fn):
    """Run fn() and return (result, Counter of os.stat calls per non-wal path)."""
    real_stat = os.stat
    counts = Counter()

    def counting_stat(path, *args, **kwargs):
        path = os.fspath(path)
        if not path.endswith("-wal"):
            counts[path] += 1
        return real_stat(path, *args, **kwargs)

    with mock.patch("os.stat", side_effect=counting_stat):
        result = fn()
    return result, counts


class TestScanDirectoryStatsOnce(unittest.TestCase):
    def test_warm_scan_stats_each_db_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [
                _write_db(os.path.join(tmp, f"s{i}.db"), mtime=1_751_300_000 + i)
                for i in range(3)
            ]
            with open(paths[0] + "-wal", "wb") as f:
                f.write(b"\0" * 32)
            cache = _new_cache(tmp)
            cold = AntigravityParser.scan_directory(tmp, source="cli", cache=cache)

            with mock.patch.object(
                AntigravityParser,
                "_parse_session_db_checked",
                wraps=AntigravityParser._parse_session_db_checked,
            ) as parse:
                warm, counts = _count_db_stats(
                    lambda: AntigravityParser.scan_directory(tmp, source="cli", cache=cache)
                )

            parse.assert_not_called()
            self.assertEqual([s.session_id for s in warm], [s.session_id for s in cold])
            self.assertEqual([s.session_id for s in warm], ["s2", "s1", "s0"])
            for path in paths:
                self.assertEqual(counts[path], 1, path)

    def test_db_deleted_after_glob_is_skipped_without_cache_put(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept = _write_db(os.path.join(tmp, "kept.db"))
            ghost = os.path.join(tmp, "ghost.db")
            cache = _new_cache(tmp)
            with mock.patch("agy_usage.parser.glob.glob", return_value=[ghost, kept]), \
                 mock.patch.object(cache, "put", wraps=cache.put) as put:
                sessions = AntigravityParser.scan_directory(tmp, source="cli", cache=cache)

            self.assertEqual([s.session_id for s in sessions], ["kept"])
            self.assertEqual([c.args[0] for c in put.call_args_list], [kept])


class TestPrecomputedFingerprint(unittest.TestCase):
    def test_file_fingerprint_with_stat_matches_plain_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_db(os.path.join(tmp, "a.db"))
            self.assertEqual(file_fingerprint(path, st=os.stat(path)), file_fingerprint(path))
            with open(path + "-wal", "wb") as f:
                f.write(b"\0" * 16)
            self.assertEqual(file_fingerprint(path, st=os.stat(path)), file_fingerprint(path))

    def _cached(self, tmp):
        path = _write_db(os.path.join(tmp, "a.db"))
        cache = _new_cache(tmp)
        summary, _ = AntigravityParser._parse_session_db_checked(path, source="cli")
        fp = file_fingerprint(path)
        cache.put(path, summary, fp)
        return cache, path, fp

    def test_get_with_fingerprint_skips_file_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, path, fp = self._cached(tmp)
            with mock.patch.object(cache_mod, "file_fingerprint") as ff:
                hit = cache.get(path, "ide", fingerprint=fp)
                miss = cache.get(path, "ide", fingerprint=[[0, 0], None])
            ff.assert_not_called()
            self.assertIsNotNone(hit)
            self.assertEqual(hit.source, "ide")
            self.assertIsNone(miss)

    def test_get_without_fingerprint_still_computes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, path, _ = self._cached(tmp)
            self.assertIsNotNone(cache.get(path, "cli", fingerprint=None))
            os.remove(path)
            self.assertIsNone(cache.get(path, "cli", fingerprint=None))
            self.assertIsNone(cache.get(path, "cli"))


class TestDedupeAndSortUnchanged(unittest.TestCase):
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
        env = mock.patch.dict(os.environ, {"AGY_USAGE_CACHE_DIR": os.path.join(self._tmp.name, "c")})
        env.start()
        self.addCleanup(env.stop)

    def _scan_both_ways(self):
        cold = AntigravityParser.scan_all()
        warm = AntigravityParser.scan_all()
        uncached = AntigravityParser.scan_all(use_cache=False)
        key = [(s.session_id, s.source) for s in cold]
        self.assertEqual([(s.session_id, s.source) for s in warm], key)
        self.assertEqual([(s.session_id, s.source) for s in uncached], key)
        return key

    def test_duplicates_keep_newest_mtime_and_cli_on_exact_tie(self):
        _write_db(os.path.join(self.cli_dir, "newer-ide.db"), mtime=1_751_300_000)
        _write_db(os.path.join(self.ide_dir, "newer-ide.db"), mtime=1_751_400_000)
        _write_db(os.path.join(self.cli_dir, "newer-cli.db"), mtime=1_751_400_000)
        _write_db(os.path.join(self.ide_dir, "newer-cli.db"), mtime=1_751_300_000)
        _write_db(os.path.join(self.cli_dir, "tie.db"), mtime=1_751_300_000)
        _write_db(os.path.join(self.ide_dir, "tie.db"), mtime=1_751_300_000)

        self.assertEqual(
            sorted(self._scan_both_ways()),
            [("newer-cli", "cli"), ("newer-ide", "ide"), ("tie", "cli")],
        )

    def test_untimed_sessions_sort_by_mtime_after_dedupe(self):
        _write_db(os.path.join(self.cli_dir, "timed.db"), mtime=1_751_000_000)
        _write_db(os.path.join(self.cli_dir, "untimed-old.db"), mtime=1_000_000_000, timed=False)
        _write_db(os.path.join(self.cli_dir, "untimed-new.db"), mtime=1_800_000_000, timed=False)
        _write_db(os.path.join(self.ide_dir, "untimed-new.db"), mtime=1_800_000_001, timed=False)

        self.assertEqual(
            self._scan_both_ways(),
            [("untimed-new", "ide"), ("timed", "cli"), ("untimed-old", "cli")],
        )

    def test_scan_all_stats_each_path_at_most_once_for_mtimes(self):
        untimed = [
            SessionSummary(session_id="a", source="cli", file_path="/x/a.db", start_time=None),
            SessionSummary(session_id="a", source="ide", file_path="/y/a.db", start_time=None),
            SessionSummary(session_id="b", source="ide", file_path="/y/b.db", start_time=None),
        ]
        timed = SessionSummary(
            session_id="t", source="cli", file_path="/x/t.db", start_time=datetime(2026, 1, 1)
        )
        mtimes = {"/x/a.db": 10.0, "/y/a.db": 30.0, "/y/b.db": 20.0}
        with mock.patch.object(
            AntigravityParser, "get_default_directories", return_value=[("/x", "cli"), ("/y", "ide")]
        ), mock.patch.object(
            AntigravityParser, "scan_directory", side_effect=[untimed[:1] + [timed], untimed[1:]]
        ), mock.patch(
            "agy_usage.parser.os.path.getmtime", side_effect=lambda p: mtimes[p]
        ) as getmtime:
            sessions = AntigravityParser.scan_all(use_cache=False)

        self.assertEqual(
            [(s.session_id, s.source) for s in sessions], [("t", "cli"), ("a", "ide"), ("b", "ide")]
        )
        self.assertEqual(max(Counter(c.args[0] for c in getmtime.call_args_list).values()), 1)


if __name__ == "__main__":
    unittest.main()
