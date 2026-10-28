from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths
from agent_diary.orientation import create_run, set_consent
from agent_diary.orientation_candidates import generate_candidates, list_candidates, set_candidate_status
from agent_diary.orientation_knowledge import list_knowledge, search_knowledge, set_knowledge_status
from agent_diary.orientation_review import show_candidate
from agent_diary.orientation_context import build_context_pack
from agent_diary.orientation_evaluation import evaluate_context_pack
from agent_diary.orientation_answer_evaluation import build_answer_eval_prompts
from agent_diary.orientation_import import import_approved_history


def make_paths(root: Path) -> Paths:
    data = root / "data"
    return Paths(
        root=root,
        data_root=data,
        entries_dir=data / "entries",
        work_trace_dir=data / "work_trace",
        overlays_dir=data / "overlays",
        artifacts_dir=data / "artifacts",
        imports_dir=data / "imports",
        index_dir=data / "index",
        config_dir=data / "config",
        archive_dir=data / "archives",
        sqlite_path=data / "index" / "memory.db",
    )


class TestOrientationCandidates(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.paths = make_paths(self.root)
        self.history = self.root / "history.jsonl"
        self.history.write_text(
            "\n".join(
                [
                    json.dumps({"message_id": "1", "created_at": "2026-01-01T00:00:00Z", "author_role": "human", "speaker": "User", "content": "I prefer plain text reports."}),
                    json.dumps({"message_id": "2", "created_at": "2026-01-01T00:01:00Z", "author_role": "agent", "speaker": "Agent", "content": "Noted."}),
                    json.dumps({"message_id": "3", "created_at": "2026-01-01T02:02:00Z", "author_role": "human", "speaker": "User", "content": "The migration is pending review."}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        create_run(
            self.paths,
            [{"source_id": "history", "kind": "hermes_history", "locator": str(self.history), "scope": "content"}],
            run_id="candidate-run",
        )
        set_consent(self.paths, "candidate-run", "history", "approved")
        import_approved_history(self.paths, run_id="candidate-run", source_id="history", format_name="generic-message-jsonl", max_messages=1)

    def test_generation_creates_pending_source_linked_candidates(self):
        result = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        self.assertGreaterEqual(result["candidate_count"], 2)
        self.assertTrue(all(item["status"] == "pending" for item in result["candidates"]))
        self.assertTrue(all(item["source_entry_ids"] for item in result["candidates"]))
        self.assertIn("user_statement", {item["kind"] for item in result["candidates"]})

    def test_review_view_combines_evidence_source_and_knowledge(self):
        result = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        candidate = next(item for item in result["candidates"] if item["kind"] == "user_statement")
        before = show_candidate(self.paths, "candidate-run", candidate["candidate_id"])
        self.assertEqual(before["candidate"]["status"], "pending")
        self.assertIsNone(before["knowledge"])
        self.assertEqual(before["source"]["consent"], "approved")
        self.assertTrue(before["candidate"]["evidence"])
        set_candidate_status(self.paths, "candidate-run", candidate["candidate_id"], "approved")
        after = show_candidate(self.paths, "candidate-run", candidate["candidate_id"])
        self.assertEqual(after["knowledge"]["status"], "active")
        self.assertEqual(after["knowledge"]["source_entry_ids"], candidate["source_entry_ids"])

    def test_context_pack_is_bounded_and_provenance_preserving(self):
        result = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        candidate = next(item for item in result["candidates"] if item["kind"] == "user_statement")
        set_candidate_status(self.paths, "candidate-run", candidate["candidate_id"], "approved")
        pack = build_context_pack(self.paths, max_items=1, max_chars=10_000)
        self.assertEqual(pack["item_count"], 1)
        self.assertEqual(pack["active_count"], 1)
        self.assertEqual(pack["omitted_count"], 0)
        self.assertEqual(pack["items"][0]["source"]["source_entry_ids"], candidate["source_entry_ids"])
        with self.assertRaises(ValueError):
            build_context_pack(self.paths, max_items=0)

    def test_context_evaluation_reports_coverage_and_budget(self):
        result = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        candidate = next(item for item in result["candidates"] if item["kind"] == "user_statement")
        set_candidate_status(self.paths, "candidate-run", candidate["candidate_id"], "approved")
        knowledge = list_knowledge(self.paths)[0]
        evaluation = evaluate_context_pack(
            self.paths,
            [{"case_id": "preference", "expected_knowledge_id": knowledge["knowledge_id"], "expected_terms": ["plain", "text"]}],
            max_chars=10_000,
        )
        self.assertEqual(evaluation["case_count"], 1)
        self.assertEqual(evaluation["baseline_hits"], 0)
        self.assertEqual(evaluation["pack_hits"], 1)
        self.assertEqual(evaluation["delta"], 1.0)
        self.assertTrue(evaluation["budget_pass"])

    def test_answer_eval_prompts_are_paired_and_scored_by_rubric(self):
        result = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        candidate = next(item for item in result["candidates"] if item["kind"] == "user_statement")
        set_candidate_status(self.paths, "candidate-run", candidate["candidate_id"], "approved")
        prompts = build_answer_eval_prompts(
            self.paths,
            [{"case_id": "preference", "question": "What reporting style should I use?", "expected_facts": ["plain text"], "forbidden_facts": ["PDF"]}],
        )
        prompt = prompts["prompts"][0]
        self.assertNotIn("Approved orientation context", prompt["without_context"])
        self.assertIn("Approved orientation context", prompt["with_context"])
        self.assertEqual(prompt["rubric"]["must_acknowledge_unknowns"], True)

    def test_approval_and_rejection_are_explicit(self):
        result = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        candidate_id = result["candidates"][0]["candidate_id"]
        approved = set_candidate_status(self.paths, "candidate-run", candidate_id, "approved")
        self.assertEqual(approved["status"], "approved")
        knowledge = list_knowledge(self.paths)
        self.assertEqual(len(knowledge), 1)
        self.assertEqual(search_knowledge(self.paths, "migration")[0]["candidate_id"], candidate_id)
        self.assertEqual(len(list_candidates(self.paths, "candidate-run", status="approved")), 1)
        set_knowledge_status(self.paths, candidate_id, "revoked")
        self.assertEqual(search_knowledge(self.paths, "migration"), [])
        rejected_id = result["candidates"][1]["candidate_id"]
        set_candidate_status(self.paths, "candidate-run", rejected_id, "rejected")
        with self.assertRaises(ValueError):
            set_candidate_status(self.paths, "candidate-run", rejected_id, "approved")

    def test_regeneration_preserves_review_status(self):
        result = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        candidate_id = result["candidates"][0]["candidate_id"]
        set_candidate_status(self.paths, "candidate-run", candidate_id, "approved")
        regenerated = generate_candidates(self.paths, run_id="candidate-run", source_id="history")
        item = next(item for item in regenerated["candidates"] if item["candidate_id"] == candidate_id)
        self.assertEqual(item["status"], "approved")


if __name__ == "__main__":
    unittest.main()
