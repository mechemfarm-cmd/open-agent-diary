from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_diary.index.retrieval_metrics import record_event, summarize_events
from agent_diary.index.sqlite_index import bootstrap_sqlite


class TestRetrievalReport(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.db = root / "index" / "memory.db"
        bootstrap_sqlite(self.db)

    def test_empty_report_is_zeroed(self):
        report = summarize_events(self.db)
        self.assertEqual(report["event_count"], 0)
        self.assertEqual(report["totals"]["estimated_input_tokens"], 0)
        self.assertEqual(report["by_route"], [])

    def test_report_groups_routes_and_sums_measurements(self):
        record_event(self.db, route="search_memory", query="a", candidate_count=4, result_count=2, returned_chars=100, latency_ms=10)
        record_event(self.db, route="search_memory", query="b", candidate_count=6, result_count=1, returned_chars=300, latency_ms=30, provider_input_tokens=88, provider_cost=0.001)
        record_event(self.db, route="recall_beliefs", query="", candidate_count=9, result_count=3, returned_chars=80, latency_ms=20)

        report = summarize_events(self.db)
        self.assertEqual(report["event_count"], 3)
        self.assertEqual(report["totals"]["candidate_count"], 19)
        self.assertEqual(report["totals"]["result_count"], 6)
        self.assertEqual(report["totals"]["returned_chars"], 480)
        self.assertEqual(report["totals"]["estimated_input_tokens"], 120)
        self.assertEqual(report["totals"]["provider_input_tokens"], 88)
        self.assertAlmostEqual(report["totals"]["provider_cost"], 0.001)
        routes = {row["route"]: row for row in report["by_route"]}
        self.assertEqual(routes["search_memory"]["event_count"], 2)
        self.assertEqual(routes["recall_beliefs"]["result_count"], 3)
        self.assertAlmostEqual(routes["search_memory"]["latency_ms_avg"], 20.0)

    def test_since_filters_older_events(self):
        record_event(self.db, route="old", query="x", returned_chars=40)
        record_event(self.db, route="new", query="y", returned_chars=80)
        report = summarize_events(self.db, since="9999-01-01T00:00:00+00:00")
        self.assertEqual(report["event_count"], 0)
        self.assertEqual(report["window_since"], "9999-01-01T00:00:00+00:00")

    def test_invalid_since_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize_events(self.db, since="not-a-timestamp")


if __name__ == "__main__":
    unittest.main()
