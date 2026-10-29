"""Rubric scoring for paired orientation answer evaluations."""

from __future__ import annotations

from typing import Any


def _contains_all(answer: str, terms: list[Any]) -> bool:
    lowered = answer.casefold()
    return all(str(term).casefold() in lowered for term in terms)


def score_answer(answer: str, case: dict[str, Any]) -> dict[str, Any]:
    text = str(answer)
    expected = [str(item) for item in case.get("expected_facts", [])]
    forbidden = [str(item) for item in case.get("forbidden_facts", [])]
    lowered = text.casefold()
    expected_hits = sum(term.casefold() in lowered for term in expected)
    forbidden_hits = sum(term.casefold() in lowered for term in forbidden)
    uncertainty_markers = ("unknown", "not enough information", "i don't know", "cannot determine")
    source_markers = ("source", "evidence", "approved orientation", "according to")
    return {
        "expected_fact_count": len(expected),
        "expected_fact_hits": expected_hits,
        "expected_facts_complete": expected_hits == len(expected),
        "forbidden_fact_hits": forbidden_hits,
        "forbidden_facts_clear": forbidden_hits == 0,
        "uncertainty_marker_present": any(marker in lowered for marker in uncertainty_markers),
        "source_marker_present": any(marker in lowered for marker in source_markers),
        "answer_chars": len(text),
    }


def score_paired_answers(cases: list[dict[str, Any]], answers: list[dict[str, Any]]) -> dict[str, Any]:
    if not cases:
        raise ValueError("scoring requires at least one case")
    by_id = {str(item.get("case_id")): item for item in cases}
    results: list[dict[str, Any]] = []
    for answer in answers:
        case_id = str(answer.get("case_id", ""))
        if case_id not in by_id:
            raise KeyError(f"answer references unknown case: {case_id}")
        case = by_id[case_id]
        without = score_answer(str(answer.get("without_context", "")), case)
        with_context = score_answer(str(answer.get("with_context", "")), case)
        results.append({"case_id": case_id, "without_context": without, "with_context": with_context})
    baseline_complete = sum(item["without_context"]["expected_facts_complete"] for item in results)
    context_complete = sum(item["with_context"]["expected_facts_complete"] for item in results)
    baseline_forbidden = sum(item["without_context"]["forbidden_fact_hits"] for item in results)
    context_forbidden = sum(item["with_context"]["forbidden_fact_hits"] for item in results)
    count = len(results)
    return {
        "evaluation_version": 1,
        "case_count": count,
        "baseline_expected_complete": baseline_complete,
        "context_expected_complete": context_complete,
        "expected_completion_delta": (context_complete - baseline_complete) / count,
        "baseline_forbidden_hits": baseline_forbidden,
        "context_forbidden_hits": context_forbidden,
        "forbidden_hit_delta": context_forbidden - baseline_forbidden,
        "results": results,
        "limitation": "rubric-based lexical scoring; human review remains required for answer quality",
    }
