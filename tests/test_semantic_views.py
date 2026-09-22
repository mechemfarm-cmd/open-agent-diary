from __future__ import annotations

import unittest

from agent_diary.analytics.semantic_sources import EvidenceRef, SemanticCandidate
from agent_diary.analytics.semantic_situations import assemble_situation, render_situation_view


def cand(source_id: str, *, text: str, predicate: str = "STATE", status: str = "current", metadata: dict | None = None, role: str = "establishes") -> SemanticCandidate:
    return SemanticCandidate(
        source_kind="graph_fact",
        source_id=source_id,
        subject="Project Atlas",
        text=text,
        timestamp="2026-09-21T10:00:00+00:00",
        status=status,
        predicate=predicate,
        confidence="high",
        metadata=metadata or {},
        evidence_refs=[EvidenceRef("raw_entry", f"entry_{source_id}", "2026-09-21T10:00:00+00:00", role)],
    )


class TestSemanticViews(unittest.TestCase):
    def test_current_status_view_prioritizes_state_sources_and_confidence(self):
        situation = assemble_situation(
            topic="Project Atlas",
            purpose="current_status",
            candidates=[cand("state", text="Project Atlas is read-only"), cand("decision", text="No external service", metadata={"semantic_role": "decision"})],
        )

        rendered = render_situation_view(situation, purpose="current_status")

        self.assertLess(rendered.index("Current state"), rendered.index("Recent evidence"))
        self.assertIn("Project Atlas is read-only", rendered)
        self.assertIn("confidence: high", rendered)
        self.assertIn("raw_entry:entry_state", rendered)

    def test_decision_rationale_view_prioritizes_decisions_before_evidence(self):
        situation = assemble_situation(
            topic="Project Atlas",
            purpose="decision_rationale",
            candidates=[cand("state", text="Project Atlas is read-only"), cand("decision", text="Keep prototype offline", metadata={"semantic_role": "decision"})],
        )

        rendered = render_situation_view(situation, purpose="decision_rationale")

        self.assertLess(rendered.index("Decisions and rationale"), rendered.index("Supporting evidence"))
        self.assertIn("Keep prototype offline", rendered)
        self.assertIn("raw_entry:entry_decision", rendered)

    def test_next_action_view_prioritizes_open_questions_and_blockers(self):
        situation = assemble_situation(
            topic="Project Atlas",
            purpose="next_action",
            candidates=[cand("open", text="Verify whether stale state is suppressed?", role="question"), cand("state", text="Project Atlas is read-only")],
        )

        rendered = render_situation_view(situation, purpose="next_action")

        self.assertLess(rendered.index("Open questions and next actions"), rendered.index("Current state"))
        self.assertIn("Verify whether stale state is suppressed?", rendered)

    def test_view_respects_character_budget(self):
        situation = assemble_situation(topic="Project Atlas", purpose="current_status", candidates=[cand("state", text="Project Atlas " + "x" * 500)])

        rendered = render_situation_view(situation, purpose="current_status", char_budget=120)

        self.assertLessEqual(len(rendered), 120)
        self.assertTrue(rendered.endswith("…"))


if __name__ == "__main__":
    unittest.main()
