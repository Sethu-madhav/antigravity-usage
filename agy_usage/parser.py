"""Zero-dependency parser for Antigravity SQLite databases and Protobuf payloads."""

from __future__ import annotations

import glob
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from agy_usage.models import SessionSummary, StepRecord, TokenUsage, normalize_model_name
from agy_usage.cache import SessionCache, file_fingerprint


logger = logging.getLogger(__name__)

_MAX_VARINT_BYTES = 10
_U64_MASK = (1 << 64) - 1


def decode_varint(data: bytes, idx: int) -> Tuple[int, int]:
    """Decode a variable-length integer (varint) starting at index.

    A varint is at most 10 bytes (64 bits). A longer continuation run is
    corruption: it raises ValueError instead of decoding to an unbounded int
    that would flow into the aggregates (BUG-017). The value is masked to 64 bits.
    """
    val = 0
    shift = 0
    while idx < len(data):
        b = data[idx]
        idx += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
        if shift >= _MAX_VARINT_BYTES * 7:
            raise ValueError("varint longer than 10 bytes")
    return val & _U64_MASK, idx


def parse_proto_fields(data: bytes) -> List[Tuple[int, int, bytes | int]]:
    """Parse raw protobuf wire-format bytes into a list of (field_num, wire_type, value)."""
    idx = 0
    fields: List[Tuple[int, int, bytes | int]] = []
    length_data = len(data)
    try:
        while idx < length_data:
            tag, idx = decode_varint(data, idx)
            wire_type = tag & 7
            field_num = tag >> 3

            if wire_type == 0:  # Varint
                val, idx = decode_varint(data, idx)
                fields.append((field_num, wire_type, val))
            elif wire_type == 1:  # 64-bit
                val = data[idx : idx + 8]
                idx += 8
                fields.append((field_num, wire_type, val))
            elif wire_type == 2:  # Length-delimited (string, bytes, embedded message)
                str_len, idx = decode_varint(data, idx)
                val = data[idx : idx + str_len]
                idx += str_len
                fields.append((field_num, wire_type, val))
            elif wire_type == 5:  # 32-bit
                val = data[idx : idx + 4]
                idx += 4
                fields.append((field_num, wire_type, val))
            else:
                # Unknown wire type or corruption; stop parsing
                break
    except ValueError:
        # Overlong varint (BUG-017): same policy as unknown wire types — keep the
        # fields that parsed cleanly and stop at the corruption.
        pass
    return fields


def parse_proto_map(data: bytes) -> Dict[int, List[bytes | int]]:
    """Helper that parses proto fields into a dict mapping field_number -> list of values."""
    fmap: Dict[int, List[bytes | int]] = {}
    for fnum, _, val in parse_proto_fields(data):
        fmap.setdefault(fnum, []).append(val)
    return fmap


def extract_string_candidates(data: bytes, min_len: int = 3) -> List[str]:
    """Extract readable ASCII/UTF-8 strings from binary payloads."""
    strings = re.findall(rb"[\x20-\x7e]{" + str(min_len).encode() + rb",}", data)
    results = []
    for s in strings:
        try:
            results.append(s.decode("utf-8").strip())
        except UnicodeDecodeError:
            continue
    return results


def parse_timestamp_proto(data: bytes) -> Optional[datetime]:
    """Parse a google.protobuf.Timestamp message {1: seconds, 2: nanos}."""
    try:
        fields = parse_proto_fields(data)
        seconds = 0
        for fnum, wtype, val in fields:
            if fnum == 1 and isinstance(val, int):
                seconds = val
                break
        if seconds > 0:
            return datetime.fromtimestamp(seconds)
    except (OSError, OverflowError, ValueError, TypeError):
        pass
    return None


# Known Antigravity model_enum IDs (both the raw M<num> suffix and the 1000+<num>
# integer stored in UsageMetadata field 1). Session-local gen_metadata overrides these.
KNOWN_MODEL_ENUMS: Dict[int, str] = {
    18: "gemini-3-flash",
    1018: "gemini-3-flash",
    19: "gemini-2.5-flash-lite",
    1019: "gemini-2.5-flash-lite",
    20: "gemini-3.5-flash",
    1020: "gemini-3.5-flash",
    21: "gemini-3.1-flash",
    1021: "gemini-3.1-flash",
    28: "gemini-2.5-flash-lite",
    1028: "gemini-2.5-flash-lite",
    50: "gemini-3.1-flash-lite",
    1050: "gemini-3.1-flash-lite",
    84: "gemini-3.5-flash",
    1084: "gemini-3.5-flash",
    187: "gemini-3.5-flash",
    1187: "gemini-3.5-flash",
    198: "gemini-3.5-flash-lite",
    1198: "gemini-3.5-flash-lite",
    298: "gemini-3.7-flash",
    1298: "gemini-3.7-flash",
    299: "gemini-3.7-flash",
    1299: "gemini-3.7-flash",
    318: "gemini-3.8-flash",
    1318: "gemini-3.8-flash",
    319: "gemini-3.8-flash",
    1319: "gemini-3.8-flash",
    320: "gemini-3.8-flash",
    1320: "gemini-3.8-flash",
    322: "gemini-3.8-flash",
    1322: "gemini-3.8-flash",
    400: "claude-opus-5-5-low",
    1400: "claude-opus-5-5-low",
    401: "claude-opus-5-5-medium",
    1401: "claude-opus-5-5-medium",
    402: "claude-opus-5-5-high",
    1402: "claude-opus-5-5-high",
    403: "claude-sonnet-5-5-low",
    1403: "claude-sonnet-5-5-low",
    404: "claude-sonnet-5-5-medium",
    1404: "claude-sonnet-5-5-medium",
    405: "claude-sonnet-5-5-high",
    1405: "claude-sonnet-5-5-high",
    417: "gemini-4-argon-trial",
    1417: "gemini-4-argon-trial",
    418: "gemini-4-argon-trial",
    1418: "gemini-4-argon-trial",
    419: "gemini-4-argon-trial",
    1419: "gemini-4-argon-trial",
}



def extract_usage_model_enum(data: bytes) -> Optional[int]:
    """Extract the model enum integer stored in UsageMetadata field 1, if present."""
    try:
        for fnum, _, val in parse_proto_fields(data):
            if fnum == 1 and isinstance(val, int) and val > 0:
                return val
    except Exception:
        pass
    return None


def parse_usage_proto(data: bytes) -> TokenUsage:
    """Parse UsageMetadata protobuf message."""
    usage = TokenUsage()
    try:
        fields = parse_proto_fields(data)
        for fnum, wtype, val in fields:
            if isinstance(val, int):
                if fnum == 2:
                    usage.prompt_tokens = val
                elif fnum == 3:
                    usage.output_tokens = val
                elif fnum == 5:
                    usage.cached_tokens = val
                elif fnum == 9:
                    usage.thought_tokens = val
                elif fnum == 10:
                    usage.candidate_tokens = val
        # If output_tokens is set but thoughts/candidates weren't separately broken out,
        # ensure candidate_tokens is at least output_tokens - thought_tokens
        if usage.output_tokens > 0 and usage.candidate_tokens == 0:
            usage.candidate_tokens = max(0, usage.output_tokens - usage.thought_tokens)
    except Exception:
        pass
    return usage


def _mtime_or_zero(path: str) -> float:
    """File mtime for sort keys; 0.0 (oldest) if the file vanished mid-scan (BUG-016)."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _fill_missing_step_timestamps(
    timestamps: List[Optional[datetime]],
    fallback: Optional[datetime] = None,
) -> List[Optional[datetime]]:
    """Fill missing step timestamps using neighbour inference (BUG-018).

    For each missing timestamp in the idx-ordered sequence:
    1. Fill with the timestamp of the nearest PRECEDING timestamped step.
    2. If there is no preceding timestamped step, fill with the nearest FOLLOWING
       timestamped step.
    3. If the session has no timestamped steps at all, fill with fallback
       (start_time_blob if set, else min(db_mtime, datetime.now()) if available).
    4. If fallback is None, missing timestamps remain None (caller drops the steps).
    """
    if not timestamps:
        return []

    # If no timestamped steps exist at all, use the session fallback
    if not any(ts is not None for ts in timestamps):
        return [fallback for _ in timestamps]

    n = len(timestamps)
    result = list(timestamps)
    last_seen: Optional[datetime] = None

    for i in range(n):
        if result[i] is not None:
            last_seen = result[i]
        else:
            if last_seen is not None:
                # Nearest PRECEDING timestamped step
                result[i] = last_seen
            else:
                # No preceding timestamped step; search forward for nearest FOLLOWING
                for j in range(i + 1, n):
                    if timestamps[j] is not None:
                        result[i] = timestamps[j]
                        break

    return result


class AntigravityParser:
    """Parser to discover and extract Antigravity conversation databases."""

    DEFAULT_CLI_DIR = os.path.expanduser("~/.gemini/antigravity/conversations")
    DEFAULT_IDE_DIR = os.path.expanduser("~/.gemini/antigravity-ide/conversations")

    @classmethod
    def get_default_directories(cls, mode: str = "all") -> List[Tuple[str, str]]:
        """Return list of (directory_path, source_label)."""
        dirs = []
        if mode in ("all", "cli"):
            if os.path.isdir(cls.DEFAULT_CLI_DIR):
                dirs.append((cls.DEFAULT_CLI_DIR, "cli"))
        if mode in ("all", "ide"):
            if os.path.isdir(cls.DEFAULT_IDE_DIR):
                dirs.append((cls.DEFAULT_IDE_DIR, "ide"))
        return dirs

    @classmethod
    def scan_directory(
        cls,
        dir_path: str,
        source: str = "custom",
        cache: Optional[SessionCache] = None,
    ) -> List[SessionSummary]:
        """Scan a directory for SQLite database files and extract session usage."""
        if not os.path.isdir(dir_path):
            return []

        db_files = sorted(
            glob.glob(os.path.join(dir_path, "*.db")),
            key=_mtime_or_zero,
            reverse=True,
        )

        sessions: List[SessionSummary] = []
        for db_file in db_files:
            summary = None
            fp = None
            if cache is not None:
                fp = file_fingerprint(db_file)
                summary = cache.get(db_file, source)
            if summary is None:
                summary, complete = cls._parse_session_db_checked(db_file, source=source)
                if summary and complete and fp is not None and cache is not None:
                    cache.put(db_file, summary, fp)
            if summary:
                sessions.append(summary)
        return sessions

    @classmethod
    def scan_all(
        cls,
        custom_dir: Optional[str] = None,
        source_mode: str = "all",
        use_cache: bool = True,
    ) -> List[SessionSummary]:
        """Scan all relevant directories or a specified directory."""
        cache = SessionCache.load() if use_cache else None
        all_sessions: List[SessionSummary] = []
        if custom_dir:
            source_label = "custom"
            if "antigravity-ide" in custom_dir:
                source_label = "ide"
            elif "antigravity" in custom_dir:
                source_label = "cli"
            all_sessions = cls.scan_directory(custom_dir, source=source_label, cache=cache)
        else:
            for d_path, s_label in cls.get_default_directories(mode=source_mode):
                all_sessions.extend(cls.scan_directory(d_path, source=s_label, cache=cache))

            all_sessions = cls._dedupe_by_session_id(all_sessions)

        if cache is not None:
            cache.save()

        # Sort all sessions descending by start_time (or file mtime if start_time is missing)
        all_sessions.sort(
            key=lambda s: s.start_time.timestamp()
            if s.start_time
            else _mtime_or_zero(s.file_path),
            reverse=True,
        )
        return all_sessions

    @staticmethod
    def _dedupe_by_session_id(sessions: List[SessionSummary]) -> List[SessionSummary]:
        """Keep one summary per session_id (BUG-015).

        The same conversation DB can exist in both the CLI and IDE directories;
        summing both copies double-counts its tokens while session counts (which
        key on session_id) stay correct. The copy with the newest file mtime wins;
        on an exact tie the first one scanned (CLI before IDE) is kept.
        """
        by_id: Dict[str, SessionSummary] = {}
        for session in sessions:
            kept = by_id.get(session.session_id)
            if kept is None or _mtime_or_zero(session.file_path) > _mtime_or_zero(kept.file_path):
                by_id[session.session_id] = session
        return list(by_id.values())

    @classmethod
    def parse_session_db(cls, db_path: str, source: str = "cli") -> Optional[SessionSummary]:
        """Extract complete usage, steps, title, and metadata from a single SQLite DB.

        Parsing is defensive (BUG-008): once the DB is validated, a lock or a
        single malformed step row keeps everything read so far and returns a
        partial summary, so one bad tick cannot drop the session from `live`.
        """
        return cls._parse_session_db_checked(db_path, source=source)[0]

    @classmethod
    def _parse_session_db_checked(
        cls, db_path: str, source: str = "cli"
    ) -> Tuple[Optional[SessionSummary], bool]:
        """parse_session_db plus a flag that is True only for a complete parse.

        Partial summaries (a SQLite error after validation) return False so
        the session cache never stores them (OPT-001).
        """
        if not os.path.isfile(db_path):
            return None, False

        filename = os.path.basename(db_path)
        session_id = filename.replace(".db", "")

        # Parsed state lives outside the try so any failure below can still be
        # reported as a partial summary instead of discarding prior work.
        workspace = "Unknown"
        title = f"Session {session_id[:8]}"
        known_models: Set[str] = set()
        step_to_model: Dict[int, str] = {}
        default_model = "gemini-3.8-flash"
        steps: List[StepRecord] = []
        aggregated_usage = TokenUsage()
        model_token_totals: Dict[str, int] = {}
        first_ts: Optional[datetime] = None
        last_ts: Optional[datetime] = None
        validated = False

        def build_summary() -> SessionSummary:
            """Assemble a summary from everything parsed so far."""
            # Order models by token volume descending so primary_model
            # (models[0]) is the dominant model of the session.
            if known_models:
                models_list = sorted(
                    known_models,
                    key=lambda m: (-model_token_totals.get(m, 0), m),
                )
            else:
                models_list = [default_model]
            return SessionSummary(
                session_id=session_id,
                source=source,
                file_path=db_path,
                title=title,
                workspace=workspace,
                models=models_list,
                start_time=first_ts,
                end_time=last_ts or first_ts,
                step_count=len(steps),
                usage=aggregated_usage,
                steps=steps,
            )

        conn: Optional[sqlite3.Connection] = None
        try:
            # Connect read-only so we never lock, write, or (re)create a live DB.
            # Percent-encode the path so '?', '#', '%' etc. stay literal in the
            # SQLite URI; quote() keeps '/' intact for ordinary absolute paths.
            # If the DB vanished or is locked, connect raises and the outer
            # except converts it to the normal None outcome - no read-write
            # fallback that could create or modify the file.
            conn = sqlite3.connect(f"file:{quote(db_path)}?mode=ro", uri=True)

            cursor = conn.cursor()

            # Verify required tables exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            if "steps" not in tables:
                return None, False
            validated = True

            # 1. Extract Workspace / Project from trajectory_metadata_blob
            start_time_blob: Optional[datetime] = None
            if "trajectory_metadata_blob" in tables:
                try:
                    cursor.execute(
                        "SELECT data FROM trajectory_metadata_blob WHERE data IS NOT NULL LIMIT 1"
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        blob_fields = parse_proto_fields(row[0])
                        for fnum, wtype, val in blob_fields:
                            if fnum == 1 and isinstance(val, bytes):
                                sub = parse_proto_fields(val)
                                for sf, sw, sv in sub:
                                    if sf == 1 and isinstance(sv, bytes):
                                        uri_str = sv.decode("utf-8", errors="ignore")
                                        if uri_str.startswith("file://"):
                                            workspace = os.path.basename(
                                                uri_str.replace("file://", "").rstrip("/")
                                            )
                                        elif uri_str:
                                            workspace = uri_str
                            elif fnum == 2 and isinstance(val, bytes):
                                ts = parse_timestamp_proto(val)
                                if ts:
                                    start_time_blob = ts
                except (
                    sqlite3.DatabaseError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    OverflowError,
                    UnicodeDecodeError,
                ) as exc:
                    logger.debug(
                        "Skipping trajectory_metadata_blob in %s: %s", db_path, exc
                    )

            # Keep the blob start time for partial summaries: a failure in any
            # later stage must not lose the only timestamp already parsed.
            first_ts = start_time_blob

            # 2. Extract Models and Step-to-Model Mapping from gen_metadata
            enum_to_model: Dict[int, str] = dict(KNOWN_MODEL_ENUMS)
            gm_rows: List[Tuple[int, str, Optional[int]]] = []
            if "gen_metadata" in tables:
                try:
                    cursor.execute("SELECT idx, data FROM gen_metadata WHERE size > 0 ORDER BY idx ASC")
                    for row_idx, data in cursor.fetchall():
                        if not data:
                            continue
                        g_fields = parse_proto_fields(data)
                        model_name = ""
                        model_enum_num: Optional[int] = None
                        model_enum_raw = ""
                        last_step_idx = -1
                        for gf, gw, gv in g_fields:
                            if gf == 1 and isinstance(gv, bytes):
                                f1_fields = parse_proto_fields(gv)
                                for f1_f, f1_w, f1_v in f1_fields:
                                    if f1_f == 19 and isinstance(f1_v, bytes):
                                        m_str = normalize_model_name(
                                            f1_v.decode("utf-8", errors="ignore").strip()
                                        )
                                        # Placeholders (M-prefixed) and digit
                                        # artifacts are not model names; letting
                                        # them seed known_models / enum_to_model
                                        # is the phantom-attribution failure
                                        # described in BUG-010.
                                        if (
                                            m_str
                                            and not m_str.startswith("M")
                                            and not m_str.isdigit()
                                        ):
                                            model_name = m_str
                                            known_models.add(m_str)
                                    elif f1_f == 20 and isinstance(f1_v, bytes):
                                        f20_fields = parse_proto_fields(f1_v)
                                        f20_map = {
                                            sf: sv
                                            for sf, sw, sv in f20_fields
                                            if isinstance(sv, bytes)
                                        }
                                        key_str = (
                                            f20_map.get(1, b"")
                                            .decode("utf-8", errors="ignore")
                                            .strip()
                                        )
                                        val_str = (
                                            f20_map.get(2, b"")
                                            .decode("utf-8", errors="ignore")
                                            .strip()
                                        )
                                        if key_str == "last_step_index" and val_str.isdigit():
                                            last_step_idx = int(val_str)
                                        elif key_str == "model_enum" and val_str:
                                            model_enum_raw = val_str.replace("MODEL_PLACEHOLDER_", "")
                                            num_str = model_enum_raw.lstrip("Mm")
                                            if num_str.isdigit():
                                                model_enum_num = int(num_str)

                        if model_name and model_enum_num is not None:
                            enum_to_model[model_enum_num] = model_name
                            enum_to_model[1000 + model_enum_num] = model_name

                        gm_rows.append((last_step_idx, model_name or model_enum_raw, model_enum_num))

                    # Second pass so rows that only recorded model_enum (without field 19)
                    # inherit the human-readable model name discovered in the same session.
                    for last_step_idx, raw_name, enum_num in gm_rows:
                        resolved = raw_name
                        # Bare enum numbers ("MODEL_PLACEHOLDER_33") are keys,
                        # not names — let the enum mapping resolve them the same
                        # way M-prefixed placeholders are resolved.
                        if (
                            not resolved or resolved.startswith("M") or resolved.isdigit()
                        ) and enum_num is not None:
                            resolved = enum_to_model.get(enum_num, resolved)
                        resolved = normalize_model_name(resolved)
                        # BUG-010: a gen_metadata row earns an entry in
                        # known_models and an attribution anchor only when its
                        # model name fully resolves; unresolvable names (empty,
                        # M-placeholder, bare enum number) must never fabricate
                        # the hardcoded default into the model distribution.
                        if resolved and not resolved.startswith("M") and not resolved.isdigit():
                            known_models.add(resolved)
                            if last_step_idx >= 0:
                                step_to_model[last_step_idx] = resolved
                                step_to_model[last_step_idx + 1] = resolved
                except (
                    sqlite3.DatabaseError,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    OverflowError,
                    UnicodeDecodeError,
                ) as exc:
                    logger.debug("Skipping gen_metadata in %s: %s", db_path, exc)

            gm_anchors: List[Tuple[int, str]] = sorted(
                (idx_anchor, m_name)
                for idx_anchor, m_name in step_to_model.items()
                if m_name
            )

            default_model = (
                sorted(list(known_models))[0]
                if known_models
                else "gemini-3.8-flash"
            )

            # 3. Extract Session Title from initial steps
            title = f"Session {session_id[:8]}"
            try:
                cursor.execute(
                    "SELECT idx, step_type, step_payload FROM steps WHERE idx IN (1, 0, 2, 3) AND step_payload IS NOT NULL ORDER BY idx ASC"
                )
                payload_rows = cursor.fetchall()
                found_title = None

                # Pass 1: Look for structured user prompt (Field 19 -> SubField 2)
                for s_idx, stype, payload in payload_rows:
                    for f, t, v in parse_proto_fields(payload):
                        if f == 19 and t == 2 and isinstance(v, bytes):
                            sub = parse_proto_fields(v)
                            for sf, st, sv in sub:
                                if sf == 2 and st == 2 and isinstance(sv, bytes):
                                    txt = sv.decode("utf-8", errors="ignore").strip()
                                    if len(txt) > 3 and not txt.startswith("/Users/"):
                                        clean = txt.replace("/goal", "").strip()
                                        found_title = clean.replace("\n", " ")[:60]
                                        break
                            if found_title:
                                break
                    if found_title:
                        break

                # Pass 2: Fallback to string search if no structured user prompt was found
                if not found_title:
                    for s_idx, stype, payload in payload_rows:
                        strs = extract_string_candidates(payload, min_len=4)
                        for s in strs:
                            if re.match(
                                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                                s,
                                re.I,
                            ):
                                continue
                            if s.startswith(("bot-", "sessionID", "file://", "MODEL_", "allow", "b$", "$")):
                                continue
                            if len(s) > 8 and not s.isdigit() and not s.startswith(("/Users/", ")/Users/", "3/Users/")):
                                clean = s.replace("/goal", "").strip().replace("\n", " ")
                                if len(clean) > 5:
                                    found_title = clean[:60]
                                    break
                        if found_title:
                            break

                if found_title:
                    title = found_title
            except (
                sqlite3.DatabaseError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                OverflowError,
                UnicodeDecodeError,
            ) as exc:
                logger.debug("Skipping title extraction in %s: %s", db_path, exc)

            # 4. Extract Step Records and Token Usage
            try:
                cursor.execute(
                    "SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx ASC"
                )
                step_rows = cursor.fetchall()
            except sqlite3.DatabaseError:
                # Locked/corrupt/vanished mid-read: keep title, workspace and any
                # steps already collected instead of dropping the whole session.
                step_rows = []

            try:
                db_mtime: Optional[datetime] = datetime.fromtimestamp(
                    os.path.getmtime(db_path)
                )
            except (OSError, OverflowError, ValueError):
                db_mtime = None

            # BUG-018: Collect raw steps without immediately falling back to db_mtime
            raw_steps: List[Tuple[int, Optional[datetime], str, TokenUsage]] = []

            for idx, meta_blob in step_rows:
                # A corrupt BLOB column can hold any SQLite type (dynamic typing).
                if meta_blob is None or meta_blob == b"":
                    continue
                if not isinstance(meta_blob, (bytes, bytearray, memoryview)):
                    logger.debug(
                        "Skipping step idx=%r in %s: non-bytes metadata (%s)",
                        idx,
                        db_path,
                        type(meta_blob).__name__,
                    )
                    continue
                meta_blob = bytes(meta_blob)
                try:
                    meta_fields = parse_proto_fields(meta_blob)
                    meta_map: Dict[int, bytes | int] = {
                        f: v for f, w, v in meta_fields
                    }

                    # Check for UsageMetadata in Field 9
                    usage_raw = meta_map.get(9)
                    if not isinstance(usage_raw, bytes):
                        continue

                    step_usage = parse_usage_proto(usage_raw)
                    # Ignore empty usage steps
                    if step_usage.total_tokens == 0:
                        continue

                    # Extract Step Timestamp from Field 1, 6, 7, 8, or 32
                    step_ts: Optional[datetime] = None
                    for ts_field_id in (1, 6, 7, 8, 32):
                        ts_raw = meta_map.get(ts_field_id)
                        if isinstance(ts_raw, bytes):
                            ts = parse_timestamp_proto(ts_raw)
                            if ts:
                                step_ts = ts
                                break

                    # Identify Model for this step:
                    # 1) Direct gen_metadata last_step_index anchor
                    # 2) UsageMetadata field 1 model_enum integer
                    # 3) Most recent preceding gen_metadata anchor in this session
                    # 4) Session default model
                    step_model = step_to_model.get(idx, "")
                    if not step_model:
                        u_enum = extract_usage_model_enum(usage_raw)
                        if u_enum is not None and u_enum in enum_to_model:
                            step_model = enum_to_model[u_enum]
                    if not step_model and gm_anchors:
                        for anchor_idx, anchor_model in reversed(gm_anchors):
                            if anchor_idx <= idx:
                                step_model = anchor_model
                                break
                    if not step_model:
                        step_model = default_model

                    # BUG-018: Record step candidate without immediate db_mtime fallback;
                    # missing timestamps will be filled via neighbour inference below.
                    raw_steps.append((idx, step_ts, step_model, step_usage))
                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    OverflowError,
                    UnicodeDecodeError,
                    sqlite3.DatabaseError,
                ) as exc:
                    # One malformed row must not discard the rest of the session
                    logger.debug(
                        "Skipping malformed step idx=%r in %s: %s", idx, db_path, exc
                    )
                    continue

            # BUG-018: Fill missing timestamps using nearest neighbour inference.
            # If no steps have timestamps, fall back to start_time_blob, or min(db_mtime, now).
            session_fallback: Optional[datetime] = None
            if start_time_blob is not None:
                session_fallback = start_time_blob
            elif db_mtime is not None:
                session_fallback = min(db_mtime, datetime.now())

            raw_timestamps = [ts for _, ts, _, _ in raw_steps]
            filled_timestamps = _fill_missing_step_timestamps(
                raw_timestamps, fallback=session_fallback
            )

            for (idx, _, step_model, step_usage), final_ts in zip(
                raw_steps, filled_timestamps
            ):
                if final_ts is None:
                    # No timestamp could be inferred; drop the step
                    continue

                # Build everything that can raise before mutating any state, so
                # `steps` and `aggregated_usage` never drift apart.
                new_usage = aggregated_usage.add(step_usage)
                step_rec = StepRecord(
                    step_idx=idx,
                    timestamp=final_ts,
                    model=step_model,
                    usage=step_usage,
                )

                if first_ts is None or final_ts < first_ts:
                    first_ts = final_ts
                if last_ts is None or final_ts > last_ts:
                    last_ts = final_ts

                known_models.add(step_model)
                model_token_totals[step_model] = (
                    model_token_totals.get(step_model, 0) + step_usage.total_tokens
                )
                steps.append(step_rec)
                aggregated_usage = new_usage

            return build_summary(), True

        except sqlite3.DatabaseError:
            # SQLite failure after validation (locked, corrupt page, vanished
            # file): emit what we have so the session stays visible, unless the
            # DB was never validated as a usable session at all.
            return (build_summary(), False) if validated else (None, False)
        except Exception:
            # Unknown failure: preserve the previous skip-the-session behavior.
            return None, False
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
