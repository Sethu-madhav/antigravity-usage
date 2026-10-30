"""Main CLI interface for Antigravity Model Usage Tracker."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Callable, List, Optional

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
    CRIT,
    TIERS,
    WARN_MODERATE,
    LimitsCalculator,
    WindowStats,
    get_status_badge,
    parse_token_amount,
    render_gauge_bar,
    render_remaining_bar,
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
    dir_mx = dir_group.add_mutually_exclusive_group()
    dir_mx.add_argument(
        "--dir",
        dest="custom_dir",
        metavar="PATH",
        help="Custom directory containing Antigravity conversation SQLite databases",
    )
    dir_mx.add_argument(
        "--cli-only",
        action="store_true",
        help="Scan Antigravity CLI conversations directory only",
    )
    dir_mx.add_argument(
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

    # Shared flags accepted before OR after any subcommand. The subparser
    # copies use default=argparse.SUPPRESS so an explicit root-level value
    # (e.g. `agy-usage --json limits`) survives namespace merging instead of
    # being clobbered by the subparser's own defaults.
    common = argparse.ArgumentParser(add_help=False)
    common_dir_group = common.add_argument_group("Data Source Options")
    common_dir_mx = common_dir_group.add_mutually_exclusive_group()
    common_dir_mx.add_argument(
        "--dir",
        dest="custom_dir",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="Custom directory containing Antigravity conversation SQLite databases",
    )
    common_dir_mx.add_argument(
        "--cli-only",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Scan Antigravity CLI conversations directory only",
    )
    common_dir_mx.add_argument(
        "--ide-only",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Scan Antigravity IDE conversations directory only",
    )
    common_out_group = common.add_argument_group("Output Formatting")
    common_out_group.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Output results as JSON"
    )
    common_out_group.add_argument(
        "--csv", action="store_true", default=argparse.SUPPRESS, help="Output session summaries as CSV"
    )
    common_out_group.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS, help="Disable ANSI color output"
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to run")

    # 1. overview / dashboard (default)
    sub_overview = subparsers.add_parser(
        "overview", help="Dashboard overview with KPI cards and heatmap (default)",
        parents=[common],
    )

    # 2. heatmap
    sub_heatmap = subparsers.add_parser(
        "heatmap", help="GitHub-style terminal contribution activity heatmap",
        parents=[common],
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
        "sessions", help="List sessions with token and cache breakdown",
        parents=[common],
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
        "current", help="Show token usage totals for the most recent session",
        parents=[common],
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
        "session", help="Inspect token usage totals for a specific session",
        parents=[common],
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
        "weekly", help="Weekly token usage table and volume chart", parents=[common]
    )

    # 6. monthly
    subparsers.add_parser(
        "monthly", help="Monthly token usage table and volume chart", parents=[common]
    )

    # 7. models
    subparsers.add_parser(
        "models", help="Token breakdown and distribution by model", parents=[common]
    )

    # 8. live / watch mode
    sub_live = subparsers.add_parser(
        "live", help="Live watch mode: automatically refresh metrics",
        parents=[common],
    )
    sub_live.add_argument(
        "--interval",
        type=int,
        default=3,
        help="Refresh interval in seconds (default: 3)",
    )

    # 9. limits
    sub_limits = subparsers.add_parser(
        "limits", help="Show current 5-hour rolling limit and weekly quota limits",
        parents=[common],
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
        help="Custom Gemini 5-hour limit in tokens (e.g. 50M, 100M)",
    )
    sub_limits.add_argument(
        "--weekly-limit",
        dest="weekly_limit",
        help="Custom Gemini weekly limit in tokens (e.g. 250M, 750M)",
    )
    sub_limits.add_argument(
        "--argon-weekly-limit",
        dest="argon_weekly_limit",
        help="Custom Gemini 4 Argon weekly limit in tokens (e.g. 25M, 50M)",
    )
    sub_limits.add_argument(
        "--argon-remaining-pct",
        dest="argon_remaining_pct",
        type=float,
        help="Calibrate Argon weekly cap from the 'Remaining %%' shown in Antigravity UI",
    )
    sub_limits.add_argument(
        "--claude-5h-limit",
        dest="claude_five_h_limit",
        help="Custom Claude 5-hour limit in tokens (e.g. 25M)",
    )
    sub_limits.add_argument(
        "--claude-weekly-limit",
        dest="claude_weekly_limit",
        help="Custom Claude weekly limit in tokens (e.g. 150M)",
    )

    # 10. statusline
    sub_statusline = subparsers.add_parser(
        "statusline", help="Compact single-line quota status for shell prompt or tmux",
        parents=[common],
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
        help="Custom Gemini 5-hour limit in tokens (e.g. 50M, 100M)",
    )
    sub_statusline.add_argument(
        "--weekly-limit",
        dest="weekly_limit",
        help="Custom Gemini weekly limit in tokens (e.g. 250M, 750M)",
    )
    sub_statusline.add_argument(
        "--argon-weekly-limit",
        dest="argon_weekly_limit",
        help="Custom Gemini 4 Argon weekly limit in tokens (e.g. 25M, 50M)",
    )
    sub_statusline.add_argument(
        "--argon-remaining-pct",
        dest="argon_remaining_pct",
        type=float,
        help="Calibrate Argon weekly cap from the 'Remaining %%' shown in Antigravity UI",
    )
    sub_statusline.add_argument(
        "--claude-5h-limit",
        dest="claude_five_h_limit",
        help="Custom Claude 5-hour limit in tokens (e.g. 25M)",
    )
    sub_statusline.add_argument(
        "--claude-weekly-limit",
        dest="claude_weekly_limit",
        help="Custom Claude weekly limit in tokens (e.g. 150M)",
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
        "-f",
        "--format",
        help="Custom template format string (e.g. '{status} 5h:{5h_pct}%% | Wk:{wk_pct}%% | {today_cost}')",
    )

    parsed = parser.parse_args(args)
    active_sources = [
        flag
        for flag, val in (
            ("--dir", bool(getattr(parsed, "custom_dir", None))),
            ("--cli-only", bool(getattr(parsed, "cli_only", False))),
            ("--ide-only", bool(getattr(parsed, "ide_only", False))),
        )
        if val
    ]
    if len(active_sources) > 1:
        parser.error(f"argument {active_sources[1]}: not allowed with argument {active_sources[0]}")
    if hasattr(parsed, "weeks") and parsed.weeks is not None:
        parsed.weeks = max(4, min(52, parsed.weeks))
    return parsed


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


def cmd_overview(sessions: List[SessionSummary], args: argparse.Namespace) -> int:
    """Display default dashboard overview."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."), file=sys.stderr)
        return 1

    summary = UsageAggregator.compute_summary(sessions)
    u = summary["total_usage"]
    tot_cost_bd = summary["total_cost"]
    bill_c = tot_cost_bd.billable
    tot_c = tot_cost_bd.total

    # 1. Header & KPI Cards
    card1 = (
        "TOTAL PROCESSED",
        Colors.bold(Colors.green(format_tokens(u.total_tokens))),
        f"{format_tokens(u.total_prompt_tokens)} in / {format_tokens(u.output_tokens)} out",
    )
    card2 = (
        "CACHE SAVINGS",
        f"{Colors.bold(Colors.green(format_tokens(u.cached_tokens)))} {Colors.dim(f'({format_cost(tot_cost_bd.cache_savings)})')}",
        f"{format_percent(u.cache_hit_rate)} prompt hit rate",
    )
    card3 = (
        "BILLABLE CONSUMED",
        f"{Colors.bold(Colors.yellow(format_tokens(u.billable_tokens)))} {Colors.dim(f'({format_cost(bill_c)})')}",
        f"{format_tokens(u.prompt_tokens)} in + {format_tokens(u.output_tokens)} out",
    )
    sess_label = f"{summary['session_count']} sessions"
    card4 = (
        "ACTIVITY",
        f"{Colors.bold(Colors.cyan(sess_label))} {Colors.dim(f'({format_cost(tot_c)})')}",
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
        m_headers = ["Model", "Requests", "Prompt", "Cached", "Output", "Total", "Share", "Cost", "Cache %"]
        m_rows = []
        for m in models:
            mu = m.usage
            m_cost = m.cost.total
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
    return 0


def cmd_heatmap(sessions: List[SessionSummary], args: argparse.Namespace) -> int:
    """Render full GitHub-style activity heatmap."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."), file=sys.stderr)
        return 1

    daily_data = UsageAggregator.aggregate_daily(sessions)
    heatmap = TerminalHeatmap(
        daily_data,
        metric=args.metric,
        weeks=args.weeks,
    )

    if heatmap.is_terminal_narrow:
        print(Colors.yellow(
            f"Warning: Terminal width ({heatmap.term_cols}) is narrower than heatmap ({heatmap.required_cols} columns); output may wrap."
        ), file=sys.stderr)
    print()
    print(Colors.bold(Colors.cyan(f"── GITHUB-STYLE CONTRIBUTION HEATMAP ({args.metric.upper()} TOKENS) " + "──" * 25)))
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
    return 0


def cmd_sessions(sessions: List[SessionSummary], args: argparse.Namespace) -> int:
    """List session summaries with token breakdowns."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."), file=sys.stderr)
        return 1

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
        s_cost = s.cost.total
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
    return 0


def cmd_session_inspect(
    sessions: List[SessionSummary],
    session_id_arg: str = "current",
    turns_limit: Optional[int] = None,
    show_details: bool = False,
) -> int:
    """Inspect a session's token totals and optionally turn-by-turn details."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."), file=sys.stderr)
        return 1

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
        print(Colors.red(f"Session '{session_id_arg}' not found."), file=sys.stderr)
        print(Colors.dim("Run 'agy-usage sessions' to see all available session IDs."), file=sys.stderr)
        return 1

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

    # Cost calculations (summed per step so multi-model sessions are exact)
    cost_bd = matched.cost
    p_cost = cost_bd.prompt_cost
    c_cost = cost_bd.cached_cost
    o_cost = cost_bd.output_cost
    total_cost = cost_bd.total
    billable_cost = cost_bd.billable
    th_cost = cost_bd.thought_cost
    ca_cost = cost_bd.candidate_cost

    # 1. KPI Cards
    card1 = (
        "TOTAL PROCESSED",
        Colors.bold(Colors.green(format_tokens(u.total_tokens))),
        f"{format_tokens(u.total_prompt_tokens)} in / {format_tokens(u.output_tokens)} out",
    )
    card2 = (
        "CACHE SAVINGS",
        f"{Colors.bold(Colors.green(format_tokens(u.cached_tokens)))} {Colors.dim(f'({format_cost(cost_bd.cache_savings)})')}",
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
    return 0


def cmd_weekly(sessions: List[SessionSummary]) -> int:
    """Display weekly aggregated token table and volume bars."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."), file=sys.stderr)
        return 1

    weeks = UsageAggregator.aggregate_weekly(sessions)
    if not weeks:
        print(Colors.yellow("No weekly usage data found."), file=sys.stderr)
        return 1

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
    return 0


def cmd_monthly(sessions: List[SessionSummary]) -> int:
    """Display monthly aggregated token table and volume bars."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."), file=sys.stderr)
        return 1

    months = UsageAggregator.aggregate_monthly(sessions)
    if not months:
        print(Colors.yellow("No monthly usage data found."), file=sys.stderr)
        return 1

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
    return 0


def cmd_models(sessions: List[SessionSummary]) -> int:
    """Display breakdown by model."""
    if not sessions:
        print(Colors.yellow("No Antigravity conversation sessions found."), file=sys.stderr)
        return 1

    models = UsageAggregator.aggregate_models(sessions)
    if not models:
        print(Colors.yellow("No model usage data found."), file=sys.stderr)
        return 1

    max_tokens = max(m.usage.total_tokens for m in models)

    headers = ["Model", "Sessions", "Requests", "Prompt", "Cached", "Output", "Thoughts", "Total Tokens", "Share", "Cost", "Cache %", "Volume"]
    rows = []
    for m in models:
        mu = m.usage
        bar = render_bar(mu.total_tokens, max_tokens, width=14)
        m_cost = m.cost.total
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
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Live monitor mode."""
    if getattr(args, "csv", False):
        print(
            "agy-usage live: error: --csv is not supported by 'live' (use --json for streaming NDJSON)",
            file=sys.stderr,
        )
        return 2

    interval = max(1, getattr(args, "interval", 3))
    is_json = getattr(args, "json", False)

    if not is_json:
        print(Colors.cyan(f"Starting Antigravity Usage live monitor (refreshing every {interval}s). Press Ctrl+C to exit."))
    try:
        while True:
            sessions = get_sessions(args)
            if is_json:
                sys.stdout.write(UsageExporter.to_json(sessions, indent=None) + "\n")
                sys.stdout.flush()
            else:
                # Clear screen
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"{Colors.bold(Colors.cyan('ANTIGRAVITY LIVE MONITOR'))}  {Colors.dim(f'Last updated: {now_str}')}")
                print()

                cmd_overview(sessions, args)

            time.sleep(interval)
    except KeyboardInterrupt:
        if not is_json:
            print()
            print(Colors.dim("Live monitor stopped."))
    return 0


def _window_to_dict(w) -> dict:
    unknown = w.is_unknown
    return {
        "unknown": unknown,
        "start": w.start_time.isoformat(),
        "end": w.end_time.isoformat(),
        "refresh_at": w.refresh_time.isoformat() if w.refresh_time else None,
        "next_relief_at": w.next_relief_time.isoformat() if w.next_relief_time else None,
        "total_tokens": w.usage.total_tokens,
        "cached_tokens": w.usage.cached_tokens,
        "prompt_tokens": w.usage.prompt_tokens,
        "output_tokens": w.usage.output_tokens,
        "billable_tokens": w.usage.billable_tokens,
        "cache_hit_rate": round(w.usage.cache_hit_rate, 2),
        "limit_tokens": w.limit_tokens,
        "pct_used": None if unknown else round(w.pct_used, 2),
        "pct_remaining": None if unknown else round(w.pct_remaining, 2),
        "tokens_remaining": None if unknown else w.tokens_remaining,
        "estimated_cost_usd": round(w.estimated_cost, 4),
        "turns": w.step_count,
        "sessions": w.session_count,
    }


def _csv_quota_cells(w) -> list:
    """Pct remaining, pct used and tokens remaining; empty when the window is unknown."""
    if w.is_unknown:
        return ["", "", ""]
    return [f"{w.pct_remaining:.1f}%", f"{w.pct_used:.1f}%", w.tokens_remaining]


def _cache_age_text(age_seconds: Optional[float]) -> str:
    """'8 min ago' style age of a cached server reading."""
    if age_seconds is None:
        return "age unknown"
    minutes = int(age_seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min ago" if minutes else f"{hours} h ago"


def _parse_limit_arg(raw: Optional[str], flag_name: str) -> Optional[int]:
    if raw is None:
        return None
    parsed_val = parse_token_amount(raw)
    if parsed_val is None:
        print(
            f"Warning: ignoring invalid {flag_name} value {raw!r}; falling back to tier default.",
            file=sys.stderr,
        )
    return parsed_val


def cmd_limits(sessions: List[SessionSummary], args: argparse.Namespace) -> int:
    """Display 5-hour rolling limit and weekly quota limits across Gemini, Argon, and Claude pools."""
    tier = getattr(args, "tier", "pro") or "pro"
    custom_5h = _parse_limit_arg(getattr(args, "five_h_limit", None), "--5h-limit")
    custom_weekly = _parse_limit_arg(getattr(args, "weekly_limit", None), "--weekly-limit")
    custom_argon_weekly = _parse_limit_arg(getattr(args, "argon_weekly_limit", None), "--argon-weekly-limit")
    argon_rem_pct = getattr(args, "argon_remaining_pct", None)
    custom_claude_5h = _parse_limit_arg(getattr(args, "claude_five_h_limit", None), "--claude-5h-limit")
    custom_claude_weekly = _parse_limit_arg(getattr(args, "claude_weekly_limit", None), "--claude-weekly-limit")

    now_dt = datetime.now()
    pools = LimitsCalculator.compute_all_pools(
        sessions,
        tier_name=tier,
        five_h_limit=custom_5h,
        weekly_limit=custom_weekly,
        argon_weekly_limit=custom_argon_weekly,
        argon_remaining_pct=argon_rem_pct,
        claude_five_h_limit=custom_claude_5h,
        claude_weekly_limit=custom_claude_weekly,
        now=now_dt,
    )
    w_5h = pools.gemini_5h
    w_7d = pools.gemini_7d
    argon_7d = pools.argon_7d
    claude_5h = pools.claude_5h
    claude_7d = pools.claude_7d
    tier_cfg = pools.tier

    badge, status_msg = get_status_badge(
        w_5h,
        w_7d,
        argon_7d=argon_7d,
        claude_5h=claude_5h,
        claude_7d=claude_7d,
    )
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    if getattr(args, "json", False) and getattr(args, "csv", False):
        print("Warning: both --json and --csv specified; using --json.", file=sys.stderr)

    # JSON export support
    if getattr(args, "json", False):
        import json
        payload = {
            "tier": tier_cfg.name,
            "checked_at": now_str,
            "live_synced": pools.live_synced,
            "live_error": pools.live_error,
            "quota_source": pools.quota_source,
            "cache_fetched_at": pools.cache_fetched_at.isoformat() if pools.cache_fetched_at else None,
            "cache_age_seconds": None if pools.cache_age_seconds is None else int(pools.cache_age_seconds),
            "status": badge,
            "pool": "Gemini Models",
            "five_hour_window": _window_to_dict(w_5h),
            "weekly_budget": _window_to_dict(w_7d),
            "argon_weekly_budget": _window_to_dict(argon_7d),
            "claude_pool": {
                "five_hour_window": _window_to_dict(claude_5h),
                "weekly_budget": _window_to_dict(claude_7d),
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    # CSV export support
    if getattr(args, "csv", False):
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Window", "Total Tokens", "Cached Tokens", "Billable Tokens", "Limit Tokens", "Pct Remaining", "Pct Used", "Tokens Remaining", "Est Cost USD", "Turns", "Sessions"])
        writer.writerow(["5-Hour Sprint", w_5h.usage.total_tokens, w_5h.usage.cached_tokens, w_5h.usage.billable_tokens, w_5h.limit_tokens, *_csv_quota_cells(w_5h), f"{w_5h.estimated_cost:.4f}", w_5h.step_count, w_5h.session_count])
        writer.writerow(["Weekly Cap", w_7d.usage.total_tokens, w_7d.usage.cached_tokens, w_7d.usage.billable_tokens, w_7d.limit_tokens, *_csv_quota_cells(w_7d), f"{w_7d.estimated_cost:.4f}", w_7d.step_count, w_7d.session_count])
        writer.writerow(["Argon Weekly Cap", argon_7d.usage.total_tokens, argon_7d.usage.cached_tokens, argon_7d.usage.billable_tokens, argon_7d.limit_tokens, *_csv_quota_cells(argon_7d), f"{argon_7d.estimated_cost:.4f}", argon_7d.step_count, argon_7d.session_count])
        writer.writerow(["Claude & GPT 5-Hour", claude_5h.usage.total_tokens, claude_5h.usage.cached_tokens, claude_5h.usage.billable_tokens, claude_5h.limit_tokens, *_csv_quota_cells(claude_5h), f"{claude_5h.estimated_cost:.4f}", claude_5h.step_count, claude_5h.session_count])
        writer.writerow(["Claude & GPT Weekly Cap", claude_7d.usage.total_tokens, claude_7d.usage.cached_tokens, claude_7d.usage.billable_tokens, claude_7d.limit_tokens, *_csv_quota_cells(claude_7d), f"{claude_7d.estimated_cost:.4f}", claude_7d.step_count, claude_7d.session_count])
        print(buf.getvalue().strip())
        return 0

    if pools.live_synced:
        sync_tag = Colors.green("Live Antigravity Server + Local DB")
    elif pools.quota_source == "cached":
        sync_tag = Colors.cyan(
            f"Last server reading, {_cache_age_text(pools.cache_age_seconds)} (Antigravity server not reachable now)"
        )
    elif pools.live_error is not None:
        sync_tag = Colors.yellow("Local estimate, this device only (Antigravity server not reachable)")
    else:
        sync_tag = Colors.dim("Local DB Cost-Proportional Model")
    print()
    print(Colors.bold(Colors.cyan("── ANTIGRAVITY QUOTA & RATE LIMITS MONITOR " + "─" * 38)))
    print(f"  {Colors.bold('Model Groups:')}   Gemini Models  |  Claude and GPT models  |  Gemini 4 Argon")
    print(f"  {Colors.bold('Subscription:')}   {tier_cfg.display_name} ({tier_cfg.description})")
    print(f"  {Colors.bold('Quota Source:')}   {sync_tag}")
    if pools.live_error is not None:
        print(f"    {Colors.dim('↳')} {Colors.dim(pools.live_error)}")
    print(f"  {Colors.bold('Checked At:')}     {now_str}")
    print(f"  {Colors.bold('System Status:')}  {badge} {status_msg}")
    print()

    def _countdown(w) -> str:
        if w.is_unknown:
            return "unknown"
        if w.refresh_time and (w.usage.total_tokens > 0 or w.pct_used > 0):
            secs = (w.refresh_time - now_dt).total_seconds()
            if secs > 0:
                return f"in {format_duration(secs)}"
        return "Ready"

    w5_countdown = _countdown(w_5h)
    w7_countdown = _countdown(w_7d)
    c5_countdown = _countdown(claude_5h)
    argon_countdown = _countdown(argon_7d)

    def _rem_color(pct_rem: float):
        if pct_rem >= 40.0:
            return Colors.green
        if pct_rem >= 15.0:
            return Colors.yellow
        return Colors.red

    def _rem_text(w) -> str:
        if w.is_unknown:
            return Colors.dim("?")
        return Colors.bold(_rem_color(w.pct_remaining)(f"{round(w.pct_remaining)}%"))

    def _remaining_cells(w) -> str:
        if w.is_unknown:
            return f"{Colors.dim('[' + '░' * 24 + ']')} {Colors.bold('?')} (unknown)"
        bar = render_remaining_bar(w.pct_remaining, width=24)
        return f"{bar} {Colors.bold(f'{round(w.pct_remaining)}%')} ({format_percent(w.pct_remaining)} exact | {format_percent(w.pct_used, high_is_good=False)} used)"

    # 1. KPI Summary Cards (Matching Official Antigravity Models & Usage Groups)
    card1 = (
        "5-HOUR REMAINING",
        _rem_text(w_5h),
        f"Gemini 5h ({w5_countdown})",
    )
    card2 = (
        "WEEKLY REMAINING",
        _rem_text(w_7d),
        f"Gemini Wk ({w7_countdown})",
    )
    claude_min_rem = min(claude_5h.pct_remaining, claude_7d.pct_remaining)
    card3 = (
        "CLAUDE & GPT QUOTA",
        f"5h: {_rem_text(claude_5h)} | Wk: {_rem_text(claude_7d)}"
        if tier_cfg.claude_weekly_limit > 0
        else Colors.dim("N/A"),
        f"5h resets {c5_countdown}" if claude_5h.is_exceeded else f"5h: {c5_countdown} ({format_cost(claude_7d.estimated_cost)})",
    )
    card4 = (
        "GEMINI 4 ARGON",
        _rem_text(argon_7d)
        if argon_7d.limit_tokens > 0
        else Colors.dim("N/A"),
        f"{format_tokens(argon_7d.usage.total_tokens)} used ({argon_countdown})"
        if argon_7d.limit_tokens > 0
        else "Unavailable on Free tier",
    )
    print(render_kpi_grid([card1, card2, card3, card4], card_width=28))
    print()

    def _format_refresh_status(w, window_type: str = "weekly", peer_5h=None) -> str:
        if w.is_unknown:
            return Colors.yellow(
                f"Unknown. The Antigravity server is not reachable, and this device has no usage in the current {window_type} cycle."
            )
        if peer_5h is not None and peer_5h.is_exceeded and peer_5h.refresh_time:
            secs_5h = max(0.0, (peer_5h.refresh_time - now_dt).total_seconds())
            return Colors.red(
                f"You have hit your 5-hour limit, so the weekly limit does not currently apply. Your 5-hour limit will refresh in {format_duration_verbose(secs_5h)}."
            )
        if w.is_exceeded and w.refresh_time:
            secs = max(0.0, (w.refresh_time - now_dt).total_seconds())
            return Colors.red(
                f"You have hit your {window_type} limit, it will refresh in {format_duration_verbose(secs)}. If on a supported paid plan, you can use AI credits in the interim."
            )
        if w.refresh_time and (w.usage.total_tokens > 0 or w.pct_used > 0):
            secs = (w.refresh_time - now_dt).total_seconds()
            if secs > 0:
                color_fn = Colors.yellow if window_type == "5-hour" else Colors.cyan
                return color_fn(
                    f"You have used some of your {window_type} limit, it will fully refresh in {format_duration_verbose(secs)}."
                )
        if w.server_description:
            return Colors.cyan(w.server_description)
        return Colors.green(f"100% capacity available (no active {window_type} usage in current cycle).")

    # 2. Group 1: Gemini Models (Weekly + 5-Hour, matching UI order)
    print(Colors.bold(Colors.cyan("── GEMINI MODELS (Gemini Flash & Gemini Pro) " + "─" * 36)))
    print(f"  {Colors.bold('Weekly Limit Remaining:')}     {_remaining_cells(w_7d)}")
    print(f"    {Colors.dim('↳')} {_format_refresh_status(w_7d, 'weekly', peer_5h=w_5h)}")
    print(f"  {Colors.bold('Five Hour Limit Remaining:')}  {_remaining_cells(w_5h)}")
    print(f"    {Colors.dim('↳')} {_format_refresh_status(w_5h, '5-hour')}")
    print(f"  {Colors.bold('Weekly Tokens Consumed:')}     {format_tokens(w_7d.usage.total_tokens)} total ({format_tokens(w_7d.usage.billable_tokens)} billable, {format_tokens(w_7d.usage.cached_tokens)} cached @ {format_percent(w_7d.usage.cache_hit_rate)} hit rate)")
    print(f"  {Colors.bold('Activity Volume:')}            {w_7d.step_count} turns across {w_7d.session_count} session(s) this week ({format_cost(w_7d.estimated_cost)}) | 5h: {w_5h.step_count} turns ({format_cost(w_5h.estimated_cost)})")
    print()

    # 3. Group 2: Claude and GPT models (Weekly + 5-Hour, matching UI order)
    print(Colors.bold(Colors.cyan("── CLAUDE AND GPT MODELS (Claude Opus, Claude Sonnet & GPT-OSS) " + "─" * 17)))
    if tier_cfg.claude_weekly_limit <= 0 and tier_cfg.name == "free":
        print(f"  {Colors.bold('Availability:')}               {Colors.yellow('Claude and GPT partner models require Google AI Pro or Ultra.')}")
    else:
        print(f"  {Colors.bold('Weekly Limit Remaining:')}     {_remaining_cells(claude_7d)}")
        print(f"    {Colors.dim('↳')} {_format_refresh_status(claude_7d, 'weekly', peer_5h=claude_5h)}")
        print(f"  {Colors.bold('Five Hour Limit Remaining:')}  {_remaining_cells(claude_5h)}")
        print(f"    {Colors.dim('↳')} {_format_refresh_status(claude_5h, '5-hour')}")
        print(f"  {Colors.bold('Weekly Tokens Consumed:')}     {format_tokens(claude_7d.usage.total_tokens)} total ({format_tokens(claude_7d.usage.billable_tokens)} billable, {format_tokens(claude_7d.usage.cached_tokens)} cached @ {format_percent(claude_7d.usage.cache_hit_rate)} hit rate)")
        print(f"  {Colors.bold('Activity Volume:')}            {claude_7d.step_count} turns across {claude_7d.session_count} session(s) this week ({format_cost(claude_7d.estimated_cost)}) | 5h: {claude_5h.step_count} turns ({format_cost(claude_5h.estimated_cost)})")
    print()

    # 4. Group 3: Gemini 4 Argon (Weekly-Only Pool)
    print(Colors.bold(Colors.cyan("── GEMINI 4 ARGON (Separate Weekly Quota Pool — No 5-Hour Limit) " + "─" * 16)))
    if argon_7d.limit_tokens <= 0 and tier_cfg.name == "free":
        print(f"  {Colors.bold('Availability:')}               {Colors.yellow('Gemini 4 Argon is not available on the Free tier (requires Google AI Pro or Ultra).')}")
    else:
        print(f"  {Colors.bold('Gemini 4 Argon Weekly Limit:')} {_remaining_cells(argon_7d)}")
        print(f"    {Colors.dim('↳')} {_format_refresh_status(argon_7d, 'weekly')}")
        argon_left = "unknown" if argon_7d.is_unknown else format_tokens(argon_7d.tokens_remaining)
        print(f"  {Colors.bold('Weekly Quota Balance:')}       {argon_left} remaining of ~{format_tokens(argon_7d.limit_tokens)} effective weekly allowance ({format_cost(argon_7d.estimated_cost)} / {format_cost(tier_cfg.argon_weekly_cost_usd)} cost cap)")
        print(f"  {Colors.bold('Weekly Tokens Consumed:')}     {format_tokens(argon_7d.usage.total_tokens)} total ({format_tokens(argon_7d.usage.billable_tokens)} billable, {format_tokens(argon_7d.usage.cached_tokens)} cached @ {format_percent(argon_7d.usage.cache_hit_rate)} hit rate)")
        print(f"  {Colors.bold('Activity Volume:')}            {argon_7d.step_count} turns across {argon_7d.session_count} session(s) ({format_cost(argon_7d.estimated_cost)})")
    print()

    # 5. Tier Comparison Table
    t_headers = ["Tier", "Gemini 5h Cap", "Gemini Weekly Cap", "Argon Weekly Cap", "Claude & GPT Weekly", "Models Supported", "Status"]
    t_rows = []
    for k, t_item in TIERS.items():
        is_active = (k == tier_cfg.name)
        status_cell = Colors.bold(Colors.green("● ACTIVE")) if is_active else Colors.dim("Available")
        t_rows.append([
            Colors.bold(t_item.name.upper()),
            f"~{format_tokens(t_item.five_hour_limit)} ({format_cost(t_item.five_hour_cost_usd)})",
            f"~{format_tokens(t_item.weekly_limit)} ({format_cost(t_item.weekly_cost_usd)})",
            f"~{format_tokens(t_item.argon_weekly_limit)} ({format_cost(t_item.argon_weekly_cost_usd)})" if t_item.argon_weekly_limit > 0 else "N/A",
            f"~{format_tokens(t_item.claude_weekly_limit)} ({format_cost(t_item.claude_weekly_cost_usd)})" if t_item.claude_weekly_limit > 0 else "N/A",
            "Flash only" if k == "free" else ("Gemini + Argon + Claude/GPT" if k == "pro" else "All Models (5x Quota)"),
            status_cell,
        ])
    print(render_table(t_headers, t_rows, alignments=["left", "right", "right", "right", "right", "left", "center"], title="ANTIGRAVITY TIER LIMITS"))
    print()
    print(Colors.dim("💡 Tip: Within each group, quota is consumed proportionally to the USD cost of the tokens."))
    print(Colors.dim("   Gemini 4 Argon uses an independent weekly-only quota pool (no 5-hour limit)."))
    print(Colors.dim("   Claude & GPT partner models share their own 5-hour and weekly quota pool."))
    return 0


def cmd_statusline(sessions: List[SessionSummary], args: argparse.Namespace) -> int:
    """Display compact single-line quota status for shell prompt, tmux, or status bars."""
    tier = getattr(args, "tier", "pro") or "pro"
    custom_5h = _parse_limit_arg(getattr(args, "five_h_limit", None), "--5h-limit")
    custom_weekly = _parse_limit_arg(getattr(args, "weekly_limit", None), "--weekly-limit")
    custom_argon_weekly = _parse_limit_arg(getattr(args, "argon_weekly_limit", None), "--argon-weekly-limit")
    argon_rem_pct = getattr(args, "argon_remaining_pct", None)
    custom_claude_5h = _parse_limit_arg(getattr(args, "claude_five_h_limit", None), "--claude-5h-limit")
    custom_claude_weekly = _parse_limit_arg(getattr(args, "claude_weekly_limit", None), "--claude-weekly-limit")

    now = datetime.now()
    pools = LimitsCalculator.compute_all_pools(
        sessions,
        tier_name=tier,
        five_h_limit=custom_5h,
        weekly_limit=custom_weekly,
        argon_weekly_limit=custom_argon_weekly,
        argon_remaining_pct=argon_rem_pct,
        claude_five_h_limit=custom_claude_5h,
        claude_weekly_limit=custom_claude_weekly,
        now=now,
    )
    w_5h = pools.gemini_5h
    w_7d = pools.gemini_7d
    argon_7d = pools.argon_7d
    claude_5h = pools.claude_5h
    claude_7d = pools.claude_7d
    tier_cfg = pools.tier

    s5 = (w_5h.refresh_time - now).total_seconds() if (w_5h.refresh_time and (w_5h.usage.total_tokens > 0 or w_5h.pct_used > 0)) else 0
    rec_5h = "unknown" if w_5h.is_unknown else (f"in {format_duration(s5)}" if s5 > 0 else "Ready")

    s7 = (w_7d.refresh_time - now).total_seconds() if (w_7d.refresh_time and (w_7d.usage.total_tokens > 0 or w_7d.pct_used > 0)) else 0
    rec_7d = "unknown" if w_7d.is_unknown else (f"in {format_duration(s7)}" if s7 > 0 else "Ready")

    s_argon = (argon_7d.refresh_time - now).total_seconds() if (argon_7d.refresh_time and (argon_7d.usage.total_tokens > 0 or argon_7d.pct_used > 0)) else 0
    rec_argon = "unknown" if argon_7d.is_unknown else (f"in {format_duration(s_argon)}" if s_argon > 0 else "Ready")

    pct_5h = None if w_5h.is_unknown else round(w_5h.pct_remaining)
    pct_7d = None if w_7d.is_unknown else round(w_7d.pct_remaining)
    pct_argon = None if argon_7d.is_unknown else round(argon_7d.pct_remaining)
    rem_5h = "?" if pct_5h is None else f"{pct_5h}%"
    rem_7d = "?" if pct_7d is None else f"{pct_7d}%"
    rem_argon = "?" if pct_argon is None else f"{pct_argon}%"
    rem_claude_5h = round(claude_5h.pct_remaining)
    rem_claude_7d = round(claude_7d.pct_remaining)

    # Today's consumption (priced per step across all models used today)
    daily = UsageAggregator.aggregate_daily(sessions)
    today_usage = daily.get(now.date())
    if today_usage:
        tok_today = today_usage.usage.total_tokens
        cost_today = today_usage.cost.total
    else:
        tok_today = 0
        cost_today = 0.0

    tok_today_str = format_tokens(tok_today)
    cost_today_str = format_cost(cost_today)

    # Health status level & badge (BUG-012: aligned with limits via get_status_badge & shared thresholds)
    badge, _ = get_status_badge(
        w_5h,
        w_7d,
        argon_7d=argon_7d,
        claude_5h=claude_5h,
        claude_7d=claude_7d,
    )
    if badge == "🔴":
        badge_plain = "[ALERT]"
        level = "critical"
        tmux_color = "red"
    elif badge == "🟡":
        badge_plain = "[WARN]"
        level = "warning"
        tmux_color = "yellow"
    elif badge == "⚪":
        badge_plain = "[UNKNOWN]"
        level = "unknown"
        tmux_color = "colour244"
    else:
        badge = "🟢"
        badge_plain = "[OK]"
        level = "healthy"
        tmux_color = "green"

    def _color_for_window(w: WindowStats) -> Callable[[str], str]:
        if w.is_unknown:
            return Colors.dim
        if w.is_exceeded or w.pct_used >= CRIT:
            return Colors.red
        if w.pct_used >= WARN_MODERATE:
            return Colors.yellow
        return Colors.green

    color_5h = _color_for_window(w_5h)
    color_7d = _color_for_window(w_7d)
    color_argon = _color_for_window(argon_7d)

    if getattr(args, "json", False) and getattr(args, "csv", False):
        print("Warning: both --json and --csv specified; using --json.", file=sys.stderr)

    # JSON export
    if getattr(args, "json", False):
        import json
        payload = {
            "status": level,
            "badge": badge,
            "tier": tier_cfg.name,
            "five_hour_pct": pct_5h,
            "five_hour_refresh": rec_5h,
            "five_hour_used_tokens": w_5h.usage.total_tokens,
            "five_hour_limit_tokens": w_5h.limit_tokens,
            "weekly_pct": pct_7d,
            "weekly_refresh": rec_7d,
            "weekly_used_tokens": w_7d.usage.total_tokens,
            "weekly_limit_tokens": w_7d.limit_tokens,
            "argon_weekly_pct": pct_argon,
            "argon_weekly_refresh": rec_argon,
            "argon_weekly_used_tokens": argon_7d.usage.total_tokens,
            "argon_weekly_limit_tokens": argon_7d.limit_tokens,
            "today_tokens": tok_today,
            "today_tokens_str": tok_today_str,
            "today_cost_usd": round(cost_today, 4),
            "today_cost_str": cost_today_str,
        }
        print(json.dumps(payload, indent=2))
        return 0

    # CSV export
    if getattr(args, "csv", False):
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        headers = [
            "status",
            "badge",
            "tier",
            "five_hour_pct",
            "five_hour_refresh",
            "five_hour_used_tokens",
            "five_hour_limit_tokens",
            "weekly_pct",
            "weekly_refresh",
            "weekly_used_tokens",
            "weekly_limit_tokens",
            "argon_weekly_pct",
            "argon_weekly_refresh",
            "argon_weekly_used_tokens",
            "argon_weekly_limit_tokens",
            "today_tokens",
            "today_tokens_str",
            "today_cost_usd",
            "today_cost_str",
        ]
        writer.writerow(headers)
        writer.writerow([
            level,
            badge,
            tier_cfg.name,
            pct_5h,
            rec_5h,
            w_5h.usage.total_tokens,
            w_5h.limit_tokens,
            pct_7d,
            rec_7d,
            w_7d.usage.total_tokens,
            w_7d.limit_tokens,
            pct_argon,
            rec_argon,
            argon_7d.usage.total_tokens,
            argon_7d.limit_tokens,
            tok_today,
            tok_today_str,
            round(cost_today, 4),
            cost_today_str,
        ])
        print(buf.getvalue().strip())
        return 0

    # Custom template format
    custom_format = getattr(args, "format", None)
    if custom_format:
        ignored = [
            f
            for f, active in (
                ("--tmux", getattr(args, "tmux", False)),
                ("--short", getattr(args, "short", False)),
            )
            if active
        ]
        if ignored:
            print(
                f"Warning: --format ignores {', '.join(ignored)}.",
                file=sys.stderr,
            )
        res = custom_format
        replacements = {
            "{status}": badge_plain if getattr(args, "plain", False) or not Colors.enabled() else badge,
            "{badge}": badge_plain if getattr(args, "plain", False) or not Colors.enabled() else badge,
            "{5h_pct}": rem_5h.rstrip("%"),
            "{5h_refresh}": rec_5h,
            "{wk_pct}": rem_7d.rstrip("%"),
            "{wk_refresh}": rec_7d,
            "{argon_pct}": rem_argon.rstrip("%"),
            "{argon_refresh}": rec_argon,
            "{today_tokens}": tok_today_str,
            "{today_cost}": cost_today_str,
            "{tier}": tier_cfg.name,
        }
        for k, v in replacements.items():
            res = res.replace(k, str(v))
        print(res)
        return 0

    has_argon = argon_7d.usage.total_tokens > 0

    # Tmux mode
    if getattr(args, "tmux", False):
        ignored = [
            f
            for f, active in (
                ("--short", getattr(args, "short", False)),
                ("--plain", getattr(args, "plain", False)),
            )
            if active
        ]
        if ignored:
            print(
                f"Warning: --tmux ignores {', '.join(ignored)}.",
                file=sys.stderr,
            )
        argon_seg = f"#[fg=colour238]| #[fg=white]Ar: {rem_argon} #[fg=colour244]({rec_argon}) " if has_argon else ""
        print(
            f"#[fg={tmux_color}]● #[fg=white]5h: {rem_5h} #[fg=colour244]({rec_5h}) "
            f"#[fg=colour238]| #[fg=white]Wk: {rem_7d} #[fg=colour244]({rec_7d}) "
            f"{argon_seg}"
            f"#[fg=colour238]| #[fg=white]Today: {tok_today_str} #[fg=green]({cost_today_str})"
        )
        return 0

    # Plain mode (no colors or emojis)
    is_plain = getattr(args, "plain", False) or not Colors.enabled() or getattr(args, "no_color", False)
    if is_plain:
        if getattr(args, "short", False):
            argon_short = f" Ar:{rem_argon}" if has_argon else ""
            print(f"{badge_plain} 5h:{rem_5h} Wk:{rem_7d}{argon_short} {cost_today_str}")
        else:
            argon_part = f" | Ar: {rem_argon} ({rec_argon})" if has_argon else ""
            print(f"{badge_plain} 5h: {rem_5h} ({rec_5h}) | Wk: {rem_7d} ({rec_7d}){argon_part} | Today: {tok_today_str} ({cost_today_str})")
        return 0

    # Default ANSI Colored mode
    if getattr(args, "short", False):
        argon_short = f" {color_argon(f'Ar:{rem_argon}')}" if has_argon else ""
        print(f"{badge} {color_5h(f'5h:{rem_5h}')} {color_7d(f'Wk:{rem_7d}')}{argon_short} {Colors.green(cost_today_str)}")
    else:
        argon_part = f"{color_argon('Ar:')} {color_argon(f'{rem_argon}')} {Colors.dim(f'({rec_argon})')} | " if has_argon else ""
        print(
            f"{badge} {color_5h('5h:')} {color_5h(f'{rem_5h}')} {Colors.dim(f'({rec_5h})')} | "
            f"{color_7d('Wk:')} {color_7d(f'{rem_7d}')} {Colors.dim(f'({rec_7d})')} | "
            f"{argon_part}"
            f"Today: {tok_today_str} {Colors.green(f'({cost_today_str})')}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for CLI execution."""
    args = parse_args(argv)

    # Reset the shared switch to auto-detection so repeated in-process calls
    # (e.g. tests, embedded use) never inherit a previous call's --no-color.
    Colors.set_override(None)
    if args.no_color:
        # Disable ANSI globally via the shared switch consulted by both
        # display.Colors and heatmap.HeatmapColor (BUG-004 fix).
        Colors.set_override(False)

    sub = args.subcommand

    # 'live' refreshes sessions on every tick and manages its own --json/--csv handling
    if sub == "live":
        return cmd_live(args)

    sessions = get_sessions(args)

    # Subcommands that handle their own formatting and exports
    if sub == "limits":
        return cmd_limits(sessions, args)
    elif sub == "statusline":
        return cmd_statusline(sessions, args)

    # Handle global export options for other commands
    if getattr(args, "json", False) and getattr(args, "csv", False):
        print("Warning: both --json and --csv specified; using --json.", file=sys.stderr)

    if args.json:
        UsageExporter.export(sessions, format_type="json")
        return 0
    elif args.csv:
        UsageExporter.export(sessions, format_type="csv")
        return 0

    if sub is None or sub == "overview":
        return cmd_overview(sessions, args)
    elif sub == "heatmap":
        return cmd_heatmap(sessions, args)
    elif sub == "sessions":
        return cmd_sessions(sessions, args)
    elif sub == "current":
        turns = getattr(args, "turns", None)
        details = getattr(args, "details", False)
        return cmd_session_inspect(sessions, "current", turns_limit=turns, show_details=details)
    elif sub == "session":
        turns = getattr(args, "turns", None)
        details = getattr(args, "details", False)
        return cmd_session_inspect(sessions, args.session_id, turns_limit=turns, show_details=details)
    elif sub == "weekly":
        return cmd_weekly(sessions)
    elif sub == "monthly":
        return cmd_monthly(sessions)
    elif sub == "models":
        return cmd_models(sessions)
    else:
        return cmd_overview(sessions, args)


if __name__ == "__main__":
    sys.exit(main())
