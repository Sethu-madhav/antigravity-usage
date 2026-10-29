# Improvements — agy-usage

Ergonomics, robustness, and hygiene ledger. Split from `BUG_TRACKER.md` on 2026-10-29; audit of commit `a2904b2`, dated 2026-10-28; current baseline `bd79c54` (PRs #1-#25 merged, 150 tests passing).

> **Status legend:** ⬜ Open · 🔧 In progress · ✅ Done · ❌ Won't do
> **Severity:** 🔴 High · 🟠 Med · 🟡 Low

## Items

| ID | Sev | Status | Summary | Related |
|-----|------|--------|---------|---------|
| IMP-001 | 🟠 | ⬜ | Exit codes and stream discipline: `main()` returns 0 unconditionally; "not found" and "no data" print to stdout. Return 1, route those messages to stderr, keep data exports on stdout | FEAT-007 |
| IMP-002 | 🟠 | ⬜ | Warn on silently-ignored inputs: `--5h-limit 5OM` falls back to the tier default with no warning; `--tmux` silently ignores `--short`/`--plain`; `--json`/`--csv` precedence on `limits` unannounced; `--dir` co-accepted with `--cli-only`/`--ide-only` (directory flag wins quietly) | BUG-024 |
| IMP-003 | 🟠 | ⬜ | Color detection hygiene: the enable gate is now single-sourced (`HeatmapColor.supports_color()` delegates to `Colors.enabled()`; BUG-004/BUG-009 fixed) but depth detection (`supports_truecolor`) is still separate; no `TERM=dumb` check; macOS gets blanket truecolor even under tmux | BUG-004, BUG-009, BUG-021 |
| IMP-004 | 🟡 | ⬜ | Truncation hygiene: `[:25]`/`[:35]` slices cut mid-word with no ellipsis; `display.truncate_ansi()` exists and is unused | FEAT-010 |
| IMP-005 | 🟡 | ⬜ | `heatmap --details` lists all-time active days beneath a "last N weeks" grid; label the scope or clip to the rendered window | BUG-014 |
| IMP-006 | 🟠 | ⬜ | Test gaps: the `parse_token_amount` edge cases, `fetch_live_quota_summary` (mocked cache/subprocess/urlopen paths) and exporter stdout encoding are now covered by BUG-003/005/020/027 tests; still open: limits/statusline tests only accidentally hermetic (a fake `/tmp/*.db` path live-probes if such a file exists) and no test-only seam for `use_live_server` | BUG-003, BUG-005, OPT-001 |
| IMP-007 | 🟡 | ⬜ | Stale epilog: `--help` examples now list `current`, `limits` and `monthly` but still omit `statusline`, `live` and `--no-color`; subcommand numbering comments restart mid-file | — |
| IMP-008 | 🟡 | ⬜ | CSV emits formatted strings ("95.0%", "$3.91"); emit raw floats for spreadsheets. Statusline JSON lacks a plain badge field (`live --json` already streams NDJSON since BUG-019; extend NDJSON to other commands only if wanted) | BUG-024, BUG-020 |
| IMP-009 | 🟡 | ⬜ | LICENSE file and SPDX `license = "MIT"` shipped with BUG-023 (PR #22); still open: add `.pytest_cache/`, `.coverage`, `.mypy_cache/`, `.ruff_cache/` to `.gitignore` | BUG-023 |

## Notes

**IMP-001.** The dispatch decisions live in `main()`: return `1` for data-not-found, `2` for usage errors; user-facing warnings to stderr. Writes change call sites, so do it before FEAT-007, which needs those conventions.

**IMP-002.** Pair each silent fallback with one stderr line, and put `--dir`/`--cli-only`/`--ide-only` into an `add_mutually_exclusive_group` so the parser enforces exclusivity (currently accepted everywhere).

**IMP-003.** One `is_color_enabled()` in display consulted by both `Colors` and `HeatmapColor`, keeping truecolor depth detection separate (BUG-004 and BUG-009 already unified the enable gate via `Colors.enabled()`; what is left is moving the depth probe next to it and adding the `TERM=dumb` check).

**IMP-006.** Done since the audit: `parse_token_amount` edge cases (BUG-003), mocked `fetch_live_quota_summary` (`test_quota_cache_freshness.py`, `test_quota_cache_stale_display.py`, `test_limits_offline_fallback.py`), exporter stdout encoding (`test_exporter_unicode_stdout.py`). Still suggested: make limits/statusline tests hermetic and add a test-only seam for `use_live_server` in `compute_all_pools`. New tests go in a new `tests/test_<topic>.py` file (see `AGENTS.md`).

## Suggested order

1. **Ergonomics PR:** IMP-001 + IMP-002 + IMP-007 (small, unrelated diffs in one sweep)
2. **Colors PR:** IMP-003 (depth probe + `TERM=dumb`; the enable gate is already unified; before any heatmap or theme work)
3. **Test PR:** IMP-006 (build confidence before the OPT-001 cache lands)
4. **Polish:** IMP-004, IMP-005, IMP-008, IMP-009 at any point
