"""Tests for case-set validation and the one-shot paired evaluation runner."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_diary.cli.main import _load_cases_argument
from agent_diary.config import default_paths
from agent_diary.orientation_eval_suite import (
    MIN_CASES_FOR_VERDICT,
    VERDICT_INSUFFICIENT,
    VERDICT_NO_BENEFIT,
    VERDICT_REGRESSES,
    VERDICT_REVIEW,
    load_case_set,
    run_paired_evaluation,
    validate_case_set,
)


def _case(case_id: str = "case-1", **overrides):
    case = {
        "case_id": case_id,
        "question": "What reporting style was agreed?",
        "expected_facts": ["concise"],
    }
    case.update(overrides)
    return case


def _case_set(*cases, data_class: str = "synthetic"):
    return {"case_set_version": 1, "data_class": data_class, "cases": list(cases) or [_case()]}


class ValidateCaseSetTests(unittest.TestCase):
    def test_accepts_minimal_case_set(self) -> None:
        result = validate_case_set(_case_set())
        self.assertEqual(result["case_set_version"], 1)
        self.assertEqual(result["data_class"], "synthetic")
        self.assertEqual(len(result["cases"]), 1)
        self.assertEqual(result["cases"][0]["forbidden_facts"], [])

    def test_rejects_wrong_version(self) -> None:
        with self.assertRaises(ValueError):
            validate_case_set({"case_set_version": 2, "data_class": "synthetic", "cases": [_case()]})

    def test_rejects_unknown_data_class(self) -> None:
        with self.assertRaises(ValueError):
            validate_case_set({"case_set_version": 1, "data_class": "private", "cases": [_case()]})

    def test_rejects_empty_cases(self) -> None:
        with self.assertRaises(ValueError):
            validate_case_set({"case_set_version": 1, "data_class": "synthetic", "cases": []})

    def test_rejects_duplicate_case_ids(self) -> None:
        with self.assertRaises(ValueError):
            validate_case_set(_case_set(_case("dup"), _case("dup")))

    def test_rejects_missing_question(self) -> None:
        with self.assertRaises(ValueError):
            validate_case_set(_case_set({"case_id": "case-1", "question": "  ", "expected_facts": ["x"]}))

    def test_rejects_missing_expected_facts(self) -> None:
        with self.assertRaises(ValueError):
            validate_case_set(_case_set({"case_id": "case-1", "question": "Q?"}))

    def test_rejects_blank_expected_fact_entry(self) -> None:
        with self.assertRaises(ValueError):
            validate_case_set(_case_set({"case_id": "case-1", "question": "Q?", "expected_facts": ["ok", " "]}))

    def test_keeps_optional_note(self) -> None:
        result = validate_case_set(_case_set(_case(note="why this case exists")))
        self.assertEqual(result["cases"][0]["note"], "why this case exists")

    def test_load_case_set_reads_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(_case_set()), encoding="utf-8")
            result = load_case_set(path)
        self.assertEqual(len(result["cases"]), 1)


class LoadCasesArgumentTests(unittest.TestCase):
    def test_accepts_bare_case_array(self) -> None:
        self.assertEqual(len(_load_cases_argument(json.dumps([_case()]))), 1)

    def test_unwraps_validated_case_set(self) -> None:
        self.assertEqual(len(_load_cases_argument(json.dumps(_case_set()))), 1)

    def test_rejects_invalid_case_set(self) -> None:
        with self.assertRaises(ValueError):
            _load_cases_argument(json.dumps({"case_set_version": 1, "data_class": "nope", "cases": [_case()]}))


class RunPairedEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = default_paths(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _answers(self, case_set, *, with_context="concise reporting style", without_context="I do not know."):
        return [
            {
                "case_id": case["case_id"],
                "without_context": without_context,
                "with_context": with_context,
            }
            for case in case_set["cases"]
        ]

    def test_insufficient_evidence_below_min_cases(self) -> None:
        case_set = _case_set(*[_case(f"case-{i}") for i in range(MIN_CASES_FOR_VERDICT - 1)])
        report = run_paired_evaluation(self.paths, case_set, self._answers(case_set))
        self.assertEqual(report["verdict"]["decision"], VERDICT_INSUFFICIENT)
        self.assertTrue(report["verdict"]["review_required"] is False)
        self.assertEqual(report["verdict"]["startup_injection"], "disabled")

    def test_insufficient_evidence_when_answers_are_missing(self) -> None:
        case_set = _case_set(*[_case(f"case-{i}") for i in range(MIN_CASES_FOR_VERDICT)])
        report = run_paired_evaluation(self.paths, case_set, self._answers(case_set)[:1])
        self.assertEqual(report["case_count"], MIN_CASES_FOR_VERDICT)
        self.assertLessEqual(report["scoring"]["case_count"], MIN_CASES_FOR_VERDICT)

    def test_reports_no_benefit_when_context_changes_nothing(self) -> None:
        case_set = _case_set(*[_case(f"case-{i}") for i in range(MIN_CASES_FOR_VERDICT)])
        answers = self._answers(case_set, with_context="I do not know.", without_context="I do not know.")
        report = run_paired_evaluation(self.paths, case_set, answers)
        self.assertEqual(report["verdict"]["decision"], VERDICT_NO_BENEFIT)
        self.assertEqual(report["scoring"]["expected_completion_delta"], 0.0)

    def test_reports_regression_when_forbidden_fact_appears(self) -> None:
        cases = [_case(f"case-{i}", forbidden_facts=["secret project"]) for i in range(MIN_CASES_FOR_VERDICT)]
        case_set = _case_set(*cases)
        answers = self._answers(case_set, with_context="concise and part of the secret project")
        report = run_paired_evaluation(self.paths, case_set, answers)
        self.assertEqual(report["verdict"]["decision"], VERDICT_REGRESSES)
        self.assertGreater(report["scoring"]["forbidden_hit_delta"], 0)

    def test_supports_review_and_never_enables_injection(self) -> None:
        case_set = _case_set(*[_case(f"case-{i}") for i in range(MIN_CASES_FOR_VERDICT)])
        report = run_paired_evaluation(self.paths, case_set, self._answers(case_set))
        self.assertEqual(report["verdict"]["decision"], VERDICT_REVIEW)
        self.assertTrue(report["verdict"]["review_required"])
        self.assertEqual(report["verdict"]["startup_injection"], "disabled")

    def test_report_carries_budget_and_prompt_digest(self) -> None:
        case_set = _case_set()
        report = run_paired_evaluation(self.paths, case_set, self._answers(case_set), max_items=5, max_chars=400)
        self.assertEqual(report["budget"]["max_items"], 5)
        self.assertEqual(report["budget"]["max_chars"], 400)
        self.assertEqual(report["prompt_digest"], run_paired_evaluation(
            self.paths, case_set, self._answers(case_set), max_items=5, max_chars=400
        )["prompt_digest"])

    def test_rejects_invalid_min_cases(self) -> None:
        case_set = _case_set()
        with self.assertRaises(ValueError):
            run_paired_evaluation(self.paths, case_set, self._answers(case_set), min_cases=0)

    def test_rejects_unvalidated_case_set(self) -> None:
        with self.assertRaises(ValueError):
            run_paired_evaluation(self.paths, {"cases": []}, [])


if __name__ == "__main__":
    unittest.main()
