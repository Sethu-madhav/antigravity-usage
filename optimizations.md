# Optimizations — agy-usage

Performance ledger. Split from `BUG_TRACKER.md` on 2026-10-29; audit of commit `a2904b2`, dated 2026-10-28; current baseline `38f9ca6` (PRs #1-#41 merged, 232 tests passing).

> **Status legend:** ⬜ Open · 🔧 In progress · ✅ Done · ❌ Won't do
> **Severity:** 🔴 High · 🟠 Med · 🟡 Low
> **Confidence:** B for all rows (verified by code trace during the audit)

## Items

| ID | Sev | Status | Location | Summary | Related |
|-----|------|--------|----------|---------|---------|
| OPT-001 | 🔴 | ✅ | `parser.py` (all entry points) | No incremental parse cache: every command re-parses every database on every run (PR #41, `38f9ca6`) | BUG-005, BUG-024, IMP-006 |
| OPT-002 | 🟠 | ✅ | `cli.py:832-844` | `live` re-scans everything per tick, clears the screen before the slow work, and runs daily aggregation twice per tick (PR #40, `b0b18a1`) | OPT-001 |
| OPT-003 | 🟠 | ✅ | `models.py:330-333, 377-382` | `step.cost` and `s.cost` recompute pricing on every access; `get_rates` does an unmemoized substring scan (PR #39, `6f10e99`) | OPT-001 |
| OPT-004 | 🟡 | ⬜ | `aggregator.py:64,97,177` | `aggregate_daily` recomputed about 3x per run, about 7x per JSON export | — |
| OPT-005 | 🟡 | ⬜ | `limits.py:654-735` | `_collect_pool_items` called 3x, each call re-walking every step through `classify_model_pool` | — |
| OPT-006 | 🟡 | ⬜ | `parser.py:503-507` | Linear reverse scan over model anchors per step, O(n·m) overall | — |
| OPT-007 | 🟡 | ⬜ | `display.py:293,306` | ANSI regex recompiled per table cell | — |
| OPT-008 | 🟡 | ⬜ | `parser.py:453-456` | `cursor.fetchall()` materializes every metadata blob before processing begins | — |

## Notes

**OPT-001.** The width: statusline (< 70 ms claim per README), `live`, and every other command re-protobuf-decode every frozen old database on each invocation. Approach: cache parsed `SessionSummary` values keyed on (path, size, mtime) in `~/.cache/agy-usage/` as stdlib JSON; reparse only files whose fingerprint changed; embed a cache-format version field so schema growth cannot poison old entries. Do OPT-003 first, then let cached summaries carry precomputed costs, so the cache stores serialized summaries and pricing becomes one-time. This single change accounts for most of the performance value in this ledger.

**OPT-002.** With OPT-001 in place, `cmd_live` still must render into an `io.StringIO` and write it after the scan so the user sees the old frame while scanning, and reuse one `aggregate_daily` result between `compute_summary` and the heatmap.

**OPT-003.** `functools.cached_property` on both cost properties plus a module-level dict memo for `get_rates` on exact model names. Cheap and self-contained; do it first.

**OPT-004 to OPT-008.** Micro batch: compute daily once and pass it through (OPT-004); one partitioning pass filling three lists in `compute_all_pools` (OPT-005); `bisect.bisect_right` over a prebuilt anchor index array (OPT-006); hoist `_ANSI_RE` to module level and share it between `strip_ansi` and `truncate_ansi` (OPT-007); iterate the cursor directly instead of `fetchall()` (OPT-008).

## Suggested order

1. **Micro PR:** OPT-004 + OPT-003 (simple, testable, no behavior change)
2. **Cache PR:** OPT-001, touching `parser.py` plus a new `agy_usage/cache.py`; carry the quota-cache rules through the same review (BUG-005's "never labeled Live" and reset-time checks, as relaxed by PR #24: a cache up to 6 h old is shown as a dated reading)
3. **Live rework:** OPT-002
4. **Sweep:** OPT-005 through OPT-008 in one "parser micro-optimizations" PR with before/after timings in the body
