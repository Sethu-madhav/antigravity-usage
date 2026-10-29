# Improvements — agy-usage

Ergonomics, robustness, and hygiene ledger. Split from `BUG_TRACKER.md` on 2026-10-29; audit of commit `a2904b2`, dated 2026-10-28; current baseline `cda76f5`.

> **Status legend:** ⬜ Open · 🔧 In progress · ✅ Done · ❌ Won't do
> **Severity:** 🔴 High · 🟠 Med · 🟡 Low

## Items

| ID | Sev | Status | Summary | Related |
|-----|------|--------|---------|---------|
| IMP-001 | 🟠 | ⬜ | Exit codes and stream discipline: `main()` returns 0 unconditionally; "not found" and "no data" print to stdout. Return 1, route those messages to stderr, keep data exports on stdout | FEAT-007 |
| IMP-002 | 🟠 | ⬜ | Warn on silently-ignored inputs: `--5h-limit 5OM` falls back to the tier default with no warning; `--tmux` silently ignores `--short`/`--plain`; `--json`/`--csv` precedence on `limits` unannounced; `--dir` co-accepted with `--cli-only`/`--ide-only` (directory flag wins quietly) | BUG-024 |
| IMP-003 | 🟠 | ⬜ | One color gate: `display.Colors` and `heatmap` each carry near-duplicate tty/COLORTERM/NO_COLOR testing (root cause of BUG-004); no `TERM=dumb` check; macOS gets blanket truecolor even under tmux | BUG-004, BUG-009, BUG-021 |
| IMP-004 | 🟡 | ⬜ | Truncation hygiene: `[:25]`/`[:35]` slices cut mid-word with no ellipsis; `display.truncate_ansi()` exists and is unused | FEAT-010 |
| IMP-005 | 🟡 | ⬜ | `heatmap --details` lists all-time active days beneath a "last N weeks" grid; label the scope or clip to the rendered window | BUG-014 |
| IMP-006 | 🟠 | ⬜ | Test gaps: `parse_token_amount` edges (would catch BUG-003), `fetch_live_quota_summary` never mocked, exporter stdout paths untested; limits/statusline tests only accidentally hermetic (a fake `/tmp/*.db` path live-probes if such a file exists) | BUG-003, BUG-005, OPT-001 |
| IMP-007 | 🟡 | ⬜ | Stale epilog: `--help` omits `current`, `limits`, `statusline`, `live`, `monthly`, `--no-color`; subcommand numbering comments restart mid-file | — |
| IMP-008 | 🟡 | ⬜ | CSV emits formatted strings ("95.0%", "$3.91"); emit raw floats for spreadsheets. Statusline JSON lacks a plain badge field; consider NDJSON as a second machine format | BUG-024, BUG-020 |
| IMP-009 | 🟡 | ⬜ | Add a LICENSE file; SPDX string in pyproject; add `.pytest_cache/`, `.coverage`, `.mypy_cache/`, `.ruff_cache/` to `.gitignore` | BUG-023 |

## Notes

**IMP-001.** The dispatch decisions live in `main()`: return `1` for data-not-found, `2` for usage errors; user-facing warnings to stderr. Writes change call sites, so do it before FEAT-007, which needs those conventions.

**IMP-002.** Pair each silent fallback with one stderr line, and put `--dir`/`--cli-only`/`--ide-only` into an `add_mutually_exclusive_group` so the parser enforces exclusivity (currently accepted everywhere).

**IMP-003.** One `is_color_enabled()` in display consulted by both `Colors` and `HeatmapColor`, keeping truecolor depth detection separate (BUG-004's fix lands the diagnostic, the de-duplication prevents the next divergence).

**IMP-006.** Suggested additions: `parse_token_amount("inf" / "-5M" / "1e309")` (BUG-003), a mocked `fetch_live_quota_summary` for the default-probe path, an exporter stdout round-trip, and a test-only seam for `use_live_server` in `compute_all_pools`.

## Suggested order

1. **Ergonomics PR:** IMP-001 + IMP-002 + IMP-007 (small, unrelated diffs in one sweep)
2. **Colors PR:** IMP-003 (after BUG-004 diagnosis, before any heatmap or theme work)
3. **Test PR:** IMP-006 (build confidence before the OPT-001 cache lands)
4. **Polish:** IMP-004, IMP-005, IMP-008, IMP-009 at any point
