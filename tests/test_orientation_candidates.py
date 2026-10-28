from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths
from agent_diary.orientation import create_run, set_consent
from agent_diary.orientation_candidates import generate_candidates, list_candidates, set_candidate_status
from agent_diary.orientation_knowledge import list_knowledge, search_knowledge, set_knowledge_status
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
