from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_diary.analytics.semantic_situations import load_semantic_evaluation_cases

FIXTURE = Path(__file__).parent / "fixtures" / "semantic-situations.jsonl"


class TestSemanticEvaluationFixture(unittest.TestCase):
    def test_fixture_loader_validates_schema_unique_ids_and_allowed_purposes(self):
        cases = load_semantic_evaluation_cases(FIXTURE)

        self.assertGreaterEqual(len(cases), 10)
        self.assertEqual(len({case.case_id for case in cases}), len(cases))
        self.assertEqual({case.purpose for case in cases}, {"current_status", "decision_rationale", "next_action"})
        for case in cases:
            self.assertTrue(case.query)
            self.assertTrue(case.expected_elements)
            self.assertTrue(case.required_source_refs)
            self.assertIsInstance(case.known_traps, list)

    def test_fixture_is_synthetic_and_avoids_private_identifiers(self):
        raw = FIXTURE.read_text(encoding="utf-8")

        forbidden = ["Willard", "Jev", "winery", "Telegram", "OpenClaw", "Hermes"]
        for token in forbidden:
            self.assertNotIn(token, raw)

    def test_loader_rejects_duplicate_case_ids(self):
        first = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
        duplicate_path = FIXTURE.parent / "_duplicate-semantic-case.tmp.jsonl"
        duplicate_path.write_text(json.dumps(first) + "\n" + json.dumps(first) + "\n", encoding="utf-8")
        try:
            with self.assertRaises(ValueError):
                load_semantic_evaluation_cases(duplicate_path)
        finally:
            duplicate_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
