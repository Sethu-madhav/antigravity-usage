# Bug Tracker — agy-usage

> **Baseline:** commit `cda76f5` (audit of `a2904b2`, 2026-10-28; ~5,000 LOC; suite was 29/29 then)
>
> **Confidence legend:**
> **A** = reproduced at runtime · **B** = confirmed by code trace (single-source of failure, quoted evidence)
> **Sev:** 🔴 High · 🟠 Med · 🟡 Low
>
> **Status legend:** ⬜ Open · 🔧 In progress · ✅ Fixed · ❌ Won't fix

---

## Summary

| Ledger | File | Items | Open | High | Med | Low |
|--------|------|------:|-----:|-----:|----:|----:|
| 🐛 Bugs (this file) | `BUG_TRACKER.md` | 24 | 22 | 3 | 10 | 9 |
| ⚡ Optimizations | [optimizations.md](optimizations.md) | 8 | 8 | 1 | 3 | 4 |
| 🔧 Improvements | [improvements.md](improvements.md) | 9 | 9 | 0 | 3 | 6 |
| ✨ Features | [features.md](features.md) | 10 | 10 | n/a | n/a | n/a |

> **Progress:** BUG-001 and BUG-002 fixed by PR #1 (merged, commit `44ecdc2`, merge of
> `8b95649`, 2026-10-29). BUG-024 was created from its post-merge review.

---

## 🐛 Bugs

| ID  | Sev | Conf | Status | Location | Summary |
|-----|-----|------|--------|----------|---------|
| BUG-001 | 🔴 | A | ✅ | `cli.py:87-95` | Global flags fail after subcommand |
| BUG-002 | 🔴 | A | ✅ | `cli.py:265-269, 319-323` | Root `--json/--csv` silently discarded before `limits`/`statusline` (parse-level clobber fixed; statusline `--csv` split out as BUG-024) |
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
| BUG-024 | 🟠 | A | ⬜ | `cli.py:1222` (`cmd_statusline`) | `statusline --csv` accepted but ignored: prints one-line text, exit 0 (found post-merge by Deepseek review of PR #1; cmd_statusline only reads `args.json`) |

### Details

**BUG-001 · Global flags fail after subcommand · 🔴 A** ✅ fixed by PR #1
`agy-usage sessions --no-color`, `current --json`, `heatmap --csv` all hard-failed with
`error: unrecognized arguments` — shared flags existed only on the root parser.
**Fix (shipped):** hidden parent parser (`add_help=False`) passed via `parents=[common]`
to every subparser; commit `8b95649`.

**BUG-002 · Root `--json/--csv` discarded before `limits`/`statusline` · 🔴 A** ✅ fixed by PR #1
Subparser re-declared flags merged over root-level parsed values.
**Fix (shipped):** parent copies use `default=argparse.SUPPRESS`; local re-declarations
removed. Parse-level behavior verified end-to-end for `limits` JSON/CSV. The statusline
CSV gap is carved out as BUG-024.

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
implement JSON-per-tick with `--interval`. Decide together with FEAT-007
(see features.md) and BUG-024.

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

**BUG-024 · `statusline --csv` silently ignored · 🟠 A**
Discovered by the Deepseek review of PR #1 (it examined the branch after the user merged
it; verdict REQUEST CHANGES). `cmd_statusline` never reads `args.csv`, so both
`agy-usage --csv statusline` and `agy-usage statusline --csv` render the normal one-line
status text and exit 0. Pre-fix (b06314f) the invocation even parsed True already, which
also makes the old test assertion `['--csv','statusline']` vacuous. Related review note:
`main()`'s early global-export branch still preempts 9 of 11 subcommands when
`--json/--csv` is set (that design predates this tracker; per-command JSON is FEAT-007 and
the `live --json` gap is BUG-019).
**Fix (choose one):** emit a one-row CSV from the computed pools the way `cmd_limits`
does, or reject the flag (`error: --csv is not supported by 'statusline'` to stderr,
exit 2) and drop `--csv` from statusline's help. Also drop or replace the vacuous
`--csv statusline` parse assertion with a behavioral test on `main()`.

---

## Suggested order of work

1. **Quick wins batch** (CLI-facing, cheap): BUG-003, BUG-014, BUG-023, BUG-024
2. **Perf batch:** [optimizations.md](optimizations.md) OPT-001 (pairs with BUG-005's cache-TTL discipline)
3. **Correctness batch:** BUG-005, BUG-007, BUG-010, BUG-011/012
4. **Robustness batch:** BUG-008/016/017/018 + [improvements.md](improvements.md) IMP-006 tests, then the rest by priority

---

*Generated by a 3-agent code audit of commit `a2904b2` on 2026-10-28; PR #1 (BUG-001/002) merged 2026-10-29. Confidence: A = runtime-reproduced, B = code-trace confirmed. Verify IDs against source before fixing if rebased.*
