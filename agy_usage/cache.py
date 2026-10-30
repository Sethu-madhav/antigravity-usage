"""Disk cache for parsed Antigravity session summaries."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Dict, List, Optional

from agy_usage.models import SessionSummary, StepRecord, TokenUsage


logger = logging.getLogger(__name__)

CACHE_FORMAT_VERSION = 1


def _cache_path() -> str:
    """Return the absolute path to the session cache file."""
    cache_dir = os.environ.get("AGY_USAGE_CACHE_DIR")
    if cache_dir:
        return os.path.join(cache_dir, "sessions.json")
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(xdg, "agy-usage", "sessions.json")
    return os.path.expanduser("~/.cache/agy-usage/sessions.json")


def _is_caching_disabled() -> bool:
    """Check if caching is disabled via environment variable."""
    val = os.environ.get("AGY_USAGE_NO_CACHE")
    return val is not None and val != "" and val != "0"


def file_fingerprint(db_path: str) -> Optional[list]:
    """Return fingerprint of a .db file and its -wal companion (if present)."""
    try:
        st = os.stat(db_path)
        db_pair = [st.st_size, st.st_mtime_ns]
    except OSError:
        return None

    wal_path = db_path + "-wal"
    try:
        st_wal = os.stat(wal_path)
        wal_pair: Optional[list] = [st_wal.st_size, st_wal.st_mtime_ns]
    except OSError:
        wal_pair = None

    return [db_pair, wal_pair]


def _compute_code_hash() -> Optional[str]:
    """Compute sha256 hex of parser.py, models.py, and cache.py concatenated."""
    pkg_dir = os.path.dirname(__file__)
    hasher = hashlib.sha256()
    for fname in ("parser.py", "models.py", "cache.py"):
        fpath = os.path.join(pkg_dir, fname)
        try:
            with open(fpath, "rb") as f:
                hasher.update(f.read())
        except OSError:
            return None
    return hasher.hexdigest()


def _current_tz_info() -> list:
    """Return the current timezone signature."""
    return [time.timezone, time.altzone, time.daylight, list(time.tzname)]


def summary_to_dict(s: SessionSummary) -> dict:
    """Serialize a SessionSummary dataclass to a compact dict."""
    return {
        "session_id": s.session_id,
        "source": s.source,
        "file_path": s.file_path,
        "title": s.title,
        "workspace": s.workspace,
        "models": list(s.models),
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "end_time": s.end_time.isoformat() if s.end_time else None,
        "step_count": s.step_count,
        "usage": [
            s.usage.prompt_tokens,
            s.usage.cached_tokens,
            s.usage.output_tokens,
            s.usage.thought_tokens,
            s.usage.candidate_tokens,
        ],
        "steps": [
            [
                step.step_idx,
                step.timestamp.isoformat(),
                step.model,
                step.usage.prompt_tokens,
                step.usage.cached_tokens,
                step.usage.output_tokens,
                step.usage.thought_tokens,
                step.usage.candidate_tokens,
                step.duration_ms,
            ]
            for step in s.steps
        ],
    }


def summary_from_dict(d: dict) -> SessionSummary:
    """Deserialize a dict into a fresh SessionSummary dataclass."""
    start_time = (
        datetime.fromisoformat(d["start_time"]) if d.get("start_time") else None
    )
    end_time = (
        datetime.fromisoformat(d["end_time"]) if d.get("end_time") else None
    )
    u = d["usage"]
    usage = TokenUsage(
        prompt_tokens=u[0],
        cached_tokens=u[1],
        output_tokens=u[2],
        thought_tokens=u[3],
        candidate_tokens=u[4],
    )
    steps = [
        StepRecord(
            step_idx=st[0],
            timestamp=datetime.fromisoformat(st[1]),
            model=st[2],
            usage=TokenUsage(
                prompt_tokens=st[3],
                cached_tokens=st[4],
                output_tokens=st[5],
                thought_tokens=st[6],
                candidate_tokens=st[7],
            ),
            duration_ms=st[8],
        )
        for st in d.get("steps", [])
    ]
    return SessionSummary(
        session_id=d["session_id"],
        source=d["source"],
        file_path=d["file_path"],
        title=d["title"],
        workspace=d["workspace"],
        models=list(d["models"]),
        start_time=start_time,
        end_time=end_time,
        step_count=d["step_count"],
        usage=usage,
        steps=steps,
    )


class SessionCache:
    """Disk cache for parsed SessionSummary objects."""

    def __init__(
        self,
        cache_file: Optional[str] = None,
        enabled: bool = True,
        code_hash: Optional[str] = None,
        tz_info: Optional[list] = None,
        entries: Optional[Dict[str, dict]] = None,
    ) -> None:
        self._cache_file = cache_file or _cache_path()
        self._enabled = enabled
        self._code_hash = code_hash or ""
        self._tz_info = tz_info or []
        self._entries: Dict[str, dict] = dict(entries) if entries else {}
        self._dirty = False

    @classmethod
    def load(cls, cache_file: Optional[str] = None) -> SessionCache:
        """Load session cache from disk. Never raises on missing/corrupt files."""
        if _is_caching_disabled():
            return cls(cache_file=cache_file, enabled=False)

        code_hash = _compute_code_hash()
        if code_hash is None:
            return cls(cache_file=cache_file, enabled=False)

        tz_info = _current_tz_info()
        path = cache_file or _cache_path()

        def empty_cache() -> SessionCache:
            return cls(
                cache_file=path,
                enabled=True,
                code_hash=code_hash,
                tz_info=tz_info,
            )

        if not os.path.isfile(path):
            return empty_cache()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.debug("Failed to load session cache from %s: %s", path, exc)
            return empty_cache()

        if not isinstance(data, dict):
            return empty_cache()

        if (
            data.get("format") != CACHE_FORMAT_VERSION
            or data.get("code") != code_hash
            or data.get("tz") != tz_info
        ):
            return empty_cache()

        raw_entries = data.get("entries")
        if not isinstance(raw_entries, dict):
            return empty_cache()

        entries: Dict[str, dict] = {}
        for k, v in raw_entries.items():
            if (
                isinstance(k, str)
                and isinstance(v, dict)
                and "fingerprint" in v
                and "summary" in v
            ):
                entries[k] = v

        return cls(
            cache_file=path,
            enabled=True,
            code_hash=code_hash,
            tz_info=tz_info,
            entries=entries,
        )

    def get(self, db_path: str, source: str) -> Optional[SessionSummary]:
        """Return a fresh cached SessionSummary if the file fingerprint matches."""
        if not self._enabled:
            return None

        abs_path = os.path.abspath(db_path)
        entry = self._entries.get(abs_path)
        if entry is None or not isinstance(entry, dict):
            return None

        stored_fp = entry.get("fingerprint")
        curr_fp = file_fingerprint(db_path)
        if curr_fp is None or curr_fp != stored_fp:
            return None

        raw_summary = entry.get("summary")
        if not isinstance(raw_summary, dict):
            return None

        try:
            summary = summary_from_dict(raw_summary)
        except Exception as exc:
            logger.debug("Failed to deserialize cached summary for %s: %s", db_path, exc)
            return None

        summary.source = source
        return summary

    def put(self, db_path: str, summary: SessionSummary, fingerprint: list) -> None:
        """Store a parsed SessionSummary with the pre-parse fingerprint."""
        if not self._enabled:
            return

        try:
            summary_dict = summary_to_dict(summary)
        except Exception as exc:
            logger.debug("Failed to serialize summary for %s: %s", db_path, exc)
            return

        abs_path = os.path.abspath(db_path)
        self._entries[abs_path] = {
            "fingerprint": fingerprint,
            "summary": summary_dict,
        }
        self._dirty = True

    def save(self) -> None:
        """Atomically persist cache to disk if dirty."""
        if not self._enabled:
            return

        # Drop entries whose .db path no longer exists
        paths_to_drop = [path for path in self._entries if not os.path.exists(path)]
        if paths_to_drop:
            for path in paths_to_drop:
                del self._entries[path]
            self._dirty = True

        if not self._dirty:
            return

        cache_file = os.path.abspath(self._cache_file)
        cache_dir = os.path.dirname(cache_file)
        tmp_path = None
        try:
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            payload = {
                "format": CACHE_FORMAT_VERSION,
                "code": self._code_hash,
                "tz": self._tz_info,
                "entries": self._entries,
            }
            data_str = json.dumps(payload, separators=(",", ":"))
            with tempfile.NamedTemporaryFile(
                dir=cache_dir,
                delete=False,
                mode="w",
                encoding="utf-8",
            ) as tf:
                tmp_path = tf.name
                tf.write(data_str)
            os.replace(tmp_path, cache_file)
            tmp_path = None
            self._dirty = False
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("Failed to save session cache to %s: %s", cache_file, exc)
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
