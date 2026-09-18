"""Main CLI interface for Antigravity Model Usage Tracker."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from typing import List, Optional

from agy_usage import __version__
from agy_usage.aggregator import UsageAggregator
from agy_usage.display import (
    Colors,
    format_cost,
    format_duration,
    format_duration_verbose,
    format_percent,
    format_tokens,
    render_bar,
    render_kpi_grid,
    render_sparkline,
    render_table,
)
from agy_usage.exporter import UsageExporter
from agy_usage.heatmap import TerminalHeatmap
from agy_usage.limits import (
    TIERS,
    LimitsCalculator,
    get_status_badge,
    parse_token_amount,
    render_gauge_bar,
)
from agy_usage.models import SessionSummary, calculate_cost
from agy_usage.parser import AntigravityParser


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agy-usage",
        description="Track and visualize Antigravity CLI & IDE model token usage and GitHub-style heatmaps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  agy-usage                       Show summary dashboard & mini heatmap
  agy-usage current               Show token usage & totals for the most recent session
  agy-usage limits                Show current 5-hour limit and weekly quota limits
  agy-usage heatmap               Show full GitHub-style contribution heatmap
  agy-usage heatmap --metric cached  Heatmap colored by cached tokens
  agy-usage sessions              List recent conversation sessions and token stats
  agy-usage session <id>          Inspect token usage totals for a session
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
        "-l",
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
        "current", help="Show token usage totals for the most recent session"
    )
    sub_current.add_argument(
        "--details",
        "--log",
        action="store_true",
        help="Display turn-by-turn token log table",
    )
    sub_current.add_argument(
        "-n",
        "--turns",
        type=int,
        default=None,
        help="Display last N turns in a log table (0 for all turns)",
    )

    # 5. session <id>
    sub_session = subparsers.add_parser(
        "session", help="Inspect token usage totals for a specific session"
    )
    sub_session.add_argument(
        "session_id",
        nargs="?",
        default="current",
        help="Session ID (UUID or first 8 characters, default: current)",
    )
    sub_session.add_argument(
        "--details",
        "--log",
        action="store_true",
        help="Display turn-by-turn token log table",
    )
    sub_session.add_argument(
        "-n",
        "--turns",
        type=int,
        default=None,
        help="Display last N turns in a log table (0 for all turns)",
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

    # 9. limits
    sub_limits = subparsers.add_parser(
        "limits", help="Show current 5-hour rolling limit and weekly quota limits"
    )
    sub_limits.add_argument(
        "--tier",
        choices=["pro", "ultra", "free"],
        default="pro",
        help="Antigravity subscription plan tier (default: pro)",
    )
    sub_limits.add_argument(
        "--5h-limit",
        "--five-hour-limit",
        dest="five_h_limit",
        help="Custom 5-hour limit in tokens (e.g. 50M, 100M)",
    )
    sub_limits.add_argument(
        "--weekly-limit",
        dest="weekly_limit",
        help="Custom weekly limit in tokens (e.g. 250M, 1B)",
    )
    sub_limits.add_argument(
        "--json", action="store_true", help="Output limits analysis as JSON"
    )
    sub_limits.add_argument(
        "--csv", action="store_true", help="Output limits analysis as CSV"
    )

    # 10. statusline
    sub_statusline = subparsers.add_parser(
        "statusline", help="Compact single-line quota status for shell prompt or tmux"
    )
    sub_statusline.add_argument(
        "--tier",
        choices=["pro", "ultra", "free"],
        default="pro",
        help="Antigravity subscription plan tier (default: pro)",
    )
    sub_statusline.add_argument(
        "--5h-limit",
        "--five-hour-limit",
        dest="five_h_limit",
        help="Custom 5-hour limit in tokens (e.g. 50M, 100M)",
    )
    sub_statusline.add_argument(
        "--weekly-limit",
        dest="weekly_limit",
        help="Custom weekly limit in tokens (e.g. 250M, 1B)",
    )
    sub_statusline.add_argument(
        "-s",
        "--short",
        action="store_true",
        help="Ultra-compact minimal format (e.g. '🟢 5h:99%% Wk:91%% $11.47')",
    )
    sub_statusline.add_argument(
        "--plain",
        action="store_true",
        help="Plain text output without ANSI color codes or emojis",
    )
    sub_statusline.add_argument(
        "--tmux",
        action="store_true",
        help="Format output using tmux color formatting directives (#[fg=...])",
    )
    sub_statusline.add_argument(
        "--json",
        action="store_true",
        help="Output statusline metrics as JSON (ideal for Waybar/Polybar/Starship)",
    )
    sub_statusline.add_argument(
        "-f",
        "--format",
        help="Custom template format string (e.g. '{status} 5h:{5h_pct}%% | Wk:{wk_pct}%% | {today_cost}')",
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
    p_c, _, o_c, tot_c = calculate_cost(u.prompt_tokens, u.cached_tokens, u.output_tokens)
    bill_c = p_c + o_c
    card3 = (
        "BILLABLE CONSUMED",
        Colors.bold(Colors.yellow(format_tokens(u.billable_tokens))),
        f"{format_tokens(u.prompt_tokens)} in + {format_tokens(u.output_tokens)} out ({format_cost(bill_c)})",
    )
    card4 = (
        "ACTIVITY",
        Colors.bold(Colors.cyan(f"{summary['session_count']} sessions")),
        f"{summary['step_count']} steps / {summary['active_days']} days ({format_cost(tot_c)})",
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
        m_headers = ["Model", "Requests", "Prompt", "Cached", "Output", "Total", "Share", "Cost", "Cache %"]
        m_rows = []
        for m in models:
            mu = m.usage
            _, _, _, m_cost = calculate_cost(mu.prompt_tokens, mu.cached_tokens, mu.output_tokens, model_name=m.model_name)
            m_rows.append([
                Colors.bold(m.model_name),
                f"{m.request_count:,}",
                format_tokens(mu.prompt_tokens),
                format_tokens(mu.cached_tokens),
                format_tokens(mu.output_tokens),
                Colors.bold(format_tokens(mu.total_tokens)),
                f"{m.percentage_of_total:.1f}%",
                format_cost(m_cost),
                format_percent(mu.cache_hit_rate),
            ])
        print(render_table(m_headers, m_rows, alignments=["left", "right", "right", "right", "right", "right", "right", "right", "right"], title="MODEL TOKEN USAGE"))
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

    headers = ["Session ID", "Src", "Workspace", "Title", "Model", "Steps", "Prompt", "Cached", "Output", "Total", "Cost", "Cache %", "Duration", "Started"]
    rows = []
    for s in selected:
        u = s.usage
        started_str = s.start_time.strftime("%Y-%m-%d %H:%M") if s.start_time else "N/A"
        _, _, _, s_cost = calculate_cost(u.prompt_tokens, u.cached_tokens, u.output_tokens, model_name=s.primary_model)
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
            format_cost(s_cost),
            format_percent(u.cache_hit_rate),
            format_duration(s.duration_seconds),
            Colors.dim(started_str),
        ])

    print()
    print(render_table(headers, rows, alignments=["left", "center", "left", "left", "left", "right", "right", "right", "right", "right", "right", "right", "right", "right"], title=f"CONVERSATION SESSIONS ({len(selected)} of {len(sessions)})"))
    print()
    print(Colors.dim(f"Hint: Inspect a specific session in detail with: agy-usage session <session_id>"))


def cmd_session_inspect(
    sessions: List[SessionSummary],
    session_id_arg: str = "current",
    turns_limit: Optional[int] = None,
    show_details: bool = False,
) -> None:
    """Inspect a session's token totals and optionally turn-by-turn details."""
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
    print(Colors.bold(Colors.cyan(f"── SESSION TOTALS: {matched.session_id}{label_suffix} " + "─" * 16)))
    print(f"  {Colors.bold('Title:')}        {matched.title}")
    print(f"  {Colors.bold('Workspace:')}    {Colors.cyan(matched.workspace)}")
    print(f"  {Colors.bold('Source:')}       {matched.source.upper()} ({Colors.dim(matched.file_path)})")
    print(f"  {Colors.bold('Model(s):')}     {', '.join(matched.models) if matched.models else 'N/A'}")
    started_str = matched.start_time.strftime("%Y-%m-%d %H:%M:%S") if matched.start_time else "N/A"
    ended_str = matched.end_time.strftime("%Y-%m-%d %H:%M:%S") if matched.end_time else "N/A"
    turns_str = f", {len(matched.steps)} turns" if matched.steps else ""
    print(f"  {Colors.bold('Time:')}         {started_str} → {ended_str} ({format_duration(matched.duration_seconds)}{turns_str})")
    print()

    # Cost calculations
    model_name = matched.primary_model or (matched.models[0] if matched.models else "gemini-3.8-flash")
    p_cost, c_cost, o_cost, total_cost = calculate_cost(
        u.prompt_tokens, u.cached_tokens, u.output_tokens, model_name=model_name
    )
    billable_cost = p_cost + o_cost
    _, _, th_cost, _ = calculate_cost(0, 0, u.thought_tokens, model_name=model_name)
    _, _, ca_cost, _ = calculate_cost(0, 0, u.candidate_tokens, model_name=model_name)

    # 1. KPI Cards
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
        "ESTIMATED COST",
        Colors.bold(Colors.green(format_cost(total_cost))),
        f"Billable: {format_cost(billable_cost)}",
    )
    print(render_kpi_grid([card1, card2, card3, card4]))
    print()

    # 2. Token Breakdown Table
    categories = [
        ("Cached Prompt", u.cached_tokens, c_cost),
        ("Non-Cached Prompt", u.prompt_tokens, p_cost),
    ]
    if u.thought_tokens > 0 or u.candidate_tokens > 0:
        categories.append(("Reasoning (Thoughts)", u.thought_tokens, th_cost))
        categories.append(("Candidate (Output)", u.candidate_tokens, ca_cost))
    else:
        categories.append(("Output Tokens", u.output_tokens, o_cost))

    max_cat = max((c[1] for c in categories), default=1) or 1
    total_tok = u.total_tokens or 1

    b_headers = ["Category", "Tokens", "Compact", "Share", "Cost", "Relative Volume"]
    b_rows = []
    for cat_name, cat_val, cat_cost in categories:
        share = (cat_val / total_tok) * 100.0
        bar = render_bar(cat_val, max_cat, width=16)
        b_rows.append([
            cat_name,
            f"{cat_val:,}",
            format_tokens(cat_val),
            format_percent(share),
            format_cost(cat_cost),
            bar,
        ])

    b_rows.append([
        Colors.bold("Total Processed"),
        Colors.bold(f"{u.total_tokens:,}"),
        Colors.bold(Colors.green(format_tokens(u.total_tokens))),
        "100.0%",
        Colors.bold(Colors.green(format_cost(total_cost))),
        "",
    ])
    billable_share = (u.billable_tokens / total_tok) * 100.0
    b_rows.append([
        Colors.bold("Billable Consumed"),
        Colors.bold(f"{u.billable_tokens:,}"),
        Colors.bold(Colors.yellow(format_tokens(u.billable_tokens))),
        format_percent(billable_share),
        Colors.bold(Colors.yellow(format_cost(billable_cost))),
        "",
    ])

    print(render_table(b_headers, b_rows, alignments=["left", "right", "right", "right", "right", "left"], title="TOKEN USAGE BREAKDOWN"))

    # 4. Turn-by-Turn Log Table (Only if requested via --details or -n)
    should_show_turns = show_details or (turns_limit is not None)
    if should_show_turns and matched.steps:
        total_steps = len(matched.steps)
        actual_limit = 30 if turns_limit is None else turns_limit
        displayed_steps = matched.steps
        title_log = "TURN-BY-TURN TOKEN LOG"
        if actual_limit > 0 and total_steps > actual_limit:
            displayed_steps = matched.steps[-actual_limit:]
            title_log = f"TURN-BY-TURN TOKEN LOG (Showing last {actual_limit} of {total_steps} turns, use -n 0 for all)"

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
        print()
        print(render_table(s_headers, s_rows, alignments=["right", "center", "left", "right", "right", "right", "right", "right", "right"], title=title_log))
    elif not should_show_turns:
        cmd_name = "current" if is_current else f"session {matched.short_id}"
        print()
        print(Colors.dim(f"Hint: For turn-by-turn logs, use: agy-usage {cmd_name} --details (or -n <count>)"))


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

    headers = ["Model", "Sessions", "Requests", "Prompt", "Cached", "Output", "Thoughts", "Total Tokens", "Share", "Cost", "Cache %", "Volume"]
    rows = []
    for m in models:
        mu = m.usage
        bar = render_bar(mu.total_tokens, max_tokens, width=14)
        _, _, _, m_cost = calculate_cost(mu.prompt_tokens, mu.cached_tokens, mu.output_tokens, model_name=m.model_name)
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
            format_cost(m_cost),
            format_percent(mu.cache_hit_rate),
            bar,
        ])

    print()
    print(render_table(headers, rows, alignments=["left", "right", "right", "right", "right", "right", "right", "right", "right", "right", "right", "left"], title="MODEL TOKEN USAGE BREAKDOWN"))


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


def cmd_limits(sessions: List[SessionSummary], args: argparse.Namespace) -> None:
    """Display 5-hour rolling limit and weekly quota limits."""
    tier = getattr(args, "tier", "pro") or "pro"
    custom_5h = parse_token_amount(getattr(args, "five_h_limit", None))
    custom_weekly = parse_token_amount(getattr(args, "weekly_limit", None))

    w_5h, w_7d, tier_cfg = LimitsCalculator.compute(
        sessions, tier_name=tier, five_h_limit=custom_5h, weekly_limit=custom_weekly
    )

    badge, status_msg = get_status_badge(w_5h, w_7d)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # JSON export support
    if getattr(args, "json", False):
        import json
        payload = {
            "tier": tier_cfg.name,
            "checked_at": now_str,
            "status": badge,
            "pool": "Gemini Models",
            "five_hour_window": {
                "start": w_5h.start_time.isoformat(),
                "end": w_5h.end_time.isoformat(),
                "refresh_at": w_5h.refresh_time.isoformat() if w_5h.refresh_time else None,
                "total_tokens": w_5h.usage.total_tokens,
                "cached_tokens": w_5h.usage.cached_tokens,
                "prompt_tokens": w_5h.usage.prompt_tokens,
                "output_tokens": w_5h.usage.output_tokens,
                "billable_tokens": w_5h.usage.billable_tokens,
                "cache_hit_rate": round(w_5h.usage.cache_hit_rate, 2),
                "limit_tokens": w_5h.limit_tokens,
                "pct_used": round(w_5h.pct_used, 2),
                "pct_remaining": round(w_5h.pct_remaining, 2),
                "tokens_remaining": w_5h.tokens_remaining,
                "estimated_cost_usd": round(w_5h.estimated_cost, 4),
                "turns": w_5h.step_count,
                "sessions": w_5h.session_count,
            },
            "weekly_budget": {
                "start": w_7d.start_time.isoformat(),
                "end": w_7d.end_time.isoformat(),
                "refresh_at": w_7d.refresh_time.isoformat() if w_7d.refresh_time else None,
                "total_tokens": w_7d.usage.total_tokens,
                "cached_tokens": w_7d.usage.cached_tokens,
                "prompt_tokens": w_7d.usage.prompt_tokens,
                "output_tokens": w_7d.usage.output_tokens,
                "billable_tokens": w_7d.usage.billable_tokens,
                "cache_hit_rate": round(w_7d.usage.cache_hit_rate, 2),
                "limit_tokens": w_7d.limit_tokens,
                "pct_used": round(w_7d.pct_used, 2),
                "pct_remaining": round(w_7d.pct_remaining, 2),
                "tokens_remaining": w_7d.tokens_remaining,
                "estimated_cost_usd": round(w_7d.estimated_cost, 4),
                "turns": w_7d.step_count,
                "sessions": w_7d.session_count,
            },
        }
        print(json.dumps(payload, indent=2))
        return

    # CSV export support
    if getattr(args, "csv", False):
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Window", "Total Tokens", "Cached Tokens", "Billable Tokens", "Limit Tokens", "Pct Remaining", "Pct Used", "Tokens Remaining", "Est Cost USD", "Turns", "Sessions"])
        writer.writerow(["5-Hour Sprint", w_5h.usage.total_tokens, w_5h.usage.cached_tokens, w_5h.usage.billable_tokens, w_5h.limit_tokens, f"{w_5h.pct_remaining:.1f}%", f"{w_5h.pct_used:.1f}%", w_5h.tokens_remaining, f"{w_5h.estimated_cost:.4f}", w_5h.step_count, w_5h.session_count])
        writer.writerow(["Weekly Cap", w_7d.usage.total_tokens, w_7d.usage.cached_tokens, w_7d.usage.billable_tokens, w_7d.limit_tokens, f"{w_7d.pct_remaining:.1f}%", f"{w_7d.pct_used:.1f}%", w_7d.tokens_remaining, f"{w_7d.estimated_cost:.4f}", w_7d.step_count, w_7d.session_count])
        print(buf.getvalue().strip())
        return

    print()
    print(Colors.bold(Colors.cyan("── ANTIGRAVITY QUOTA & RATE LIMITS MONITOR " + "─" * 38)))
    print(f"  {Colors.bold('Model Pool:')}     Gemini Models (Flash 3.8 / 3.7 / Pro 3.1)")
    print(f"  {Colors.bold('Subscription:')}   {tier_cfg.display_name} ({tier_cfg.description})")
    print(f"  {Colors.bold('Checked At:')}     {now_str}")
    print(f"  {Colors.bold('System Status:')}  {badge} {status_msg}")
    print()

    # Recovery countdowns for KPI cards
    w5_countdown = "Ready"
    if w_5h.refresh_time and w_5h.usage.total_tokens > 0:
        secs_5h = (w_5h.refresh_time - datetime.now()).total_seconds()
        if secs_5h > 0:
            w5_countdown = f"in {format_duration(secs_5h)}"

    w7_countdown = "Ready"
    if w_7d.refresh_time and w_7d.usage.total_tokens > 0:
        secs_7d = (w_7d.refresh_time - datetime.now()).total_seconds()
        if secs_7d > 0:
            w7_countdown = f"in {format_duration(secs_7d)}"

    # 1. KPI Cards (Matching Official Google Antigravity UI Remaining Cards)
    rem_5h_color = Colors.green if w_5h.pct_remaining >= 40.0 else (Colors.yellow if w_5h.pct_remaining >= 15.0 else Colors.red)
    rem_7d_color = Colors.green if w_7d.pct_remaining >= 40.0 else (Colors.yellow if w_7d.pct_remaining >= 15.0 else Colors.red)

    card1 = (
        "5-HOUR REMAINING",
        Colors.bold(rem_5h_color(f"{w_5h.pct_remaining:.0f}%")),
        f"{format_tokens(w_5h.tokens_remaining)} left ({w_5h.pct_used:.0f}% used)",
    )
    card2 = (
        "5-HOUR REFRESH",
        Colors.bold(Colors.cyan(w5_countdown)),
        f"{format_tokens(w_5h.usage.total_tokens)} / {format_tokens(w_5h.limit_tokens)}",
    )
    card3 = (
        "WEEKLY REMAINING",
        Colors.bold(rem_7d_color(f"{w_7d.pct_remaining:.0f}%")),
        f"{format_tokens(w_7d.tokens_remaining)} left ({w_7d.pct_used:.0f}% used)",
    )
    card4 = (
        "WEEKLY REFRESH",
        Colors.bold(Colors.cyan(w7_countdown)),
        f"{format_tokens(w_7d.usage.total_tokens)} / {format_tokens(w_7d.limit_tokens)}",
    )
    print(render_kpi_grid([card1, card2, card3, card4], card_width=28))
    print()

    # 2. Five Hour Limit Remaining Details
    print(Colors.bold(Colors.cyan("── FIVE HOUR LIMIT REMAINING (Gemini Models) " + "─" * 35)))
    w5_start = w_5h.start_time.strftime("%Y-%m-%d %H:%M:%S")
    w5_end = w_5h.end_time.strftime("%H:%M:%S")
    bar_5h = render_gauge_bar(w_5h.pct_used, width=24)
    print(f"  {Colors.bold('Limit Remaining:')}  {bar_5h} {format_percent(w_5h.pct_remaining)} Remaining ({format_percent(w_5h.pct_used)} Used)")
    print(f"  {Colors.bold('Quota Balance:')}    {format_tokens(w_5h.tokens_remaining)} remaining of {format_tokens(w_5h.limit_tokens)} allowance")
    print(f"  {Colors.bold('Tokens Consumed:')}  {format_tokens(w_5h.usage.total_tokens)} total ({format_tokens(w_5h.usage.billable_tokens)} billable, {format_tokens(w_5h.usage.cached_tokens)} cached @ {format_percent(w_5h.usage.cache_hit_rate)} hit rate)")
    print(f"  {Colors.bold('Activity Volume:')}  {w_5h.step_count} turns across {w_5h.session_count} session(s) ({format_cost(w_5h.estimated_cost)})")

    # Refresh countdown
    if w_5h.refresh_time and w_5h.usage.total_tokens > 0:
        secs_left = (w_5h.refresh_time - datetime.now()).total_seconds()
        if secs_left > 0:
            refresh_time_str = w_5h.refresh_time.strftime("%H:%M:%S")
            print(f"  {Colors.bold('Refresh Status:')}   {Colors.yellow(f'You have used some of your 5-hour limit, it will fully refresh in {format_duration_verbose(secs_left)}')} (at {refresh_time_str})")
        else:
            print(f"  {Colors.bold('Refresh Status:')}   {Colors.green('100% capacity available (no active turns in the last 5 hours)')}")
    else:
        print(f"  {Colors.bold('Refresh Status:')}   {Colors.green('100% capacity available (no active turns in the last 5 hours)')}")
    print()

    # 3. Weekly Limit Remaining Details
    print(Colors.bold(Colors.cyan("── WEEKLY LIMIT REMAINING (Gemini Models) " + "─" * 38)))
    w7_start = w_7d.start_time.strftime("%Y-%m-%d %H:%M:%S")
    w7_end = w_7d.end_time.strftime("%H:%M:%S")
    bar_7d = render_gauge_bar(w_7d.pct_used, width=24)
    print(f"  {Colors.bold('Limit Remaining:')}  {bar_7d} {format_percent(w_7d.pct_remaining)} Remaining ({format_percent(w_7d.pct_used)} Used)")
    print(f"  {Colors.bold('Quota Balance:')}    {format_tokens(w_7d.tokens_remaining)} remaining of {format_tokens(w_7d.limit_tokens)} allowance")
    print(f"  {Colors.bold('Tokens Consumed:')}  {format_tokens(w_7d.usage.total_tokens)} total ({format_tokens(w_7d.usage.billable_tokens)} billable, {format_tokens(w_7d.usage.cached_tokens)} cached @ {format_percent(w_7d.usage.cache_hit_rate)} hit rate)")
    print(f"  {Colors.bold('Activity Volume:')}  {w_7d.step_count} turns across {w_7d.session_count} session(s) ({format_cost(w_7d.estimated_cost)})")

    if w_7d.refresh_time and w_7d.usage.total_tokens > 0:
        secs_left_7d = (w_7d.refresh_time - datetime.now()).total_seconds()
        if secs_left_7d > 0:
            print(f"  {Colors.bold('Refresh Status:')}   {Colors.cyan(f'You have used some of your weekly limit, it will fully refresh in {format_duration_verbose(secs_left_7d)}.')}")
        else:
            print(f"  {Colors.bold('Refresh Status:')}   {Colors.green('Weekly limit is currently at full capacity')}")
    else:
        print(f"  {Colors.bold('Refresh Status:')}   {Colors.green('Weekly limit is currently at full capacity (100% available)')}")
    print()

    # 4. Tier Comparison Table
    t_headers = ["Tier", "5-Hour Cap", "Weekly Cap", "Models Supported", "Status"]
    t_rows = []
    for k, t_item in TIERS.items():
        is_active = (k == tier_cfg.name)
        status_cell = Colors.bold(Colors.green("● ACTIVE")) if is_active else Colors.dim("Available")
        t_rows.append([
            Colors.bold(t_item.name.upper()),
            format_tokens(t_item.five_hour_limit),
            format_tokens(t_item.weekly_limit),
            "Flash only" if k == "free" else ("Flash + Pro" if k == "pro" else "All Models"),
            status_cell,
        ])
    print(render_table(t_headers, t_rows, alignments=["left", "right", "right", "left", "center"], title="ANTIGRAVITY TIER LIMITS"))
    print()
    print(Colors.dim("💡 Tip: The 5-hour limit refreshes rolling sprint capacity every 5 hours."))
    print(Colors.dim("   If you hit the weekly hard cap, Google locks out the agent until the 7-day cycle clears."))
    print(Colors.dim("   Use lighter Flash models and plan-first agent prompts to maximize quota efficiency."))


def cmd_statusline(sessions: List[SessionSummary], args: argparse.Namespace) -> None:
    """Display compact single-line quota status for shell prompt, tmux, or status bars."""
    tier = getattr(args, "tier", "pro") or "pro"
    custom_5h = parse_token_amount(getattr(args, "five_h_limit", None))
    custom_weekly = parse_token_amount(getattr(args, "weekly_limit", None))

    w_5h, w_7d, tier_cfg = LimitsCalculator.compute(
        sessions, tier_name=tier, five_h_limit=custom_5h, weekly_limit=custom_weekly
    )

    now = datetime.now()
    s5 = (w_5h.refresh_time - now).total_seconds() if (w_5h.refresh_time and w_5h.usage.total_tokens > 0) else 0
    rec_5h = f"in {format_duration(s5)}" if s5 > 0 else "Ready"

    s7 = (w_7d.refresh_time - now).total_seconds() if (w_7d.refresh_time and w_7d.usage.total_tokens > 0) else 0
    rec_7d = f"in {format_duration(s7)}" if s7 > 0 else "Ready"

    rem_5h = round(w_5h.pct_remaining)
    rem_7d = round(w_7d.pct_remaining)

    # Today's consumption
    daily = UsageAggregator.aggregate_daily(sessions)
    today_usage = daily.get(now.date())
    if today_usage:
        tok_today = today_usage.usage.total_tokens
        _, _, _, cost_today = calculate_cost(
            today_usage.usage.prompt_tokens,
            today_usage.usage.cached_tokens,
            today_usage.usage.output_tokens,
        )
    else:
        tok_today = 0
        cost_today = 0.0

    tok_today_str = format_tokens(tok_today)
    cost_today_str = format_cost(cost_today)

    # Health status level & badge
    if rem_5h <= 0 or rem_7d <= 0 or w_5h.is_exceeded or w_7d.is_exceeded:
        badge = "🔴"
        badge_plain = "[ALERT]"
        level = "critical"
        color_5h = Colors.red
        color_7d = Colors.red
        tmux_color = "red"
    elif rem_5h < 20 or rem_7d < 20:
        badge = "🟡"
        badge_plain = "[WARN]"
        level = "warning"
        color_5h = Colors.yellow if rem_5h < 20 else Colors.green
        color_7d = Colors.yellow if rem_7d < 20 else Colors.green
        tmux_color = "yellow"
    else:
        badge = "🟢"
        badge_plain = "[OK]"
        level = "healthy"
        color_5h = Colors.green
        color_7d = Colors.green
        tmux_color = "green"

    # JSON export
    if getattr(args, "json", False):
        import json
        payload = {
            "status": level,
            "badge": badge,
            "tier": tier_cfg.name,
            "five_hour_pct": rem_5h,
            "five_hour_refresh": rec_5h,
            "five_hour_used_tokens": w_5h.usage.total_tokens,
            "five_hour_limit_tokens": w_5h.limit_tokens,
            "weekly_pct": rem_7d,
            "weekly_refresh": rec_7d,
            "weekly_used_tokens": w_7d.usage.total_tokens,
            "weekly_limit_tokens": w_7d.limit_tokens,
            "today_tokens": tok_today,
            "today_tokens_str": tok_today_str,
            "today_cost_usd": round(cost_today, 4),
            "today_cost_str": cost_today_str,
        }
        print(json.dumps(payload, indent=2))
        return

    # Custom template format
    custom_format = getattr(args, "format", None)
    if custom_format:
        res = custom_format
        replacements = {
            "{status}": badge_plain if getattr(args, "plain", False) or not Colors.enabled() else badge,
            "{badge}": badge_plain if getattr(args, "plain", False) or not Colors.enabled() else badge,
            "{5h_pct}": f"{rem_5h}",
            "{5h_refresh}": rec_5h,
            "{wk_pct}": f"{rem_7d}",
            "{wk_refresh}": rec_7d,
            "{today_tokens}": tok_today_str,
            "{today_cost}": cost_today_str,
            "{tier}": tier_cfg.name,
        }
        for k, v in replacements.items():
            res = res.replace(k, str(v))
        print(res)
        return

    # Tmux mode
    if getattr(args, "tmux", False):
        print(
            f"#[fg={tmux_color}]● #[fg=white]5h: {rem_5h}% #[fg=colour244]({rec_5h}) "
            f"#[fg=colour238]| #[fg=white]Wk: {rem_7d}% #[fg=colour244]({rec_7d}) "
            f"#[fg=colour238]| #[fg=white]Today: {tok_today_str} #[fg=green]({cost_today_str})"
        )
        return

    # Plain mode (no colors or emojis)
    is_plain = getattr(args, "plain", False) or not Colors.enabled() or getattr(args, "no_color", False)
    if is_plain:
        if getattr(args, "short", False):
            print(f"{badge_plain} 5h:{rem_5h}% Wk:{rem_7d}% {cost_today_str}")
        else:
            print(f"{badge_plain} 5h: {rem_5h}% ({rec_5h}) | Wk: {rem_7d}% ({rec_7d}) | Today: {tok_today_str} ({cost_today_str})")
        return

    # Default ANSI Colored mode
    if getattr(args, "short", False):
        print(f"{badge} {color_5h(f'5h:{rem_5h}%')} {color_7d(f'Wk:{rem_7d}%')} {Colors.green(cost_today_str)}")
    else:
        print(
            f"{badge} {color_5h('5h:')} {color_5h(f'{rem_5h}%')} {Colors.dim(f'({rec_5h})')} | "
            f"{color_7d('Wk:')} {color_7d(f'{rem_7d}%')} {Colors.dim(f'({rec_7d})')} | "
            f"Today: {tok_today_str} {Colors.green(f'({cost_today_str})')}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for CLI execution."""
    args = parse_args(argv)

    if args.no_color:
        # Disable ANSI
        Colors.enabled = classmethod(lambda cls: False)

    sessions = get_sessions(args)
    sub = args.subcommand

    # Subcommands that handle their own formatting and exports
    if sub == "limits":
        cmd_limits(sessions, args)
        return 0
    elif sub == "statusline":
        cmd_statusline(sessions, args)
        return 0

    # Handle global export options for other commands
    if args.json:
        UsageExporter.export(sessions, format_type="json")
        return 0
    elif args.csv:
        UsageExporter.export(sessions, format_type="csv")
        return 0

    if sub is None or sub == "overview":
        cmd_overview(sessions, args)
    elif sub == "heatmap":
        cmd_heatmap(sessions, args)
    elif sub == "sessions":
        cmd_sessions(sessions, args)
    elif sub == "current":
        turns = getattr(args, "turns", None)
        details = getattr(args, "details", False)
        cmd_session_inspect(sessions, "current", turns_limit=turns, show_details=details)
    elif sub == "session":
        turns = getattr(args, "turns", None)
        details = getattr(args, "details", False)
        cmd_session_inspect(sessions, args.session_id, turns_limit=turns, show_details=details)
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
