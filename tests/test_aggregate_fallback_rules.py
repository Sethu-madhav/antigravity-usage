"""Pins the step/session fallback rules that differ between the aggregations."""

import unittest
from datetime import date, datetime

from agy_usage.aggregator import UsageAggregator
from agy_usage.models import SessionSummary, StepRecord, TokenUsage, normalize_model_name


def _step(idx, ts, model, prompt=100, cached=50, output=10):
    return StepRecord(
        step_idx=idx,
        timestamp=ts,
        model=model,
        usage=TokenUsage(prompt_tokens=prompt, cached_tokens=cached, output_tokens=output),
    )


def _stepful(session_id, steps, models):
    usage = TokenUsage()
    for st in steps:
        usage = usage.add(st.usage)
    return SessionSummary(
        session_id=session_id,
        source="cli",
        file_path=f"/tmp/{session_id}.db",
        models=models,
        start_time=steps[0].timestamp,
        end_time=steps[-1].timestamp,
        step_count=len(steps),
        usage=usage,
        steps=steps,
    )


def _stepless(session_id, models, step_count, start_time, usage=None):
    return SessionSummary(
        session_id=session_id,
        source="ide",
        file_path=f"/tmp/{session_id}.db",
        models=models,
        start_time=start_time,
        end_time=start_time,
        step_count=step_count,
        usage=usage or TokenUsage(prompt_tokens=1000, cached_tokens=300, output_tokens=70),
    )


class TestAggregateFallbackRules(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 9, 1, 10, 30)
        self.stepful = _stepful(
            "with-steps",
            [
                _step(1, datetime(2026, 9, 1, 9, 0), "gemini-3.8-flash"),
                _step(2, datetime(2026, 9, 1, 9, 45), "gemini-3.7-flash"),
                _step(3, datetime(2026, 9, 2, 23, 0), ""),
            ],
            ["gemini-3.8-flash", "gemini-3.7-flash"],
        )
        self.dated = _stepless(
            "stepless-dated", ["gemini-3.7-flash", "gemini-3.8-flash"], 6, self.start
        )
        self.undated = _stepless("stepless-undated", ["gemini-3.7-flash"], 4, None)

    def _all(self):
        return [self.stepful, self.dated, self.undated]

    def test_undated_stepless_session_is_skipped_by_daily_and_hourly(self):
        sessions = [self.undated]
        self.assertEqual(UsageAggregator.aggregate_daily(sessions), {})
        self.assertEqual(UsageAggregator.aggregate_hourly(sessions), {})

        daily = UsageAggregator.aggregate_daily(self._all())
        for item in daily.values():
            self.assertNotIn("stepless-undated", item.session_ids)

    def test_undated_stepless_session_counts_in_models(self):
        models = UsageAggregator.aggregate_models([self.undated])
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].model_name, normalize_model_name("gemini-3.7-flash"))
        self.assertEqual(models[0].request_count, self.undated.step_count)
        self.assertEqual(models[0].session_count, 1)
        self.assertEqual(models[0].usage.to_dict(), self.undated.usage.to_dict())

    def test_stepless_session_without_models_is_keyed_unknown(self):
        s = _stepless("no-models", [], 3, None)
        models = UsageAggregator.aggregate_models([s])
        self.assertEqual([m.model_name for m in models], ["unknown"])
        self.assertEqual(models[0].request_count, 3)

    def test_dated_stepless_session_daily_uses_session_totals(self):
        daily = UsageAggregator.aggregate_daily([self.dated])
        self.assertEqual(list(daily), [date(2026, 9, 1)])
        item = daily[date(2026, 9, 1)]
        self.assertEqual(item.steps_count, self.dated.step_count)
        self.assertEqual(
            item.models, {normalize_model_name(m) for m in self.dated.models}
        )
        self.assertEqual(len(item.models), 2)
        self.assertEqual(item.usage.to_dict(), self.dated.usage.to_dict())

    def test_stepful_daily_counts_one_per_step_and_keeps_empty_model(self):
        daily = UsageAggregator.aggregate_daily([self.stepful])
        self.assertEqual(daily[date(2026, 9, 1)].steps_count, 2)
        self.assertEqual(daily[date(2026, 9, 2)].steps_count, 1)
        # An empty step model is normalized but not replaced with "unknown" in daily.
        self.assertEqual(daily[date(2026, 9, 2)].models, {normalize_model_name("")})
        # Models aggregation, by contrast, folds the empty name into "unknown".
        names = [m.model_name for m in UsageAggregator.aggregate_models([self.stepful])]
        self.assertIn("unknown", names)
        self.assertEqual(sum(1 for m in UsageAggregator.aggregate_models([self.stepful])
                             if m.model_name == "unknown"), 1)

    def test_models_request_count_per_step_vs_per_session(self):
        by_name = {m.model_name: m for m in UsageAggregator.aggregate_models(self._all())}
        flash37 = by_name[normalize_model_name("gemini-3.7-flash")]
        # 1 step from the stepful session + dated (6) uses 3.7 as primary + undated (4).
        self.assertEqual(flash37.request_count, 1 + 4 + 6)
        self.assertEqual(flash37.session_count, 3)

    def test_models_percentages_and_order(self):
        models = UsageAggregator.aggregate_models(self._all())
        totals = [m.usage.total_tokens for m in models]
        self.assertEqual(totals, sorted(totals, reverse=True))
        self.assertAlmostEqual(sum(m.percentage_of_total for m in models), 100.0)

    def test_hourly_totals_equal_daily_totals(self):
        sessions = self._all()
        daily = UsageAggregator.aggregate_daily(sessions)
        hourly = UsageAggregator.aggregate_hourly(sessions)
        self.assertEqual(
            sum(u.total_tokens for u in hourly.values()),
            sum(d.usage.total_tokens for d in daily.values()),
        )
        self.assertEqual(
            sum(u.prompt_tokens for u in hourly.values()),
            sum(d.usage.prompt_tokens for d in daily.values()),
        )
        self.assertIn((date(2026, 9, 1), 10), hourly)
        self.assertIn((date(2026, 9, 1), 9), hourly)

    def test_weekly_and_monthly_session_counts_are_deduplicated(self):
        s = _stepful(
            "multi-day",
            [
                _step(1, datetime(2026, 9, 1, 9, 0), "gemini-3.8-flash"),
                _step(2, datetime(2026, 9, 2, 9, 0), "gemini-3.8-flash"),
                _step(3, datetime(2026, 9, 3, 9, 0), "gemini-3.8-flash"),
            ],
            ["gemini-3.8-flash"],
        )
        other = _stepless("other", ["gemini-3.8-flash"], 2, datetime(2026, 9, 2, 12, 0))
        weekly = UsageAggregator.aggregate_weekly([s, other])
        monthly = UsageAggregator.aggregate_monthly([s, other])
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0].session_count, 2)
        self.assertEqual(weekly[0].step_count, 5)
        self.assertEqual(len(monthly), 1)
        self.assertEqual(monthly[0].session_count, 2)
        self.assertEqual(monthly[0].step_count, 5)

    def test_year_boundary_lands_in_correct_iso_week_and_month(self):
        s = _stepful(
            "boundary",
            [
                _step(1, datetime(2026, 12, 31, 23, 0), "gemini-3.8-flash"),
                _step(2, datetime(2027, 1, 1, 1, 0), "gemini-3.8-flash"),
            ],
            ["gemini-3.8-flash"],
        )
        weekly = UsageAggregator.aggregate_weekly([s])
        self.assertEqual([(w.year, w.week_num) for w in weekly], [(2026, 53)])
        self.assertEqual(weekly[0].start_date, date(2026, 12, 28))
        self.assertEqual(weekly[0].end_date, date(2027, 1, 3))
        self.assertEqual(weekly[0].session_count, 1)
        self.assertEqual(weekly[0].step_count, 2)
        monthly = UsageAggregator.aggregate_monthly([s])
        self.assertEqual([(m.year, m.month) for m in monthly], [(2026, 12), (2027, 1)])
        self.assertEqual([m.session_count for m in monthly], [1, 1])

    def test_rollups_are_sorted(self):
        sessions = [
            _stepless("late", ["gemini-3.8-flash"], 1, datetime(2027, 2, 1, 1, 0)),
            _stepless("early", ["gemini-3.8-flash"], 1, datetime(2026, 11, 3, 1, 0)),
            _stepless("mid", ["gemini-3.8-flash"], 1, datetime(2026, 12, 9, 1, 0)),
        ]
        weekly = UsageAggregator.aggregate_weekly(sessions)
        self.assertEqual([w.start_date for w in weekly], sorted(w.start_date for w in weekly))
        monthly = UsageAggregator.aggregate_monthly(sessions)
        self.assertEqual(
            [(m.year, m.month) for m in monthly], [(2026, 11), (2026, 12), (2027, 2)]
        )

    def test_precomputed_daily_data_is_used_for_rollups(self):
        sessions = self._all()
        daily = UsageAggregator.aggregate_daily(sessions)
        self.assertEqual(
            [w.to_dict() for w in UsageAggregator.aggregate_weekly(sessions, daily_data=daily)],
            [w.to_dict() for w in UsageAggregator.aggregate_weekly(sessions)],
        )
        self.assertEqual(
            [m.to_dict() for m in UsageAggregator.aggregate_monthly(sessions, daily_data=daily)],
            [m.to_dict() for m in UsageAggregator.aggregate_monthly(sessions)],
        )

    def test_aggregation_does_not_mutate_input_usage_or_cost(self):
        sessions = self._all()
        # Price everything first so cached costs exist before the comparison.
        before = [
            (s.usage.to_dict(), s.cost.to_dict(), [(st.usage.to_dict(), st.cost.to_dict()) for st in s.steps])
            for s in sessions
        ]
        UsageAggregator.aggregate_daily(sessions)
        UsageAggregator.aggregate_hourly(sessions)
        UsageAggregator.aggregate_weekly(sessions)
        UsageAggregator.aggregate_monthly(sessions)
        UsageAggregator.aggregate_models(sessions)
        UsageAggregator.compute_summary(sessions)
        after = [
            (s.usage.to_dict(), s.cost.to_dict(), [(st.usage.to_dict(), st.cost.to_dict()) for st in s.steps])
            for s in sessions
        ]
        self.assertEqual(before, after)

    def test_buckets_do_not_alias_input_objects(self):
        sessions = self._all()
        inputs = {id(s.usage) for s in sessions} | {id(s.cost) for s in sessions}
        for s in sessions:
            inputs |= {id(st.usage) for st in s.steps} | {id(st.cost) for st in s.steps}

        daily = UsageAggregator.aggregate_daily(sessions)
        hourly = UsageAggregator.aggregate_hourly(sessions)
        models = UsageAggregator.aggregate_models(sessions)
        weekly = UsageAggregator.aggregate_weekly(sessions)
        monthly = UsageAggregator.aggregate_monthly(sessions)
        summary = UsageAggregator.compute_summary(sessions)

        owned = [d.usage for d in daily.values()] + [d.cost for d in daily.values()]
        owned += list(hourly.values())
        owned += [m.usage for m in models] + [m.cost for m in models]
        owned += [w.usage for w in weekly] + [w.cost for w in weekly]
        owned += [m.usage for m in monthly] + [m.cost for m in monthly]
        owned += [summary["total_usage"], summary["total_cost"]]
        for obj in owned:
            self.assertNotIn(id(obj), inputs)

        # A single-unit bucket must be a copy, not the step's own object.
        solo = _stepful("solo", [_step(1, datetime(2026, 9, 5, 5, 0), "gemini-3.8-flash")], ["gemini-3.8-flash"])
        hourly = UsageAggregator.aggregate_hourly([solo])
        self.assertIsNot(hourly[(date(2026, 9, 5), 5)], solo.steps[0].usage)
        self.assertIsNot(hourly[(date(2026, 9, 5), 5)], solo.usage)
        self.assertEqual(hourly[(date(2026, 9, 5), 5)].to_dict(), solo.steps[0].usage.to_dict())


if __name__ == "__main__":
    unittest.main()
