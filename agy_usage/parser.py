"""Zero-dependency parser for Antigravity SQLite databases and Protobuf payloads."""

from __future__ import annotations

import glob
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from agy_usage.models import SessionSummary, StepRecord, TokenUsage


def decode_varint(data: bytes, idx: int) -> Tuple[int, int]:
    """Decode a variable-length integer (varint) starting at index."""
    val = 0
    shift = 0
    while idx < len(data):
        b = data[idx]
        idx += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return val, idx


def parse_proto_fields(data: bytes) -> List[Tuple[int, int, bytes | int]]:
    """Parse raw protobuf wire-format bytes into a list of (field_num, wire_type, value)."""
    idx = 0
    fields: List[Tuple[int, int, bytes | int]] = []
    length_data = len(data)
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
    def scan_directory(cls, dir_path: str, source: str = "custom") -> List[SessionSummary]:
        """Scan a directory for SQLite database files and extract session usage."""
        if not os.path.isdir(dir_path):
            return []

        db_files = sorted(
            glob.glob(os.path.join(dir_path, "*.db")),
            key=lambda p: os.path.getmtime(p),
            reverse=True,
        )

        sessions: List[SessionSummary] = []
        for db_file in db_files:
            summary = cls.parse_session_db(db_file, source=source)
            if summary:
                sessions.append(summary)
        return sessions

    @classmethod
    def scan_all(
        cls,
        custom_dir: Optional[str] = None,
        source_mode: str = "all",
    ) -> List[SessionSummary]:
        """Scan all relevant directories or a specified directory."""
        all_sessions: List[SessionSummary] = []
        if custom_dir:
            source_label = "custom"
            if "antigravity-ide" in custom_dir:
                source_label = "ide"
            elif "antigravity" in custom_dir:
                source_label = "cli"
            return cls.scan_directory(custom_dir, source=source_label)

        for d_path, s_label in cls.get_default_directories(mode=source_mode):
            all_sessions.extend(cls.scan_directory(d_path, source=s_label))

        # Sort all sessions descending by start_time (or file mtime if start_time is missing)
        all_sessions.sort(
            key=lambda s: s.start_time.timestamp()
            if s.start_time
            else os.path.getmtime(s.file_path),
            reverse=True,
        )
        return all_sessions

    @classmethod
    def parse_session_db(cls, db_path: str, source: str = "cli") -> Optional[SessionSummary]:
        """Extract complete usage, steps, title, and metadata from a single SQLite DB."""
        if not os.path.isfile(db_path):
            return None

        filename = os.path.basename(db_path)
        session_id = filename.replace(".db", "")

        conn: Optional[sqlite3.Connection] = None
        try:
            # Connect in read-only mode to prevent write locks or interfering with active runs
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            except Exception:
                conn = sqlite3.connect(db_path)

            cursor = conn.cursor()

            # Verify required tables exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
            if "steps" not in tables:
                return None

            # 1. Extract Workspace / Project from trajectory_metadata_blob
            workspace = "Unknown"
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
                except Exception:
                    pass

            # 2. Extract Models and Step-to-Model Mapping from gen_metadata
            step_to_model: Dict[int, str] = {}
            known_models: Set[str] = set()
            if "gen_metadata" in tables:
                try:
                    cursor.execute("SELECT idx, data FROM gen_metadata WHERE size > 0")
                    for row_idx, data in cursor.fetchall():
                        if not data:
                            continue
                        g_fields = parse_proto_fields(data)
                        model_name = ""
                        last_step_idx = -1
                        for gf, gw, gv in g_fields:
                            if gf == 1 and isinstance(gv, bytes):
                                f1_fields = parse_proto_fields(gv)
                                for f1_f, f1_w, f1_v in f1_fields:
                                    if f1_f == 19 and isinstance(f1_v, bytes):
                                        m_str = f1_v.decode("utf-8", errors="ignore").strip()
                                        if m_str:
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
                                        elif (
                                            key_str == "model_enum"
                                            and not model_name
                                            and val_str
                                        ):
                                            # Friendly fallback for model_enum
                                            model_name = val_str.replace("MODEL_PLACEHOLDER_", "")

                        if model_name and last_step_idx >= 0:
                            # Map nearby model step (last_step_index + 1 is typically the response step)
                            step_to_model[last_step_idx] = model_name
                            step_to_model[last_step_idx + 1] = model_name
                except Exception:
                    pass

            default_model = (
                sorted(list(known_models))[0]
                if known_models
                else "gemini-3.7-flash"
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
            except Exception:
                pass

            # 4. Extract Step Records and Token Usage
            steps: List[StepRecord] = []
            aggregated_usage = TokenUsage()
            first_ts: Optional[datetime] = start_time_blob
            last_ts: Optional[datetime] = None

            cursor.execute(
                "SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx ASC"
            )
            for idx, meta_blob in cursor.fetchall():
                if not meta_blob:
                    continue
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

                if not step_ts:
                    # Fallback to DB file modification time
                    step_ts = datetime.fromtimestamp(os.path.getmtime(db_path))

                if first_ts is None or step_ts < first_ts:
                    first_ts = step_ts
                if last_ts is None or step_ts > last_ts:
                    last_ts = step_ts

                # Identify Model for this step
                step_model = step_to_model.get(idx, default_model)

                step_rec = StepRecord(
                    step_idx=idx,
                    timestamp=step_ts,
                    model=step_model,
                    usage=step_usage,
                )
                steps.append(step_rec)
                aggregated_usage = aggregated_usage.add(step_usage)

            # Assemble session summary
            models_list = sorted(list(known_models)) if known_models else [default_model]
            summary = SessionSummary(
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
            return summary

        except Exception as e:
            # Return graceful partial summary if available or None
            return None
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
