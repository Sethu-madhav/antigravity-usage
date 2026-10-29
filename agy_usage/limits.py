"""Quota and rate limits tracking for Google Antigravity (5-hour and weekly limits)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from agy_usage.display import (
    Colors,
    format_cost,
    format_duration,
    format_percent,
    format_tokens,
    render_kpi_grid,
    render_table,
)
from agy_usage.models import (
    CostBreakdown,
    SessionSummary,
    StepRecord,
    TokenUsage,
)


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
        return (self.usage.total_tokens / self.limit_tokens) * 100.0

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


def parse_token_amount(text: Optional[str]) -> Optional[int]:
    """Parse string representations like '50M', '1.5B', '200K' into integer tokens."""
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
        return int(float(s) * multiplier)
    except ValueError:
        return None


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


_QUOTA_CACHE_PATH = os.path.expanduser(
    "~/.gemini/antigravity/agy_usage_quota_cache.json"
)


def fetch_live_quota_summary(use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """Query the local Antigravity language_server for authoritative quota buckets.

    Calls ``RetrieveUserQuotaSummary`` and ``GetUserStatus`` on the localhost port
    of the running Antigravity ``language_server`` process (if active), falling back
    to the last cached response on disk when valid.
    """
    try:
        ps_out = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).decode("utf-8", errors="ignore")
    except Exception:
        ps_out = ""

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

        try:
            lsof_out = subprocess.check_output(
                ["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).decode("utf-8", errors="ignore")
        except Exception:
            continue

        ports: List[int] = []
        for lline in lsof_out.splitlines():
            m_port = re.search(r"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", lline)
            if m_port:
                ports.append(int(m_port.group(1)))

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
            except Exception:
                continue

    if use_cache and os.path.isfile(_QUOTA_CACHE_PATH):
        try:
            with open(_QUOTA_CACHE_PATH, "r", encoding="utf-8") as fp:
                cached = json.load(fp)
            if isinstance(cached, dict) and "buckets" in cached:
                return cached
        except Exception:
            pass
    return None


class LimitsCalculator:
    """Calculates 5-hour and 7-day weekly quota cycle usage across Antigravity model pools."""

    @staticmethod
    def _collect_pool_items(
        sessions: List[SessionSummary],
        pool: Optional[str] = "gemini",
    ) -> List[Tuple[datetime, TokenUsage, float, str]]:
        """Collect chronological (timestamp, usage, step_cost_usd, session_id) for a pool."""
        items: List[Tuple[datetime, TokenUsage, float, str]] = []
        filter_pool = None if pool in (None, "all") else pool
        for s in sessions:
            if s.steps:
                for step in s.steps:
                    if filter_pool and classify_model_pool(step.model) != filter_pool:
                        continue
                    st_ts = step.timestamp
                    if st_ts.tzinfo:
                        st_ts = st_ts.replace(tzinfo=None)
                    items.append((st_ts, step.usage, step.cost.total, s.session_id))
            else:
                if filter_pool and classify_model_pool(s.primary_model) != filter_pool:
                    continue
                s_time = s.start_time or s.end_time
                if s_time:
                    s_ts = s_time.replace(tzinfo=None) if s_time.tzinfo else s_time
                    items.append((s_ts, s.usage, s.cost.total, s.session_id))

        items.sort(key=lambda x: x[0])
        return items

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
        usage = TokenUsage()
        cost = 0.0
        steps_count = 0
        sessions_ids = set()
        earliest: Optional[datetime] = None
        latest: Optional[datetime] = None
        cycle_start = current_time - duration
        cycle_end = current_time

        if override_reset_time is not None and override_reset_time > current_time:
            cycle_end = override_reset_time
            cycle_start = override_reset_time - duration
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
    def _apply_bucket_calibration(
        w: WindowStats,
        bucket: Optional[Dict[str, Any]],
        current_time: datetime,
        duration: timedelta,
        items: List[Tuple[datetime, TokenUsage, float, str]],
        has_custom_token_limit: bool,
    ) -> WindowStats:
        """Apply live server bucket fraction/resetTime or calibrate effective token limit."""
        if bucket is not None and not has_custom_token_limit:
            reset_dt = _parse_utc_iso_to_local_naive(bucket.get("resetTime"))
            rem_frac = float(bucket.get("remainingFraction", 0.0))
            desc = bucket.get("description") or None
            if reset_dt is not None and reset_dt > current_time:
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
            else:
                w.server_remaining_fraction = rem_frac
                w.server_description = desc

        # Keep limit_tokens consistent with pct_used when cost/server fraction drives quota
        if not has_custom_token_limit and w.usage.total_tokens > 0:
            calibrated = calibrate_limit_from_remaining(
                w.usage.total_tokens, w.pct_remaining
            )
            if calibrated is not None and calibrated > 0:
                w.limit_tokens = calibrated
        return w

    @staticmethod
    def compute_all_pools(
        sessions: List[SessionSummary],
        tier_name: str = "pro",
        five_h_limit: Optional[int] = None,
        weekly_limit: Optional[int] = None,
        argon_weekly_limit: Optional[int] = None,
        argon_remaining_pct: Optional[float] = None,
        claude_five_h_limit: Optional[int] = None,
        claude_weekly_limit: Optional[int] = None,
        now: Optional[datetime] = None,
        use_live_server: Optional[bool] = None,
    ) -> MultiPoolLimits:
        """Compute quota windows for Gemini (Flash/Pro), Argon (weekly-only), and Claude/GPT pools."""
        current_time = now or datetime.now()
        tier_key = tier_name.lower().strip()
        tier = TIERS.get(tier_key, TIERS["pro"])

        # Automatically query the live local Antigravity language_server when running
        # against real on-disk sessions (disabled automatically for synthetic unit tests).
        if use_live_server is None:
            use_live_server = any(
                bool(s.file_path and os.path.isfile(s.file_path)) for s in sessions
            )

        live_data = fetch_live_quota_summary() if use_live_server else None
        live_buckets = (live_data or {}).get("buckets", {}) if live_data else {}
        live_synced = bool(live_buckets)

        # 1. Gemini Flash / Pro Pool
        gemini_items = LimitsCalculator._collect_pool_items(sessions, pool="gemini")
        g5_custom = five_h_limit is not None and five_h_limit > 0
        g7_custom = weekly_limit is not None and weekly_limit > 0
        gemini_5h, gemini_7d = LimitsCalculator._compute_windows_from_items(
            gemini_items,
            current_time=current_time,
            limit_5h=five_h_limit if g5_custom else tier.five_hour_limit,
            limit_7d=weekly_limit if g7_custom else tier.weekly_limit,
            pool="gemini",
            window_5h_label="Gemini 5-Hour Limit",
            window_7d_label="Gemini Weekly Limit",
            cost_limit_5h_usd=None if g5_custom or not use_live_server else tier.five_hour_cost_usd,
            cost_limit_7d_usd=None if g7_custom or not use_live_server else tier.weekly_cost_usd,
        )
        if live_synced:
            gemini_5h = LimitsCalculator._apply_bucket_calibration(
                gemini_5h,
                live_buckets.get("gemini-5h"),
                current_time,
                timedelta(hours=5),
                gemini_items,
                g5_custom,
            )
            gemini_7d = LimitsCalculator._apply_bucket_calibration(
                gemini_7d,
                live_buckets.get("gemini-weekly"),
                current_time,
                timedelta(days=7),
                gemini_items,
                g7_custom,
            )

        # 2. Gemini 4 Argon Pool (weekly-only quota pool, no 5-hour window)
        argon_items = LimitsCalculator._collect_pool_items(sessions, pool="argon")
        ar_custom = (argon_weekly_limit is not None and argon_weekly_limit > 0) or (
            argon_remaining_pct is not None
        )
        _, argon_7d = LimitsCalculator._compute_windows_from_items(
            argon_items,
            current_time=current_time,
            limit_5h=0,
            limit_7d=argon_weekly_limit
            if (argon_weekly_limit is not None and argon_weekly_limit > 0)
            else tier.argon_weekly_limit,
            pool="argon",
            window_5h_label="Argon (No 5-Hour Limit)",
            window_7d_label="Gemini 4 Argon Weekly Limit",
            cost_limit_5h_usd=None,
            cost_limit_7d_usd=None if ar_custom else tier.argon_weekly_cost_usd,
        )
        if argon_weekly_limit is not None and argon_weekly_limit > 0:
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
        elif live_synced and "gemini-argon-wtu-weekly" in live_buckets:
            argon_7d = LimitsCalculator._apply_bucket_calibration(
                argon_7d,
                live_buckets.get("gemini-argon-wtu-weekly"),
                current_time,
                timedelta(days=7),
                argon_items,
                False,
            )
        else:
            argon_7d = LimitsCalculator._apply_bucket_calibration(
                argon_7d,
                None,
                current_time,
                timedelta(days=7),
                argon_items,
                ar_custom,
            )

        # 3. Claude and GPT Partner Pool (5-hour + weekly quota pool)
        claude_items = LimitsCalculator._collect_pool_items(sessions, pool="claude")
        c5_custom = claude_five_h_limit is not None and claude_five_h_limit > 0
        c7_custom = claude_weekly_limit is not None and claude_weekly_limit > 0
        claude_5h, claude_7d = LimitsCalculator._compute_windows_from_items(
            claude_items,
            current_time=current_time,
            limit_5h=claude_five_h_limit if c5_custom else tier.claude_five_hour_limit,
            limit_7d=claude_weekly_limit if c7_custom else tier.claude_weekly_limit,
            pool="claude",
            window_5h_label="Claude & GPT 5-Hour Limit",
            window_7d_label="Claude & GPT Weekly Limit",
            cost_limit_5h_usd=None if c5_custom or not use_live_server else tier.claude_five_hour_cost_usd,
            cost_limit_7d_usd=None if c7_custom or not use_live_server else tier.claude_weekly_cost_usd,
        )
        if live_synced:
            claude_5h = LimitsCalculator._apply_bucket_calibration(
                claude_5h,
                live_buckets.get("3p-5h"),
                current_time,
                timedelta(hours=5),
                claude_items,
                c5_custom,
            )
            claude_7d = LimitsCalculator._apply_bucket_calibration(
                claude_7d,
                live_buckets.get("3p-weekly"),
                current_time,
                timedelta(days=7),
                claude_items,
                c7_custom,
            )

        return MultiPoolLimits(
            tier=tier,
            gemini_5h=gemini_5h,
            gemini_7d=gemini_7d,
            argon_7d=argon_7d,
            claude_5h=claude_5h,
            claude_7d=claude_7d,
            live_synced=live_synced,
        )


def render_gauge_bar(pct: float, width: int = 24) -> str:
    """Render a colored gauge progress bar according to quota usage percentage."""
    clamped_ratio = min(1.0, max(0.0, pct / 100.0))
    filled_chars = int(round(clamped_ratio * width))
    empty_chars = max(0, width - filled_chars)

    bar_str = "█" * filled_chars + "░" * empty_chars

    if pct >= 85.0:
        colored_bar = Colors.red(bar_str)
    elif pct >= 60.0:
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
    elif pct_remaining <= 40.0:
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
    if w_7d.is_exceeded:
        return "🔴", Colors.bold(
            Colors.red("GEMINI WEEKLY LIMIT REACHED (Risk of lockout until weekly cycle reset)")
        )
    if argon_7d is not None and (argon_7d.usage.total_tokens > 0 or argon_7d.server_remaining_fraction is not None) and argon_7d.is_exceeded:
        return "🔴", Colors.bold(
            Colors.red("GEMINI 4 ARGON WEEKLY LIMIT REACHED (Switch to Gemini Flash until Argon cycle resets)")
        )
    if claude_7d is not None and (claude_7d.usage.total_tokens > 0 or claude_7d.server_remaining_fraction is not None) and claude_7d.is_exceeded:
        return "🔴", Colors.bold(
            Colors.red("CLAUDE & GPT WEEKLY LIMIT REACHED (Use Gemini models or AI credits until reset)")
        )
    if w_5h.is_exceeded:
        return "🔴", Colors.bold(
            Colors.red("GEMINI 5-HOUR LIMIT REACHED (Cooling down until 5-hour window refreshes)")
        )
    if claude_5h is not None and (claude_5h.usage.total_tokens > 0 or claude_5h.server_remaining_fraction is not None) and claude_5h.is_exceeded:
        return "🔴", Colors.bold(
            Colors.red("CLAUDE & GPT 5-HOUR LIMIT REACHED (Weekly limit paused until 5-hour window refreshes)")
        )

    all_windows = [w for w in (w_5h, w_7d, argon_7d, claude_5h, claude_7d) if w is not None]
    if any(w.pct_used >= 80.0 for w in all_windows):
        return "🟡", Colors.bold(
            Colors.yellow("HIGH LOAD WARNING (Approaching quota limits on one or more model pools)")
        )
    if any(w.pct_used >= 60.0 for w in all_windows):
        return "🟡", Colors.yellow("MODERATE LOAD (Active window consumption, steady pace)")
    return "🟢", Colors.green("HEALTHY (All sprint and weekly quotas are well within safe thresholds)")
