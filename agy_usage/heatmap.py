"""GitHub-style activity heatmap renderer for terminal CLI."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from agy_usage.display import Colors, is_color_enabled, supports_truecolor
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
        return is_color_enabled()

    @classmethod
    def wrap(cls, text: str, code: str) -> str:
        """Style text with an ANSI code, or return it plain when colors are disabled."""
        return Colors.color(text, code)

    @classmethod
    def supports_truecolor(cls) -> bool:
        """Check if terminal supports 24-bit TrueColor."""
        return supports_truecolor()

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

    @property
    def metric_label(self) -> str:
        """Human-readable name of the plotted metric, used in titles and the summary."""
        if self.metric == "cache-efficiency":
            return "Cache Efficiency"
        if self.metric == "hour-of-day":
            return "Tokens by Hour of Day"
        return self.metric.capitalize() + " Tokens"

    def _get_metric_val(self, usage: TokenUsage) -> float:
        """Extract requested metric value from TokenUsage."""
        if self.metric == "cache-efficiency":
            # Same definition as TokenUsage.cache_hit_rate (0.0 when there is no input).
            return usage.cache_hit_rate
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
        """Calculate 3 cut-offs separating levels 1–4 (the brightest level starts at 65% of the max)."""
        if max_val <= 0:
            return [1, 2, 3]

        return [
            max(1, int(max_val * 0.15)),
            max(2, int(max_val * 0.35)),
            max(3, int(max_val * 0.65)),
        ]

    def _get_level(self, val: int, thresholds: List[int]) -> int:
        """Return 0 for no activity, else level 1–4 by which of the 3 cut-offs ``val`` falls below."""
        if val <= 0:
            return 0
        if val < thresholds[0]:
            return 1
        elif val < thresholds[1]:
            return 2
        elif val < thresholds[2]:
            return 3
        return 4

    def _week_bounds(self) -> Tuple[date, date]:
        """Compute the rendered window bounds so render() and the
        window_start_date/window_end_date properties cannot drift."""
        # Align end_date to end of the week (Sunday: weekday 6)
        end_sunday = self.end_date + timedelta(days=(6 - self.end_date.weekday()))
        start_monday = end_sunday - timedelta(days=(self.num_weeks * 7) - 1)
        return start_monday, end_sunday

    @property
    def window_start_date(self) -> date:
        """Monday that begins the oldest displayed week."""
        return self._week_bounds()[0]

    @property
    def window_end_date(self) -> date:
        """Sunday that ends the final displayed week."""
        return self._week_bounds()[1]

    def render(self) -> str:
        """Render the complete GitHub heatmap as a multi-line string."""
        start_monday, end_sunday = self._week_bounds()

        # Collect day values and find max
        max_val = 0
        total_period_val = 0
        active_days_count = 0
        window_usage = TokenUsage()

        # Build 7 x num_weeks matrix
        matrix: List[List[Tuple[date, int, int]]] = [[] for _ in range(7)]

        for w in range(self.num_weeks):
            for d_idx in range(7):
                curr_date = start_monday + timedelta(days=w * 7 + d_idx)
                val = 0
                if curr_date in self.daily_data:
                    usage = self.daily_data[curr_date].usage
                    val = self._get_metric_val(usage)
                    window_usage = window_usage.add(usage)
                    if val > 0:
                        total_period_val += val
                        if val > max_val:
                            max_val = val
                    # A ratio is meaningful whenever the day had any input, even at 0%.
                    has_data = usage.total_prompt_tokens > 0 if self.metric == "cache-efficiency" else val > 0
                    if has_data:
                        active_days_count += 1
                matrix[d_idx].append((curr_date, val, 0))

        thresholds = self._calculate_thresholds(max_val)

        # Build Month Header Line
        # Determine for each week column which month should be displayed
        prev_month = -1
        month_positions: Dict[int, str] = {}

        for w in range(self.num_weeks):
            col_date = matrix[0][w][0]
            if col_date.month != prev_month:
                # Place month label
                month_positions[w] = self.MONTH_NAMES[col_date.month - 1]
                prev_month = col_date.month

        # BUG-025: a label at column 0 spans columns 0 and 1, so when the window
        # starts on the last Monday of a month the next month's label (column 1)
        # would be skipped. Drop the one-week partial month and keep the full one.
        if 0 in month_positions and 1 in month_positions:
            del month_positions[0]

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
        metric_title = self.metric_label
        if self.metric == "cache-efficiency":
            # Overall ratio across the window, not a sum of per-day percentages.
            summary_value = f"{window_usage.cache_hit_rate:.1f}%"
        else:
            summary_value = f"{total_period_val:,}"
        summary_info = (
            f"{HeatmapColor.wrap(summary_value, HeatmapColor.BOLD)} {metric_title} "
            f"across {HeatmapColor.wrap(str(active_days_count), HeatmapColor.CYAN)} active days "
            f"in last {self.num_weeks} weeks"
        )

        lines.append("")
        lines.append(f" {legend_str}    {summary_info}")

        return "\n".join(lines)


class HourOfDayHeatmap(TerminalHeatmap):
    """Weekday x hour-of-day grid (7 rows x 24 columns) of token activity.

    Aggregates the last ``weeks`` weeks of per-hour usage by weekday and local
    hour, answering "when do I use the CLI?" rather than "which days?".
    """

    HOUR_CELL_WIDTH = 2
    HOUR_LABEL_STEP = 3

    def __init__(
        self,
        hourly_data: Dict[Tuple[date, int], TokenUsage],
        weeks: Optional[int] = None,
        end_date: Optional[date] = None,
        term_cols: Optional[int] = None,
    ):
        super().__init__({}, metric="hour-of-day", weeks=weeks, end_date=end_date, term_cols=term_cols)
        self.hourly_data = hourly_data

    @property
    def required_cols(self) -> int:
        return 24 * self.HOUR_CELL_WIDTH + 8

    def render(self) -> str:
        start_monday, _ = self._week_bounds()
        grid = [[0] * 24 for _ in range(7)]
        total = 0
        for (d, hour), usage in self.hourly_data.items():
            if start_monday <= d <= self.end_date:
                grid[d.weekday()][hour] += usage.total_tokens
                total += usage.total_tokens

        max_val = max(max(row) for row in grid)
        thresholds = self._calculate_thresholds(max_val)

        header = "".join(
            f"{h:02d}".ljust(self.HOUR_LABEL_STEP * self.HOUR_CELL_WIDTH)
            for h in range(0, 24, self.HOUR_LABEL_STEP)
        )
        lines = [HeatmapColor.wrap("     " + header.rstrip(), HeatmapColor.DIM)]
        for day_idx in range(7):
            row_str = HeatmapColor.wrap(self.WEEKDAY_LABELS[day_idx], HeatmapColor.DIM) + " "
            for hour in range(24):
                level = self._get_level(grid[day_idx][hour], thresholds)
                row_str += HeatmapColor.cell(level, "■") + " "
            lines.append(row_str)

        legend_cells = "".join(HeatmapColor.cell(lvl, "■") + " " for lvl in range(5))
        legend_str = f"{HeatmapColor.wrap('Less ', HeatmapColor.DIM)}{legend_cells}{HeatmapColor.wrap('More', HeatmapColor.DIM)}"

        hour_totals = [sum(grid[d][h] for d in range(7)) for h in range(24)]
        summary_info = f"{HeatmapColor.wrap(f'{total:,}', HeatmapColor.BOLD)} Tokens in last {self.num_weeks} weeks"
        if total > 0:
            peak = hour_totals.index(max(hour_totals))
            summary_info += f", busiest hour {HeatmapColor.wrap(f'{peak:02d}:00', HeatmapColor.CYAN)}"

        lines.append("")
        lines.append(f" {legend_str}    {summary_info}")
        return "\n".join(lines)
