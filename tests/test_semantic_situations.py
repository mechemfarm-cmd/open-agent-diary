from __future__ import annotations

import unittest

from agent_diary.analytics.semantic_sources import EvidenceRef, SemanticCandidate
from agent_diary.analytics.semantic_situations import assemble_situation


def candidate(
    source_id: str,
    *,
    subject: str = "Project Atlas",
    text: str,
    timestamp: str,
    status: str = "current",
    predicate: str = "STATE",
    object_value: str | None = None,
    source_kind: str = "graph_fact",
    superseded_by: str | None = None,
    provisional: bool = False,
    metadata: dict | None = None,
    evidence_role: str = "establishes",
) -> SemanticCandidate:
    return SemanticCandidate(
        source_kind=source_kind,
        source_id=source_id,
        subject=subject,
        text=text,
        timestamp=timestamp,
        status=status,
        predicate=predicate,
        object_value=object_value or text,
        superseded_by=superseded_by,
        provisional=provisional,
        metadata=metadata or {},
        evidence_refs=[EvidenceRef(source_kind="raw_entry", source_id=f"entry_{source_id}", timestamp=timestamp, role=evidence_role)],
    )


class TestSemanticSituations(unittest.TestCase):
    def test_clean_project_situation_keeps_source_links_and_inference_note(self):
        situation = assemble_situation(
            topic="Project Atlas",
            purpose="current_status",
            candidates=[
                candidate("gf_atlas_state", text="Project Atlas is a read-only semantic-memory prototype", timestamp="2026-09-21T10:00:00+00:00"),
                candidate("gf_atlas_decision", text="Keep Project Atlas data out of external services", timestamp="2026-09-21T11:00:00+00:00", metadata={"semantic_role": "decision"}),
            ],
        )

        self.assertEqual(situation.topic, "Project Atlas")
        self.assertEqual(situation.purpose, "current_status")
        self.assertEqual(situation.episode["anchor"], "Project Atlas")
        self.assertEqual(situation.current_state[0].source_refs[0].source_id, "entry_gf_atlas_state")
        self.assertEqual(situation.decisions[0].text, "Keep Project Atlas data out of external services")
        self.assertEqual(situation.inference_notes[0].kind, "grouping")

    def test_generic_metadata_and_evidence_roles_classify_without_predicate_vocabulary(self):
        situation = assemble_situation(
            topic="Blue Finch",
            purpose="next_action",
            candidates=[
                candidate("choice", subject="Blue Finch", text="Use staged rollout because it is reversible", timestamp="2026-09-21T10:00:00+00:00", predicate="RELATES_TO", metadata={"semantic_role": "decision"}),
                candidate("question", subject="Blue Finch", text="Can checksum comparison finish before review?", timestamp="2026-09-21T11:00:00+00:00", predicate="RELATES_TO", evidence_role="question"),
            ],
        )

        self.assertEqual([item.source_id for item in situation.decisions], ["choice"])
        self.assertEqual([item.source_id for item in situation.open_questions], ["question"])

    def test_semantic_situation_code_does_not_name_deployment_specific_predicates(self):
        from pathlib import Path

        source = Path("src/agent_diary/analytics/semantic_situations.py").read_text(encoding="utf-8")

        for token in ["DECISION", "OPEN_QUESTION", "NEXT_ACTION", "BLOCKED_BY"]:
            self.assertNotIn(token, source)

    def test_changed_state_marks_historical_candidate_superseded_not_current(self):
        situation = assemble_situation(
            topic="Service Cedar",
            purpose="current_status",
            candidates=[
                candidate("gf_old", subject="Service Cedar", text="Service Cedar ran on laptop", timestamp="2026-09-18T10:00:00+00:00", status="historical", superseded_by="gf_new"),
                candidate("gf_new", subject="Service Cedar", text="Service Cedar runs on Server Alpha", timestamp="2026-09-19T10:00:00+00:00", status="current"),
            ],
        )

        self.assertEqual([item.source_id for item in situation.current_state], ["gf_new"])
        self.assertEqual(situation.history[0].source_id, "gf_old")
        self.assertEqual(situation.history[0].status, "superseded")

    def test_conflicting_deployment_candidates_are_visible(self):
        situation = assemble_situation(
            topic="deployment",
            purpose="current_status",
            candidates=[
                candidate("gf_alpha", subject="Service Cedar", text="Service Cedar runs on Server Alpha", timestamp="2026-09-20T10:00:00+00:00", predicate="RUNS_ON", object_value="Server Alpha"),
                candidate("gf_beta", subject="Service Cedar", text="Service Cedar runs on Server Beta", timestamp="2026-09-20T11:00:00+00:00", predicate="RUNS_ON", object_value="Server Beta"),
            ],
        )

        self.assertEqual(len(situation.conflicts), 1)
        self.assertEqual(set(situation.conflicts[0].candidate_ids), {"gf_alpha", "gf_beta"})
        self.assertIn("RUNS_ON", situation.conflicts[0].text)

    def test_empty_new_user_situation_is_explicitly_empty(self):
        situation = assemble_situation(topic="new user", purpose="next_action", candidates=[])

        self.assertEqual(situation.current_state, [])
        self.assertEqual(situation.open_questions, [])
        self.assertEqual(situation.episode["status"], "empty")
        self.assertIn("No source candidates", situation.inference_notes[0].text)

    def test_item_budget_and_character_budget_are_applied(self):
        situation = assemble_situation(
            topic="Project Atlas",
            purpose="current_status",
            candidates=[
                candidate("gf_one", text="Project Atlas " + "x" * 100, timestamp="2026-09-21T10:00:00+00:00"),
                candidate("gf_two", text="Project Atlas second", timestamp="2026-09-21T11:00:00+00:00"),
            ],
            item_limit=1,
            char_budget=30,
        )

        self.assertEqual(len(situation.current_state), 1)
        self.assertLessEqual(len(situation.current_state[0].text), 30)


if __name__ == "__main__":
    unittest.main()
