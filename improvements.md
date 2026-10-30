# Improvements — agy-usage

Ergonomics, robustness, and hygiene ledger. Split from `BUG_TRACKER.md` on 2026-10-29; audit of commit `a2904b2`, dated 2026-10-28; current baseline `3599733` (PRs #1-#37 merged, 210 tests passing).

> **Status legend:** ⬜ Open · 🔧 In progress · ✅ Done · ❌ Won't do
> **Severity:** 🔴 High · 🟠 Med · 🟡 Low

## Items

| ID | Sev | Status | Summary | Related |
|-----|------|--------|---------|---------|
| IMP-001 | 🟠 | ✅ | Exit codes and stream discipline: `main()` returns 0 unconditionally; "not found" and "no data" print to stdout. Return 1, route those messages to stderr, keep data exports on stdout (PR #30, `9bdb7d4`) | FEAT-007 |
| IMP-002 | 🟠 | ✅ | Warn on silently-ignored inputs: `--5h-limit 5OM` falls back to the tier default with no warning; `--tmux` silently ignores `--short`/`--plain`; `--json`/`--csv` precedence on `limits` unannounced; `--dir` co-accepted with `--cli-only`/`--ide-only` (directory flag wins quietly) (PR #31, `7019697`) | BUG-024 |
| IMP-003 | 🟠 | ✅ | Color detection hygiene: the enable gate is now single-sourced (`HeatmapColor.supports_color()` delegates to `Colors.enabled()`; BUG-004/BUG-009 fixed) and depth detection (`supports_truecolor`) is now unified in `display.py` with `TERM=dumb` and macOS/tmux checks (PR #29, `5ee1ab3`) | BUG-004, BUG-009, BUG-021 |
| IMP-004 | 🟡 | ✅ | Truncation hygiene: displayed workspace/title/models cells now go through `display.truncate_ansi()`, so clipped text ends in `…` and keeps ANSI styling instead of hard-slicing mid-word (PR #32, `33ff01b`) | FEAT-010 |
| IMP-005 | 🟡 | ✅ | `heatmap --details` now clips rows to the rendered week window and labels the table `ACTIVE DAYS DETAILS (Last N Weeks)`; an empty window prints a hint instead of an empty table (PR #33, `2967f42`) | BUG-014 |
| IMP-006 | 🟠 | ✅ | Test gaps: quota/statusline tests are now hermetic via the patchable `limits.should_use_live_server()` seam; fake `/tmp/*.db` paths can no longer trigger a live probe, and explicit/auto/patched branches are covered (PR #34, `727f58f`) | BUG-003, BUG-005, OPT-001 |
| IMP-007 | 🟡 | ✅ | `--help` epilog now lists `statusline`, `live` and `--no-color`, and the subcommand numbering comments in `parse_args()` run 1-11 without repeats (PR #35) | — |
| IMP-008 | 🟡 | ✅ | `limits --csv` emits raw numbers for percentages and cost; statusline JSON/CSV gain `badge_plain`, and the statusline CSV drops the formatted `*_str` columns. NDJSON for other commands stays out of scope (PR #37, `3599733`) | BUG-024, BUG-020 |
| IMP-009 | 🟡 | ✅ | `.gitignore` now covers `.pytest_cache/`, `.coverage`, `.mypy_cache/` and `.ruff_cache/`; pinned by `test_gitignore_hygiene.py` (PR #36, `599cf66`) | BUG-023 |

## Notes

**IMP-001.** The dispatch decisions live in `main()`: return `1` for data-not-found, `2` for usage errors; user-facing warnings to stderr. Writes change call sites, so do it before FEAT-007, which needs those conventions.

**IMP-002.** Pair each silent fallback with one stderr line, and put `--dir`/`--cli-only`/`--ide-only` into an `add_mutually_exclusive_group` so the parser enforces exclusivity (currently accepted everywhere).

**IMP-003.** One `is_color_enabled()` in display consulted by both `Colors` and `HeatmapColor`, keeping truecolor depth detection separate (BUG-004 and BUG-009 already unified the enable gate via `Colors.enabled()`; what is left is moving the depth probe next to it and adding the `TERM=dumb` check).

**IMP-006.** Done since the audit: `parse_token_amount` edge cases (BUG-003), mocked `fetch_live_quota_summary` (`test_quota_cache_freshness.py`, `test_quota_cache_stale_display.py`, `test_limits_offline_fallback.py`), exporter stdout encoding (`test_exporter_unicode_stdout.py`). Closed in PR #34: auto-detection moved to the patchable `limits.should_use_live_server()` seam, the limits/statusline/multi-pool tests pin it off, and `test_live_server_seam.py` covers the explicit and automatic paths with `tempfile` fixtures. New tests go in a new `tests/test_<topic>.py` file (see `AGENTS.md`).

## Suggested order

1. **Ergonomics PR:** IMP-001 + IMP-002 + IMP-007 (small, unrelated diffs in one sweep)
2. **Colors PR:** IMP-003 (depth probe + `TERM=dumb`; the enable gate is already unified; before any heatmap or theme work)
3. **Test PR:** IMP-006 (build confidence before the OPT-001 cache lands)
4. **Polish:** IMP-004, IMP-005, IMP-008, IMP-009 at any point
