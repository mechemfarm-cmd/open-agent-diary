from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_diary.index.graph_repository import insert_entity, insert_evidence, insert_fact
from agent_diary.index.retrieval_metrics import list_events
from agent_diary.models.types import GraphEntity, GraphFact, GraphFactEvidence
from agent_diary.service.handlers import recall_beliefs
from tests.test_belief_recall_route import make_paths


class TestRetrievalWiring(unittest.TestCase):
    def test_belief_recall_records_shadow_measurement(self):
        root = Path(tempfile.mkdtemp())
        paths = make_paths(root)
        subject = GraphEntity(canonical_name="Subject")
        obj = GraphEntity(canonical_name="Object")
        insert_entity(paths.sqlite_path, subject)
        insert_entity(paths.sqlite_path, obj)
        fact = GraphFact(
            subject_entity_id=subject.entity_id,
            predicate="HAS",
            object_kind="entity",
            object_entity_id=obj.entity_id,
            metadata={
                "belief": {
                    "strength": 1.0,
                    "confidence": 0.9,
                    "provenance": "harvested",
                    "provisional": True,
                }
            },
        )
        fact_id = insert_fact(paths.sqlite_path, fact)
        insert_evidence(
            paths.sqlite_path,
            GraphFactEvidence(fact_id=fact_id, source_kind="raw_entry", source_id="entry-1"),
        )

        result = recall_beliefs(paths, {"limit": 1, "char_budget": 200})

        events = list_events(paths.sqlite_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["route"], "recall_beliefs")
        self.assertEqual(events[0]["candidate_count"], result["considered"])
        self.assertEqual(events[0]["result_count"], result["surfaced"])
        self.assertEqual(events[0]["returned_chars"], len(result["block"]))
        self.assertGreaterEqual(events[0]["estimated_input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
