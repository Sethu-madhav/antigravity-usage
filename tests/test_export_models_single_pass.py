"""to_json should reuse compute_summary's model list instead of aggregating models again."""

import json
import unittest
from datetime import datetime
from unittest import mock

from agy_usage.aggregator import UsageAggregator
from agy_usage.exporter import UsageExporter
from agy_usage.models import SessionSummary, StepRecord, TokenUsage


def _make_sessions():
    t1 = datetime(2026, 9, 1, 10, 0)
    t2 = datetime(2026, 9, 2, 11, 0)
    s1 = SessionSummary(
        session_id="session-001",
        source="cli",
        file_path="/tmp/s1.db",
        models=["gemini-3.8-flash", "gemini-3.7-flash"],
        start_time=t1,
        end_time=t2,
        step_count=2,
        usage=TokenUsage(prompt_tokens=1500, cached_tokens=300, output_tokens=150),
        steps=[
            StepRecord(1, t1, "gemini-3.8-flash", TokenUsage(prompt_tokens=1000, cached_tokens=200, output_tokens=100)),
            StepRecord(2, t2, "gemini-3.7-flash", TokenUsage(prompt_tokens=500, cached_tokens=100, output_tokens=50)),
        ],
    )
    s2 = SessionSummary(
        session_id="session-002",
        source="ide",
        file_path="/tmp/s2.db",
        models=["gemini-3.8-flash"],
        start_time=t2,
        end_time=t2,
        step_count=1,
        usage=TokenUsage(prompt_tokens=200, cached_tokens=0, output_tokens=20),
        steps=[
            StepRecord(1, t2, "gemini-3.8-flash", TokenUsage(prompt_tokens=200, cached_tokens=0, output_tokens=20)),
        ],
    )
    return [s1, s2]


class TestExportModelsSinglePass(unittest.TestCase):
    def test_to_json_aggregates_models_once(self):
        sessions = _make_sessions()
        with mock.patch.object(
            UsageAggregator, "aggregate_models", wraps=UsageAggregator.aggregate_models
        ) as spy:
            UsageExporter.to_json(sessions)
        self.assertEqual(spy.call_count, 1)

    def test_models_section_matches_aggregate_models(self):
        sessions = _make_sessions()
        data = json.loads(UsageExporter.to_json(sessions))
        expected = [m.to_dict() for m in UsageAggregator.aggregate_models(sessions)]
        self.assertEqual(len(expected), 2)
        self.assertEqual(data["models"], expected)


if __name__ == "__main__":
    unittest.main()
