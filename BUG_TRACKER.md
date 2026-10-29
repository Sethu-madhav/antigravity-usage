# Bug Tracker — agy-usage

> **Baseline:** commit `a2904b2` (branch `master`) · Audited 2026-10-28 · Scope: full codebase (~5,000 LOC) · Test suite: **29/29 passing**
>
> **Confidence legend:**
> **A** = reproduced at runtime · **B** = confirmed by code trace (single-source of failure, quoted evidence)
> **Sev:** 🔴 High · 🟠 Med · 🟡 Low
>
> **Status legend:** ⬜ Open · 🔧 In progress · ✅ Fixed · ❌ Won't fix

---

## Summary

| Category | Open | High | Med | Low |
|----------|-----:|-----:|----:|----:|
| 🐛 Bugs       | 23 | 5 | 9 | 9 |
| ⚡ Optimizations | 8 | 1 | 3 | 4 |
| 🔧 Improvements | 9 | 0 | 3 | 6 |
| ✨ Feature ideas | 10 | — | — | — |

---

## 🐛 Bugs

| ID  | Sev | Conf | Status | Location | Summary |
|-----|-----|------|--------|----------|---------|
| BUG-001 | 🔴 | A | ⬜ | `cli.py:87-95` | Global flags fail after subcommand |
| BUG-002 | 🔴 | A | ⬜ | `cli.py:265-269, 319-323` | Root `--json/--csv` silently discarded before `limits`/`statusline` |
| BUG-003 | 🔴 | A | ⬜ | `limits.py:210-228` | `parse_token_amount` crashes on `inf`, accepts negatives |
| BUG-004 | 🔴 | A | ⬜ | `cli.py` + `heatmap.py` | `--no-color` does not disable heatmap colors |
| BUG-005 | 🔴 | B | ⬜ | `limits.py:375-382, 644-649` | Stale quota cache served as authoritative "Live" data (no TTL) |
| BUG-006 | 🟠 | A | ⬜ | `heatmap.py:178-189` | Month labels drift right, point at wrong weeks (dead `chars_consumed`) |
| BUG-007 | 🟠 | B | ⬜ | `parser.py:266-269` | Unquoted SQLite URI + silent read-write fallback |
| BUG-008 | 🟠 | B | ⬜ | `parser.py:549-551` | One exception kills whole session parse (flickering in `live`) |
| BUG-009 | 🟠 | B | ⬜ | `display.py:36-39, heatmap.py:41-45` | Colors leak into piped output when `COLORTERM` set |
| BUG-010 | 🟠 | B | ⬜ | `parser.py:371-377` | Phantom `gemini-3.8-flash` attribution for unresolvable rows |
| BUG-011 | 🟠 | B | ⬜ | `cli.py:1035-1054, display.py:93-100` | "Used %" quota colored green (thresholds inverted for `used`) |
| BUG-012 | 🟠 | B | ⬜ | `limits.py` vs `cli.py:1146-1186` | Health badge thresholds disagree (60/80% vs 80% only) |
| BUG-013 | 🟠 | B | ⬜ | `limits.py:176-194` | `tokens_remaining` ignores cost/server-fraction quota model |
| BUG-014 | 🟡 | A | ⬜ | `cli.py` (`-w` flag) | `-w 500` accepted (help says "up to 52"), renders 500 columns |
| BUG-015 | 🟡 | B | ⬜ | `parser.py:242-243` | No dedup: session in both CLI+IDE dirs → tokens double-counted |
| BUG-016 | 🟡 | B | ⬜ | `parser.py:213-217, 246-251` | `getmtime` in sort key crashes whole scan if DB vanishes mid-scan |
| BUG-017 | 🟡 | B | ⬜ | `parser.py:15-26` | Unbounded varint decode (no 10-byte/64-bit cap) |
| BUG-018 | 🟡 | B | ⬜ | `limits.py:458-464` + `parser.py:484-486` | mtime fallback drags old steps into 5h window; future-dated step hijacks anchor |
| BUG-019 | 🟡 | B | ⬜ | `cli.py:1287-1300` | `live` + `--json` semantics incoherent (banner becomes one-shot dump) |
| BUG-020 | 🟡 | B | ⬜ | `exporter.py:117-119` | `UnicodeEncodeError` on non-UTF-8 stdout (Windows cp1252) |
| BUG-021 | 🟡 | B | ⬜ | `display.py:42-45` | Nested color calls: inner `RESET` wipes outer attribute |
| BUG-022 | 🟡 | B | ⬜ | `parser.py:240` | `--dir` path skips chronological re-sort (mtime vs start_time) |
| BUG-023 | 🟠 | B | ⬜ | `setup.py` + `pyproject.toml` + `__init__.py` | Triplicated metadata/entry-point; no LICENSE file |

### Details

**BUG-001 · Global flags fail after subcommand · 🔴 A**
`agy-usage sessions --no-color`, `current --json`, `heatmap --csv` all hard-fail with
`error: unrecognized arguments` — shared flags exist only on the root parser.
Repro: `parse_args(["sessions", "--no-color"])` → SystemExit.
**Fix:** shared parent parser (`add_help=False`) passed via `parents=[common]` to every subparser.

**BUG-002 · Root `--json --csv` discarded before `limits`/`statusline` · 🔴 A**
Those subparsers re-declare the flags with `store_true` defaults; argparse merges the
subparser namespace over the root's.
Repro: `parse_args(["--json", "limits"])` → `json=False` → silent text output instead of JSON.
**Fix:** re-declare with `default=argparse.SUPPRESS` (or the BUG-001 parent-parser fix).

**BUG-003 · `parse_token_amount` crashes on `inf`, accepts negatives · 🔴 A**
Only `ValueError` is caught. Repro: `"inf"` → `OverflowError`, `"1e309"` → `OverflowError`,
`"-5M"` → `-5000000`.
**Fix:**
```python
val = float(s) * multiplier
if not (val >= 1):  # rejects NaN, inf, negatives, 0
    return None
return int(val)
# except (ValueError, OverflowError) -> None
```

**BUG-004 · `--no-color` doesn't disable the heatmap · 🔴 A**
`main()` patches `display.Colors` only; `HeatmapColor.cell()` consults its own
`supports_color()` — RGB escapes still emitted with the patch applied.
**Fix:** module-level `color_enabled` switch consulted by **both** classes.

**BUG-005 · Stale quota cache shown as "Live" · 🔴 B**
`fetch_live_quota_summary` falls back to `agy_usage_quota_cache.json` whenever the language
server is down; `fetched_at` is stored but never validated → hours-old `remainingFraction`
renders under "Live Antigravity Server", and past-`resetTime` fractions get grafted onto
freshly computed windows. **Fix:** reject cache older than ~60 s; only use
`remaining_fraction` when `reset_dt > now`.

**BUG-006 · Heatmap month labels drift · 🟠 A**
Each week column is 2 chars, but a 3-char label advances `col_idx` by only 1 — drift grows
to +11 chars at 52 weeks. Dead variable `chars_consumed = 2` (line 185).
**Fix:** `cols_skip = max(1, -(-len(name) // 2))`; `ljust(cols_skip * 2)`; `col_idx += cols_skip`.

**BUG-007 · SQLite URI quoting + RW fallback · 🟠 B**
`f"file:{db_path}?mode=ro"`: paths containing `?`/`#`/`%` corrupt the URI; the bare
`except: sqlite3.connect(db_path)` then opens the DB **read-write** (also creates the file
if it vanished — TOCTOU), contradicting the README's read-only guarantee.
**Fix:** `file:{quote(db_path)}?mode=ro` (urllib.parse.quote); drop/log the fallback.

**BUG-008 · One exception kills whole session parse · 🟠 B**
Outer try wraps everything; a transient `database is locked` discards all already-parsed
steps/title/workspace (despite the "partial summary" comment) → sessions flicker out of
`live` dashboards. **Fix:** catch `sqlite3.OperationalError` separately; emit partial
summary in `finally`.

**BUG-009 · ANSI escapes leak into piped output · 🟠 B**
`COLORTERM` presence (VS Code shell, Warp, CI) forces colors even when stdout is a file:
`agy-usage overview > report.txt` contains escape garbage.
**Fix:** gate on `sys.stdout.isatty() and not NO_COLOR`; `COLORTERM` only upgrades depth.

**BUG-010 · Phantom model attribution · 🟠 B**
`gen_metadata` rows with no resolvable name → `normalize_model_name("")` returns hardcoded
`"gemini-3.8-flash"` → row becomes an anchor and enters `known_models` with zero evidence,
distorting model distribution & per-model cost. **Fix:** `if not resolved: continue`, or
attribute to `"unknown"`.

**BUG-011 · "Used %" colored green · 🟠 B**
`format_percent` (green ≥75) is correct for *remaining*, inverted for *used* —
"92.0% used" renders green next to a red gauge. **Fix:** semantic param (`high_is_good`).

**BUG-012 · Badge thresholds disagree · 🟠 B**
`limits` badge: yellow at 60% used, red/dark at 80%; `statusline`: warns only at 80%
used → same state = 🟡 in one command, 🟢 in the other. **Fix:** shared constants
(`WARN_MODERATE=60, WARN_HIGH=80, CRIT=100`) used by both.

**BUG-013 · `tokens_remaining` ignores cost/server quota · 🟠 B**
Always `max(0, limit_tokens - total_tokens)` while `pct_used` prefers
`server_remaining_fraction`/cost → contradictory "100% used / 2.75B remaining". Token
pct also unclamped (>100%). **Fix:** derive remaining from pct when cost/server model active; clamp.

**BUG-014 · `-w` unclamped · 🟡 A**
`parse_args(["heatmap", "-w", "500"])` → accepted; help says "up to 52" → 2,000+ char
output wraps into garbage. **Fix:** `max(4, min(52, weeks))` + terminal-width warning.

**BUG-015 · No dedup across CLI/IDE dirs · 🟡 B**
Same DB in both dirs: tokens summed twice while session counts dedupe by `session_id`
→ internal inconsistency. **Fix:** dedupe by `session_id` in `scan_all` (keep newest mtime).

**BUG-016 · Crash if DB vanishes mid-scan · 🟡 B**
`os.path.getmtime` inside sort lambdas raises `FileNotFoundError` during `sorted()`
(if Antigravity housekeeping deletes a DB) → whole command dies. **Fix:**
`key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0`.

**BUG-017 · Unbounded varint decode · 🟡 B**
No 10-byte/64-bit cap; corrupt blobs produce astronomically large ints that flow into
every aggregate unfiltered. **Fix:** abort blob on `shift >= 70`; mask to 64 bits.

**BUG-018 · Timestamp fallbacks skew quota windows · 🟡 B**
Steps without timestamps inherit the file's `getmtime` (can pull an old session into the
current 5h window); a future-dated step re-anchors the cycle in the future → whole window
renders 0% used. **Fix:** clamp fallback to `[first_ts, now]`; skip steps > now + skew.

**BUG-019 · `live` + `--json` incoherent · 🟡 B**
`--json live` (root form) runs one-shot JSON export and returns — never reaches
`cmd_live`; `live --json` errors out (BUG-001). **Fix:** document/reject clearly, or
implement JSON-per-tick with `--interval`.

**BUG-020 · `UnicodeEncodeError` on Windows consoles · 🟡 B**
`sys.stdout.write(content)` uses console encoding (cp1252) — titles with emoji/CJK crash
`--json/--csv` stdout export (file path already pins utf-8). **Fix:**
`sys.stdout.reconfigure(encoding="utf-8", errors="replace")` guarded by availability.

**BUG-021 · Nested colors lose outer attribute · 🟡 B**
`Colors.bold(Colors.green(x))` emits `[1m[92m x [0m[0m` — inner RESET kills bold.
All `bold(green(...))` patterns silently lose bold. **Fix:** skip RESET when the text
already ends with `RESET`. (Cosmetic; alignment unaffected.)

**BUG-022 · `--dir` skips chronological sort · 🟡 B**
`scan_all(custom_dir=...)` returns directory-scan output ordered by file mtime, while the
default path sorts by `start_time` → `sessions[0]` ("current") means different things.
**Fix:** apply the same start-time sort on the custom path.

**BUG-023 · Packaging metadata triplicated · 🟠 B**
`setup.py` and `pyproject.toml` both define name/version/console-script; `__init__.py:6`
is a third version source (feeds `-v`). Benign today (setuptools ≥61 prefers
`[project]`) but a drift hazard; MIT declared, no LICENSE file shipped.
**Fix:** delete `setup.py`; single-source version via
`dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "agy_usage.__version__"}`;
add LICENSE; use SPDX `license = "MIT"`.

---

## ⚡ Optimizations

| ID | Sev | Status | Location | Summary |
|----|-----|--------|----------|---------|
| OPT-001 | 🔴 | ⬜ | `parser.py` (all entry points) | No incremental parse cache — every command re-parses every DB |
| OPT-002 | 🟠 | ⬜ | `cli.py:832-844` | `live`: full re-scan per tick, screen cleared *before* slow work, daily agg run 2×/tick |
| OPT-003 | 🟠 | ⬜ | `models.py:330-333, 377-382` | `step.cost`/`s.cost` re-price on every access; `get_rates` substring-scan unmemoized |
| OPT-004 | 🟡 | ⬜ | `aggregator.py:64,97,177` | `aggregate_daily` recomputed ~3× per run (7× per JSON export) |
| OPT-005 | 🟡 | ⬜ | `limits.py:654-735` | `_collect_pool_items` called 3×, re-walking every step + `classify_model_pool` |
| OPT-006 | 🟡 | ⬜ | `parser.py:503-507` | O(n·m) reverse linear scan over model anchors per step |
| OPT-007 | 🟡 | ⬜ | `display.py:276-292` | ANSI regex recompiled per cell |
| OPT-008 | 🟡 | ⬜ | `parser.py:453-456` | `cursor.fetchall()` materializes all blobs before processing |

**OPT-001 (top win):** statusline/<70ms claim + `live` watch re-decode every frozen old DB on
every invocation. **Fix:** cache parsed `SessionSummary` per `(path, size, mtime)` in
`~/.cache/agy-usage/` (stdlib json); re-parse only changed files. pairs with BUG-008 (also
live-probes `ps`/`lsof`/HTTP with 1–1.5 s timeouts, no TTL on cache).
Fixes: cached_property on the two cost props + dict memo for exact model names.

---

## 🔧 Improvements

| ID | Sev | Status | Summary |
|----|-----|--------|---------|
| IMP-001 | 🟠 | ⬜ | Exit codes + stderr discipline: `main()` always returns 0; "not found"/"no data" prints to stdout → breaks `| jq` pipelines. Return 1; route messages to stderr |
| IMP-002 | 🟠 | ⬜ | Warn on silently-ignored inputs: `--5h-limit 5OM` typo → tier default with no warning; `--tmux` ignores `--short/--plain`; `--json --csv` precedence unannounced; `--dir`+`--cli-only` co-accepted (see BUG-005 family) |
| IMP-003 | 🟠 | ⬜ | Unified color gating: `Colors`/`HeatmapColor` duplicate logic (source of BUG-004); no `TERM=dumb` check; macOS blanket-truecolor even under tmux |
| IMP-004 | 🟡 | ⬜ | Truncation hygiene: `[:25]`/`[:35]` slices cut mid-word, no ellipsis — `display.truncate_ansi()` exists and is unused |
| IMP-005 | 🟡 | ⬜ | `heatmap --details` lists all-time active days beneath a "last N weeks" grid — label/scope mismatch |
| IMP-006 | 🟠 | ⬜ | Test gaps: `parse_token_amount` edges (would catch BUG-003), `fetch_live_quota_summary` never mocked, exporter stdout paths untested; limits/statusline tests only accidentally hermetic (fake `/tmp/*.db` path live-probes if it exists) |
| IMP-007 | 🟡 | ⬜ | Stale epilog: `--help` omits `current`, `limits`, `statusline`, `live`, `monthly`, `--no-color` |
| IMP-008 | 🟡 | ⬜ | CSV emits formatted strings ("95.0%", "$3.91") — emit raw floats; statusline JSON lacks `badge_plain`/CSV parity; add NDJSON |
| IMP-009 | 🟡 | ⬜ | Add LICENSE file; SPDX license string; gitignore: add `.pytest_cache/`, `.coverage`, `.mypy_cache/`, `.ruff_cache/` |

---

## ✨ Feature Ideas

| ID | Idea | One-liner |
|----|------|-----------|
| FEAT-001 | `--since` / `--until` global date filters | Single choke point in `get_sessions()`; TerminalHeatmap already parameterizes by `end_date` |
| FEAT-002 | Burn-rate projection | "At current pace the 5h cap is hit at ~17:40" — inputs already computed in `limits.py` |
| FEAT-003 | Tier auto-detect | `GetUserStatus` already returns `user_tier_id` — map to Free/Pro/Ultra when `--tier` omitted |
| FEAT-004 | Sparkline in `statusline --short` | `render_sparkline()` is fully implemented and unused; 7-day token sparkline nearly free |
| FEAT-005 | macOS notifications | `osascript` hook on 🔴/🟡 threshold transitions — stays zero-dependency |
| FEAT-006 | `workspaces` subcommand | Ranked per-project token/cost leaderboard ("where did my quota go") |
| FEAT-007 | Per-command JSON | `session <id> --json` emits that session (+ steps); today `--json` dumps everything |
| FEAT-008 | New heatmap metrics | Cache-efficiency + hour-of-day/qta-peak: aggregator-only change, `metric=` arg exists |
| FEAT-009 | `limits --watch N` / `--ics` | Wall-mounted quota monitor reusing the `live` loop; `.ics` feed of predicted resets |
| FEAT-010 | Width-aware tables | Auto-shrink Title/Workspace columns via `shutil.get_terminal_size` + `truncate_ansi` |

---

## Suggested order of work

1. **Quick wins batch** (CLI-facing, cheap): BUG-001, BUG-002, BUG-003, BUG-014, BUG-023
2. **Perf batch:** OPT-001 (+ BUG-008 statusline/live probe TTL) — makes the <70 ms claim real
3. **Correctness batch:** BUG-005 (stale "Live" quota), BUG-007 (URI), BUG-010 (phantom model), BUG-011/012 (colors for used %, badge consistency)
4. **Robustness batch:** BUG-008/016/017/018 + IMP-006 tests, then the rest by priority

---

*Generated by a 3-agent code audit tracked against commit `a2904b2`. Confidence: A = runtime-reproduced, B = code-trace confirmed. Verify IDs against source before fixing if rebased.*
