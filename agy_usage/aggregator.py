"""Aggregation engine for daily, weekly, monthly, and model token usage."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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


class UsageAggregator:
    """Aggregates sessions and steps across time dimensions and models."""

    @staticmethod
    def aggregate_daily(sessions: List[SessionSummary]) -> Dict[date, DailyUsage]:
        """Aggregate usage by calendar date based on step timestamps."""
        daily: Dict[date, DailyUsage] = {}

        for s in sessions:
            # If session has individual steps, aggregate per step timestamp
            if s.steps:
                for step in s.steps:
                    d = step.timestamp.date()
                    if d not in daily:
                        daily[d] = DailyUsage(date=d)
                    item = daily[d]
                    item.usage = item.usage.add(step.usage)
                    item.cost = item.cost.add(step.cost)
                    item.session_ids.add(s.session_id)
                    item.models.add(normalize_model_name(step.model))
                    item.steps_count += 1
            elif s.start_time:
                # Session has usage but steps weren't separately detailed
                d = s.start_time.date()
                if d not in daily:
                    daily[d] = DailyUsage(date=d)
                item = daily[d]
                item.usage = item.usage.add(s.usage)
                item.cost = item.cost.add(s.cost)
                item.session_ids.add(s.session_id)
                for m in s.models:
                    item.models.add(normalize_model_name(m))
                item.steps_count += s.step_count

        return daily

    @staticmethod
    def aggregate_weekly(
        sessions: List[SessionSummary],
        daily_data: Optional[Dict[date, DailyUsage]] = None,
    ) -> List[WeeklyUsage]:
        """Aggregate usage by ISO calendar week."""
        # Key: (iso_year, iso_week)
        weekly_map: Dict[Tuple[int, int], WeeklyUsage] = {}
        weekly_sessions: Dict[Tuple[int, int], set] = defaultdict(set)

        if daily_data is None:
            daily_data = UsageAggregator.aggregate_daily(sessions)

        for d, daily_item in daily_data.items():
            iso_year, iso_week, _ = d.isocalendar()
            key = (iso_year, iso_week)
            if key not in weekly_map:
                # Calculate Monday of this ISO week
                monday = d - timedelta(days=d.weekday())
                sunday = monday + timedelta(days=6)
                weekly_map[key] = WeeklyUsage(
                    year=iso_year,
                    week_num=iso_week,
                    start_date=monday,
                    end_date=sunday,
                )
            w = weekly_map[key]
            w.usage = w.usage.add(daily_item.usage)
            w.cost = w.cost.add(daily_item.cost)
            w.step_count += daily_item.steps_count
            weekly_sessions[key].update(daily_item.session_ids)

        for key, w in weekly_map.items():
            w.session_count = len(weekly_sessions[key])

        # Sort ascending by start_date
        return sorted(weekly_map.values(), key=lambda x: x.start_date)

    @staticmethod
    def aggregate_monthly(
        sessions: List[SessionSummary],
        daily_data: Optional[Dict[date, DailyUsage]] = None,
    ) -> List[MonthlyUsage]:
        """Aggregate usage by calendar month."""
        monthly_map: Dict[Tuple[int, int], MonthlyUsage] = {}
        monthly_sessions: Dict[Tuple[int, int], set] = defaultdict(set)

        if daily_data is None:
            daily_data = UsageAggregator.aggregate_daily(sessions)

        for d, daily_item in daily_data.items():
            key = (d.year, d.month)
            if key not in monthly_map:
                monthly_map[key] = MonthlyUsage(year=d.year, month=d.month)
            m = monthly_map[key]
            m.usage = m.usage.add(daily_item.usage)
            m.cost = m.cost.add(daily_item.cost)
            m.step_count += daily_item.steps_count
            monthly_sessions[key].update(daily_item.session_ids)

        for key, m in monthly_map.items():
            m.session_count = len(monthly_sessions[key])

        # Sort ascending by (year, month)
        return sorted(monthly_map.values(), key=lambda x: (x.year, x.month))

    @staticmethod
    def aggregate_models(sessions: List[SessionSummary]) -> List[ModelUsage]:
        """Aggregate token usage and distribution by model."""
        model_map: Dict[str, ModelUsage] = {}
        model_sessions: Dict[str, set] = defaultdict(set)
        total_tokens_all = 0

        for s in sessions:
            if s.steps:
                for step in s.steps:
                    m_name = normalize_model_name(step.model) or "unknown"
                    if m_name not in model_map:
                        model_map[m_name] = ModelUsage(model_name=m_name)
                    mu = model_map[m_name]
                    mu.usage = mu.usage.add(step.usage)
                    mu.cost = mu.cost.add(step.cost)
                    mu.request_count += 1
                    model_sessions[m_name].add(s.session_id)
                    total_tokens_all += step.usage.total_tokens
            else:
                m_name = normalize_model_name(s.primary_model) or "unknown"
                if m_name not in model_map:
                    model_map[m_name] = ModelUsage(model_name=m_name)
                mu = model_map[m_name]
                mu.usage = mu.usage.add(s.usage)
                mu.cost = mu.cost.add(s.cost)
                mu.request_count += s.step_count
                model_sessions[m_name].add(s.session_id)
                total_tokens_all += s.usage.total_tokens

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
            total_usage = total_usage.add(s.usage)
            total_cost = total_cost.add(s.cost)
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
