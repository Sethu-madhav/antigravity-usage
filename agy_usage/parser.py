"""Zero-dependency parser for Antigravity SQLite databases and Protobuf payloads."""

from __future__ import annotations

import bisect
import glob
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
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


_DEFAULT_MODEL = "gemini-3.8-flash"
# Errors a single parse stage or row may raise; the stage is skipped, the session kept.
_ROW_ERRORS = (
    sqlite3.DatabaseError,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    OverflowError,
    UnicodeDecodeError,
)
_UUID_PREFIX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)

# (idx, timestamp or None, model, usage) for a step that carried token usage.
_RawStep = Tuple[int, Optional[datetime], str, TokenUsage]


@dataclass
class _ParseState:
    """Everything parsed from one session DB so far.

    Parsed state lives outside the stage helpers so any failure can still be
    reported as a partial summary instead of discarding prior work.
    """

    session_id: str
    source: str
    db_path: str
    title: str
    workspace: str = "Unknown"
    default_model: str = _DEFAULT_MODEL
    known_models: Set[str] = field(default_factory=set)
    step_to_model: Dict[int, str] = field(default_factory=dict)
    enum_to_model: Dict[int, str] = field(default_factory=lambda: dict(KNOWN_MODEL_ENUMS))
    model_token_totals: Dict[str, int] = field(default_factory=dict)
    steps: List[StepRecord] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    start_time_blob: Optional[datetime] = None
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    validated: bool = False

    def build_summary(self) -> SessionSummary:
        """Assemble a summary from everything parsed so far."""
        # Order models by token volume descending so primary_model
        # (models[0]) is the dominant model of the session.
        if self.known_models:
            models_list = sorted(
                self.known_models,
                key=lambda m: (-self.model_token_totals.get(m, 0), m),
            )
        else:
            models_list = [self.default_model]
        return SessionSummary(
            session_id=self.session_id,
            source=self.source,
            file_path=self.db_path,
            title=self.title,
            workspace=self.workspace,
            models=models_list,
            start_time=self.first_ts,
            end_time=self.last_ts or self.first_ts,
            step_count=len(self.steps),
            usage=self.usage,
            steps=self.steps,
        )


def _apply_workspace_uris(blob: bytes, state: _ParseState) -> None:
    """Set state.workspace from each workspace URI (sub-field 1) in `blob`; the last one wins."""
    for sf, sw, sv in parse_proto_fields(blob):
        if sf == 1 and isinstance(sv, bytes):
            uri_str = sv.decode("utf-8", errors="ignore")
            if uri_str.startswith("file://"):
                state.workspace = os.path.basename(uri_str.replace("file://", "").rstrip("/"))
            elif uri_str:
                state.workspace = uri_str


def _read_trajectory_meta(cursor: sqlite3.Cursor, tables: Set[str], state: _ParseState) -> None:
    """Stage 1: workspace and start time from trajectory_metadata_blob."""
    if "trajectory_metadata_blob" not in tables:
        return
    try:
        cursor.execute(
            "SELECT data FROM trajectory_metadata_blob WHERE data IS NOT NULL LIMIT 1"
        )
        row = cursor.fetchone()
        if row and row[0]:
            for fnum, wtype, val in parse_proto_fields(row[0]):
                if fnum == 1 and isinstance(val, bytes):
                    _apply_workspace_uris(val, state)
                elif fnum == 2 and isinstance(val, bytes):
                    ts = parse_timestamp_proto(val)
                    if ts:
                        state.start_time_blob = ts
    except _ROW_ERRORS as exc:
        logger.debug("Skipping trajectory_metadata_blob in %s: %s", state.db_path, exc)


def _decode_text(value: bytes) -> str:
    return value.decode("utf-8", errors="ignore").strip()


def _decode_key_value(data: bytes) -> Tuple[str, str]:
    """Decode a {1: key, 2: value} string message (gen_metadata field 20)."""
    fields = {sf: sv for sf, sw, sv in parse_proto_fields(data) if isinstance(sv, bytes)}
    return _decode_text(fields.get(1, b"")), _decode_text(fields.get(2, b""))


def _decode_gen_metadata_row(
    data: bytes, known_models: Set[str]
) -> Tuple[str, Optional[int], str, int]:
    """Decode one gen_metadata blob into (model_name, model_enum_num, model_enum_raw, last_step_idx).

    Valid model names are added to known_models as soon as they are seen, so
    they survive a later field of the same row raising.
    """
    model_name = ""
    model_enum_num: Optional[int] = None
    model_enum_raw = ""
    last_step_idx = -1
    for gf, gw, gv in parse_proto_fields(data):
        if not (gf == 1 and isinstance(gv, bytes)):
            continue
        for f1_f, f1_w, f1_v in parse_proto_fields(gv):
            if f1_f == 19 and isinstance(f1_v, bytes):
                m_str = normalize_model_name(_decode_text(f1_v))
                # Placeholders (M-prefixed) and digit artifacts are not model
                # names; letting them seed known_models / enum_to_model is the
                # phantom-attribution failure described in BUG-010.
                if m_str and not m_str.startswith("M") and not m_str.isdigit():
                    model_name = m_str
                    known_models.add(m_str)
            elif f1_f == 20 and isinstance(f1_v, bytes):
                key_str, val_str = _decode_key_value(f1_v)
                if key_str == "last_step_index" and val_str.isdigit():
                    last_step_idx = int(val_str)
                elif key_str == "model_enum" and val_str:
                    model_enum_raw = val_str.replace("MODEL_PLACEHOLDER_", "")
                    num_str = model_enum_raw.lstrip("Mm")
                    if num_str.isdigit():
                        model_enum_num = int(num_str)
    return model_name, model_enum_num, model_enum_raw, last_step_idx


def _read_gen_metadata(cursor: sqlite3.Cursor, tables: Set[str], state: _ParseState) -> None:
    """Stage 2: models and the step-to-model mapping from gen_metadata."""
    if "gen_metadata" not in tables:
        return
    gm_rows: List[Tuple[int, str, Optional[int]]] = []
    try:
        cursor.execute("SELECT idx, data FROM gen_metadata WHERE size > 0 ORDER BY idx ASC")
        for row_idx, data in cursor.fetchall():
            if not data:
                continue
            model_name, model_enum_num, model_enum_raw, last_step_idx = (
                _decode_gen_metadata_row(data, state.known_models)
            )
            if model_name and model_enum_num is not None:
                state.enum_to_model[model_enum_num] = model_name
                state.enum_to_model[1000 + model_enum_num] = model_name

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
                resolved = state.enum_to_model.get(enum_num, resolved)
            resolved = normalize_model_name(resolved)
            # BUG-010: a gen_metadata row earns an entry in
            # known_models and an attribution anchor only when its
            # model name fully resolves; unresolvable names (empty,
            # M-placeholder, bare enum number) must never fabricate
            # the hardcoded default into the model distribution.
            if resolved and not resolved.startswith("M") and not resolved.isdigit():
                state.known_models.add(resolved)
                if last_step_idx >= 0:
                    state.step_to_model[last_step_idx] = resolved
                    state.step_to_model[last_step_idx + 1] = resolved
    except _ROW_ERRORS as exc:
        logger.debug("Skipping gen_metadata in %s: %s", state.db_path, exc)


def _title_from_prompt_fields(payload_rows: List[tuple]) -> Optional[str]:
    """Title from the structured user prompt (Field 19 -> SubField 2), if any."""
    for s_idx, stype, payload in payload_rows:
        for f, t, v in parse_proto_fields(payload):
            if not (f == 19 and t == 2 and isinstance(v, bytes)):
                continue
            for sf, st, sv in parse_proto_fields(v):
                if sf == 2 and st == 2 and isinstance(sv, bytes):
                    txt = sv.decode("utf-8", errors="ignore").strip()
                    if len(txt) > 3 and not txt.startswith("/Users/"):
                        clean = txt.replace("/goal", "").strip()
                        found = clean.replace("\n", " ")[:60]
                        if found:
                            return found
                        break
    return None


def _title_from_strings(payload_rows: List[tuple]) -> Optional[str]:
    """Fallback title: the first plausible printable string in the payloads."""
    for s_idx, stype, payload in payload_rows:
        for s in extract_string_candidates(payload, min_len=4):
            if _UUID_PREFIX_RE.match(s):
                continue
            if s.startswith(("bot-", "sessionID", "file://", "MODEL_", "allow", "b$", "$")):
                continue
            if len(s) > 8 and not s.isdigit() and not s.startswith(("/Users/", ")/Users/", "3/Users/")):
                clean = s.replace("/goal", "").strip().replace("\n", " ")
                if len(clean) > 5:
                    return clean[:60]
    return None


def _extract_title(cursor: sqlite3.Cursor, db_path: str) -> Optional[str]:
    """Stage 3: session title from the initial steps, or None if none is found."""
    try:
        cursor.execute(
            "SELECT idx, step_type, step_payload FROM steps WHERE idx IN (1, 0, 2, 3) AND step_payload IS NOT NULL ORDER BY idx ASC"
        )
        payload_rows = cursor.fetchall()
        return _title_from_prompt_fields(payload_rows) or _title_from_strings(payload_rows)
    except _ROW_ERRORS as exc:
        logger.debug("Skipping title extraction in %s: %s", db_path, exc)
        return None


class _StepRowStream:
    """Yield (idx, metadata) rows straight from the cursor.

    Rows are streamed instead of fetchall()-ed so metadata blobs are never all
    held in memory at once. A SQLite error, whether at execute or part-way
    through the read, ends the stream and sets `failed`; the caller then
    discards every step read so far, exactly as a failed fetchall() produced
    no rows at all.
    """

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor
        self.failed = False

    def __iter__(self):
        try:
            self._cursor.execute(
                "SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx ASC"
            )
            yield from self._cursor
        except sqlite3.DatabaseError:
            # Locked/corrupt/vanished mid-read: keep title, workspace and any
            # steps already collected instead of dropping the whole session.
            self.failed = True


def _resolve_step_model(
    idx: int,
    usage_raw: bytes,
    state: _ParseState,
    gm_anchors: List[Tuple[int, str]],
    gm_anchor_positions: List[int],
) -> str:
    """Identify the model for a step:

    1) Direct gen_metadata last_step_index anchor
    2) UsageMetadata field 1 model_enum integer
    3) Most recent preceding gen_metadata anchor in this session
    4) Session default model
    """
    step_model = state.step_to_model.get(idx, "")
    if not step_model:
        u_enum = extract_usage_model_enum(usage_raw)
        if u_enum is not None and u_enum in state.enum_to_model:
            step_model = state.enum_to_model[u_enum]
    if not step_model and gm_anchors:
        anchor_pos = bisect.bisect_right(gm_anchor_positions, idx) - 1
        if anchor_pos >= 0:
            step_model = gm_anchors[anchor_pos][1]
    if not step_model:
        step_model = state.default_model
    return step_model


def _parse_step_row(
    idx: int,
    meta_blob: bytes,
    state: _ParseState,
    gm_anchors: List[Tuple[int, str]],
    gm_anchor_positions: List[int],
) -> Optional[_RawStep]:
    """Parse one step's metadata; None if it carries no usage."""
    meta_map: Dict[int, bytes | int] = {f: v for f, w, v in parse_proto_fields(meta_blob)}

    # Check for UsageMetadata in Field 9
    usage_raw = meta_map.get(9)
    if not isinstance(usage_raw, bytes):
        return None

    step_usage = parse_usage_proto(usage_raw)
    # Ignore empty usage steps
    if step_usage.total_tokens == 0:
        return None

    # Extract Step Timestamp from Field 1, 6, 7, 8, or 32
    step_ts: Optional[datetime] = None
    for ts_field_id in (1, 6, 7, 8, 32):
        ts_raw = meta_map.get(ts_field_id)
        if isinstance(ts_raw, bytes):
            ts = parse_timestamp_proto(ts_raw)
            if ts:
                step_ts = ts
                break

    step_model = _resolve_step_model(idx, usage_raw, state, gm_anchors, gm_anchor_positions)
    return idx, step_ts, step_model, step_usage


def _read_steps(
    cursor: sqlite3.Cursor,
    db_path: str,
    state: _ParseState,
    gm_anchors: List[Tuple[int, str]],
    gm_anchor_positions: List[int],
) -> None:
    """Stage 4: step records and aggregated token usage."""
    stream = _StepRowStream(cursor)

    try:
        db_mtime: Optional[datetime] = datetime.fromtimestamp(os.path.getmtime(db_path))
    except (OSError, OverflowError, ValueError):
        db_mtime = None

    # BUG-018: Collect raw steps without immediately falling back to db_mtime
    raw_steps: List[_RawStep] = []

    for idx, meta_blob in stream:
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
            # BUG-018: Record step candidate without immediate db_mtime fallback;
            # missing timestamps will be filled via neighbour inference below.
            raw_step = _parse_step_row(idx, meta_blob, state, gm_anchors, gm_anchor_positions)
        except _ROW_ERRORS as exc:
            # One malformed row must not discard the rest of the session
            logger.debug("Skipping malformed step idx=%r in %s: %s", idx, db_path, exc)
            continue
        if raw_step is not None:
            raw_steps.append(raw_step)

    if stream.failed:
        raw_steps.clear()

    # BUG-018: Fill missing timestamps using nearest neighbour inference.
    # If no steps have timestamps, fall back to start_time_blob, or min(db_mtime, now).
    session_fallback: Optional[datetime] = None
    if state.start_time_blob is not None:
        session_fallback = state.start_time_blob
    elif db_mtime is not None:
        session_fallback = min(db_mtime, datetime.now())

    filled_timestamps = _fill_missing_step_timestamps(
        [ts for _, ts, _, _ in raw_steps], fallback=session_fallback
    )

    for (idx, _, step_model, step_usage), final_ts in zip(raw_steps, filled_timestamps):
        if final_ts is None:
            # No timestamp could be inferred; drop the step
            continue

        # Build everything that can raise before mutating any state, so
        # `steps` and `usage` never drift apart.
        new_usage = state.usage.add(step_usage)
        step_rec = StepRecord(
            step_idx=idx,
            timestamp=final_ts,
            model=step_model,
            usage=step_usage,
        )

        if state.first_ts is None or final_ts < state.first_ts:
            state.first_ts = final_ts
        if state.last_ts is None or final_ts > state.last_ts:
            state.last_ts = final_ts

        state.known_models.add(step_model)
        state.model_token_totals[step_model] = (
            state.model_token_totals.get(step_model, 0) + step_usage.total_tokens
        )
        state.steps.append(step_rec)
        state.usage = new_usage


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
        state = _ParseState(
            session_id=session_id,
            source=source,
            db_path=db_path,
            title=f"Session {session_id[:8]}",
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
            state.validated = True

            _read_trajectory_meta(cursor, tables, state)
            # Keep the blob start time for partial summaries: a failure in any
            # later stage must not lose the only timestamp already parsed.
            state.first_ts = state.start_time_blob

            _read_gen_metadata(cursor, tables, state)
            gm_anchors: List[Tuple[int, str]] = sorted(
                (idx_anchor, m_name)
                for idx_anchor, m_name in state.step_to_model.items()
                if m_name
            )
            # Positions of gm_anchors, for bisecting the nearest preceding anchor.
            gm_anchor_positions: List[int] = [a_idx for a_idx, _ in gm_anchors]
            state.default_model = (
                min(state.known_models) if state.known_models else _DEFAULT_MODEL
            )

            found_title = _extract_title(cursor, db_path)
            if found_title:
                state.title = found_title

            _read_steps(cursor, db_path, state, gm_anchors, gm_anchor_positions)
            return state.build_summary(), True

        except sqlite3.DatabaseError:
            # SQLite failure after validation (locked, corrupt page, vanished
            # file): emit what we have so the session stays visible, unless the
            # DB was never validated as a usable session at all.
            return (state.build_summary(), False) if state.validated else (None, False)
        except Exception:
            # Unknown failure: preserve the previous skip-the-session behavior.
            return None, False
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
