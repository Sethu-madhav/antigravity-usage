"""GitHub-style activity heatmap renderer for terminal CLI."""

from __future__ import annotations

import os
import shutil
import sys
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from agy_usage.display import Colors
from agy_usage.models import DailyUsage, TokenUsage


class HeatmapColor:
    """TrueColor and ANSI escape sequences for GitHub-style green contribution shades."""

    # GitHub contribution shades (RGB)
    # Level 0: GitHub dark theme empty square #161b22
    # Level 1: #0e4429
    # Level 2: #006d32
    # Level 3: #26a641
    # Level 4: #39d353
    RGB_LEVELS = [
        (22, 27, 34),     # 0: empty
        (14, 68, 41),     # 1: light
        (0, 109, 50),     # 2: medium
        (38, 166, 65),    # 3: high
        (57, 211, 83),    # 4: intense
    ]

    # Fallback 256-color palette
    XTERM_LEVELS = [236, 22, 28, 35, 46]

    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"

    @classmethod
    def supports_color(cls) -> bool:
        """Check if ANSI colors are enabled (honors the global --no-color override, BUG-004)."""
        return Colors.enabled()

    @classmethod
    def wrap(cls, text: str, code: str) -> str:
        """Style text with an ANSI code, or return it plain when colors are disabled."""
        if not cls.supports_color():
            return text
        return f"{code}{text}{cls.RESET}"

    @classmethod
    def supports_truecolor(cls) -> bool:
        """Check if terminal supports 24-bit TrueColor."""
        colorterm = os.environ.get("COLORTERM", "")
        term = os.environ.get("TERM", "")
        return "truecolor" in colorterm or "24bit" in colorterm or "xterm-256color" in term or sys.platform == "darwin"

    @classmethod
    def cell(cls, level: int, char: str = "■") -> str:
        """Render a single colored cell."""
        if not cls.supports_color():
            level = max(0, min(4, level))
            symbols = ["·", "░", "▒", "▓", "█"]
            return symbols[level]

        level = max(0, min(4, level))
        if cls.supports_truecolor():
            r, g, b = cls.RGB_LEVELS[level]
            # Foreground TrueColor
            return f"\033[38;2;{r};{g};{b}m{char}{cls.RESET}"
        else:
            code = cls.XTERM_LEVELS[level]
            return f"\033[38;5;{code}m{char}{cls.RESET}"


class TerminalHeatmap:
    """Renders GitHub-style terminal contribution grid."""

    WEEKDAY_LABELS = ["Mon", "   ", "Wed", "   ", "Fri", "   ", "Sun"]
    MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    def __init__(
        self,
        daily_data: Dict[date, DailyUsage],
        metric: str = "total",
        weeks: Optional[int] = None,
        end_date: Optional[date] = None,
        term_cols: Optional[int] = None,
    ):
        self.daily_data = daily_data
        self.metric = metric.lower()
        self.end_date = end_date or date.today()

        # Determine terminal width and default weeks
        self.term_cols = term_cols if term_cols is not None else shutil.get_terminal_size((80, 24)).columns
        # Each week takes 2 characters ("■ "), plus 6 chars for weekday label
        self.max_fit_weeks = max(8, (self.term_cols - 8) // 2)

        if weeks is not None:
            self.num_weeks = max(4, min(52, weeks))
        else:
            # Default to 26 weeks (half year) if fits, else max fit, but up to 52
            self.num_weeks = min(26, self.max_fit_weeks)

    @property
    def required_cols(self) -> int:
        """Minimum terminal columns required to render heatmap without wrapping."""
        return self.num_weeks * 2 + 8

    @property
    def is_terminal_narrow(self) -> bool:
        """Check if terminal is narrower than required columns."""
        return self.term_cols < self.required_cols

    def _get_metric_val(self, usage: TokenUsage) -> int:
        """Extract requested metric value from TokenUsage."""
        if self.metric == "cached":
            return usage.cached_tokens
        elif self.metric in ("input", "prompt"):
            return usage.prompt_tokens
        elif self.metric == "output":
            return usage.output_tokens
        elif self.metric == "thoughts":
            return usage.thought_tokens
        return usage.total_tokens

    def _calculate_thresholds(self, max_val: int) -> List[int]:
        """Calculate 4 positive thresholds for levels 1, 2, 3, 4."""
        if max_val <= 0:
            return [1, 2, 3, 4]

        # Use quartiles or log scale
        return [
            max(1, int(max_val * 0.15)),
            max(2, int(max_val * 0.35)),
            max(3, int(max_val * 0.65)),
            max(4, int(max_val * 0.85)),
        ]

    def _get_level(self, val: int, thresholds: List[int]) -> int:
        if val <= 0:
            return 0
        if val < thresholds[0]:
            return 1
        elif val < thresholds[1]:
            return 2
        elif val < thresholds[2]:
            return 3
        return 4

    def render(self) -> str:
        """Render the complete GitHub heatmap as a multi-line string."""
        # Align end_date to end of the week (Sunday: weekday 6)
        end_sunday = self.end_date + timedelta(days=(6 - self.end_date.weekday()))
        start_monday = end_sunday - timedelta(days=(self.num_weeks * 7) - 1)

        # Collect day values and find max
        max_val = 0
        total_period_val = 0
        active_days_count = 0

        # Build 7 x num_weeks matrix
        matrix: List[List[Tuple[date, int, int]]] = [[] for _ in range(7)]

        for w in range(self.num_weeks):
            for d_idx in range(7):
                curr_date = start_monday + timedelta(days=w * 7 + d_idx)
                val = 0
                if curr_date in self.daily_data:
                    val = self._get_metric_val(self.daily_data[curr_date].usage)
                    if val > 0:
                        active_days_count += 1
                        total_period_val += val
                        if val > max_val:
                            max_val = val
                matrix[d_idx].append((curr_date, val, 0))

        thresholds = self._calculate_thresholds(max_val)

        # Build Month Header Line
        # Determine for each week column which month should be displayed
        month_line_chars: List[str] = [" " * 5]  # align with weekday labels
        prev_month = -1
        month_positions: Dict[int, str] = {}

        for w in range(self.num_weeks):
            col_date = matrix[0][w][0]
            if col_date.month != prev_month:
                # Place month label
                month_positions[w] = self.MONTH_NAMES[col_date.month - 1]
                prev_month = col_date.month

        # Format month line with exact spacing
        col_idx = 0
        month_header_str = "     "  # 5 spaces for label indentation
        while col_idx < self.num_weeks:
            if col_idx in month_positions:
                name = month_positions[col_idx]
                # BUG-006: a month name spans ceil(len / 2) of the 2-char week
                # columns; skip exactly those columns so later labels don't drift
                # right and point at the wrong weeks.
                cols_skip = max(1, -(-len(name) // 2))
                month_header_str += name.ljust(cols_skip * 2)
                col_idx += cols_skip
            else:
                month_header_str += "  "
                col_idx += 1

        lines = [HeatmapColor.wrap(month_header_str.rstrip(), HeatmapColor.DIM)]

        # Build Weekday Rows
        for day_idx in range(7):
            label = self.WEEKDAY_LABELS[day_idx]
            row_str = HeatmapColor.wrap(label, HeatmapColor.DIM) + " "
            for w in range(self.num_weeks):
                curr_date, val, _ = matrix[day_idx][w]
                if curr_date > self.end_date:
                    # Future dates within the padding week
                    cell_rendered = "  "
                else:
                    level = self._get_level(val, thresholds)
                    cell_rendered = HeatmapColor.cell(level, "■") + " "
                row_str += cell_rendered
            lines.append(row_str)

        # Build Legend & Summary Line
        legend_cells = "".join(
            HeatmapColor.cell(lvl, "■") + " " for lvl in range(5)
        )
        legend_str = f"{HeatmapColor.wrap('Less ', HeatmapColor.DIM)}{legend_cells}{HeatmapColor.wrap('More', HeatmapColor.DIM)}"

        # Metric label
        metric_title = self.metric.capitalize() + " Tokens"
        summary_info = (
            f"{HeatmapColor.wrap(f'{total_period_val:,}', HeatmapColor.BOLD)} {metric_title} "
            f"across {HeatmapColor.wrap(str(active_days_count), HeatmapColor.CYAN)} active days "
            f"in last {self.num_weeks} weeks"
        )

        lines.append("")
        lines.append(f" {legend_str}    {summary_info}")

        return "\n".join(lines)
