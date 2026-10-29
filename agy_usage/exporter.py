"""Export utilities for Antigravity usage data in JSON and CSV formats."""

from __future__ import annotations

import csv
import io
import json
import sys
from typing import List, Optional

from agy_usage.aggregator import UsageAggregator
from agy_usage.models import SessionSummary


class UsageExporter:
    """Exports session and token usage data to JSON or CSV formats."""

    @staticmethod
    def to_json(
        sessions: List[SessionSummary],
        include_steps: bool = False,
        indent: Optional[int] = 2,
    ) -> str:
        """Serialize all usage data, rollups, and sessions to JSON."""
        summary = UsageAggregator.compute_summary(sessions)
        daily = UsageAggregator.aggregate_daily(sessions)
        weekly = UsageAggregator.aggregate_weekly(sessions)
        monthly = UsageAggregator.aggregate_monthly(sessions)
        models = UsageAggregator.aggregate_models(sessions)

        data = {
            "summary": {
                "session_count": summary["session_count"],
                "step_count": summary["step_count"],
                "active_days": summary["active_days"],
                "unique_workspaces_count": summary["unique_workspaces_count"],
                "earliest_date": summary["earliest_date"],
                "latest_date": summary["latest_date"],
                "cache_hit_rate_pct": round(summary["cache_hit_rate"], 2),
                "total_usage": summary["total_usage"].to_dict(),
                "total_cost": summary["total_cost"].to_dict(),
            },
            "models": [m.to_dict() for m in models],
            "monthly": [m.to_dict() for m in monthly],
            "weekly": [w.to_dict() for w in weekly],
            "daily": [d.to_dict() for d in sorted(daily.values(), key=lambda x: x.date)],
            "sessions": [s.to_dict(include_steps=include_steps) for s in sessions],
        }
        return json.dumps(data, indent=indent)

    @staticmethod
    def to_csv(sessions: List[SessionSummary]) -> str:
        """Export session summaries as CSV."""
        output = io.StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow([
            "Session ID",
            "Source",
            "Workspace",
            "Title",
            "Model",
            "Start Time",
            "End Time",
            "Duration (s)",
            "Steps",
            "Prompt Tokens",
            "Cached Tokens",
            "Output Tokens",
            "Thought Tokens",
            "Total Tokens",
            "Cache Hit Rate (%)",
            "Estimated Cost (USD)",
        ])

        for s in sessions:
            u = s.usage
            writer.writerow([
                s.session_id,
                s.source,
                s.workspace,
                s.title,
                s.primary_model,
                s.start_time.isoformat() if s.start_time else "",
                s.end_time.isoformat() if s.end_time else "",
                round(s.duration_seconds, 1),
                s.step_count,
                u.prompt_tokens,
                u.cached_tokens,
                u.output_tokens,
                u.thought_tokens,
                u.total_tokens,
                round(u.cache_hit_rate, 2),
                round(s.cost.total, 4),
            ])

        return output.getvalue()

    @staticmethod
    def export(
        sessions: List[SessionSummary],
        format_type: str = "json",
        output_path: Optional[str] = None,
        include_steps: bool = False,
    ) -> None:
        """Export data to specified file or standard output."""
        if format_type.lower() == "csv":
            content = UsageExporter.to_csv(sessions)
        else:
            content = UsageExporter.to_json(sessions, include_steps=include_steps)

        if output_path and output_path != "-":
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            UsageExporter._write_stdout(content)

    @staticmethod
    def _write_stdout(content: str) -> None:
        """Write text to stdout without crashing on non-console-encodable characters."""
        stream = sys.stdout
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

        text = content if content.endswith("\n") else f"{content}\n"
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
            try:
                stream.write(safe_text)
            except UnicodeEncodeError:
                ascii_text = text.encode("ascii", errors="replace").decode("ascii", errors="replace")
                stream.write(ascii_text)
