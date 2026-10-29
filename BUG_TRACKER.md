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
| 🐛 Bugs (this file) | `BUG_TRACKER.md` | 26 | 17 | 0 | 7 | 10 |
| ⚡ Optimizations | [optimizations.md](optimizations.md) | 8 | 8 | 1 | 3 | 4 |
| 🔧 Improvements | [improvements.md](improvements.md) | 9 | 9 | 0 | 3 | 6 |
| ✨ Features | [features.md](features.md) | 10 | 10 | n/a | n/a | n/a |

> **Progress:** BUG-001 and BUG-002 fixed by PR #1 (merged, commit `44ecdc2`, merge of
> `8b95649`, 2026-10-29); BUG-024 was created from its post-merge review.
> BUG-003 fixed by PR #2 (2026-10-29). BUG-004 fixed by PR #3 (2026-10-29).
> BUG-006 fixed by PR #4 (stacked on #3; merge `5f1f4b8`, 2026-10-29); BUG-025 was created from its
> post-merge DeepSeek V4.1 Flash review. BUG-005 fixed by PR #6 (merge `76dbad0`, 2026-10-29).
> BUG-007 fixed by PR #5 (merge `118fb51`, 2026-10-29). BUG-008 fix open as PR #7.
>
> **Settle check 2026-10-29 (coordinator):** master suite 46/46 after PRs #3-#6 (each fix PR
> carries tests); BUG-004 re-verified at runtime with COLORTERM forced (86 ANSI escapes in
> plain `heatmap -w 4` output vs 0 under `--no-color`).
>
> PR #7 (BUG-008) review verdict: APPROVE WITH NITS, no blockers (DeepSeek V4.1 Flash);
> nits filed as BUG-026. Merge + settle check pending. Note: PR #7 carries its own
> BUG_TRACKER.md hunks based on the pre-settle ledger, so integration + tracker resolution
> is the coordinator's settle step.

---

## 🐛 Bugs

| ID  | Sev | Conf | Status | Location | Summary |
|-----|-----|------|--------|----------|---------|
| BUG-001 | 🔴 | A | ✅ | `cli.py:87-95` | Global flags fail after subcommand |
| BUG-002 | 🔴 | A | ✅ | `cli.py:265-269, 319-323` | Root `--json/--csv` silently discarded before `limits`/`statusline` (parse-level clobber fixed; statusline `--csv` split out as BUG-024) |
| BUG-003 | 🔴 | A | ✅ | `limits.py:210-236` | `parse_token_amount` crashes on `inf`, accepts negatives |
| BUG-004 | 🔴 | A | ✅ | `cli.py` + `heatmap.py` + `display.py` | `--no-color` does not disable heatmap colors |
| BUG-005 | 🔴 | B | ✅ | `limits.py:375-382, 644-649` | Stale quota cache served as authoritative "Live" data (no TTL) |
| BUG-006 | 🟠 | A | ✅ | `heatmap.py:178-189` | Month labels drift right, point at wrong weeks (dead `chars_consumed`) |
| BUG-007 | 🟠 | B | ✅ | `parser.py:266-269` | Unquoted SQLite URI + silent read-write fallback (fixed by PR #5, merge `118fb51`) |
| BUG-008 | 🟠 | B | ⬜ | `parser.py:549-551` | One exception kills whole session parse (flickering in `live`) |
| BUG-009 | 🟠 | B | ⬜ | `display.py:48-53, heatmap.py:42-44` | Colors leak into piped output when `COLORTERM` set |
| BUG-010 | 🟠 | B | ⬜ | `parser.py:371-377` | Phantom `gemini-3.8-flash` attribution for unresolvable rows |
| BUG-011 | 🟠 | B | ⬜ | `cli.py:1035-1054, display.py:107-114` | "Used %" quota colored green (thresholds inverted for `used`) |
| BUG-012 | 🟠 | B | ⬜ | `limits.py` vs `cli.py:1146-1186` | Health badge thresholds disagree (60/80% vs 80% only) |
| BUG-013 | 🟠 | B | ✅ | `limits.py:176-194` | `tokens_remaining` ignores cost/server-fraction quota model (fixed by PR #12, merge `536926b`) |
| BUG-014 | 🟡 | A | ✅ | `cli.py` (`-w` flag) | `-w 500` accepted (help says "up to 52"), renders 500 columns (fixed by PR #14, merge `359cf2d`) |
| BUG-015 | 🟡 | B | ⬜ | `parser.py:242-243` | No dedup: session in both CLI+IDE dirs → tokens double-counted |
| BUG-016 | 🟡 | B | ⬜ | `parser.py:213-217, 246-251` | `getmtime` in sort key crashes whole scan if DB vanishes mid-scan |
| BUG-017 | 🟡 | B | ⬜ | `parser.py:15-26` | Unbounded varint decode (no 10-byte/64-bit cap) |
| BUG-018 | 🟡 | B | 🔧 | `limits.py` (`_compute_cycle_window`) + `parser.py` (step loop) | mtime fallback drags old steps into 5h window; future-dated step hijacks anchor |
| BUG-019 | 🟡 | B | ⬜ | `cli.py:1287-1300` | `live` + `--json` semantics incoherent (banner becomes one-shot dump) |
| BUG-020 | 🟡 | B | ⬜ | `exporter.py:117-119` | `UnicodeEncodeError` on non-UTF-8 stdout (Windows cp1252) |
| BUG-021 | 🟡 | B | ⬜ | `display.py:56-60` | Nested color calls: inner `RESET` wipes outer attribute |
| BUG-022 | 🟡 | B | ⬜ | `parser.py:240` | `--dir` path skips chronological re-sort (mtime vs start_time) |
| BUG-023 | 🟠 | B | ⬜ | `setup.py` + `pyproject.toml` + `__init__.py` | Triplicated metadata/entry-point; no LICENSE file |
| BUG-024 | 🟠 | A | ⬜ | `cli.py:1222` (`cmd_statusline`) | `statusline --csv` accepted but ignored: prints one-line text, exit 0 (found post-merge by Deepseek review of PR #1; cmd_statusline only reads `args.json`) |
| BUG-025 | 🟡 | A | ⬜ | `heatmap.py:176-198` | Adjacent first-month labels: middle month's label dropped (w0's 2-col span skips w1) — found post-merge by DeepSeek V4.1 Flash review of PR #4 |
| BUG-026 | 🟡 | B | ⬜ | `parser.py:478-508` | Partial-parse hardening nits from the PR #7 review (parent DBError catch, mtime guard, silent row-skip, missing test cases) |

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

**BUG-003 · `parse_token_amount` crashes on `inf`, accepts negatives · 🔴 A** ✅ fixed by PR #2
Only `ValueError` was caught. Repro: `"inf"` → `OverflowError`, `"1e309"` → `OverflowError`,
`"-5M"` → `-5000000`.
**Fix (shipped):** `int(float(s) * multiplier)` wrapped in `except (ValueError, OverflowError)`
→ None; results `< 1` rejected, so negatives, zero, and sub-token fractions fall back to the
tier defaults (downstream honors custom limits only when positive).
Note: the originally suggested `if not (val >= 1)` guard was insufficient alone —
`inf >= 1` is True in Python, so infinity still reached `int()` and crashed; the exception
handler covers that path. Known remaining gap (pre-existing, out of scope): huge-but-finite
values like `"1e308"` are still accepted.

**BUG-004 · `--no-color` doesn't disable the heatmap · 🔴 A** ✅ fixed by PR #3
`main()` patched `display.Colors` only; `HeatmapColor.cell()` consulted its own
`supports_color()` — RGB escapes still emitted with the patch applied. `render()`
also wrapped month header / weekday labels / legend / summary in DIM/BOLD/CYAN
escapes unconditionally.
**Fix (shipped):** module-level `display._COLOR_OVERRIDE` switch with
`Colors.set_override()`; `HeatmapColor.supports_color()` now delegates to
`Colors.enabled()` and its `wrap()` helper styles text only when colors are on.
`main()` resets the switch to auto-detect then sets it instead of monkey-patching
the classmethod. Tests: switch-forced differential render checks (override on/off),
deterministic auto-detect coverage, and `main()` wiring incl. a real render
integration pass (38/38 pass).

**BUG-005 · Stale quota cache shown as "Live" · 🔴 B**
`fetch_live_quota_summary` falls back to `agy_usage_quota_cache.json` whenever the language
server is down; `fetched_at` is stored but never validated → hours-old `remainingFraction`
renders under "Live Antigravity Server", and past-`resetTime` fractions get grafted onto
freshly computed windows. **Fix:** reject cache older than ~60 s; only use
`remaining_fraction` when `reset_dt > now`.
**Fix (shipped):** `fetch_live_quota_summary` now rejects the cached payload when
`fetched_at` is older than ~60 s (parsed with `datetime.fromisoformat`, naive stamps
treated as UTC), each bucket's `remaining_fraction` is honored only while its
`resetTime` is still in the future, and a stale or invalid cache returns `None` so
`live_synced` goes false instead of rendering hours-old fractions under "Live
Antigravity Server". Tests: fresh/ expired/ already-reset-bucket coverage without
network. ✅ fixed by PR #6 (merge `76dbad0`).

**BUG-006 · Heatmap month labels drift · 🟠 A**
Each week column is 2 chars, but a 3-char label advances `col_idx` by only 1 — drift grows
to +11 chars at 52 weeks. Dead variable `chars_consumed = 2` (line 185).
**Fix:** `cols_skip = max(1, -(-len(name) // 2))`; `ljust(cols_skip * 2)`; `col_idx += cols_skip`.
**Fix (shipped):** labels now advance the cursor by their true 2-char-column footprint and
pad to exactly that width; dead `chars_consumed` deleted. Stacked PR #4 merged after PR #3
(`5f1f4b8`, 2026-10-29). Reviewed post-merge by DeepSeek V4.1 Flash: APPROVE WITH NITS
(1,413-window fuzz, zero geometry failures besides adjacent-first-labels, 40/40 tests);
the one real edge case is logged as BUG-025.

**BUG-007 · SQLite URI quoting + RW fallback · 🟠 B**
`f"file:{db_path}?mode=ro"`: paths containing `?`/`#`/`%` corrupt the URI; the bare
`except: sqlite3.connect(db_path)` then opens the DB **read-write** (also creates the file
if it vanished — TOCTOU), contradicting the README's read-only guarantee.
**Fix:** `file:{quote(db_path)}?mode=ro` (urllib.parse.quote); drop/log the fallback.
**Fix (shipped):** URI built as `file:{quote(db_path)}?mode=ro` with `uri=True`, `quote()`
keeping `/` intact for ordinary absolute paths (`parser.py:268-272`); the bare-read-write
fallback is gone. PR #5 merged (`118fb51`, 2026-10-29) with quoted-URI tests.

**BUG-008 · One exception kills whole session parse · 🟠 B**
Outer try wraps everything; a transient `database is locked` discards all already-parsed
steps/title/workspace (despite the "partial summary" comment) → sessions flicker out of
`live` dashboards. **Fix:** catch `sqlite3.OperationalError` separately; emit partial
summary in `finally`.

**Review (PR #7):** DeepSeek V4.1 Flash — **APPROVE WITH NITS, no blockers** (2026-10-29).
Verified: suite 49 on the branch (46 master + 3 new), and the tests pin the bug for real
(2 of 3 new tests failed on pre-fix master, rerun in a temporary worktree). Hardening
follow-ups filed as BUG-026.

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

**BUG-013 · `tokens_remaining` ignores cost/server quota · 🟠 B** ✅ fixed by PR #12
Always `max(0, limit_tokens - total_tokens)` while `pct_used` prefers
`server_remaining_fraction`/cost → contradictory "100% used / 2.75B remaining". Token
pct also unclamped (>100%).
**Fix (shipped):** `WindowStats.tokens_remaining` returns 0 if `is_exceeded` or
`limit_tokens <= 0`, and derives from `pct_remaining` when `server_remaining_fraction`
or cost model is active (`round(limit_tokens * pct_remaining / 100)`), clamped to
`[0, limit_tokens]`; `pct_used` token branch clamped to `min(100.0, ...)`. Tests:
server fraction exhausted/partial, cost limit exceeded/partial, token clamping/partial,
and zero limit (76/76 suite pass). Reviewed by Claude Sonnet 5.5: APPROVE WITH NITS;
merged as commit `536926b` (2026-10-29).

**BUG-014 · `-w` unclamped · 🟡 A** ✅ fixed by PR #14
`parse_args(["heatmap", "-w", "500"])` → accepted; help says "up to 52" → 2,000+ char
output wraps into garbage.
**Fix (shipped):** `-w`/`--weeks` clamped to `[4, 52]` (`max(4, min(52, weeks))`) in both
`parse_args` (`cli.py`) and `TerminalHeatmap.__init__` (`heatmap.py`); `required_cols` and
`is_terminal_narrow` added to `TerminalHeatmap`; yellow warning emitted in `cmd_heatmap`
when terminal width is narrower than required columns (respects `--no-color`). Tests:
`parse_args` clamping across boundaries (-10, 0, 1, 4, 26, 52, 100, 500), `TerminalHeatmap`
clamping, terminal width / narrow detection, and `cmd_heatmap` warning output with/without
color (80/80 suite pass). Reviewed by Claude Sonnet 5.5: APPROVE WITH NITS; merged as commit
`359cf2d` (2026-10-29).

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

**BUG-018 · Timestamp fallbacks skew quota windows · 🟡 B** 🔧 fix in review
Steps without timestamps inherit the file's `getmtime` (can pull an old session into the
current 5h window); a future-dated step re-anchors the cycle in the future → whole window
renders 0% used. **Fix:** clamp fallback to `[first_ts, now]`; skip steps > now + skew.
**Fix (in review):** untimestamped steps no longer take the DB mtime; `_fill_missing_step_timestamps`
(`parser.py`) gives them the nearest preceding timestamped step, else the nearest following one;
sessions with no step timestamps fall back to the blob start time, then `min(db_mtime, now)`,
else the step is dropped. `_compute_cycle_window` (`limits.py`) drops items more than
`_FUTURE_SKEW` (5 min) past `current_time` and clamps items within the skew to `current_time`,
so a future step can't re-anchor the cycle. Tests: `TestTimestampFallbacks` (4, all fail on the
old code); `test_unreadable_mtime_skips_untimed_rows` updated to the new semantics (98/98 suite
pass). Coded by Gemini 3.8 Flash (High), reviewed by Claude Opus 5.5.

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

**BUG-025 · Adjacent first-month labels: middle month dropped · 🟡 A** ⬜
Found post-merge by the DeepSeek V4.1 Flash review of PR #4 (BUG-006 fix). When the
window's first Monday is the last Monday of a month, w0/w1 are both month starts; w0's
label spans 2 columns, so the loop crosses w1's label slot and never prints it — the
middle month vanishes (`weeks=26, end=2026-01-19` renders `Jul … Sep` missing `Aug`;
1,413-window fuzz hit ~23% of 365 sampled end dates, first two columns only). Pre-fix
output overlapped labels (`JulAug`) instead.
**Fix idea:** when both `month_positions[0]` and `[1]` exist, drop the leading
partial-month (mostly-offscreen) label or compact the pair; pin with a regression test
alongside BUG-006's pinned layout test. Related cleanup candidate: dead
`month_line_chars` (`heatmap.py:173`).

**BUG-026 · Partial-parse hardening nits from the PR #7 review · 🟡 B** ⬜
Logged by the DeepSeek V4.1 Flash review of PR #7 (APPROVE WITH NITS, no blockers):
- `parser.py:498`: only `sqlite3.OperationalError` is caught outside the step guard; a
  plain `sqlite3.DatabaseError` (corrupt DB) mid-steps-read discards already-parsed
  title/workspace/first_ts, inconsistent with the keep-what-we-read policy. Catch the
  parent or document intent.
- `parser.py:503-508`: `datetime.fromtimestamp` can raise `OverflowError`/`ValueError`,
  only `OSError` is caught; suggest `except (OSError, OverflowError, ValueError)`.
- Per-step guard: today unreachable via real corruption and silently swallows programming
  errors; worth narrowing/logging eventually. Invariant note: `steps.append` precedes
  `aggregated_usage.add` inside the same try.
- Test gaps (non-blocking): lock during the title/gen_metadata query (start_time None
  hole), plain `DatabaseError` mid-steps-read, empty steps table still emits the session,
  missing-mtime skip path.

---

## Suggested order of work

1. **Cleared, awaiting merge:** BUG-008 (PR #7, review APPROVE WITH NITS) — coordinator resolves the carried tracker hunks at integration, then settles
2. **Quick wins batch** (CLI-facing, cheap): BUG-014, BUG-023, BUG-024
3. **Correctness batch:** BUG-010, BUG-011/012
4. **Robustness batch:** BUG-016/017/018 + [improvements.md](improvements.md) IMP-006 tests, then the rest by priority
5. **Follow-up:** BUG-025 (label regression logged by the PR #4 review)

---

*Generated by a 3-agent code audit of commit `a2904b2` on 2026-10-28; PR #1 (BUG-001/002) merged 2026-10-29. Confidence: A = runtime-reproduced, B = code-trace confirmed. Verify IDs against source before fixing if rebased.*

---

## Working agreements

> Adopted 2026-10-29 after the parallel-lane collisions. These bind every thread working in
> this repo.

- **Ledger owner:** the coordinator thread ("Repo Bug and Improvement Review") is the only
  writer of the four ledgers (`BUG_TRACKER.md`, `optimizations.md`, `improvements.md`,
  `features.md`); ledger edits land as docs-only commits on `master`.
- **No ledger edits in code PRs:** fix branches must not touch the ledgers; the coordinator
  flips statuses at settle time. (PRs #2-#7 predate the rule and are grandfathered.)
- **One worktree per lane:** every worker owns a dedicated worktree and branch; the main
  checkout stays on `master`, pulls only, and never hosts lane commits or branch switches.
- **Verify before ✅:** a row flips to ✅ only after the coordinator runs a runtime check at
  settle time (green suite is the baseline; targeted probes where cheap).
- **Review gate:** a Deepseek review (`opencode/deepseek-v4.1-flash-free`) runs on every code
  PR before merge; verdicts and follow-up bugs (BUG-024, BUG-025) are recorded here.
- **Conflict check first:** run `git fetch origin` + `gh pr list` before any ledger edit; on
  collision, `git restore` the file and re-apply on top of the newer side.
