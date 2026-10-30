"""Case-set validation and one-shot paired orientation evaluation.

A case set is a reviewable artifact. It declares what an orientation answer is
expected to contain, what it must not contain, and whether its cases come from
synthetic or derived data. The runner combines context packing, paired prompt
generation, and rubric scoring into a single dated report, so a decision about
startup injection rests on recorded evidence instead of a one-off manual run.

The verdict never enables startup injection. It can only report that the evidence
is insufficient, that context regressed, that no benefit was measured, or that the
result supports human review.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from agent_diary.config import Paths
from agent_diary.orientation_answer_evaluation import build_answer_eval_prompts
from agent_diary.orientation_answer_scoring import score_paired_answers

CASE_SET_VERSION = 1
REPORT_VERSION = 1
DATA_CLASSES = {"synthetic", "derived"}
MIN_CASES_FOR_VERDICT = 5

VERDICT_INSUFFICIENT = "insufficient_evidence"
VERDICT_REGRESSES = "regresses"
VERDICT_NO_BENEFIT = "no_measured_benefit"
VERDICT_REVIEW = "supports_review"
DECISIONS = {VERDICT_INSUFFICIENT, VERDICT_REGRESSES, VERDICT_NO_BENEFIT, VERDICT_REVIEW}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_list(value: Any, *, field: str, case_id: str, required: bool) -> list[str]:
    if value is None:
        if required:
            raise ValueError(f"case {case_id} requires at least one {field} entry")
        return []
    if not isinstance(value, list):
        raise ValueError(f"case {case_id} {field} must be a list")
    cleaned = [str(item).strip() for item in value]
    if any(not item for item in cleaned):
        raise ValueError(f"case {case_id} {field} entries must be non-empty strings")
    if required and not cleaned:
        raise ValueError(f"case {case_id} requires at least one {field} entry")
    return cleaned


_NEGATION_WORD_RE = re.compile(r"\b(?:not|never|no|without|cannot)\b", re.IGNORECASE)


def _check_forbidden_phrases(case_id: str, forbidden: list[str], expected: list[str]) -> None:
    """Reject traps that a correct answer would have to produce.

    Forbidden matching is negation-blind, so a trap phrased with a negation word
    ("it is never acceptable to X") is produced by the very answer that denies it.
    Traps must be positive assertions.
    """
    for phrase in forbidden:
        if _NEGATION_WORD_RE.search(phrase):
            raise ValueError(
                f"case {case_id} forbidden phrase must be a positive assertion, not a negated form: {phrase!r}"
            )
        for fact in expected:
            lowered_phrase = phrase.casefold()
            lowered_fact = fact.casefold()
            if lowered_phrase in lowered_fact or lowered_fact in lowered_phrase:
                raise ValueError(
                    f"case {case_id} forbidden phrase overlaps an expected fact and a correct answer "
                    f"would produce it: {phrase!r}"
                )


def validate_case_set(payload: Any) -> dict[str, Any]:
    """Validate a case set and return a normalized copy.

    Cases must name their provenance class so a public fixture cannot silently
    carry derived private content, and every case needs at least one expected
    fact because the paired score measures expected-fact coverage.
    """
    if not isinstance(payload, dict):
        raise ValueError("case set must be an object")
    version = payload.get("case_set_version")
    if version != CASE_SET_VERSION:
        raise ValueError(f"case_set_version must be {CASE_SET_VERSION}")
    data_class = str(payload.get("data_class", "")).strip()
    if data_class not in DATA_CLASSES:
        raise ValueError(f"data_class must be one of: {', '.join(sorted(DATA_CLASSES))}")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("case set requires at least one case")

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"case {index} must be an object")
        case_id = str(raw.get("case_id", "")).strip()
        if not case_id:
            raise ValueError(f"case {index} requires a case_id")
        if case_id in seen:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen.add(case_id)
        question = str(raw.get("question", "")).strip()
        if not question:
            raise ValueError(f"case {case_id} requires a question")
        case: dict[str, Any] = {
            "case_id": case_id,
            "question": question,
            "expected_facts": _text_list(raw.get("expected_facts"), field="expected_facts", case_id=case_id, required=True),
            "forbidden_facts": _text_list(raw.get("forbidden_facts"), field="forbidden_facts", case_id=case_id, required=False),
        }
        note = raw.get("note")
        if isinstance(note, str) and note.strip():
            case["note"] = note.strip()
        _check_forbidden_phrases(case_id, case["forbidden_facts"], case["expected_facts"])
        cases.append(case)

    normalized = {
        "case_set_version": CASE_SET_VERSION,
        "data_class": data_class,
        "cases": cases,
    }
    description = payload.get("description")
    if isinstance(description, str) and description.strip():
        normalized["description"] = description.strip()
    return normalized


def load_case_set(path: str | Path) -> dict[str, Any]:
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_case_set(body)


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decide(case_count: int, scoring: dict[str, Any], min_cases: int) -> dict[str, Any]:
    if case_count < min_cases:
        return {
            "decision": VERDICT_INSUFFICIENT,
            "reason": f"{case_count} case(s) is below the {min_cases} required before a verdict is meaningful",
        }
    forbidden_delta = int(scoring["forbidden_hit_delta"])
    if forbidden_delta > 0:
        return {
            "decision": VERDICT_REGRESSES,
            "reason": f"context raised forbidden-fact hits by {forbidden_delta}",
        }
    delta = float(scoring["expected_completion_delta"])
    if delta <= 0:
        return {
            "decision": VERDICT_NO_BENEFIT,
            "reason": f"expected-fact completion delta was {delta:+.3f}",
        }
    return {
        "decision": VERDICT_REVIEW,
        "reason": (
            f"expected-fact completion improved by {delta:+.3f} with no additional forbidden-fact hits; "
            "human review is still required before enabling startup injection"
        ),
    }


def run_paired_evaluation(
    paths: Paths,
    case_set: Any,
    answers: list[dict[str, Any]],
    *,
    max_items: int = 20,
    max_chars: int = 8_000,
    min_cases: int = MIN_CASES_FOR_VERDICT,
) -> dict[str, Any]:
    """Run packing, paired prompt generation, and scoring into one dated report.

    ``answers`` are produced outside this module; the runner never calls a model,
    so results stay reproducible and no data leaves the machine on their account.
    """
    if min_cases < 1:
        raise ValueError("min_cases must be >= 1")
    normalized = validate_case_set(case_set)
    cases = list(normalized["cases"])
    prompts = build_answer_eval_prompts(paths, cases, max_items=max_items, max_chars=max_chars)
    scoring = score_paired_answers(cases, answers)
    verdict = _decide(len(cases), scoring, min_cases)
    return {
        "report_version": REPORT_VERSION,
        "generated_at": _now(),
        "case_set_version": normalized["case_set_version"],
        "data_class": normalized["data_class"],
        "case_count": len(cases),
        "budget": {
            "max_items": max_items,
            "max_chars": max_chars,
            "pack_estimated_chars": prompts["pack_estimated_chars"],
            "pack_estimated_tokens": prompts["pack_estimated_tokens"],
        },
        "prompt_digest": _digest(prompts["prompts"]),
        "scoring": scoring,
        "verdict": {
            **verdict,
            "min_cases_required": min_cases,
            "startup_injection": "disabled",
            "review_required": verdict["decision"] == VERDICT_REVIEW,
        },
        "limitations": [
            "rubric scoring is lexical and cannot judge reasoning quality",
            "answers are supplied out of band, so this report covers scoring and packing only",
            "a passing verdict is not authorization to enable startup injection",
        ],
    }
