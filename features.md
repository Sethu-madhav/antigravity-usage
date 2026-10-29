# Features — agy-usage

Feature backlog. Split from `BUG_TRACKER.md` on 2026-10-29; audit of commit `a2904b2`, dated 2026-10-28; current baseline `cda76f5`. All feasibility notes were checked against the code at audit time.

> **Status legend:** 💡 Idea · 🔍 Scoped · 🔧 In progress · ✅ Shipped · ❌ Rejected
> **Slice:** s = small (a PR or less) · m = medium (design + one PR) · l = large (design doc first)

## Backlog

| ID | Status | Slice | Feature | Feasibility notes | Related |
|-----|--------|-------|-----------------|-------------------|---------|
| FEAT-001 | 💡 | m | `--since`/`--until` global date filters | One choke point in `get_sessions()`; `TerminalHeatmap(end_date=...)` already parameterizes the grid | — |
| FEAT-002 | 💡 | m | Burn-rate projection | Extrapolate from the already-computed cycle start, timestamps, and estimated cost: "the 5h cap is hit around 17:40" | — |
| FEAT-003 | 💡 | s | Tier auto-detect | `GetUserStatus` already returns `user_tier_id`; map to Free/Pro/Ultra when `--tier` omitted and print "Tier: auto-detected (Pro)" | — |
| FEAT-004 | 💡 | s | Sparkline in `statusline --short` | `render_sparkline()` is fully implemented and unused; a 7-day token sparkline needs one `aggregate_daily` slice | — |
| FEAT-005 | 💡 | s | macOS notifications | `osascript` hook on health transitions; stays zero-dependency; gate behind a `--notify <pct>` threshold | IMP-003 |
| FEAT-006 | 💡 | m | `workspaces` subcommand | Ranked per-project token/cost leaderboard; `compute_summary` already computes the unique-workspace set | IMP-001 |
| FEAT-007 | 💡 | m | Per-command JSON output | `session <id> --json` emits that session (steps included via `to_dict(include_steps=True)`). Must decide the global-export gate in `main()`: today `--json` preempts 9 of 11 subcommands | BUG-019, BUG-024, IMP-001 |
| FEAT-008 | 💡 | s | New heatmap metrics | Cache-efficiency and hour-of-day as `--metric` values; aggregator-only change, the `metric=` plumbing exists | IMP-003 |
| FEAT-009 | 💡 | m | `limits --watch N` and `--ics` | Reuse the `cmd_live` refresh loop for a wall-mounted quota monitor; `.ics` of predicted reset times from existing `refresh_time` values | — |
| FEAT-010 | 💡 | m | Width-aware tables | Auto-shrink Title/Workspace columns via `shutil.get_terminal_size` plus `truncate_ansi` | IMP-004 |

## Design decisions to make before building

**FEAT-007** is the highest-value item because it also settles the global-export trap flagged in the PR #1 review: decide whether `--json`/`--csv` mean "export the current view" (per command) or "export everything" (root only). Carrying BUG-019 (`live --json`) and BUG-024 (`statusline --csv`) through the same decision avoids three half-answers.

**FEAT-002** depends on pool windows being trustworthy: BUG-018 (time fallbacks skewing window anchors) should land first, otherwise projections inherit skewed anchors.

**FEAT-010** should reuse whatever IMP-004 turns `truncate_ansi` into, adding a `width:` budget per table rather than fixed slices.

## Suggested order

1. **Quick PRs:** FEAT-004 + FEAT-003 + FEAT-008 (each independently acceptable to merge)
2. **After IMP-001/IMP-003:** FEAT-006, then the FEAT-007 semantic decision (write it as a short design note in the PR body)
3. **Later:** FEAT-001, FEAT-002, FEAT-005, FEAT-009, FEAT-010
