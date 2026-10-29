"""Terminal display formatting utilities: tables, KPI cards, bars, and colors."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, List, Optional, Tuple


class Colors:
    """ANSI color codes for terminal styling."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Bright variants
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"
    GRAY = "\033[90m"

    @classmethod
    def enabled(cls) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        return sys.stdout.isatty() or os.environ.get("COLORTERM") is not None

    @classmethod
    def color(cls, text: str, code: str) -> str:
        if not cls.enabled():
            return text
        return f"{code}{text}{cls.RESET}"

    @classmethod
    def bold(cls, text: str) -> str:
        return cls.color(text, cls.BOLD)

    @classmethod
    def dim(cls, text: str) -> str:
        return cls.color(text, cls.DIM)

    @classmethod
    def green(cls, text: str) -> str:
        return cls.color(text, cls.BRIGHT_GREEN)

    @classmethod
    def red(cls, text: str) -> str:
        return cls.color(text, cls.RED)

    @classmethod
    def cyan(cls, text: str) -> str:
        return cls.color(text, cls.BRIGHT_CYAN)

    @classmethod
    def yellow(cls, text: str) -> str:
        return cls.color(text, cls.BRIGHT_YELLOW)

    @classmethod
    def magenta(cls, text: str) -> str:
        return cls.color(text, cls.MAGENTA)

    @classmethod
    def gray(cls, text: str) -> str:
        return cls.color(text, cls.GRAY)


def format_tokens(num: int, compact: bool = True) -> str:
    """Format token count with optional compact metric suffix (M, K)."""
    if not compact or abs(num) < 1000:
        return f"{num:,}"
    if abs(num) >= 1_000_000_000:
        return f"{num / 1_000_000_000:.2f}B"
    if abs(num) >= 1_000_000:
        return f"{num / 1_000_000:.2f}M"
    if abs(num) >= 1_000:
        return f"{num / 1_000:.1f}K"
    return f"{num:,}"


def format_percent(val: float) -> str:
    """Format a percentage value with color coding."""
    pct_str = f"{val:.1f}%"
    if val >= 75.0:
        return Colors.green(pct_str)
    elif val >= 40.0:
        return Colors.yellow(pct_str)
    return Colors.gray(pct_str)


def format_cost(val: float) -> str:
    """Format USD cost with appropriate decimal precision."""
    if val <= 0:
        return "$0.00"
    if val < 0.001:
        return f"${val:.4f}"
    if val < 0.01:
        return f"${val:.3f}"
    return f"${val:.2f}"


def format_duration(seconds: float) -> str:
    """Format duration in seconds into human-readable compact time (e.g. '6d 16h', '4h 56m')."""
    if seconds <= 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def format_duration_verbose(seconds: float) -> str:
    """Format duration in sentence style matching Google Antigravity UI (e.g. '6 days, 16 hours')."""
    if seconds <= 0:
        return "0 minutes"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d > 0:
        parts.append(f"{d} day{'s' if d != 1 else ''}")
    if h > 0:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if not d and m > 0:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if not d and not h and s > 0:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return ", ".join(parts) if parts else "0 minutes"



def render_bar(val: float, max_val: float, width: int = 20, color_func=None) -> str:
    """Render a horizontal Unicode bar chart."""
    if max_val <= 0 or val <= 0:
        return " " * width
    ratio = min(1.0, val / max_val)
    total_blocks = ratio * width
    full_blocks = int(total_blocks)
    remainder = total_blocks - full_blocks

    sub_blocks = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█"]
    rem_char = sub_blocks[int(remainder * 8)] if full_blocks < width else ""

    bar_str = ("█" * full_blocks) + rem_char
    bar_str = bar_str.ljust(width)

    if color_func and Colors.enabled():
        return color_func(bar_str)
    return Colors.cyan(bar_str)


def render_sparkline(values: List[float]) -> str:
    """Render an inline sparkline string from a series of numbers."""
    if not values:
        return ""
    ticks = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    min_v = min(values)
    max_v = max(values)
    span = max_v - min_v
    if span == 0:
        return ticks[3] * len(values)

    res = []
    for v in values:
        idx = int(((v - min_v) / span) * 7)
        idx = max(0, min(7, idx))
        res.append(ticks[idx])
    return "".join(res)


def render_table(
    headers: List[str],
    rows: List[List[Any]],
    alignments: Optional[List[str]] = None,
    title: Optional[str] = None,
) -> str:
    """Render a modern Unicode box-drawing table."""
    if not headers:
        return ""

    num_cols = len(headers)
    if not alignments:
        alignments = ["left"] * num_cols

    # Calculate max column widths
    str_rows: List[List[str]] = []
    col_widths = [len(h) for h in headers]

    for row in rows:
        row_strs = []
        for i, cell in enumerate(row):
            s = str(cell)
            # Strip ANSI escape codes when computing visual string width
            visible_len = len(strip_ansi(s))
            if visible_len > col_widths[i]:
                col_widths[i] = visible_len
            row_strs.append(s)
        str_rows.append(row_strs)

    # Box-drawing characters
    TOP_L, TOP_R = "┌", "┐"
    BOT_L, BOT_R = "└", "┘"
    MID_L, MID_R = "├", "┤"
    VERT, HORIZ = "│", "─"
    CROSS = "┼"
    TOP_DIV = "┬"
    BOT_DIV = "┴"

    top_border = TOP_L + TOP_DIV.join(HORIZ * (w + 2) for w in col_widths) + TOP_R
    mid_border = MID_L + CROSS.join(HORIZ * (w + 2) for w in col_widths) + MID_R
    bot_border = BOT_L + BOT_DIV.join(HORIZ * (w + 2) for w in col_widths) + BOT_R

    lines: List[str] = []

    if title:
        lines.append(Colors.bold(Colors.cyan(f"── {title} " + "─" * max(0, sum(col_widths) + (num_cols * 3) - len(title) - 4))))

    lines.append(Colors.dim(top_border))

    # Header row
    h_cells = []
    for i, h in enumerate(headers):
        w = col_widths[i]
        align = alignments[i]
        if align == "right":
            padded = h.rjust(w)
        elif align == "center":
            padded = h.center(w)
        else:
            padded = h.ljust(w)
        h_cells.append(f" {Colors.bold(padded)} ")
    lines.append(f"{Colors.dim(VERT)}{f'{Colors.dim(VERT)}'.join(h_cells)}{Colors.dim(VERT)}")
    lines.append(Colors.dim(mid_border))

    # Data rows
    for row in str_rows:
        r_cells = []
        for i, cell in enumerate(row):
            w = col_widths[i]
            align = alignments[i]
            visible_len = len(strip_ansi(cell))
            padding_len = max(0, w - visible_len)
            if align == "right":
                padded = (" " * padding_len) + cell
            elif align == "center":
                left_pad = padding_len // 2
                right_pad = padding_len - left_pad
                padded = (" " * left_pad) + cell + (" " * right_pad)
            else:
                padded = cell + (" " * padding_len)
            r_cells.append(f" {padded} ")
        lines.append(f"{Colors.dim(VERT)}{f'{Colors.dim(VERT)}'.join(r_cells)}{Colors.dim(VERT)}")

    lines.append(Colors.dim(bot_border))
    return "\n".join(lines)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences to compute raw printable character length."""
    import re
    ansi_pattern = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_pattern.sub("", text)


def truncate_ansi(text: str, max_visible: int) -> str:
    """Truncate a string to max_visible printable characters while preserving ANSI codes."""
    if max_visible <= 0:
        return ""
    vis_len = len(strip_ansi(text))
    if vis_len <= max_visible:
        return text

    import re
    ansi_pattern = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    target_chars = max(0, max_visible - 1)
    out: List[str] = []
    vis_count = 0
    has_ansi = False
    pos = 0
    while pos < len(text) and vis_count < target_chars:
        m = ansi_pattern.match(text, pos)
        if m:
            out.append(m.group(0))
            has_ansi = True
            pos = m.end()
        else:
            out.append(text[pos])
            vis_count += 1
            pos += 1

    if has_ansi:
        out.append(Colors.RESET)
    out.append("…")
    return "".join(out)


def render_kpi_card(title: str, value: str, subtext: str = "", width: int = 26) -> str:
    """Render a single boxed KPI stat card with strict width alignment."""
    h_line = "─" * (width - 2)
    top = f"┌{h_line}┐"
    bot = f"└{h_line}┘"

    def pad_line(content: str) -> str:
        max_content = width - 4
        content = truncate_ansi(content, max_content)
        vis_len = len(strip_ansi(content))
        padding = " " * max(0, width - 3 - vis_len)
        return f"│ {content}{padding}│"

    return "\n".join([top, pad_line(title), pad_line(value), pad_line(subtext), bot])


def render_kpi_grid(cards: List[Tuple[str, str, str]], card_width: int = 26) -> str:
    """Render multiple KPI cards side-by-side (wrapping into balanced rows if terminal is narrow)."""
    if not cards:
        return ""

    import shutil
    term_cols = shutil.get_terminal_size((120, 24)).columns
    if len(cards) == 4 and term_cols < (4 * card_width + 6):
        cards_per_row = 2
    else:
        cards_per_row = max(1, (term_cols + 2) // (card_width + 2))
        cards_per_row = min(cards_per_row, len(cards))

    chunks = [cards[i : i + cards_per_row] for i in range(0, len(cards), cards_per_row)]
    row_strings = []

    for chunk in chunks:
        card_boxes = [render_kpi_card(t, v, s, width=card_width).split("\n") for t, v, s in chunk]
        num_lines = len(card_boxes[0])
        for line_idx in range(num_lines):
            line_parts = [box[line_idx] for box in card_boxes]
            row_strings.append("  ".join(line_parts))

    return "\n".join(row_strings)

