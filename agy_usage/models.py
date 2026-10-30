"""Data models for Antigravity model and token usage tracking."""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple


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


@dataclass(frozen=True)
class ModelRates:
    """USD rates per 1,000,000 tokens for one model during one pricing period.

    ``prompt`` is the non-cached input rate, ``cached`` the cache-read rate and
    ``output`` the output rate (output includes thinking tokens on every model
    listed below). Models with long-context pricing switch to the ``long_*``
    rates when a single request's prompt context (non-cached + cached) exceeds
    ``long_context_threshold`` tokens.
    """

    prompt: float
    cached: float
    output: float
    long_context_threshold: int = 0
    long_prompt: float = 0.0
    long_cached: float = 0.0
    long_output: float = 0.0

    def for_context(self, prompt_context: Optional[int]) -> "ModelRates":
        """Return the effective rates for a request with the given prompt context size."""
        if (
            self.long_context_threshold
            and prompt_context is not None
            and prompt_context > self.long_context_threshold
        ):
            return ModelRates(self.long_prompt, self.long_cached, self.long_output)
        return self

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.prompt, self.cached, self.output)


# Pricing schedule: (effective_from, rates). ``None`` means "since launch".
PricingSchedule = Tuple[Tuple[Optional[date], ModelRates], ...]

_FLASH_3X: PricingSchedule = (
    # Introductory Flash pricing runs through Dec 31, 2026.
    (None, ModelRates(0.75, 0.075, 3.75)),
    (date(2027, 1, 1), ModelRates(1.50, 0.15, 7.50)),
)

# Official paid-tier (Standard) API rates, verified against
# https://ai.google.dev/gemini-api/docs/pricing and
# https://docs.anthropic.com/en/docs/about-claude/pricing on 2026-10-18.
# Keys are matched as substrings of the model name; the LONGEST matching key wins,
# so e.g. "gemini-2.5-flash-lite" never falls through to "gemini-2.5-flash".
MODEL_PRICING: Dict[str, PricingSchedule] = {
    # Gemini 4 Argon: introductory pricing through Nov 30, 2026 (implicit caching only).
    "gemini-4-argon": (
        (None, ModelRates(2.00, 0.10, 10.00)),
        (date(2026, 12, 1), ModelRates(4.00, 0.20, 20.00)),
    ),
    "gemini-3.8-flash": _FLASH_3X,
    # gemini-3.7-flash is no longer listed on the pricing page; it was billed at the
    # same introductory Flash 3.x schedule as 3.6 / 3.8.
    "gemini-3.7-flash": _FLASH_3X,
    "gemini-3.6-flash": _FLASH_3X,
    "gemini-3.5-flash-lite": ((None, ModelRates(0.30, 0.03, 2.50)),),
    "gemini-3.5-flash": ((None, ModelRates(0.50, 0.05, 3.00)),),
    "gemini-3.1-flash-lite": ((None, ModelRates(0.25, 0.025, 1.50)),),
    "gemini-3.1-flash": ((None, ModelRates(0.50, 0.05, 3.00)),),
    "gemini-3-flash": ((None, ModelRates(0.50, 0.05, 3.00)),),
    "gemini-3.1-pro": (
        (None, ModelRates(2.00, 0.20, 12.00, 200_000, 4.00, 0.40, 18.00)),
    ),
    "gemini-2.5-pro": (
        (None, ModelRates(1.25, 0.125, 10.00, 200_000, 2.50, 0.25, 15.00)),
    ),
    "gemini-2.5-flash-lite": ((None, ModelRates(0.10, 0.01, 0.40)),),
    "gemini-2.5-flash": ((None, ModelRates(0.30, 0.03, 2.50)),),
    "gemini-2.0-flash": ((None, ModelRates(0.10, 0.025, 0.40)),),
    # Legacy 1.5 models (historical <=128k rates).
    "gemini-1.5-flash": ((None, ModelRates(0.075, 0.01875, 0.30)),),
    "gemini-1.5-pro": ((None, ModelRates(1.25, 0.3125, 5.00)),),
    # Anthropic & OpenAI partner models served in Antigravity. Cache reads on Opus/Sonnet 5.5
    # are billed at 5% of the input price. Antigravity's usage metadata does not
    # expose cache-write tokens, so cache-write premiums are not modelled.
    "claude-opus-5-5": ((None, ModelRates(4.00, 0.20, 20.00)),),
    "claude-opus-5.5": ((None, ModelRates(4.00, 0.20, 20.00)),),
    "claude-sonnet-5-5": ((None, ModelRates(2.00, 0.10, 10.00)),),
    "claude-sonnet-5.5": ((None, ModelRates(2.00, 0.10, 10.00)),),
    "gpt-oss-120b": ((None, ModelRates(0.50, 0.05, 2.00)),),
}

# Family-level fallbacks for model names that don't match an exact key above.
_FAMILY_FALLBACKS: Tuple[Tuple[str, str], ...] = (
    ("argon", "gemini-4-argon"),
    ("claude-opus", "claude-opus-5-5"),
    ("claude", "claude-sonnet-5-5"),
    ("gpt-oss", "gpt-oss-120b"),
    ("pro", "gemini-3.1-pro"),
)
_DEFAULT_PRICING_KEY = "gemini-3.8-flash"


def normalize_model_name(model_name: str) -> str:
    """Normalize internal Antigravity model variants into canonical model names.

    Groups ``gemini-3.8-flash``, ``gemini-3.8-flash-control``, ``gemini-3.8-n``,
    and ``gemini-3.8-flash-n`` (as well as low/medium/high/tiered suffixes)
    under ``gemini-3.8-flash``.

    An empty (or whitespace-only) name is *unresolvable* and normalizes to
    ``''`` — returning the hardcoded default here used to fabricate phantom
    ``gemini-3.8-flash`` model entries with zero token evidence (BUG-010).
    """
    if not model_name:
        return ""
    m = model_name.strip()
    m_low = m.lower()
    if m_low in (
        "gemini-3.8-flash",
        "gemini-3.8-flash-control",
        "gemini-3.8-flash-n",
        "gemini-3.8-n",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-3.8-flash-tiered",
    ) or m_low.startswith("gemini-3.8-flash"):
        return "gemini-3.8-flash"
    return m


@functools.lru_cache(maxsize=None)
def _resolve_pricing_key(model_name: str) -> str:
    m = (model_name or "").lower()
    matches = [key for key in MODEL_PRICING if key in m]
    if matches:
        return max(matches, key=len)
    for needle, key in _FAMILY_FALLBACKS:
        if needle in m:
            return key
    return _DEFAULT_PRICING_KEY


def get_rates(
    model_name: str,
    on_date: Optional[date] = None,
    prompt_context: Optional[int] = None,
) -> ModelRates:
    """Resolve the effective ModelRates for a model on a given date / context size."""
    if isinstance(on_date, datetime):
        on_date = on_date.date()
    target = on_date or date.today()
    schedule = MODEL_PRICING[_resolve_pricing_key(model_name)]
    rates = schedule[0][1]
    for effective_from, period_rates in schedule:
        if effective_from is None or target >= effective_from:
            rates = period_rates
    return rates.for_context(prompt_context)


def get_model_rates(
    model_name: str, on_date: Optional[date] = None
) -> tuple[float, float, float]:
    """Get (prompt_per_m, cache_per_m, output_per_m) rates for a given model."""
    return get_rates(model_name, on_date).as_tuple()


def calculate_cost(
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    model_name: str = "gemini-3.8-flash",
    on_date: Optional[date] = None,
    prompt_context: Optional[int] = None,
) -> tuple[float, float, float, float]:
    """
    Calculate estimated cost in USD based on official token pricing.

    ``on_date`` selects the pricing period (defaults to today). ``prompt_context``
    is the per-request prompt size used for long-context tiers; only pass it when
    pricing a single request, never for aggregated totals.

    Returns: (prompt_cost, cache_cost, output_cost, total_cost)
    """
    rates = get_rates(model_name, on_date, prompt_context)
    p_cost = (prompt_tokens / 1_000_000.0) * rates.prompt
    c_cost = (cached_tokens / 1_000_000.0) * rates.cached
    o_cost = (output_tokens / 1_000_000.0) * rates.output
    total = p_cost + c_cost + o_cost
    return p_cost, c_cost, o_cost, total


@dataclass
class CostBreakdown:
    """Estimated USD cost split by token category (additive across steps)."""

    prompt_cost: float = 0.0       # Non-cached input
    cached_cost: float = 0.0       # Cache reads
    output_cost: float = 0.0       # All output (thoughts + candidates)
    thought_cost: float = 0.0      # Subset of output_cost spent on reasoning tokens
    cache_savings: float = 0.0     # What cached tokens would have cost at the full input rate, minus what they cost

    @property
    def total(self) -> float:
        return self.prompt_cost + self.cached_cost + self.output_cost

    @property
    def billable(self) -> float:
        """Cost of non-cached input + output (excludes cache reads)."""
        return self.prompt_cost + self.output_cost

    @property
    def candidate_cost(self) -> float:
        return max(0.0, self.output_cost - self.thought_cost)

    def add(self, other: "CostBreakdown") -> "CostBreakdown":
        return CostBreakdown(
            prompt_cost=self.prompt_cost + other.prompt_cost,
            cached_cost=self.cached_cost + other.cached_cost,
            output_cost=self.output_cost + other.output_cost,
            thought_cost=self.thought_cost + other.thought_cost,
            cache_savings=self.cache_savings + other.cache_savings,
        )

    def to_dict(self) -> dict:
        return {
            "prompt_cost_usd": round(self.prompt_cost, 6),
            "cached_cost_usd": round(self.cached_cost, 6),
            "output_cost_usd": round(self.output_cost, 6),
            "thought_cost_usd": round(self.thought_cost, 6),
            "total_cost_usd": round(self.total, 6),
            "cache_savings_usd": round(self.cache_savings, 6),
        }


def price_usage(
    usage: TokenUsage,
    model_name: str,
    on_date: Optional[date] = None,
    per_request: bool = False,
) -> CostBreakdown:
    """Price a TokenUsage with a single model's rates.

    Set ``per_request=True`` when ``usage`` describes a single model request so that
    long-context (>200k prompt) tiers can be applied correctly.
    """
    context = usage.total_prompt_tokens if per_request else None
    rates = get_rates(model_name, on_date, context)
    per_m = 1_000_000.0
    return CostBreakdown(
        prompt_cost=usage.prompt_tokens / per_m * rates.prompt,
        cached_cost=usage.cached_tokens / per_m * rates.cached,
        output_cost=usage.output_tokens / per_m * rates.output,
        thought_cost=usage.thought_tokens / per_m * rates.output,
        cache_savings=usage.cached_tokens / per_m * max(0.0, rates.prompt - rates.cached),
    )


@dataclass
class StepRecord:
    """Represents a single model generation step in a conversation."""

    step_idx: int
    timestamp: datetime
    model: str
    usage: TokenUsage
    duration_ms: float = 0.0

    @functools.cached_property
    def cost(self) -> CostBreakdown:
        """Cost of this single request, priced with its own model and date.

        The value is computed once per instance, so the record must not be mutated after its cost is read.
        """
        return price_usage(self.usage, self.model, self.timestamp.date(), per_request=True)

    def to_dict(self) -> dict:
        return {
            "step_idx": self.step_idx,
            "timestamp": self.timestamp.isoformat(),
            "model": self.model,
            "duration_ms": self.duration_ms,
            "usage": self.usage.to_dict(),
            "cost": self.cost.to_dict(),
        }


def price_steps(steps: List[StepRecord]) -> CostBreakdown:
    """Sum per-step costs (each step priced with its own model and date)."""
    total = CostBreakdown()
    for step in steps:
        total = total.add(step.cost)
    return total


@dataclass
class SessionSummary:
    """Summary of a single Antigravity session/conversation."""

    session_id: str
    source: str                 # "cli" or "ide"
    file_path: str
    title: str = "Untitled Session"
    workspace: str = "Unknown"
    models: List[str] = field(default_factory=list)  # Ordered by token volume (primary first)
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

    @functools.cached_property
    def cost(self) -> CostBreakdown:
        """Estimated cost, priced per step so mixed-model sessions are correct.

        The value is computed once per instance, so the record must not be mutated after its cost is read.
        """
        if self.steps:
            return price_steps(self.steps)
        on_date = self.start_time.date() if self.start_time else None
        return price_usage(self.usage, self.primary_model, on_date)

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
            "cost": self.cost.to_dict(),
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
    cost: CostBreakdown = field(default_factory=CostBreakdown)

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "sessions_count": len(self.session_ids),
            "steps_count": self.steps_count,
            "models": sorted(list(self.models)),
            "usage": self.usage.to_dict(),
            "cost": self.cost.to_dict(),
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
    cost: CostBreakdown = field(default_factory=CostBreakdown)

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
            "cost": self.cost.to_dict(),
        }


@dataclass
class MonthlyUsage:
    """Usage aggregated by month."""

    year: int
    month: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    session_count: int = 0
    step_count: int = 0
    cost: CostBreakdown = field(default_factory=CostBreakdown)

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
            "cost": self.cost.to_dict(),
        }


@dataclass
class ModelUsage:
    """Usage breakdown for a single model."""

    model_name: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    request_count: int = 0
    session_count: int = 0
    percentage_of_total: float = 0.0
    cost: CostBreakdown = field(default_factory=CostBreakdown)

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "request_count": self.request_count,
            "session_count": self.session_count,
            "percentage_of_total": round(self.percentage_of_total, 2),
            "usage": self.usage.to_dict(),
            "cost": self.cost.to_dict(),
        }
