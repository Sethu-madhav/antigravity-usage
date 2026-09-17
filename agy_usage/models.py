"""Data models for Antigravity model and token usage tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Set


@dataclass
class TokenUsage:
    """Represents token counts for a single step, session, or period."""

    prompt_tokens: int = 0      # Non-cached input tokens processed
    cached_tokens: int = 0      # Input tokens served from prompt cache
    output_tokens: int = 0      # Total output tokens (thoughts + candidate)
    thought_tokens: int = 0     # Reasoning / thinking tokens
    candidate_tokens: int = 0   # Generated response tokens

    @property
    def total_prompt_tokens(self) -> int:
        """Total prompt context = non-cached + cached."""
        return self.prompt_tokens + self.cached_tokens

    @property
    def total_tokens(self) -> int:
        """Total tokens across prompt, cached, and output."""
        return self.prompt_tokens + self.cached_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """Billable tokens (non-cached prompt + output)."""
        return self.prompt_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Percentage of prompt tokens served from cache."""
        total_prompt = self.total_prompt_tokens
        if total_prompt == 0:
            return 0.0
        return (self.cached_tokens / total_prompt) * 100.0

    def add(self, other: TokenUsage) -> TokenUsage:
        """Add another TokenUsage into a new TokenUsage instance."""
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            thought_tokens=self.thought_tokens + other.thought_tokens,
            candidate_tokens=self.candidate_tokens + other.candidate_tokens,
        )

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "thought_tokens": self.thought_tokens,
            "candidate_tokens": self.candidate_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_tokens": self.total_tokens,
            "billable_tokens": self.billable_tokens,
            "cache_hit_rate_pct": round(self.cache_hit_rate, 2),
        }


# Standard official Google Gemini API rates per 1,000,000 tokens:
# Format: (prompt_rate, cached_read_rate, output_rate)
MODEL_RATES: Dict[str, tuple[float, float, float]] = {
    "gemini-3.8-flash": (0.75, 0.075, 3.75),
    "gemini-3.7-flash": (0.75, 0.075, 3.75),
    "gemini-2.5-flash": (0.30, 0.030, 2.50),
    "gemini-2.0-flash": (0.10, 0.025, 0.40),
    "gemini-1.5-flash": (0.075, 0.01875, 0.30),
    "gemini-2.5-pro": (1.25, 0.3125, 5.00),
    "gemini-1.5-pro": (1.25, 0.3125, 5.00),
}


def get_model_rates(model_name: str) -> tuple[float, float, float]:
    """Get (prompt_per_m, cache_per_m, output_per_m) rates for a given model."""
    m = (model_name or "").lower()
    for key, rates in MODEL_RATES.items():
        if key in m:
            return rates
    if "pro" in m:
        return (1.25, 0.3125, 5.00)
    return (0.75, 0.075, 3.75)


def calculate_cost(
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    model_name: str = "gemini-3.8-flash",
) -> tuple[float, float, float, float]:
    """
    Calculate estimated cost in USD based on official token pricing.
    Returns: (prompt_cost, cache_cost, output_cost, total_cost)
    """
    p_rate, c_rate, o_rate = get_model_rates(model_name)
    p_cost = (prompt_tokens / 1_000_000.0) * p_rate
    c_cost = (cached_tokens / 1_000_000.0) * c_rate
    o_cost = (output_tokens / 1_000_000.0) * o_rate
    total = p_cost + c_cost + o_cost
    return p_cost, c_cost, o_cost, total


@dataclass
class StepRecord:
    """Represents a single model generation step in a conversation."""

    step_idx: int
    timestamp: datetime
    model: str
    usage: TokenUsage
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "step_idx": self.step_idx,
            "timestamp": self.timestamp.isoformat(),
            "model": self.model,
            "duration_ms": self.duration_ms,
            "usage": self.usage.to_dict(),
        }


@dataclass
class SessionSummary:
    """Summary of a single Antigravity session/conversation."""

    session_id: str
    source: str                 # "cli" or "ide"
    file_path: str
    title: str = "Untitled Session"
    workspace: str = "Unknown"
    models: List[str] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    step_count: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    steps: List[StepRecord] = field(default_factory=list)

    @property
    def primary_model(self) -> str:
        if self.models:
            return self.models[0]
        return "unknown"

    @property
    def duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            delta = (self.end_time - self.start_time).total_seconds()
            return max(0.0, delta)
        return 0.0

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    def to_dict(self, include_steps: bool = False) -> dict:
        d = {
            "session_id": self.session_id,
            "short_id": self.short_id,
            "source": self.source,
            "title": self.title,
            "workspace": self.workspace,
            "primary_model": self.primary_model,
            "models": self.models,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": round(self.duration_seconds, 1),
            "step_count": self.step_count,
            "usage": self.usage.to_dict(),
        }
        if include_steps:
            d["steps"] = [s.to_dict() for s in self.steps]
        return d


@dataclass
class DailyUsage:
    """Usage aggregated by calendar date."""

    date: date
    usage: TokenUsage = field(default_factory=TokenUsage)
    session_ids: Set[str] = field(default_factory=set)
    models: Set[str] = field(default_factory=set)
    steps_count: int = 0

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "sessions_count": len(self.session_ids),
            "steps_count": self.steps_count,
            "models": sorted(list(self.models)),
            "usage": self.usage.to_dict(),
        }


@dataclass
class WeeklyUsage:
    """Usage aggregated by ISO week."""

    year: int
    week_num: int
    start_date: date
    end_date: date
    usage: TokenUsage = field(default_factory=TokenUsage)
    session_count: int = 0
    step_count: int = 0

    @property
    def label(self) -> str:
        return f"{self.year}-W{self.week_num:02d}"

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "week_num": self.week_num,
            "label": self.label,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "session_count": self.session_count,
            "step_count": self.step_count,
            "usage": self.usage.to_dict(),
        }


@dataclass
class MonthlyUsage:
    """Usage aggregated by month."""

    year: int
    month: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    session_count: int = 0
    step_count: int = 0

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "month": self.month,
            "label": self.label,
            "session_count": self.session_count,
            "step_count": self.step_count,
            "usage": self.usage.to_dict(),
        }


@dataclass
class ModelUsage:
    """Usage breakdown for a single model."""

    model_name: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    request_count: int = 0
    session_count: int = 0
    percentage_of_total: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "request_count": self.request_count,
            "session_count": self.session_count,
            "percentage_of_total": round(self.percentage_of_total, 2),
            "usage": self.usage.to_dict(),
        }
