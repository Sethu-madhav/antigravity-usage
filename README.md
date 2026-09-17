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

# Display 52-week annual heatmap
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
# Show current session totals (KPI cards, breakdown table, progression sparkline)
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

### 6. Weekly Rollups (`agy-usage weekly`)
Aggregates usage by ISO week with dates and relative volume bars:
```bash
agy-usage weekly
```

### 6. Monthly Rollups (`agy-usage monthly`)
Aggregates usage by calendar month:
```bash
agy-usage monthly
```

### 7. Model Distribution (`agy-usage models`)
Breakdown of token volume and percentage share per model:
```bash
agy-usage models
```

### 8. Live Watch Mode (`agy-usage live`)
Auto-refreshes the dashboard in real-time as Antigravity executes prompts:
```bash
agy-usage live --interval 2
```

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

# Scan a custom directory only
agy-usage --dir /path/to/custom/conversations
```

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

Run the unit test suite:
```bash
python3 -m unittest discover tests
```

---

## 📄 License

MIT License.
