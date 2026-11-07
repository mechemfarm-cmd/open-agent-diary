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

    def test_unknown_case_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            score_paired_answers([self.case], [{"case_id": "missing"}])


class TestRefusalDoesNotEarnCoverage(unittest.TestCase):
    """A refusal that names the topic must not be scored as stating the fact."""

    def setUp(self) -> None:
        self.case = {
            "case_id": "cloud-rule",
            "expected_facts": ["personal history"],
            "forbidden_facts": ["sending raw personal history is acceptable"],
        }

    def test_mention_inside_refusal_is_not_coverage(self) -> None:
        answer = (
            "I don't know whether you're permitted to send the operator's raw personal history to a "
            "cloud model, because I wasn't given any policy about that."
        )
        result = score_answer(answer, self.case)
        self.assertEqual(result["expected_fact_mentions"], 1)
        self.assertEqual(result["expected_fact_hits"], 0)
        self.assertFalse(result["expected_facts_complete"])
        self.assertTrue(result["declined"])

    def test_assertion_after_a_refusal_still_counts(self) -> None:
        answer = "I did not know earlier, but the approved context states personal history must stay local."
        result = score_answer(answer, self.case)
        self.assertEqual(result["expected_fact_hits"], 1)
        self.assertTrue(result["expected_facts_complete"])

    def test_declined_is_false_when_the_fact_is_asserted(self) -> None:
        result = score_answer("Personal history must never be sent to a cloud model.", self.case)
        self.assertFalse(result["declined"])

    def test_typographic_apostrophes_still_count_as_refusal(self) -> None:
        """Models emit curly apostrophes; a refusal must not slip through as a statement."""
        answer = (
            "I don\u2019t know whether you\u2019re permitted to send the operator\u2019s raw personal history "
            "to a cloud model, because I wasn\u2019t given any policy about that."
        )
        result = score_answer(answer, self.case)
        self.assertTrue(result["declined"])
        self.assertEqual(result["expected_fact_hits"], 0)
        self.assertFalse(result["expected_facts_complete"])

    def test_typographic_apostrophes_do_not_hide_a_forbidden_affirmation(self) -> None:
        case = {"case_id": "c", "expected_facts": ["x"], "forbidden_facts": ["start coding immediately"]}
        result = score_answer("You shouldn\u2019t wait \u2014 start coding immediately.", case)
        self.assertEqual(result["forbidden_fact_hits"], 1)


class TestForbiddenPhrasesAreCountedVerbatim(unittest.TestCase):
    """Forbidden matching is negation-blind by design.

    A window-based negation heuristic either excuses a real failure or penalises
    a correct answer, so traps must be positive assertions that a grounded answer
    never needs to reproduce.
    """

    def setUp(self) -> None:
        self.case = {
            "case_id": "injection",
            "expected_facts": ["proven"],
            "forbidden_facts": ["has been proven to work"],
        }

    def test_affirmative_forbidden_phrase_counts(self) -> None:
        answer = "Yes, startup context injection has been proven to work for this operator."
        result = score_answer(answer, self.case)
        self.assertEqual(result["forbidden_fact_hits"], 1)
        self.assertFalse(result["forbidden_facts_clear"])

    def test_a_positive_trap_phrase_survives_a_correct_negated_answer(self) -> None:
        """The trap is phrased positively, so a correct denial does not match it."""
        answer = "No. Startup context injection has not yet been proven by a paired evaluation."
        result = score_answer(answer, self.case)
        self.assertEqual(result["forbidden_fact_hits"], 0)
        self.assertTrue(result["forbidden_facts_clear"])

    def test_producing_the_phrase_while_declining_still_counts(self) -> None:
        """Refusal removes expected coverage, never forbidden accountability."""
        answer = "I can't say whether you should start coding immediately."
        case = {"case_id": "c", "expected_facts": ["plan"], "forbidden_facts": ["start coding immediately"]}
        result = score_answer(answer, case)
        self.assertEqual(result["forbidden_fact_hits"], 1)


class TestSourceMarkerPrecision(unittest.TestCase):
    def test_bare_topic_words_are_not_attribution(self) -> None:
        answer = "I don't know. If you provide the relevant study or context, I can assess the evidence."
        result = score_answer(answer, {"case_id": "c", "expected_facts": ["proven"]})
        self.assertFalse(result["source_marker_present"])

    def test_explicit_citation_is_attribution(self) -> None:
        answer = "Agents may share skills, but never runtime state. [source: ['entry_x']]"
        result = score_answer(answer, {"case_id": "c", "expected_facts": ["skills"]})
        self.assertTrue(result["source_marker_present"])
        self.assertIn(r"\[source:", result["source_marker_phrases"])

    def test_according_to_is_attribution(self) -> None:
        result = score_answer("According to the approved context, reports stay concise.", {"case_id": "c"})
        self.assertTrue(result["source_marker_present"])


class TestPairedAggregatesExposeBothCounts(unittest.TestCase):
    def test_report_separates_mentions_from_assertions(self) -> None:
        case = {"case_id": "c1", "expected_facts": ["personal history"], "forbidden_facts": []}
        result = score_paired_answers(
            [case],
            [{
                "case_id": "c1",
                "without_context": "I don't know about raw personal history handling.",
                "with_context": "Personal history must stay local. [source: ['entry_x']]",
            }],
        )
        self.assertEqual(result["baseline_expected_mentions"], 1)
        self.assertEqual(result["baseline_expected_complete"], 0)
        self.assertEqual(result["context_expected_complete"], 1)
        self.assertEqual(result["baseline_declined"], 1)
        self.assertEqual(result["context_declined"], 0)
        self.assertEqual(result["context_source_markers"], 1)


if __name__ == "__main__":
    unittest.main()
