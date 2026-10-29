# agy-usage: Antigravity Model & Token Usage Tracker

A lightweight, zero-dependency command-line analytics and visualization tool for **Google Antigravity** CLI & IDE model token usage. It reads directly from local conversation databases, calculating per-session tokens, cached tokens, prompt/output/thoughts tokens, weekly and monthly rollups, and rendering an authentic **GitHub-style activity heatmap** directly in your terminal.

---

## ✨ Features

- **⚡ Zero External Dependencies**: Written completely in pure Python 3 (standard library only: `sqlite3`, `argparse`, `datetime`, `json`, `math`). No `pip install` required to run immediately.
- **🟩 GitHub-Style Terminal Heatmap**: 7-row weekday contribution grid with month headers, 24-bit TrueColor/ANSI 256 green shading levels (`■`), and customizable metrics (`total`, `cached`, `input`, `output`).
- **💾 Prompt Caching Analytics**: Tracks exact prompt cache hits, cache hit percentages (`cached / total_prompt`), and billable tokens saved.
- **🔍 Turn-by-Turn Inspection**: Deep dive into individual sessions to inspect step-by-step token consumption, reasoning/thought tokens, response tokens, and token volume sparklines.
- **📅 Weekly & Monthly Rollups**: Aggregates token usage across ISO calendar weeks and months with visual relative volume bars.
- **🤖 Model Distribution**: Breaks down requests, prompt tokens, cached tokens, output tokens, and percentage share across models (e.g., `gemini-3.7-flash`, `gemini-3.8-flash`).
- **📁 Dual Source & Custom Directory Support**: Automatically aggregates both Antigravity CLI (`~/.gemini/antigravity`) and Antigravity IDE (`~/.gemini/antigravity-ide`) conversations, or isolates with `--cli-only`, `--ide-only`, or `--dir <path>`.
- **📊 Export Formats**: Instant JSON and CSV export for spreadsheet analysis, automated reporting, or dashboards.

---

## 🚀 Quickstart

You can run `agy-usage` immediately from this directory:

```bash
# Display summary dashboard and 16-week activity heatmap
./agy-usage

# Or invoke as a Python module
python3 -m agy_usage
```

### Optional: Install as a System CLI Command

```bash
pip install -e .
# Now you can run it anywhere:
agy-usage
```

Or add to your shell profile (`~/.zshrc` or `~/.bashrc`):
```bash
alias agy-usage="/Users/sethu/projects/Antigravity/antigravity-usage/agy-usage"
```

---

## 📖 CLI Commands & Examples

### 1. Dashboard Overview (`agy-usage` or `agy-usage overview`)
Displays KPI stat cards, mini activity heatmap, model distribution, and recent sessions:
```bash
agy-usage
```

### 2. GitHub-Style Activity Heatmap (`agy-usage heatmap`)
Displays full terminal activity heatmap:
```bash
# Default 26-week heatmap colored by total tokens
agy-usage heatmap

# Show active dates list below heatmap
agy-usage heatmap --details

# Display 52-week annual heatmap (-w is clamped to 4-52 weeks; a warning is printed if the terminal is narrower than the grid)
agy-usage heatmap -w 52

# Color heatmap by CACHED tokens to inspect cache efficiency over time
agy-usage heatmap --metric cached

# Color heatmap by non-cached prompt tokens or generated output tokens
agy-usage heatmap --metric input
agy-usage heatmap --metric output
```

### 3. Session List (`agy-usage sessions`)
Displays recent conversations with token breakdowns, cache hit rates, durations, and timestamps:
```bash
# List recent 15 sessions
agy-usage sessions

# Sort by cached tokens or total tokens
agy-usage sessions --sort cached
agy-usage sessions --sort tokens

# Filter by model or workspace
agy-usage sessions --model 3.8-flash
agy-usage sessions --workspace mongo
```

### 4. Inspect Current Session Totals (`agy-usage current`)
Immediately inspects your active/most recent conversation totals without turn-by-turn clutter:
```bash
# Show current session totals (KPI cards, breakdown table, cost analysis)
agy-usage current

# Optionally show turn-by-turn logs for the last 10 turns (or -n 0 for all turns)
agy-usage current -n 10
agy-usage current --details
```

### 5. Session Totals & Turn Deep Dive (`agy-usage session [id]`)
Inspects totals for any specific session (or defaults to current if omitted):
```bash
# Inspect session totals
agy-usage session b4cba2f9

# View turn-by-turn logs table for that session
agy-usage session b4cba2f9 --details
```
Outputs:
- Session metadata (title, workspace, duration, model, start/end time, turns)
- KPI cards: Total Processed, Cache Savings, Billable Consumed, Estimated Cost
- Token Usage Breakdown table with exact counts, compact suffixes, share percentages, estimated USD cost, and relative volume bars
- Optional turn-by-turn log table (when `--details` or `-n <count>` is passed)

### 6. Quota & Rate Limits Monitor (`agy-usage limits`)
Monitors your active **5-Hour Rolling Sprint Limit** and **7-Day Weekly Baseline Hard Cap** in real-time to prevent unexpected model lockouts:
```bash
# Display 5-hour and weekly limits for default Pro subscription
agy-usage limits

# Check limits under Ultra tier or Free tier
agy-usage limits --tier ultra
agy-usage limits --tier free

# Set custom capacity limits (supports K, M, B suffixes)
agy-usage limits --5h-limit 80M --weekly-limit 500M

# Export limits analysis to JSON or CSV
agy-usage limits --json
agy-usage limits --csv
```
Outputs:
- **System Health Status Badge**: Real-time diagnostic badge (🟢 Healthy, 🟡 High Load Warning, 🔴 Limit Exceeded / Multi-day Lockout Risk, ⚪ Unknown when the quota cannot be determined). `limits` and `statusline` use the same thresholds (warn at 60% used, high at 80%).
- **Quota Source Line**: Shows where the numbers came from: live Antigravity server, `Last server reading, N min ago (Antigravity server not reachable now)` (the last cached server reading, used for up to 6 hours), or `Local estimate, this device only`. If the server cannot be reached and this device has no usage in a window, that window is reported as unknown (`?` in text, `null` in JSON, empty CSV cells) instead of 100% remaining, with the reason the server call failed. `--json` adds `quota_source`, `cache_fetched_at` and `cache_age_seconds`.
- **4 KPI Cards (Matching Official Antigravity UI)**: `5-HOUR REMAINING`, `5-HOUR REFRESH`, `WEEKLY REMAINING`, `WEEKLY REFRESH`.
- **Five Hour Limit Remaining**: Gauge bar, exact remaining vs consumed, billable vs cached breakdowns, prompt cache hit rate, turn volume, and countdown until window fully refreshes.
- **Weekly Limit Remaining**: Gauge bar, active 7-day quota cycle consumption, cost, and exact refresh countdown matching Google Antigravity (e.g. `You have used some of your weekly limit, it will fully refresh in 6 days, 16 hours.`).
- **Antigravity Tier Limits Table**: Quick reference across Free, Pro (100M/750M), and Ultra (500M/3.75B) tiers with active tier indicator.

### 7. Shell & Tmux Statusline (`agy-usage statusline`)
Ultra-fast (<70ms), single-line quota and health snapshot designed for shell prompts (`zsh`, `bash`, `starship`) and `tmux` status bars:
```bash
# Default colored statusline (with emoji badge, recovery countdowns, today's spend)
agy-usage statusline
# Output: 🟢 5h: 97% (in 4h 59m) | Wk: 91% (in 6d 1h) | Today: 68.85M ($11.80)

# Ultra-compact minimal format for tight prompts
agy-usage statusline --short
# Output: 🟢 5h:97% Wk:91% $11.80

# Plain text without ANSI colors or emojis
agy-usage statusline --plain
# Output: [OK] 5h: 97% (in 4h 59m) | Wk: 91% (in 6d 1h) | Today: 68.85M ($11.80)

# Native tmux statusline formatting
agy-usage statusline --tmux
# Output: #[fg=green]● #[fg=white]5h: 97% #[fg=colour244](in 4h 59m) #[fg=colour238]| #[fg=white]Wk: 91% #[fg=colour244](in 6d 1h) #[fg=colour238]| #[fg=white]Today: 68.85M #[fg=green]($11.80)

# JSON metrics for Starship, Waybar, or Polybar integrations
agy-usage statusline --json

# One-row CSV (header + data row, same fields as --json) for spreadsheets or logging
agy-usage statusline --csv

# Custom template string format
agy-usage statusline --format "{status} [5h: {5h_pct}%] [Wk: {wk_pct}%] Spent: {today_cost}"
# Output: 🟢 [5h: 97%] [Wk: 91%] Spent: $11.80
```

#### Quick Integration Snippets:
* **tmux (`~/.tmux.conf`)**:
  ```tmux
  set -g status-interval 60
  set -g status-right '#(agy-usage statusline --tmux)'
  ```
* **zsh (`~/.zshrc`)**:
  ```zsh
  RPROMPT='$(agy-usage statusline --short)'
  ```
* **Starship (`~/.config/starship.toml`)**:
  ```toml
  [custom.agy]
  command = "agy-usage statusline --short"
  when = "command -v agy-usage >/dev/null"
  shell = ["sh"]
  style = "bold green"
  ```

### 8. Weekly Rollups (`agy-usage weekly`)
Aggregates usage by ISO week with dates and relative volume bars:
```bash
agy-usage weekly
```

### 9. Monthly Rollups (`agy-usage monthly`)
Aggregates usage by calendar month:
```bash
agy-usage monthly
```

### 10. Model Distribution (`agy-usage models`)
Breakdown of token volume and percentage share per model:
```bash
agy-usage models
```

### 11. Live Watch Mode (`agy-usage live`)
Auto-refreshes the dashboard in real-time as Antigravity executes prompts:
```bash
agy-usage live --interval 2

# Stream one compact JSON object per refresh (NDJSON), no banner or screen clearing
agy-usage live --json --interval 5
```
`--csv` is not supported by `live` (error on stderr, exit code 2).

---

## 📂 Data Sources & Filtering

By default, `agy-usage` scans both CLI and IDE directories:
- **CLI directory**: `~/.gemini/antigravity/conversations`
- **IDE directory**: `~/.gemini/antigravity-ide/conversations`

Use flags to restrict the scope:
```bash
# Scan Antigravity CLI conversations only
agy-usage --cli-only

# Scan Antigravity IDE conversations only
agy-usage --ide-only

# Scan a custom directory only (sessions are ordered by start time, newest first, like the default scan)
agy-usage --dir /path/to/custom/conversations
```
A session present in both the CLI and IDE directories is counted once (the copy with the newest modification time wins).

---

## 💾 Exporting Data

Export complete analytics to JSON or CSV:
```bash
# Export complete JSON report to stdout
agy-usage --json

# Export sessions table to CSV
agy-usage --csv > usage_report.csv
```

---

## 🔬 How It Works

Antigravity stores conversations in SQLite databases containing Protobuf blobs:
- **`steps` table**:
  - `metadata` blob: Decoded using custom zero-dependency wire-format Protobuf decoder.
    - `Field 1`: `google.protobuf.Timestamp` (seconds, nanos).
    - `Field 9`: `UsageMetadata` containing:
      - `Field 2`: Prompt tokens (`prompt_token_count`).
      - `Field 3`: Output tokens (`candidates_token_count`).
      - `Field 5`: Cached content tokens (`cached_content_token_count`).
      - `Field 9`: Reasoning / thought tokens (`thoughts_token_count`).
      - `Field 10`: Candidate response tokens.
  - `step_payload` blob: User goal and prompt text (`Field 19 -> SubField 2`).
- **`gen_metadata` table**:
  - Contains model strings (e.g. `gemini-3.7-flash`, `gemini-3.8-flash`) and request mappings.
- **`trajectory_metadata_blob` table**:
  - Contains workspace URIs and session start metadata.

All reads are executed using SQLite URI read-only mode (`file:<path>?mode=ro`), guaranteeing that running conversations are never locked or modified.

---

## 🧪 Testing

Run the unit test suite (stdlib `unittest`; `pytest -q` also works):
```bash
python3 -m unittest discover tests
```
Tests live in `tests/`, one `test_<topic>.py` file per area. See `AGENTS.md` for how to add tests.

---

## 📄 License

MIT License.
