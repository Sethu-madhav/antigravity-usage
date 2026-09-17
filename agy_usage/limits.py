"""Quota and rate limits tracking for Google Antigravity (5-hour and weekly limits)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

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
    SessionSummary,
    StepRecord,
    TokenUsage,
    calculate_cost,
)


@dataclass
class TierConfig:
    """Antigravity subscription plan tier quotas."""

    name: str
    display_name: str
    five_hour_limit: int
    weekly_limit: int
    description: str


TIERS: Dict[str, TierConfig] = {
    "free": TierConfig(
        name="free",
        display_name="Google AI Free",
        five_hour_limit=20_000_000,
        weekly_limit=150_000_000,
        description="Free Tier (Flash models only)",
    ),
    "pro": TierConfig(
        name="pro",
        display_name="Google AI Pro",
        five_hour_limit=100_000_000,
        weekly_limit=750_000_000,
        description="Pro Tier (Default Antigravity subscription)",
    ),
    "ultra": TierConfig(
        name="ultra",
        display_name="Google AI Ultra",
        five_hour_limit=500_000_000,
        weekly_limit=3_750_000_000,
        description="Ultra Tier (Highest quota, resilient against caps)",
    ),
}


@dataclass
class WindowStats:
    """Usage statistics within a specific rolling or cycle time window."""

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

    @property
    def pct_used(self) -> float:
        """Percentage of limit consumed."""
        if self.limit_tokens <= 0:
            return 0.0
        return (self.usage.total_tokens / self.limit_tokens) * 100.0

    @property
    def pct_remaining(self) -> float:
        """Percentage of limit remaining."""
        if self.limit_tokens <= 0:
            return 0.0
        return max(0.0, 100.0 - self.pct_used)

    @property
    def tokens_remaining(self) -> int:
        """Tokens remaining before hitting limit."""
        return max(0, self.limit_tokens - self.usage.total_tokens)

    @property
    def is_exceeded(self) -> bool:
        """Whether limit has been breached."""
        return self.usage.total_tokens >= self.limit_tokens


def parse_token_amount(text: str) -> Optional[int]:
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


class LimitsCalculator:
    """Calculates rolling 5-hour sprint usage and active 7-day weekly quota cycle usage."""

    @staticmethod
    def compute(
        sessions: List[SessionSummary],
        tier_name: str = "pro",
        five_h_limit: Optional[int] = None,
        weekly_limit: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[WindowStats, WindowStats, TierConfig]:
        """Compute rolling 5-hour and active 7-day cycle statistics."""
        current_time = now or datetime.now()
        tier_key = tier_name.lower().strip()
        tier = TIERS.get(tier_key, TIERS["pro"])

        limit_5h = five_h_limit if five_h_limit is not None and five_h_limit > 0 else tier.five_hour_limit
        limit_7d = weekly_limit if weekly_limit is not None and weekly_limit > 0 else tier.weekly_limit

        # Collect all activity records chronologically
        items: List[Tuple[datetime, TokenUsage, str]] = []
        for s in sessions:
            if s.steps:
                for step in s.steps:
                    st_ts = step.timestamp
                    if st_ts.tzinfo:
                        st_ts = st_ts.replace(tzinfo=None)
                    items.append((st_ts, step.usage, s.session_id))
            else:
                s_time = s.start_time or s.end_time
                if s_time:
                    s_ts = s_time.replace(tzinfo=None) if s_time.tzinfo else s_time
                    items.append((s_ts, s.usage, s.session_id))

        items.sort(key=lambda x: x[0])

        # 1. Rolling 5-Hour Window: [current_time - 5h, current_time]
        start_5h = current_time - timedelta(hours=5)
        usage_5h = TokenUsage()
        steps_5h_count = 0
        sessions_5h_ids = set()
        earliest_5h: Optional[datetime] = None
        latest_5h: Optional[datetime] = None

        for ts, u, sid in items:
            if start_5h <= ts <= current_time:
                usage_5h = usage_5h.add(u)
                steps_5h_count += 1
                sessions_5h_ids.add(sid)
                if earliest_5h is None or ts < earliest_5h:
                    earliest_5h = ts
                if latest_5h is None or ts > latest_5h:
                    latest_5h = ts

        refresh_5h: Optional[datetime] = None
        if latest_5h and usage_5h.total_tokens > 0:
            refresh_5h = latest_5h + timedelta(hours=5)

        # 2. Active 7-Day Weekly Quota Cycle
        # In Google Antigravity, the weekly limit operates on 7-day cycles. When a cycle expires,
        # the allowance resets completely (100%), and the next turn establishes the new cycle anchor.
        usage_7d = TokenUsage()
        steps_7d_count = 0
        sessions_7d_ids = set()
        earliest_7d: Optional[datetime] = None
        latest_7d: Optional[datetime] = None
        cycle_start = current_time
        cycle_end = current_time + timedelta(days=7)

        if items:
            c_start = items[0][0]
            c_end = c_start + timedelta(days=7)
            for ts, u, sid in items:
                if ts >= c_end:
                    c_start = ts
                    c_end = c_start + timedelta(days=7)

            if current_time >= c_end:
                # Cycle has already expired prior to now; full 100% capacity is ready
                cycle_start = current_time
                cycle_end = current_time + timedelta(days=7)
            else:
                cycle_start = c_start
                cycle_end = c_end
                for ts, u, sid in items:
                    if cycle_start <= ts <= current_time:
                        usage_7d = usage_7d.add(u)
                        steps_7d_count += 1
                        sessions_7d_ids.add(sid)
                        if earliest_7d is None or ts < earliest_7d:
                            earliest_7d = ts
                        if latest_7d is None or ts > latest_7d:
                            latest_7d = ts

        refresh_7d: Optional[datetime] = None
        if usage_7d.total_tokens > 0 and cycle_end > current_time:
            refresh_7d = cycle_end

        # Cost calculations
        _, _, _, cost_5h = calculate_cost(
            usage_5h.prompt_tokens, usage_5h.cached_tokens, usage_5h.output_tokens
        )
        _, _, _, cost_7d = calculate_cost(
            usage_7d.prompt_tokens, usage_7d.cached_tokens, usage_7d.output_tokens
        )

        w_5h = WindowStats(
            window_name="5-Hour Sprint Window",
            start_time=start_5h,
            end_time=current_time,
            usage=usage_5h,
            step_count=steps_5h_count,
            session_count=len(sessions_5h_ids),
            limit_tokens=limit_5h,
            estimated_cost=cost_5h,
            earliest_activity=earliest_5h,
            latest_activity=latest_5h,
            refresh_time=refresh_5h,
        )

        w_7d = WindowStats(
            window_name="Weekly Quota Cycle",
            start_time=cycle_start,
            end_time=cycle_end,
            usage=usage_7d,
            step_count=steps_7d_count,
            session_count=len(sessions_7d_ids),
            limit_tokens=limit_7d,
            estimated_cost=cost_7d,
            earliest_activity=earliest_7d,
            latest_activity=latest_7d,
            refresh_time=refresh_7d,
        )

        return w_5h, w_7d, tier


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


def get_status_badge(w_5h: WindowStats, w_7d: WindowStats) -> Tuple[str, str]:
    """Return status emoji badge and human-readable diagnostic message."""
    if w_7d.pct_used >= 100.0:
        return "🔴", Colors.bold(Colors.red("WEEKLY HARD CAP REACHED (Risk of multi-day lockout until cycle reset)"))
    if w_5h.pct_used >= 100.0:
        return "🔴", Colors.bold(Colors.red("5-HOUR LIMIT REACHED (Cooling down until oldest activity rolls off)"))
    if w_5h.pct_used >= 80.0 or w_7d.pct_used >= 80.0:
        return "🟡", Colors.bold(Colors.yellow("HIGH LOAD WARNING (Approaching limits, consider switching to Flash models)"))
    if w_5h.pct_used >= 60.0 or w_7d.pct_used >= 60.0:
        return "🟡", Colors.yellow("MODERATE LOAD (5-hour window is active, steady consumption)")
    return "🟢", Colors.green("HEALTHY (All sprint and weekly quotas are well within safe thresholds)")
