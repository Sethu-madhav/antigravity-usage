"""Aggregation engine for daily, weekly, monthly, and model token usage."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple, TypeVar

from agy_usage.models import (
    CostBreakdown,
    DailyUsage,
    ModelUsage,
    MonthlyUsage,
    SessionSummary,
    TokenUsage,
    WeeklyUsage,
    normalize_model_name,
)

_K = TypeVar("_K")
_B = TypeVar("_B", WeeklyUsage, MonthlyUsage)


class _UsageUnit(NamedTuple):
    """One unit of usage: a step, or a whole session that has no steps."""

    timestamp: Optional[datetime]  # step.timestamp, or s.start_time (may be None) for a step-less session
    usage: TokenUsage
    cost: CostBreakdown
    session_id: str
    model: str                     # step.model, or s.primary_model
    models: Sequence[str]          # (step.model,), or s.models
    count: int                     # 1 per step, or s.step_count


def _iter_usage_units(sessions: List[SessionSummary]) -> Iterator[_UsageUnit]:
    """Yield one unit per step, or one for the whole session when it has no steps."""
    for s in sessions:
        if s.steps:
            for step in s.steps:
                yield _UsageUnit(
                    step.timestamp, step.usage, step.cost, s.session_id, step.model, (step.model,), 1
                )
        else:
            yield _UsageUnit(
                s.start_time, s.usage, s.cost, s.session_id, s.primary_model, s.models, s.step_count
            )


def _add_usage(into: TokenUsage, other: TokenUsage) -> None:
    """Accumulate other into into, in place. Only ever pass a bucket-owned object as into."""
    into.prompt_tokens += other.prompt_tokens
    into.cached_tokens += other.cached_tokens
    into.output_tokens += other.output_tokens
    into.thought_tokens += other.thought_tokens
    into.candidate_tokens += other.candidate_tokens


def _add_cost(into: CostBreakdown, other: CostBreakdown) -> None:
    """Accumulate other into into, in place. Only ever pass a bucket-owned object as into."""
    into.prompt_cost += other.prompt_cost
    into.cached_cost += other.cached_cost
    into.output_cost += other.output_cost
    into.thought_cost += other.thought_cost
    into.cache_savings += other.cache_savings


def _rollup(
    daily_data: Dict[date, DailyUsage],
    key_fn: Callable[[date], _K],
    factory: Callable[[date, _K], _B],
) -> Dict[_K, _B]:
    """Fold daily buckets into coarser buckets keyed by key_fn(date)."""
    buckets: Dict[_K, _B] = {}
    sessions_by_key: Dict[_K, set] = {}

    for d, daily_item in daily_data.items():
        key = key_fn(d)
        if key not in buckets:
            buckets[key] = factory(d, key)
            sessions_by_key[key] = set()
        b = buckets[key]
        _add_usage(b.usage, daily_item.usage)
        _add_cost(b.cost, daily_item.cost)
        b.step_count += daily_item.steps_count
        sessions_by_key[key].update(daily_item.session_ids)

    for key, b in buckets.items():
        b.session_count = len(sessions_by_key[key])

    return buckets


def _weekly_bucket(d: date, key: Tuple[int, int]) -> WeeklyUsage:
    # Calculate Monday of this ISO week
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return WeeklyUsage(year=key[0], week_num=key[1], start_date=monday, end_date=sunday)


class UsageAggregator:
    """Aggregates sessions and steps across time dimensions and models."""

    @staticmethod
    def aggregate_daily(sessions: List[SessionSummary]) -> Dict[date, DailyUsage]:
        """Aggregate usage by calendar date based on step timestamps."""
        daily: Dict[date, DailyUsage] = {}

        for unit in _iter_usage_units(sessions):
            # A session without steps falls back to its start time; without one it is skipped
            if unit.timestamp is None:
                continue
            d = unit.timestamp.date()
            if d not in daily:
                daily[d] = DailyUsage(date=d)
            item = daily[d]
            _add_usage(item.usage, unit.usage)
            _add_cost(item.cost, unit.cost)
            item.session_ids.add(unit.session_id)
            for m in unit.models:
                item.models.add(normalize_model_name(m))
            item.steps_count += unit.count

        return daily

    @staticmethod
    def aggregate_hourly(sessions: List[SessionSummary]) -> Dict[Tuple[date, int], TokenUsage]:
        """Aggregate usage by (calendar date, local hour) of step timestamps.

        Mirrors aggregate_daily's bucketing (step timestamps, falling back to the
        session start time) so hourly totals sum to the daily totals.
        """
        hourly: Dict[Tuple[date, int], TokenUsage] = {}

        for unit in _iter_usage_units(sessions):
            if unit.timestamp is None:
                continue
            key = (unit.timestamp.date(), unit.timestamp.hour)
            if key not in hourly:
                hourly[key] = TokenUsage()
            _add_usage(hourly[key], unit.usage)

        return hourly

    @staticmethod
    def aggregate_weekly(
        sessions: List[SessionSummary],
        daily_data: Optional[Dict[date, DailyUsage]] = None,
    ) -> List[WeeklyUsage]:
        """Aggregate usage by ISO calendar week."""
        if daily_data is None:
            daily_data = UsageAggregator.aggregate_daily(sessions)

        # Key: (iso_year, iso_week)
        weekly_map = _rollup(daily_data, lambda d: d.isocalendar()[:2], _weekly_bucket)

        # Sort ascending by start_date
        return sorted(weekly_map.values(), key=lambda x: x.start_date)

    @staticmethod
    def aggregate_monthly(
        sessions: List[SessionSummary],
        daily_data: Optional[Dict[date, DailyUsage]] = None,
    ) -> List[MonthlyUsage]:
        """Aggregate usage by calendar month."""
        if daily_data is None:
            daily_data = UsageAggregator.aggregate_daily(sessions)

        monthly_map = _rollup(
            daily_data,
            lambda d: (d.year, d.month),
            lambda d, key: MonthlyUsage(year=d.year, month=d.month),
        )

        # Sort ascending by (year, month)
        return sorted(monthly_map.values(), key=lambda x: (x.year, x.month))

    @staticmethod
    def aggregate_models(sessions: List[SessionSummary]) -> List[ModelUsage]:
        """Aggregate token usage and distribution by model."""
        model_map: Dict[str, ModelUsage] = {}
        model_sessions: Dict[str, set] = {}
        total_tokens_all = 0

        for unit in _iter_usage_units(sessions):
            m_name = normalize_model_name(unit.model) or "unknown"
            if m_name not in model_map:
                model_map[m_name] = ModelUsage(model_name=m_name)
                model_sessions[m_name] = set()
            mu = model_map[m_name]
            _add_usage(mu.usage, unit.usage)
            _add_cost(mu.cost, unit.cost)
            mu.request_count += unit.count
            model_sessions[m_name].add(unit.session_id)
            total_tokens_all += unit.usage.total_tokens

        result = list(model_map.values())
        for mu in result:
            mu.session_count = len(model_sessions[mu.model_name])
            if total_tokens_all > 0:
                mu.percentage_of_total = (mu.usage.total_tokens / total_tokens_all) * 100.0

        # Sort descending by total tokens
        return sorted(result, key=lambda x: x.usage.total_tokens, reverse=True)

    @staticmethod
    def compute_summary(
        sessions: List[SessionSummary],
        daily_data: Optional[Dict[date, DailyUsage]] = None,
    ) -> Dict[str, Any]:
        """Compute high-level summary KPIs across all sessions."""
        total_usage = TokenUsage()
        total_cost = CostBreakdown()
        total_steps = 0
        earliest_ts: Optional[datetime] = None
        latest_ts: Optional[datetime] = None
        unique_workspaces = set()

        for s in sessions:
            _add_usage(total_usage, s.usage)
            _add_cost(total_cost, s.cost)
            total_steps += s.step_count
            if s.workspace and s.workspace != "Unknown":
                unique_workspaces.add(s.workspace)
            if s.start_time:
                if earliest_ts is None or s.start_time < earliest_ts:
                    earliest_ts = s.start_time
            if s.end_time:
                if latest_ts is None or s.end_time > latest_ts:
                    latest_ts = s.end_time

        if daily_data is None:
            daily_data = UsageAggregator.aggregate_daily(sessions)
        active_days = len(daily_data)

        models = UsageAggregator.aggregate_models(sessions)

        return {
            "session_count": len(sessions),
            "step_count": total_steps,
            "active_days": active_days,
            "unique_workspaces_count": len(unique_workspaces),
            "earliest_date": earliest_ts.date().isoformat() if earliest_ts else None,
            "latest_date": latest_ts.date().isoformat() if latest_ts else None,
            "total_usage": total_usage,
            "total_cost": total_cost,
            "cache_hit_rate": total_usage.cache_hit_rate,
            "models": models,
        }
