from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_diary.index.retrieval_metrics import (
    estimate_tokens,
    list_events,
    record_event,
)
from agent_diary.index.sqlite_index import bootstrap_sqlite


class TestRetrievalMetrics(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "index" / "memory.db"
        bootstrap_sqlite(self.db)

    def test_estimate_tokens_is_deterministic_and_conservative(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("1234"), 1)
        self.assertEqual(estimate_tokens("12345"), 2)

    def test_record_event_stores_measurement_without_raw_query(self):
        event_id = record_event(
            self.db,
            route="search_memory",
            query="private user question",
            candidate_count=12,
            result_count=3,
            returned_chars=240,
            latency_ms=17.5,
            provider_input_tokens=None,
            provider_cost=None,
        )

        rows = list_events(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_id"], event_id)
        self.assertEqual(rows[0]["route"], "search_memory")
        self.assertEqual(rows[0]["query_chars"], len("private user question"))
        self.assertEqual(rows[0]["candidate_count"], 12)
        self.assertEqual(rows[0]["result_count"], 3)
        self.assertEqual(rows[0]["returned_chars"], 240)
        self.assertEqual(rows[0]["estimated_input_tokens"], 60)
        self.assertNotIn("private user question", str(rows[0]))

    def test_record_event_is_append_only(self):
        first = record_event(
            self.db,
            route="recall_beliefs",
            query="same",
            candidate_count=2,
            result_count=1,
            returned_chars=20,
            latency_ms=1,
        )
        second = record_event(
            self.db,
            route="recall_beliefs",
            query="same",
            candidate_count=2,
            result_count=1,
            returned_chars=20,
            latency_ms=1,
        )
        self.assertNotEqual(first, second)
        self.assertEqual(len(list_events(self.db)), 2)

    def test_schema_is_safe_to_initialize_on_existing_database(self):
        with sqlite3.connect(self.db) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
        self.assertIn("retrieval_events", names)


if __name__ == "__main__":
    unittest.main()
