"""Main CLI interface for Antigravity Model Usage Tracker."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import List, Optional

from agy_usage import __version__
from agy_usage.aggregator import UsageAggregator
from agy_usage.display import (
    Colors,
    format_duration,
    format_percent,
    format_tokens,
    render_bar,
    render_kpi_grid,
    render_sparkline,
    render_table,
)
from agy_usage.exporter import UsageExporter
from agy_usage.heatmap import TerminalHeatmap
from agy_usage.models import SessionSummary
from agy_usage.parser import AntigravityParser


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agy-usage",
        description="Track and visualize Antigravity CLI & IDE model token usage and GitHub-style heatmaps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  agy-usage                       Show summary dashboard & mini heatmap
  agy-usage current               Show token usage & turns for the most recent session
  agy-usage heatmap               Show full GitHub-style contribution heatmap
  agy-usage heatmap --metric cached  Heatmap colored by cached tokens
  agy-usage sessions              List recent conversation sessions and token stats
  agy-usage session <id>          Inspect turn-by-turn token usage for a session
  agy-usage weekly                Show weekly aggregated usage table & bar chart
  agy-usage monthly               Show monthly aggregated usage table & bar chart
  agy-usage models                Show token breakdown by model
  agy-usage --dir ~/.gemini/antigravity/conversations  Analyze a specific directory
  agy-usage --json                Export usage data as JSON
  agy-usage --csv                 Export usage data as CSV
""",
    )

    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    # Directory selection flags
    dir_group = parser.add_argument_group("Data Source Options")
    dir_group.add_argument(
        "--dir",
        dest="custom_dir",
        metavar="PATH",
        help="Custom directory containing Antigravity conversation SQLite databases",
    )
    dir_group.add_argument(
        "--cli-only",
        action="store_true",
        help="Scan Antigravity CLI conversations directory only",
    )
    dir_group.add_argument(
        "--ide-only",
        action="store_true",
        help="Scan Antigravity IDE conversations directory only",
    )

    # Output format flags
    out_group = parser.add_argument_group("Output Formatting")
    out_group.add_argument(
        "--json", action="store_true", help="Output results as JSON"
    )
    out_group.add_argument(
        "--csv", action="store_true", help="Output session summaries as CSV"
    )
    out_group.add_argument(
        "--no-color", action="store_true", help="Disable ANSI color output"
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to run")

    # 1. overview / dashboard (default)
    sub_overview = subparsers.add_parser(
        "overview", help="Dashboard overview with KPI cards and heatmap (default)"
    )

    # 2. heatmap
    sub_heatmap = subparsers.add_parser(
        "heatmap", help="GitHub-style terminal contribution activity heatmap"
    )
    sub_heatmap.add_argument(
        "-w",
        "--weeks",
        type=int,
        default=26,
        help="Number of weeks to display (default: 26, up to 52)",
    )
    sub_heatmap.add_argument(
        "-m",
        "--metric",
        choices=["total", "cached", "input", "output", "thoughts"],
        default="total",
        help="Metric used to calculate heatmap color intensity (default: total)",
    )
    sub_heatmap.add_argument(
        "--details",
        action="store_true",
        help="Show detailed list of active dates below heatmap",
    )

    # 3. sessions
    sub_sessions = subparsers.add_parser(
        "sessions", help="List sessions with token and cache breakdown"
    )
    sub_sessions.add_argument(
        "-n",
        "--limit",
        type=int,
        default=15,
        help="Maximum sessions to display (0 for all, default: 15)",
    )
    sub_sessions.add_argument(
        "--sort",
        choices=["date", "tokens", "cached", "steps", "duration"],
        default="date",
        help="Sort field for sessions (default: date)",
    )
    sub_sessions.add_argument(
        "--model", help="Filter sessions by model name"
    )
    sub_sessions.add_argument(
        "--workspace", help="Filter sessions by workspace name"
    )

    # 4. current
    sub_current = subparsers.add_parser(
        "current", help="Show turn-by-turn token usage for the most recent session"
    )
    sub_current.add_argument(
        "-n",
        "--turns",
        type=int,
        default=30,
        help="Maximum turns to display in the log table (0 for all, default: 30)",
    )

    # 5. session <id>
    sub_session = subparsers.add_parser(
        "session", help="Inspect turn-by-turn token details for a specific session"
    )
    sub_session.add_argument(
        "session_id",
        nargs="?",
        default="current",
        help="Session ID (UUID or first 8 characters, default: current)",
    )
    sub_session.add_argument(
        "-n",
        "--turns",
        type=int,
        default=30,
        help="Maximum turns to display in the log table (0 for all, default: 30)",
    )

    # 6. weekly
    subparsers.add_parser(
        "weekly", help="Weekly token usage table and volume chart"
    )

    # 6. monthly
    subparsers.add_parser(
        "monthly", help="Monthly token usage table and volume chart"
    )

    # 7. models
    subparsers.add_parser(
        "models", help="Token breakdown and distribution by model"
    )

    # 8. live / watch mode
    sub_live = subparsers.add_parser(
        "live", help="Live watch mode: automatically refresh metrics"
    )
    sub_live.add_argument(
        "--interval",
        type=int,
        default=3,
        help="Refresh interval in seconds (default: 3)",
    )

    return parser.parse_args(args)


def get_sessions(args: argparse.Namespace) -> List[SessionSummary]:
    """Retrieve sessions based on CLI arguments."""
    source_mode = "all"
    if args.cli_only:
        source_mode = "cli"
    elif args.ide_only:
        source_mode = "ide"

    sessions = AntigravityParser.scan_all(
        custom_dir=args.custom_dir, source_mode=source_mode
    )

    # Optional model filter
    if getattr(args, "model", None):
        target_model = args.model.lower()
        sessions = [
            s
            for s in sessions
            if any(target_model in m.lower() for m in s.models)
        ]

    # Optional workspace filter
    if getattr(args, "workspace", None):
        target_ws = args.workspace.lower()
        sessions = [
            s for s in sessions if target_ws in s.workspace.lower()
        ]

    return sessions


def print_banner(sessions_count: int, sources_desc: str) -> None:
    """Print header banner."""
    title = f"ANTIGRAVITY MODEL USAGE TRACKER"
    sub = f"Loaded {sessions_count} sessions from {sources_desc}"
    print(Colors.bold(Colors.cyan(title)))
    print(Colors.dim(sub))
    print()


def cmd_overview(sessions: List[SessionSummary], args: argparse.Namespace) -> None:
    """Display default dashboard overview."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."))
        return

    summary = UsageAggregator.compute_summary(sessions)
    u = summary["total_usage"]

    # 1. Header & KPI Cards
    card1 = (
        "TOTAL PROCESSED",
        Colors.bold(Colors.green(format_tokens(u.total_tokens))),
        f"{format_tokens(u.total_prompt_tokens)} in / {format_tokens(u.output_tokens)} out",
    )
    card2 = (
        "CACHE SAVINGS",
        Colors.bold(Colors.green(format_tokens(u.cached_tokens))),
        f"{format_percent(u.cache_hit_rate)} prompt hit rate",
    )
    card3 = (
        "BILLABLE CONSUMED",
        Colors.bold(Colors.yellow(format_tokens(u.billable_tokens))),
        f"{format_tokens(u.prompt_tokens)} in + {format_tokens(u.output_tokens)} out",
    )
    card4 = (
        "ACTIVITY",
        Colors.bold(Colors.cyan(f"{summary['session_count']} sessions")),
        f"{summary['step_count']} steps / {summary['active_days']} days",
    )

    print(render_kpi_grid([card1, card2, card3, card4]))
    print()

    # 2. GitHub Heatmap (last 16 weeks)
    print(Colors.bold(Colors.cyan("── ACTIVITY HEATMAP (Last 16 Weeks) " + "─" * 40)))
    daily_data = UsageAggregator.aggregate_daily(sessions)
    heatmap = TerminalHeatmap(daily_data, metric="total", weeks=16)
    print(heatmap.render())
    print()

    # 3. Model Breakdown Table
    models = summary["models"]
    if models:
        m_headers = ["Model", "Requests", "Prompt", "Cached", "Output", "Total", "Share", "Cache %"]
        m_rows = []
        for m in models:
            mu = m.usage
            m_rows.append([
                Colors.bold(m.model_name),
                f"{m.request_count:,}",
                format_tokens(mu.prompt_tokens),
                format_tokens(mu.cached_tokens),
                format_tokens(mu.output_tokens),
                Colors.bold(format_tokens(mu.total_tokens)),
                f"{m.percentage_of_total:.1f}%",
                format_percent(mu.cache_hit_rate),
            ])
        print(render_table(m_headers, m_rows, alignments=["left", "right", "right", "right", "right", "right", "right", "right"], title="MODEL TOKEN USAGE"))
        print()

    # 4. Recent Sessions Table
    recent_headers = ["ID", "Src", "Workspace", "Title", "Model", "Cached", "Total", "Cache %", "Started"]
    recent_rows = []
    for s in sessions[:8]:
        su = s.usage
        started_str = s.start_time.strftime("%Y-%m-%d %H:%M") if s.start_time else "N/A"
        recent_rows.append([
            Colors.cyan(s.short_id),
            Colors.dim(s.source.upper()),
            s.workspace[:14],
            s.title[:35],
            s.primary_model.replace("gemini-", ""),
            format_tokens(su.cached_tokens),
            Colors.bold(format_tokens(su.total_tokens)),
            format_percent(su.cache_hit_rate),
            Colors.dim(started_str),
        ])
    print(render_table(recent_headers, recent_rows, alignments=["left", "center", "left", "left", "left", "right", "right", "right", "right"], title="RECENT SESSIONS"))


def cmd_heatmap(sessions: List[SessionSummary], args: argparse.Namespace) -> None:
    """Render full GitHub-style activity heatmap."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."))
        return

    daily_data = UsageAggregator.aggregate_daily(sessions)
    heatmap = TerminalHeatmap(
        daily_data,
        metric=args.metric,
        weeks=args.weeks,
    )

    print()
    print(Colors.bold(Colors.cyan(f"── GITHUB-STYLE CONTRIBUTION HEATMAP ({args.metric.upper()} TOKENS) " + "─" * 25)))
    print(heatmap.render())
    print()

    if args.details:
        # Print active days list
        sorted_days = sorted(
            [d for d in daily_data.values() if d.usage.total_tokens > 0],
            key=lambda x: x.date,
            reverse=True,
        )
        d_headers = ["Date", "Day", "Sessions", "Steps", "Models", "Prompt", "Cached", "Output", "Total", "Cache %"]
        d_rows = []
        for item in sorted_days:
            u = item.usage
            d_rows.append([
                item.date.isoformat(),
                item.date.strftime("%a"),
                str(len(item.session_ids)),
                str(item.steps_count),
                ", ".join(sorted(list(item.models)))[:25],
                format_tokens(u.prompt_tokens),
                format_tokens(u.cached_tokens),
                format_tokens(u.output_tokens),
                Colors.bold(format_tokens(u.total_tokens)),
                format_percent(u.cache_hit_rate),
            ])
        print(render_table(d_headers, d_rows, alignments=["left", "left", "right", "right", "left", "right", "right", "right", "right", "right"], title="ACTIVE DAYS DETAILS"))


def cmd_sessions(sessions: List[SessionSummary], args: argparse.Namespace) -> None:
    """List session summaries with token breakdowns."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."))
        return

    # Sort
    sort_key = args.sort
    if sort_key == "tokens":
        sessions = sorted(sessions, key=lambda s: s.usage.total_tokens, reverse=True)
    elif sort_key == "cached":
        sessions = sorted(sessions, key=lambda s: s.usage.cached_tokens, reverse=True)
    elif sort_key == "steps":
        sessions = sorted(sessions, key=lambda s: s.step_count, reverse=True)
    elif sort_key == "duration":
        sessions = sorted(sessions, key=lambda s: s.duration_seconds, reverse=True)
    else:  # date
        sessions = sorted(
            sessions,
            key=lambda s: s.start_time.timestamp() if s.start_time else 0,
            reverse=True,
        )

    limit = args.limit if args.limit > 0 else len(sessions)
    selected = sessions[:limit]

    headers = ["Session ID", "Src", "Workspace", "Title", "Model", "Steps", "Prompt", "Cached", "Output", "Total", "Cache %", "Duration", "Started"]
    rows = []
    for s in selected:
        u = s.usage
        started_str = s.start_time.strftime("%Y-%m-%d %H:%M") if s.start_time else "N/A"
        rows.append([
            Colors.cyan(s.short_id),
            Colors.dim(s.source.upper()),
            s.workspace[:14],
            s.title[:32],
            s.primary_model.replace("gemini-", ""),
            str(s.step_count),
            format_tokens(u.prompt_tokens),
            format_tokens(u.cached_tokens),
            format_tokens(u.output_tokens),
            Colors.bold(format_tokens(u.total_tokens)),
            format_percent(u.cache_hit_rate),
            format_duration(s.duration_seconds),
            Colors.dim(started_str),
        ])

    print()
    print(render_table(headers, rows, alignments=["left", "center", "left", "left", "left", "right", "right", "right", "right", "right", "right", "right", "right"], title=f"CONVERSATION SESSIONS ({len(selected)} of {len(sessions)})"))
    print()
    print(Colors.dim(f"Hint: Inspect a specific session in detail with: agy-usage session <session_id>"))


def cmd_session_inspect(
    sessions: List[SessionSummary],
    session_id_arg: str = "current",
    turns_limit: int = 30,
) -> None:
    """Inspect a single session's step-by-step token details."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."))
        return

    target_id = (session_id_arg or "current").strip().lower()
    matched = None

    if target_id in ("current", "latest", "recent"):
        matched = sessions[0]
    else:
        for s in sessions:
            if s.session_id.lower().startswith(target_id):
                matched = s
                break

    if not matched:
        print(Colors.red(f"Session '{session_id_arg}' not found."))
        print(Colors.dim("Run 'agy-usage sessions' to see all available session IDs."))
        return

    is_current = (matched == sessions[0])
    label_suffix = " (Most Recent)" if is_current else ""

    u = matched.usage
    print()
    print(Colors.bold(Colors.cyan(f"── SESSION DETAILS: {matched.session_id}{label_suffix} " + "─" * 16)))
    print(f"  {Colors.bold('Title:')}        {matched.title}")
    print(f"  {Colors.bold('Workspace:')}    {matched.workspace}")
    print(f"  {Colors.bold('Source:')}       {matched.source.upper()} ({matched.file_path})")
    print(f"  {Colors.bold('Model(s):')}     {', '.join(matched.models)}")
    started_str = matched.start_time.strftime("%Y-%m-%d %H:%M:%S") if matched.start_time else "N/A"
    ended_str = matched.end_time.strftime("%Y-%m-%d %H:%M:%S") if matched.end_time else "N/A"
    print(f"  {Colors.bold('Time:')}         {started_str} -> {ended_str} ({format_duration(matched.duration_seconds)})")
    print(f"  {Colors.bold('Total Tokens:')} {u.total_tokens:,} ({format_tokens(u.total_tokens)})")
    print(f"  {Colors.bold('Cached Tokens:')}{u.cached_tokens:,} ({format_percent(u.cache_hit_rate)} prompt hit rate)")
    print(f"  {Colors.bold('Prompt Tokens:')}{u.prompt_tokens:,} (non-cached)")
    print(f"  {Colors.bold('Output Tokens:')}{u.output_tokens:,} (thoughts: {u.thought_tokens:,}, text: {u.candidate_tokens:,})")
    print()

    if matched.steps:
        total_steps = len(matched.steps)
        step_tokens = [float(st.usage.total_tokens) for st in matched.steps]
        spark = render_sparkline(step_tokens)
        print(Colors.bold("Token Volume Progression Across Turns:"))
        print(f"  [{Colors.green(spark)}] ({total_steps} turns)")
        print()

        displayed_steps = matched.steps
        title_log = "TURN-BY-TURN TOKEN LOG"
        if turns_limit > 0 and total_steps > turns_limit:
            displayed_steps = matched.steps[-turns_limit:]
            title_log = f"TURN-BY-TURN TOKEN LOG (Showing last {turns_limit} of {total_steps} turns, use -n 0 for all)"

        # Step Details Table
        s_headers = ["Step", "Time", "Model", "Prompt", "Cached", "Thoughts", "Output", "Total", "Cache %"]
        s_rows = []
        for step in displayed_steps:
            su = step.usage
            t_str = step.timestamp.strftime("%H:%M:%S")
            s_rows.append([
                f"#{step.step_idx}",
                Colors.dim(t_str),
                step.model.replace("gemini-", ""),
                format_tokens(su.prompt_tokens),
                format_tokens(su.cached_tokens),
                format_tokens(su.thought_tokens),
                format_tokens(su.output_tokens),
                Colors.bold(format_tokens(su.total_tokens)),
                format_percent(su.cache_hit_rate),
            ])
        print(render_table(s_headers, s_rows, alignments=["right", "center", "left", "right", "right", "right", "right", "right", "right"], title=title_log))
    else:
        print(Colors.dim("No individual step token records found in this session database."))


def cmd_weekly(sessions: List[SessionSummary]) -> None:
    """Display weekly aggregated token table and volume bars."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."))
        return

    weeks = UsageAggregator.aggregate_weekly(sessions)
    if not weeks:
        print(Colors.yellow("No weekly usage data found."))
        return

    max_tokens = max(w.usage.total_tokens for w in weeks)

    headers = ["Week", "Date Range", "Sessions", "Steps", "Prompt", "Cached", "Output", "Total Tokens", "Cache %", "Relative Volume"]
    rows = []
    for w in weeks:
        wu = w.usage
        date_range = f"{w.start_date.strftime('%b %d')} - {w.end_date.strftime('%b %d')}"
        bar = render_bar(wu.total_tokens, max_tokens, width=16)
        rows.append([
            Colors.bold(w.label),
            Colors.dim(date_range),
            str(w.session_count),
            str(w.step_count),
            format_tokens(wu.prompt_tokens),
            format_tokens(wu.cached_tokens),
            format_tokens(wu.output_tokens),
            Colors.bold(format_tokens(wu.total_tokens)),
            format_percent(wu.cache_hit_rate),
            bar,
        ])

    print()
    print(render_table(headers, rows, alignments=["left", "left", "right", "right", "right", "right", "right", "right", "right", "left"], title="WEEKLY TOKEN USAGE"))


def cmd_monthly(sessions: List[SessionSummary]) -> None:
    """Display monthly aggregated token table and volume bars."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."))
        return

    months = UsageAggregator.aggregate_monthly(sessions)
    if not months:
        print(Colors.yellow("No monthly usage data found."))
        return

    max_tokens = max(m.usage.total_tokens for m in months)

    headers = ["Month", "Sessions", "Steps", "Prompt", "Cached", "Output", "Total Tokens", "Cache %", "Relative Volume"]
    rows = []
    for m in months:
        mu = m.usage
        bar = render_bar(mu.total_tokens, max_tokens, width=20)
        rows.append([
            Colors.bold(m.label),
            str(m.session_count),
            str(m.step_count),
            format_tokens(mu.prompt_tokens),
            format_tokens(mu.cached_tokens),
            format_tokens(mu.output_tokens),
            Colors.bold(format_tokens(mu.total_tokens)),
            format_percent(mu.cache_hit_rate),
            bar,
        ])

    print()
    print(render_table(headers, rows, alignments=["left", "right", "right", "right", "right", "right", "right", "right", "left"], title="MONTHLY TOKEN USAGE"))


def cmd_models(sessions: List[SessionSummary]) -> None:
    """Display breakdown by model."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."))
        return

    models = UsageAggregator.aggregate_models(sessions)
    if not models:
        print(Colors.yellow("No model usage data found."))
        return

    max_tokens = max(m.usage.total_tokens for m in models)

    headers = ["Model", "Sessions", "Requests", "Prompt", "Cached", "Output", "Thoughts", "Total Tokens", "Share", "Cache %", "Volume"]
    rows = []
    for m in models:
        mu = m.usage
        bar = render_bar(mu.total_tokens, max_tokens, width=14)
        rows.append([
            Colors.bold(m.model_name),
            str(m.session_count),
            str(m.request_count),
            format_tokens(mu.prompt_tokens),
            format_tokens(mu.cached_tokens),
            format_tokens(mu.output_tokens),
            format_tokens(mu.thought_tokens),
            Colors.bold(format_tokens(mu.total_tokens)),
            f"{m.percentage_of_total:.1f}%",
            format_percent(mu.cache_hit_rate),
            bar,
        ])

    print()
    print(render_table(headers, rows, alignments=["left", "right", "right", "right", "right", "right", "right", "right", "right", "right", "left"], title="MODEL TOKEN USAGE BREAKDOWN"))


def cmd_live(args: argparse.Namespace) -> None:
    """Live monitor mode."""
    interval = max(1, args.interval)
    print(Colors.cyan(f"Starting Antigravity Usage live monitor (refreshing every {interval}s). Press Ctrl+C to exit."))
    try:
        while True:
            # Clear screen
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"{Colors.bold(Colors.cyan('ANTIGRAVITY LIVE MONITOR'))}  {Colors.dim(f'Last updated: {now_str}')}")
            print()

            sessions = get_sessions(args)
            cmd_overview(sessions, args)

            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print(Colors.dim("Live monitor stopped."))


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for CLI execution."""
    args = parse_args(argv)

    if args.no_color:
        # Disable ANSI
        Colors.enabled = classmethod(lambda cls: False)

    sessions = get_sessions(args)

    # Handle export options
    if args.json:
        UsageExporter.export(sessions, format_type="json")
        return 0
    elif args.csv:
        UsageExporter.export(sessions, format_type="csv")
        return 0

    sub = args.subcommand
    if sub is None or sub == "overview":
        cmd_overview(sessions, args)
    elif sub == "heatmap":
        cmd_heatmap(sessions, args)
    elif sub == "sessions":
        cmd_sessions(sessions, args)
    elif sub == "current":
        cmd_session_inspect(sessions, "current", turns_limit=getattr(args, "turns", 30))
    elif sub == "session":
        cmd_session_inspect(sessions, args.session_id, turns_limit=getattr(args, "turns", 30))
    elif sub == "weekly":
        cmd_weekly(sessions)
    elif sub == "monthly":
        cmd_monthly(sessions)
    elif sub == "models":
        cmd_models(sessions)
    elif sub == "live":
        cmd_live(args)
    else:
        cmd_overview(sessions, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
