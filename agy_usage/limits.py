"""Quota and rate limits tracking for Google Antigravity (5-hour and weekly limits)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from agy_usage.display import (
    Colors,
)
from agy_usage.models import (
    SessionSummary,
    TokenUsage,
)


# Shared quota warning and critical thresholds in percentage consumed (BUG-012)
WARN_MODERATE: float = 60.0
WARN_HIGH: float = 80.0
CRIT: float = 100.0

# Clock skew tolerance for cycle window calculations (BUG-018)
_FUTURE_SKEW: timedelta = timedelta(minutes=5)


def classify_model_pool(model_name: str) -> str:
    """Classify a model name into its independent Antigravity quota pool.

    - ``"argon"``: Gemini 4 Argon (dedicated weekly-only quota pool, separate from Flash/Pro)
    - ``"claude"``: Claude & GPT partner models (independent 5-hour + weekly quota pool)
    - ``"gemini"``: Standard Google Gemini Flash / Pro models (5-hour sprint + weekly cycle)
    """
    m = (model_name or "").lower()
    if "argon" in m:
        return "argon"
    if (
        "claude" in m
        or "sonnet" in m
        or "opus" in m
        or "haiku" in m
        or "gpt" in m
        or "openai" in m
    ):
        return "claude"
    return "gemini"


@dataclass
class TierConfig:
    """Antigravity subscription plan tier quotas across model pools.

    Antigravity's backend enforces quotas proportionally to the USD cost of tokens
    consumed within each pool's 5-hour or 7-day cycle. Both token allowances (at
    typical ~90% prompt cache hit rates) and calibrated USD cost allowances are
    stored below.
    """

    name: str
    display_name: str
    five_hour_limit: int
    weekly_limit: int
    description: str
    argon_weekly_limit: int = 31_000_000
    claude_five_hour_limit: int = 3_450_000
    claude_weekly_limit: int = 6_910_000
    five_hour_cost_usd: float = 11.30
    weekly_cost_usd: float = 56.50
    argon_weekly_cost_usd: float = 13.31
    claude_five_hour_cost_usd: float = 2.33
    claude_weekly_cost_usd: float = 4.66


TIERS: Dict[str, TierConfig] = {
    "free": TierConfig(
        name="free",
        display_name="Google AI Free",
        five_hour_limit=20_000_000,
        weekly_limit=150_000_000,
        description="Free Tier (Flash models only; Argon & Claude unavailable)",
        argon_weekly_limit=0,
        claude_five_hour_limit=0,
        claude_weekly_limit=0,
        five_hour_cost_usd=2.25,
        weekly_cost_usd=11.30,
        argon_weekly_cost_usd=0.0,
        claude_five_hour_cost_usd=0.0,
        claude_weekly_cost_usd=0.0,
    ),
    "pro": TierConfig(
        name="pro",
        display_name="Google AI Pro",
        five_hour_limit=100_000_000,
        weekly_limit=750_000_000,
        description="Pro Tier (Default Antigravity subscription)",
        argon_weekly_limit=31_000_000,
        claude_five_hour_limit=3_450_000,
        claude_weekly_limit=6_910_000,
        five_hour_cost_usd=11.30,
        weekly_cost_usd=56.50,
        argon_weekly_cost_usd=13.31,
        claude_five_hour_cost_usd=2.33,
        claude_weekly_cost_usd=4.66,
    ),
    "ultra": TierConfig(
        name="ultra",
        display_name="Google AI Ultra",
        five_hour_limit=500_000_000,
        weekly_limit=3_750_000_000,
        description="Ultra Tier (Highest quota across Gemini, Argon & Claude)",
        argon_weekly_limit=155_000_000,
        claude_five_hour_limit=17_250_000,
        claude_weekly_limit=34_550_000,
        five_hour_cost_usd=56.50,
        weekly_cost_usd=282.50,
        argon_weekly_cost_usd=66.55,
        claude_five_hour_cost_usd=11.65,
        claude_weekly_cost_usd=23.30,
    ),
}


def tier_key_from_server_id(user_tier_id: Any) -> Optional[str]:
    """Map a ``GetUserStatus`` ``userTier.id`` to a ``TIERS`` key, or None if unrecognised.

    Only ``g1-pro-tier`` has been observed on a real server; the ultra and free
    ids are assumed to follow the same ``<word>-tier`` shape, so match on the
    dash-separated words and refuse anything ambiguous or unknown.
    """
    if not isinstance(user_tier_id, str):
        return None
    words = set(re.split(r"[^a-z0-9]+", user_tier_id.lower()))
    matches = [key for key in TIERS if key in words]
    return matches[0] if len(matches) == 1 else None


@dataclass
class WindowStats:
    """Usage statistics within a specific 5-hour or 7-day cycle window."""

    window_name: str
    start_time: datetime
    end_time: datetime
    usage: TokenUsage
    step_count: int
    session_count: int
    limit_tokens: int
    estimated_cost: float
    earliest_activity: Optional[datetime] = None
    latest_activity: Optional[datetime] = None
    refresh_time: Optional[datetime] = None
    next_relief_time: Optional[datetime] = None
    pool: str = "gemini"
    cost_limit_usd: Optional[float] = None
    server_remaining_fraction: Optional[float] = None
    server_description: Optional[str] = None
    server_unreachable: bool = False

    @property
    def is_unknown(self) -> bool:
        """Whether nothing is known about this window's quota.

        True when the live server could not be reached, no server fraction was
        supplied, and this device's history has no usage in the window. Usage
        from other devices only shows up through the server, so ``pct_remaining``
        (100%) would be a guess, not a measurement.
        """
        return (
            self.server_unreachable
            and self.server_remaining_fraction is None
            and self.usage.total_tokens <= 0
        )

    @property
    def pct_used(self) -> float:
        """Percentage of limit consumed."""
        if self.server_remaining_fraction is not None:
            rem = max(0.0, min(1.0, float(self.server_remaining_fraction)))
            return (1.0 - rem) * 100.0
        if (
            self.cost_limit_usd is not None
            and self.cost_limit_usd > 0
            and self.estimated_cost > 0
        ):
            return min(100.0, (self.estimated_cost / self.cost_limit_usd) * 100.0)
        if self.limit_tokens <= 0:
            return 100.0 if self.usage.total_tokens > 0 else 0.0
        return min(100.0, (self.usage.total_tokens / self.limit_tokens) * 100.0)

    @property
    def pct_remaining(self) -> float:
        """Percentage of limit remaining."""
        if self.server_remaining_fraction is not None:
            rem = max(0.0, min(1.0, float(self.server_remaining_fraction)))
            return rem * 100.0
        if self.limit_tokens <= 0 and (
            self.cost_limit_usd is None or self.cost_limit_usd <= 0
        ):
            return 0.0
        return max(0.0, 100.0 - self.pct_used)

    @property
    def tokens_remaining(self) -> int:
        """Tokens remaining before hitting limit."""
        if self.limit_tokens <= 0 or self.is_exceeded:
            return 0
        if self.server_remaining_fraction is not None or (
            self.cost_limit_usd is not None
            and self.cost_limit_usd > 0
            and self.estimated_cost > 0
        ):
            return max(
                0,
                min(
                    self.limit_tokens,
                    int(round(self.limit_tokens * (self.pct_remaining / 100.0))),
                ),
            )
        return max(0, self.limit_tokens - self.usage.total_tokens)

    @property
    def is_exceeded(self) -> bool:
        """Whether limit has been breached."""
        if self.server_remaining_fraction is not None:
            return float(self.server_remaining_fraction) <= 0.0
        if (
            self.cost_limit_usd is not None
            and self.cost_limit_usd > 0
            and self.estimated_cost > 0
        ):
            return self.estimated_cost >= self.cost_limit_usd
        if self.limit_tokens <= 0:
            return self.usage.total_tokens > 0
        return self.usage.total_tokens >= self.limit_tokens


@dataclass
class MultiPoolLimits:
    """Quota window statistics across all separate Antigravity model pools."""

    tier: TierConfig
    gemini_5h: WindowStats
    gemini_7d: WindowStats
    argon_7d: WindowStats
    claude_5h: WindowStats
    claude_7d: WindowStats
    live_synced: bool = False
    live_error: Optional[str] = None
    # Where the server fractions came from: "live" (the language_server just
    # answered), "cached" (the last server reading on disk, server unreachable
    # now), or "local" (no server data, local DB estimate only).
    quota_source: str = "local"
    cache_fetched_at: Optional[datetime] = None
    cache_age_seconds: Optional[float] = None
    # How the tier was chosen: "explicit" (caller named one), "auto" (read from
    # the live server), or "default" (auto requested but the server gave no
    # usable tier id, so pro was assumed).
    tier_source: str = "explicit"


def parse_token_amount(text: Optional[str]) -> Optional[int]:
    """Parse string representations like '50M', '1.5B', '200K' into integer tokens.

    Returns None for anything that is not a positive, finite token count of at
    least 1: unparseable text, NaN, infinity, negatives, zero, and positive
    fractions below 1 token (int() truncation).
    """
    if not text:
        return None
    s = text.strip().upper()
    multiplier = 1
    if s.endswith("B"):
        multiplier = 1_000_000_000
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1_000_000
        s = s[:-1]
    elif s.endswith("K"):
        multiplier = 1_000
        s = s[:-1]
    try:
        tokens = int(float(s) * multiplier)
    except (ValueError, OverflowError):
        # ValueError: unparseable text or NaN; OverflowError: infinity
        # (e.g. "inf", "1e309") or float multiplication overflowing to inf.
        return None
    if tokens < 1:  # rejects negatives and zero
        return None
    return tokens


def calibrate_limit_from_remaining(
    used_tokens: int, remaining_pct: Optional[float]
) -> Optional[int]:
    """Infer total token cap from observed token usage and UI 'Remaining %'."""
    if remaining_pct is None or used_tokens <= 0:
        return None
    used_pct = 100.0 - float(remaining_pct)
    if used_pct <= 0.0:
        return None
    if used_pct >= 100.0:
        return used_tokens
    return int(round(used_tokens / (used_pct / 100.0)))


def _parse_utc_iso_to_local_naive(iso_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp (e.g. '2026-10-25T00:21:51Z') into local naive datetime."""
    if not iso_str:
        return None
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt_utc = datetime.fromisoformat(s)
        if dt_utc.tzinfo is not None:
            return dt_utc.astimezone().replace(tzinfo=None)
        return dt_utc
    except Exception:
        return None


def _parse_fetched_at(iso_str: Optional[str]) -> Optional[datetime]:
    """Parse a quota cache ``fetched_at`` stamp into an aware UTC datetime.

    Naive stamps are treated as UTC. Returns None for missing or unparseable
    stamps so the caller rejects the cache (BUG-005).
    """
    if not iso_str:
        return None
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt_utc = datetime.fromisoformat(s)
    except (AttributeError, TypeError, ValueError):
        return None
    if dt_utc.tzinfo is None:
        return dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(timezone.utc)


_QUOTA_CACHE_PATH = os.path.expanduser(
    "~/.gemini/antigravity/agy_usage_quota_cache.json"
)

# BUG-005: the on-disk quota cache is a fallback, not a live feed — treat it as
# valid for at most this long after it was fetched, and never rotate hours-old
# remainingFractions in place of a live server response.
_QUOTA_CACHE_TTL_SECONDS = 60.0

# A cache older than the TTL but younger than this is still the last word the
# server gave about this account (local DBs only see this device's usage), so
# it is returned tagged ``"source": "cached"`` and shown with its age, never as
# live. Buckets whose resetTime has passed are still dropped downstream.
_QUOTA_CACHE_MAX_AGE_SECONDS = 6 * 3600.0


def fetch_live_quota_summary(
    use_cache: bool = True, errors: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """Query the local Antigravity language_server for authoritative quota buckets.

    Calls ``RetrieveUserQuotaSummary`` and ``GetUserStatus`` on the localhost port
    of the running Antigravity ``language_server`` process (if active), falling back
    to the last cached response on disk. A cache up to ~60s old is returned as is.
    An older one, up to ``_QUOTA_CACHE_MAX_AGE_SECONDS``, is returned with
    ``"source": "cached"`` and its ``"age_seconds"`` (the same age the error note
    reports) so callers never render it as live server data
    (BUG-005). Older caches, or caches with no buckets, are rejected.

    When ``errors`` is a list, a short reason is appended for each step that
    failed, so callers can tell the user why live data is missing.
    """

    def _note(reason: str) -> None:
        if errors is not None:
            errors.append(reason)

    ps_ok = True
    try:
        ps_out = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).decode("utf-8", errors="ignore")
    except Exception as exc:
        ps_out = ""
        ps_ok = False
        _note(f"could not list processes ({exc.__class__.__name__})")

    found_server = False
    for line in ps_out.splitlines():
        if "language_server" not in line or "--csrf_token" not in line:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        pid, cmd = parts[0], parts[1]
        m_csrf = re.search(r"--csrf_token\s+([^\s]+)", cmd)
        if not m_csrf:
            continue
        csrf = m_csrf.group(1)
        found_server = True

        try:
            lsof_out = subprocess.check_output(
                ["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).decode("utf-8", errors="ignore")
        except Exception as exc:
            _note(f"could not list ports of language_server pid {pid} ({exc.__class__.__name__})")
            continue

        ports: List[int] = []
        for lline in lsof_out.splitlines():
            m_port = re.search(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", lline)
            if m_port:
                ports.append(int(m_port.group(1)))
        if not ports:
            _note(f"language_server pid {pid} is not listening on 127.0.0.1")

        for port in sorted(ports):
            base_url = f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService"
            headers = {
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "x-codeium-csrf-token": csrf,
            }
            try:
                req = urllib.request.Request(
                    f"{base_url}/RetrieveUserQuotaSummary",
                    data=b"{}",
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=1.2) as resp:
                    quota_data = json.loads(resp.read().decode("utf-8"))
                if "response" not in quota_data or "groups" not in quota_data["response"]:
                    _note(f"port {port}: response has no quota groups")
                    continue

                buckets: Dict[str, Dict[str, Any]] = {}
                for grp in quota_data["response"].get("groups", []):
                    for b in grp.get("buckets", []):
                        bid = b.get("bucketId")
                        if bid:
                            buckets[bid] = {
                                "bucketId": bid,
                                "displayName": b.get("displayName", ""),
                                "description": b.get("description", ""),
                                "window": b.get("window", ""),
                                "remainingFraction": float(b.get("remainingFraction", 0.0)),
                                "resetTime": b.get("resetTime"),
                            }

                user_tier_id = None
                user_tier_name = None
                try:
                    req_u = urllib.request.Request(
                        f"{base_url}/GetUserStatus",
                        data=b"{}",
                        headers=headers,
                        method="POST",
                    )
                    with urllib.request.urlopen(req_u, timeout=1.0) as resp_u:
                        u_data = json.loads(resp_u.read().decode("utf-8"))
                        ut = u_data.get("userStatus", {}).get("userTier", {})
                        user_tier_id = ut.get("id")
                        user_tier_name = ut.get("name")
                except Exception:
                    pass

                result = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "user_tier_id": user_tier_id,
                    "user_tier_name": user_tier_name,
                    "buckets": buckets,
                }
                try:
                    os.makedirs(os.path.dirname(_QUOTA_CACHE_PATH), exist_ok=True)
                    with open(_QUOTA_CACHE_PATH, "w", encoding="utf-8") as fp:
                        json.dump(result, fp, indent=2)
                except Exception:
                    pass
                return result
            except Exception as exc:
                _note(f"port {port}: {str(exc)[:80] or exc.__class__.__name__}")
                continue

    if ps_ok and not found_server:
        _note("no running Antigravity language_server found")

    if use_cache and os.path.isfile(_QUOTA_CACHE_PATH):
        try:
            with open(_QUOTA_CACHE_PATH, "r", encoding="utf-8") as fp:
                cached = json.load(fp)
            if isinstance(cached, dict) and "buckets" in cached:
                fetched_at = _parse_fetched_at(cached.get("fetched_at"))
                if fetched_at is not None:
                    age = datetime.now(timezone.utc) - fetched_at
                    if age <= timedelta(seconds=_QUOTA_CACHE_TTL_SECONDS):
                        return cached
                    _note(f"cached server reading is {int(age.total_seconds() // 60)} min old")
                    if (
                        age <= timedelta(seconds=_QUOTA_CACHE_MAX_AGE_SECONDS)
                        and isinstance(cached["buckets"], dict)
                        and cached["buckets"]
                    ):
                        cached["source"] = "cached"
                        cached["age_seconds"] = max(0.0, age.total_seconds())
                        return cached
        except Exception:
            pass
    return None


def should_use_live_server(sessions: List[SessionSummary]) -> bool:
    """Return True when local history cannot fully explain quota.

    A device with no local history still asks: its quota may have been spent
    on another device, which only the server knows about.
    """
    return not sessions or any(
        bool(s.file_path and os.path.isfile(s.file_path)) for s in sessions
    )


class LimitsCalculator:
    """Calculates 5-hour and 7-day weekly quota cycle usage across Antigravity model pools."""

    @staticmethod
    def _iter_pool_items(
        sessions: List[SessionSummary],
    ) -> Iterator[Tuple[str, Tuple[datetime, TokenUsage, float, str]]]:
        """Yield ``(pool, (timestamp, usage, step_cost_usd, session_id))`` in session/step order.

        Each step, or each step-less session (timestamped by its start/end), is
        classified exactly once. Timestamps are stripped of their timezone.
        """
        for s in sessions:
            if s.steps:
                for step in s.steps:
                    st_ts = step.timestamp
                    if st_ts.tzinfo:
                        st_ts = st_ts.replace(tzinfo=None)
                    yield classify_model_pool(step.model), (
                        st_ts,
                        step.usage,
                        step.cost.total,
                        s.session_id,
                    )
            else:
                s_time = s.start_time or s.end_time
                if s_time:
                    s_ts = s_time.replace(tzinfo=None) if s_time.tzinfo else s_time
                    yield classify_model_pool(s.primary_model), (
                        s_ts,
                        s.usage,
                        s.cost.total,
                        s.session_id,
                    )

    @staticmethod
    def _collect_pool_items(
        sessions: List[SessionSummary],
        pool: Optional[str] = "gemini",
    ) -> List[Tuple[datetime, TokenUsage, float, str]]:
        """Collect chronological (timestamp, usage, step_cost_usd, session_id) for a pool."""
        filter_pool = None if pool in (None, "all") else pool
        items = [
            item
            for item_pool, item in LimitsCalculator._iter_pool_items(sessions)
            if not filter_pool or item_pool == filter_pool
        ]
        items.sort(key=lambda x: x[0])
        return items

    @staticmethod
    def _partition_pool_items(
        sessions: List[SessionSummary],
    ) -> Dict[str, List[Tuple[datetime, TokenUsage, float, str]]]:
        """Split sessions into chronological per-pool item lists, classifying each step once.

        Equivalent to calling ``_collect_pool_items`` for "gemini", "argon" and "claude",
        but walks the sessions a single time.
        """
        pools: Dict[str, List[Tuple[datetime, TokenUsage, float, str]]] = {
            "gemini": [],
            "argon": [],
            "claude": [],
        }
        for item_pool, item in LimitsCalculator._iter_pool_items(sessions):
            pools[item_pool].append(item)

        for items in pools.values():
            items.sort(key=lambda x: x[0])
        return pools

    @staticmethod
    def _compute_cycle_window(
        items: List[Tuple[datetime, TokenUsage, float, str]],
        current_time: datetime,
        duration: timedelta,
        limit_tokens: int,
        pool: str,
        window_label: str,
        cost_limit_usd: Optional[float] = None,
        override_reset_time: Optional[datetime] = None,
        server_remaining_fraction: Optional[float] = None,
        server_description: Optional[str] = None,
    ) -> WindowStats:
        """Compute usage within a single fixed-duration Antigravity quota cycle.

        In Google Antigravity, both the 5-hour limit and the 7-day weekly limit operate
        on fixed-duration cycles anchored at the first turn after the previous cycle
        expired: when ``c_start + duration`` passes, the window resets completely to 100%.
        """
        # BUG-018: Normalize items against current_time to prevent future-dated steps
        # from corrupting cycle anchoring or inflating windows.
        # - Items with ts > current_time + _FUTURE_SKEW are dropped entirely.
        # - Items with current_time < ts <= current_time + _FUTURE_SKEW are clamped to current_time.
        # Clamping preserves the sorted order.
        normalized_items: List[Tuple[datetime, TokenUsage, float, str]] = []
        for ts, u, step_cost, sid in items:
            if ts > current_time + _FUTURE_SKEW:
                continue
            if current_time < ts <= current_time + _FUTURE_SKEW:
                ts = current_time
            normalized_items.append((ts, u, step_cost, sid))
        items = normalized_items

        usage = TokenUsage()
        cost = 0.0
        steps_count = 0
        sessions_ids = set()
        earliest: Optional[datetime] = None
        latest: Optional[datetime] = None
        cycle_start = current_time - duration
        cycle_end = current_time

        # Decide the cycle bounds first; an expired or empty cycle accumulates nothing.
        accumulate = False
        if override_reset_time is not None and override_reset_time > current_time:
            cycle_end = override_reset_time
            cycle_start = override_reset_time - duration
            accumulate = True
        elif items:
            c_start = items[0][0]
            c_end = c_start + duration
            for ts, u, step_cost, sid in items:
                if ts >= c_end:
                    c_start = ts
                    c_end = c_start + duration

            if current_time < c_end:
                cycle_start = c_start
                cycle_end = c_end
                accumulate = True

        if accumulate:
            for ts, u, step_cost, sid in items:
                if cycle_start <= ts <= current_time:
                    usage = usage.add(u)
                    cost += step_cost
                    steps_count += 1
                    sessions_ids.add(sid)
                    if earliest is None or ts < earliest:
                        earliest = ts
                    if latest is None or ts > latest:
                        latest = ts

        has_activity = usage.total_tokens > 0 or (
            server_remaining_fraction is not None and server_remaining_fraction < 1.0
        )
        refresh_dt: Optional[datetime] = None
        if has_activity and cycle_end > current_time:
            refresh_dt = cycle_end

        return WindowStats(
            window_name=window_label,
            start_time=cycle_start,
            end_time=cycle_end,
            usage=usage,
            step_count=steps_count,
            session_count=len(sessions_ids),
            limit_tokens=limit_tokens,
            estimated_cost=cost,
            earliest_activity=earliest,
            latest_activity=latest,
            refresh_time=refresh_dt,
            next_relief_time=refresh_dt,
            pool=pool,
            cost_limit_usd=cost_limit_usd,
            server_remaining_fraction=server_remaining_fraction,
            server_description=server_description,
        )

    @staticmethod
    def _compute_windows_from_items(
        items: List[Tuple[datetime, TokenUsage, float, str]],
        current_time: datetime,
        limit_5h: int,
        limit_7d: int,
        pool: str = "gemini",
        window_5h_label: str = "5-Hour Sprint Window",
        window_7d_label: str = "Weekly Quota Cycle",
        cost_limit_5h_usd: Optional[float] = None,
        cost_limit_7d_usd: Optional[float] = None,
    ) -> Tuple[WindowStats, WindowStats]:
        """Compute 5-hour and 7-day cycle stats from filtered items."""
        w_5h = LimitsCalculator._compute_cycle_window(
            items,
            current_time=current_time,
            duration=timedelta(hours=5),
            limit_tokens=limit_5h,
            pool=pool,
            window_label=window_5h_label,
            cost_limit_usd=cost_limit_5h_usd,
        )
        w_7d = LimitsCalculator._compute_cycle_window(
            items,
            current_time=current_time,
            duration=timedelta(days=7),
            limit_tokens=limit_7d,
            pool=pool,
            window_label=window_7d_label,
            cost_limit_usd=cost_limit_7d_usd,
        )
        return w_5h, w_7d

    @staticmethod
    def compute(
        sessions: List[SessionSummary],
        tier_name: str = "pro",
        five_h_limit: Optional[int] = None,
        weekly_limit: Optional[int] = None,
        now: Optional[datetime] = None,
        pool: Optional[str] = "gemini",
    ) -> Tuple[WindowStats, WindowStats, TierConfig]:
        """Compute 5-hour and 7-day cycle statistics for a single model pool."""
        current_time = now or datetime.now()
        tier_key = tier_name.lower().strip()
        tier = TIERS.get(tier_key, TIERS["pro"])

        if pool == "argon":
            default_5h = 0
            default_7d = tier.argon_weekly_limit
        elif pool == "claude":
            default_5h = tier.claude_five_hour_limit
            default_7d = tier.claude_weekly_limit
        else:
            default_5h = tier.five_hour_limit
            default_7d = tier.weekly_limit

        limit_5h = (
            five_h_limit
            if five_h_limit is not None and five_h_limit > 0
            else default_5h
        )
        limit_7d = (
            weekly_limit
            if weekly_limit is not None and weekly_limit > 0
            else default_7d
        )

        items = LimitsCalculator._collect_pool_items(sessions, pool=pool)
        w_5h, w_7d = LimitsCalculator._compute_windows_from_items(
            items,
            current_time=current_time,
            limit_5h=limit_5h,
            limit_7d=limit_7d,
            pool=pool or "gemini",
        )
        return w_5h, w_7d, tier

    @staticmethod
    def _bucket_override(
        bucket: Optional[Dict[str, Any]],
        current_time: datetime,
        has_custom_token_limit: bool,
    ) -> Optional[Tuple[datetime, float, Optional[str]]]:
        """Return ``(reset_dt, remaining_fraction, description)`` when a bucket drives a window.

        None when there is no bucket, the limit is custom, or the bucket's
        resetTime is unparseable or not in the future. BUG-005: a bucket whose
        resetTime is not in the future describes an expired cycle, so its
        remainingFraction must not be grafted onto the freshly computed window.
        """
        if bucket is None or has_custom_token_limit:
            return None
        reset_dt = _parse_utc_iso_to_local_naive(bucket.get("resetTime"))
        rem_frac = float(bucket.get("remainingFraction", 0.0))
        desc = bucket.get("description") or None
        if reset_dt is not None and reset_dt > current_time:
            return reset_dt, rem_frac, desc
        return None

    @staticmethod
    def _calibrate_limit_tokens(w: WindowStats, has_custom_token_limit: bool) -> None:
        """Keep limit_tokens consistent with pct_used when cost/server fraction drives quota."""
        if not has_custom_token_limit and w.usage.total_tokens > 0:
            calibrated = calibrate_limit_from_remaining(
                w.usage.total_tokens, w.pct_remaining
            )
            if calibrated is not None and calibrated > 0:
                w.limit_tokens = calibrated

    @staticmethod
    def _compute_pool_window(
        items: List[Tuple[datetime, TokenUsage, float, str]],
        current_time: datetime,
        duration: timedelta,
        limit_tokens: int,
        pool: str,
        window_label: str,
        cost_limit_usd: Optional[float],
        bucket: Optional[Dict[str, Any]],
        has_custom_token_limit: bool,
        calibrate: bool = True,
    ) -> WindowStats:
        """Compute one window once, driven by its server bucket when valid, then calibrate.

        ``calibrate=False`` skips the limit calibration (no server data to calibrate from).
        """
        override = LimitsCalculator._bucket_override(
            bucket, current_time, has_custom_token_limit
        )
        reset_dt, rem_frac, desc = override if override is not None else (None, None, None)
        w = LimitsCalculator._compute_cycle_window(
            items,
            current_time=current_time,
            duration=duration,
            limit_tokens=limit_tokens,
            pool=pool,
            window_label=window_label,
            cost_limit_usd=cost_limit_usd,
            override_reset_time=reset_dt,
            server_remaining_fraction=rem_frac,
            server_description=desc,
        )
        if calibrate:
            LimitsCalculator._calibrate_limit_tokens(w, has_custom_token_limit)
        return w

    @staticmethod
    def _apply_bucket_calibration(
        w: WindowStats,
        bucket: Optional[Dict[str, Any]],
        current_time: datetime,
        duration: timedelta,
        items: List[Tuple[datetime, TokenUsage, float, str]],
        has_custom_token_limit: bool,
    ) -> WindowStats:
        """Apply live server bucket fraction/resetTime or calibrate effective token limit."""
        override = LimitsCalculator._bucket_override(
            bucket, current_time, has_custom_token_limit
        )
        if override is not None:
            reset_dt, rem_frac, desc = override
            w = LimitsCalculator._compute_cycle_window(
                items,
                current_time=current_time,
                duration=duration,
                limit_tokens=w.limit_tokens,
                pool=w.pool,
                window_label=w.window_name,
                cost_limit_usd=w.cost_limit_usd,
                override_reset_time=reset_dt,
                server_remaining_fraction=rem_frac,
                server_description=desc,
            )
        LimitsCalculator._calibrate_limit_tokens(w, has_custom_token_limit)
        return w

    @staticmethod
    def compute_all_pools(
        sessions: List[SessionSummary],
        tier_name: Optional[str] = "pro",
        five_h_limit: Optional[int] = None,
        weekly_limit: Optional[int] = None,
        argon_weekly_limit: Optional[int] = None,
        argon_remaining_pct: Optional[float] = None,
        claude_five_h_limit: Optional[int] = None,
        claude_weekly_limit: Optional[int] = None,
        now: Optional[datetime] = None,
        use_live_server: Optional[bool] = None,
    ) -> MultiPoolLimits:
        """Compute quota windows for Gemini (Flash/Pro), Argon (weekly-only), and Claude/GPT pools.

        ``tier_name=None`` asks for auto-detection from the live server's tier id;
        pro is used when the server is unreachable or its id is not recognised.
        """
        current_time = now or datetime.now()
        tier_key = (tier_name or "pro").lower().strip()
        tier = TIERS.get(tier_key, TIERS["pro"])
        tier_source = "explicit" if tier_name is not None else "default"

        # Automatically query the live local Antigravity language_server when running
        # against real on-disk sessions (disabled automatically for synthetic unit tests).
        if use_live_server is None:
            use_live_server = should_use_live_server(sessions)

        live_errors: List[str] = []
        live_data = fetch_live_quota_summary(errors=live_errors) if use_live_server else None
        live_buckets = (live_data or {}).get("buckets", {}) if live_data else {}
        from_cache = bool(live_data) and live_data.get("source") == "cached"
        if tier_name is None and live_data and not from_cache:
            detected = tier_key_from_server_id(live_data.get("user_tier_id"))
            if detected:
                tier = TIERS[detected]
                tier_source = "auto"
        cache_fetched_at: Optional[datetime] = None
        cache_age_seconds: Optional[float] = None
        if from_cache:
            # Only buckets whose cycle is still running say anything about now.
            running: Dict[str, Dict[str, Any]] = {}
            for bid, b in live_buckets.items():
                if not isinstance(b, dict):
                    continue
                reset_dt = _parse_utc_iso_to_local_naive(b.get("resetTime"))
                if reset_dt is not None and reset_dt > current_time:
                    running[bid] = b
            live_buckets = running
            if live_buckets:
                cache_fetched_at = _parse_fetched_at(live_data.get("fetched_at"))
                cache_age_seconds = live_data.get("age_seconds")
            else:
                live_errors.append("every cached quota cycle has since reset")
        has_server_buckets = bool(live_buckets)
        live_synced = has_server_buckets and not from_cache
        if live_synced:
            quota_source = "live"
        elif has_server_buckets:
            quota_source = "cached"
        else:
            quota_source = "local"
        live_error: Optional[str] = None
        if use_live_server and not live_synced:
            live_error = "; ".join(live_errors) or "server returned no quota buckets"

        # OPT-005: classify every step once, then split into the three pools.
        pool_items = LimitsCalculator._partition_pool_items(sessions)

        # 1. Gemini Flash / Pro Pool
        gemini_items = pool_items["gemini"]
        g5_custom = five_h_limit is not None and five_h_limit > 0
        g7_custom = weekly_limit is not None and weekly_limit > 0
        gemini_5h = LimitsCalculator._compute_pool_window(
            gemini_items,
            current_time=current_time,
            duration=timedelta(hours=5),
            limit_tokens=five_h_limit if g5_custom else tier.five_hour_limit,
            pool="gemini",
            window_label="Gemini 5-Hour Limit",
            cost_limit_usd=None if g5_custom or not use_live_server else tier.five_hour_cost_usd,
            bucket=live_buckets.get("gemini-5h") if has_server_buckets else None,
            has_custom_token_limit=g5_custom,
            calibrate=has_server_buckets,
        )
        gemini_7d = LimitsCalculator._compute_pool_window(
            gemini_items,
            current_time=current_time,
            duration=timedelta(days=7),
            limit_tokens=weekly_limit if g7_custom else tier.weekly_limit,
            pool="gemini",
            window_label="Gemini Weekly Limit",
            cost_limit_usd=None if g7_custom or not use_live_server else tier.weekly_cost_usd,
            bucket=live_buckets.get("gemini-weekly") if has_server_buckets else None,
            has_custom_token_limit=g7_custom,
            calibrate=has_server_buckets,
        )

        # 2. Gemini 4 Argon Pool (weekly-only quota pool, no 5-hour window)
        argon_items = pool_items["argon"]
        ar_limit_set = argon_weekly_limit is not None and argon_weekly_limit > 0
        ar_custom = ar_limit_set or (argon_remaining_pct is not None)
        ar_has_bucket = (
            not ar_limit_set
            and argon_remaining_pct is None
            and has_server_buckets
            and "gemini-argon-wtu-weekly" in live_buckets
        )
        argon_7d = LimitsCalculator._compute_pool_window(
            argon_items,
            current_time=current_time,
            duration=timedelta(days=7),
            limit_tokens=argon_weekly_limit if ar_limit_set else tier.argon_weekly_limit,
            pool="argon",
            window_label="Gemini 4 Argon Weekly Limit",
            cost_limit_usd=None if ar_custom else tier.argon_weekly_cost_usd,
            bucket=live_buckets.get("gemini-argon-wtu-weekly") if ar_has_bucket else None,
            has_custom_token_limit=ar_custom,
            calibrate=not ar_custom,
        )
        if ar_limit_set:
            argon_7d.limit_tokens = argon_weekly_limit
        elif argon_remaining_pct is not None:
            argon_7d.server_remaining_fraction = max(
                0.0, min(1.0, float(argon_remaining_pct) / 100.0)
            )
            calibrated = calibrate_limit_from_remaining(
                argon_7d.usage.total_tokens, argon_remaining_pct
            )
            if calibrated is not None and calibrated > 0:
                argon_7d.limit_tokens = calibrated

        # 3. Claude and GPT Partner Pool (5-hour + weekly quota pool)
        claude_items = pool_items["claude"]
        c5_custom = claude_five_h_limit is not None and claude_five_h_limit > 0
        c7_custom = claude_weekly_limit is not None and claude_weekly_limit > 0
        claude_5h = LimitsCalculator._compute_pool_window(
            claude_items,
            current_time=current_time,
            duration=timedelta(hours=5),
            limit_tokens=claude_five_h_limit if c5_custom else tier.claude_five_hour_limit,
            pool="claude",
            window_label="Claude & GPT 5-Hour Limit",
            cost_limit_usd=None if c5_custom or not use_live_server else tier.claude_five_hour_cost_usd,
            bucket=live_buckets.get("3p-5h") if has_server_buckets else None,
            has_custom_token_limit=c5_custom,
            calibrate=has_server_buckets,
        )
        claude_7d = LimitsCalculator._compute_pool_window(
            claude_items,
            current_time=current_time,
            duration=timedelta(days=7),
            limit_tokens=claude_weekly_limit if c7_custom else tier.claude_weekly_limit,
            pool="claude",
            window_label="Claude & GPT Weekly Limit",
            cost_limit_usd=None if c7_custom or not use_live_server else tier.claude_weekly_cost_usd,
            bucket=live_buckets.get("3p-weekly") if has_server_buckets else None,
            has_custom_token_limit=c7_custom,
            calibrate=has_server_buckets,
        )

        if live_error is not None:
            for w in (gemini_5h, gemini_7d, argon_7d, claude_5h, claude_7d):
                w.server_unreachable = True

        return MultiPoolLimits(
            tier=tier,
            gemini_5h=gemini_5h,
            gemini_7d=gemini_7d,
            argon_7d=argon_7d,
            claude_5h=claude_5h,
            claude_7d=claude_7d,
            live_synced=live_synced,
            live_error=live_error,
            quota_source=quota_source,
            cache_fetched_at=cache_fetched_at,
            cache_age_seconds=cache_age_seconds,
            tier_source=tier_source,
        )


def render_gauge_bar(pct: float, width: int = 24) -> str:
    """Render a colored gauge progress bar according to quota usage percentage."""
    clamped_ratio = min(1.0, max(0.0, pct / 100.0))
    filled_chars = int(round(clamped_ratio * width))
    empty_chars = max(0, width - filled_chars)

    bar_str = "█" * filled_chars + "░" * empty_chars

    # Note: visual gauge bar color scale (>=85% red) is intentionally distinct from the
    # status badge scale (CRIT=100% red) to provide advance visual tension in the progress bar.
    if pct >= 85.0:
        colored_bar = Colors.red(bar_str)
    elif pct >= WARN_MODERATE:
        colored_bar = Colors.yellow(bar_str)
    else:
        colored_bar = Colors.green(bar_str)

    return f"[{colored_bar}]"


def render_remaining_bar(pct_remaining: float, width: int = 24) -> str:
    """Render a colored progress bar where filled blocks represent remaining quota."""
    clamped_ratio = min(1.0, max(0.0, pct_remaining / 100.0))
    filled_chars = int(round(clamped_ratio * width))
    empty_chars = max(0, width - filled_chars)

    bar_str = "█" * filled_chars + "░" * empty_chars

    if pct_remaining <= 15.0:
        colored_bar = Colors.red(bar_str)
    elif pct_remaining <= (100.0 - WARN_MODERATE):
        colored_bar = Colors.yellow(bar_str)
    else:
        colored_bar = Colors.green(bar_str)

    return f"[{colored_bar}]"



def get_status_badge(
    w_5h: WindowStats,
    w_7d: WindowStats,
    argon_7d: Optional[WindowStats] = None,
    claude_5h: Optional[WindowStats] = None,
    claude_7d: Optional[WindowStats] = None,
) -> Tuple[str, str]:
    """Return status emoji badge and human-readable diagnostic message."""
    if w_7d.is_exceeded or w_7d.pct_used >= CRIT:
        return "🔴", Colors.bold(
            Colors.red("GEMINI WEEKLY LIMIT REACHED (Risk of lockout until weekly cycle reset)")
        )
    if argon_7d is not None and (argon_7d.usage.total_tokens > 0 or argon_7d.server_remaining_fraction is not None) and (argon_7d.is_exceeded or argon_7d.pct_used >= CRIT):
        return "🔴", Colors.bold(
            Colors.red("GEMINI 4 ARGON WEEKLY LIMIT REACHED (Switch to Gemini Flash until Argon cycle resets)")
        )
    if claude_7d is not None and (claude_7d.usage.total_tokens > 0 or claude_7d.server_remaining_fraction is not None) and (claude_7d.is_exceeded or claude_7d.pct_used >= CRIT):
        return "🔴", Colors.bold(
            Colors.red("CLAUDE & GPT WEEKLY LIMIT REACHED (Use Gemini models or AI credits until reset)")
        )
    if w_5h.is_exceeded or w_5h.pct_used >= CRIT:
        return "🔴", Colors.bold(
            Colors.red("GEMINI 5-HOUR LIMIT REACHED (Cooling down until 5-hour window refreshes)")
        )
    if claude_5h is not None and (claude_5h.usage.total_tokens > 0 or claude_5h.server_remaining_fraction is not None) and (claude_5h.is_exceeded or claude_5h.pct_used >= CRIT):
        return "🔴", Colors.bold(
            Colors.red("CLAUDE & GPT 5-HOUR LIMIT REACHED (Weekly limit paused until 5-hour window refreshes)")
        )

    all_windows = [w for w in (w_5h, w_7d, argon_7d, claude_5h, claude_7d) if w is not None]
    if any(w.pct_used >= WARN_HIGH for w in all_windows):
        return "🟡", Colors.bold(
            Colors.yellow("HIGH LOAD WARNING (Approaching quota limits on one or more model pools)")
        )
    if any(w.pct_used >= WARN_MODERATE for w in all_windows):
        return "🟡", Colors.yellow("MODERATE LOAD (Active window consumption, steady pace)")
    if any(w.is_unknown and w.limit_tokens > 0 for w in all_windows):
        return "⚪", Colors.yellow(
            "UNKNOWN (Antigravity server not reachable, and this device has no usage in some quota windows)"
        )
    return "🟢", Colors.green("HEALTHY (All sprint and weekly quotas are well within safe thresholds)")
