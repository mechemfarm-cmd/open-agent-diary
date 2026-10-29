from __future__ import annotations

import unittest

from agent_diary.orientation_answer_scoring import score_answer, score_paired_answers


class TestOrientationAnswerScoring(unittest.TestCase):
    def setUp(self):
        self.case = {
            "case_id": "preference",
            "expected_facts": ["plain text"],
            "forbidden_facts": ["PDF"],
        }

    def test_score_tracks_expected_forbidden_and_provenance_signals(self):
        result = score_answer(
            "According to the approved orientation source, plain text is preferred; PDF is unknown.",
            self.case,
        )
        self.assertTrue(result["expected_facts_complete"])
        self.assertEqual(result["forbidden_fact_hits"], 1)
        self.assertTrue(result["source_marker_present"])

    def test_paired_score_reports_delta(self):
        result = score_paired_answers(
            [self.case],
            [{
                "case_id": "preference",
                "without_context": "I don't know.",
                "with_context": "According to the source, plain text is preferred.",
            }],
        )
        self.assertEqual(result["baseline_expected_complete"], 0)
        self.assertEqual(result["context_expected_complete"], 1)
        self.assertEqual(result["expected_completion_delta"], 1.0)
        self.assertEqual(result["context_forbidden_hits"], 0)

    def test_unknown_case_is_rejected(self):
        with self.assertRaises(KeyError):
            score_paired_answers([self.case], [{"case_id": "missing"}])


if __name__ == "__main__":
    unittest.main()
