"""Rubric scoring for paired orientation answer evaluations.

The scorer separates two things a naive substring check conflates:

* **assertion** - the answer states a fact. Only an asserted expected fact earns
  coverage credit.
* **mention** - the term occurs anywhere, including inside a refusal. A correct
  "I was not given that" answer mentions the topic without asserting anything, so
  it must not be scored as coverage.

Expected facts are therefore scanned in statement text only, with unattributed
refusal sentences removed.

Forbidden phrases are scanned across the whole answer, negation-blind. Producing
the phrase at all is the risk, and no window-based "was that negated?" rule
survives contact with real prose: it either excuses a genuine failure or
penalises a correct answer. Write traps as positive assertions that a grounded
answer never needs to reproduce. ``orientation_eval_suite`` enforces the rest.
"""

from __future__ import annotations

import re
from typing import Any

REFUSAL_PATTERNS = (
    r"\bi\s+(?:do not|don't)\s+know\b",
    r"\bi\s+(?:am|'m)\s+not\s+(?:sure|aware|certain)\b",
    r"\b(?:was|were|is|are)\s+not\s+given\b",
    r"\b(?:wasn't|weren't|isn't|aren't)\s+given\b",
    r"\bnot\s+(?:provided|specified|given|supplied|stated|mentioned)\b",
    r"\bno\s+(?:information|approved\s+context|relevant\s+context)\b",
    r"\bnot\s+enough\s+information\b",
    r"\bcannot\s+(?:determine|say|know|tell)\b",
    r"\bcan'?t\s+(?:determine|say|tell)\b",
    r"\bunable\s+to\s+(?:determine|say|tell)\b",
    r"\bdo\s+not\s+have\s+(?:that|this|any)\s+information\b",
)

SOURCE_MARKER_PATTERNS = (
    r"\[source:",
    r"\bsource:",
    r"approved orientation",
    r"approved context",
    r"orientation context",
    r"according to",
    r"per the",
    r"the context states",
    r"provided context",
    r"given context",
    r"based on the approved",
)

UNCERTAINTY_PATTERNS = REFUSAL_PATTERNS + (
    r"\bunknown\b",
)

_REFUSAL_RE = re.compile("|".join(REFUSAL_PATTERNS), re.IGNORECASE)
_SOURCE_RE = re.compile("|".join(SOURCE_MARKER_PATTERNS), re.IGNORECASE)
_UNCERTAINTY_RE = re.compile("|".join(UNCERTAINTY_PATTERNS), re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Models emit typographic punctuation. Without folding it, "don’t know" never
# matches a refusal pattern written with an ASCII apostrophe, and the refusal
# silently counts as a statement.
_PUNCTUATION_FOLD = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u02bc": "'",
    "\u00b4": "'",
    "\u0060": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u00a0": " ",
})


def normalize_text(text: Any) -> str:
    return str(text).translate(_PUNCTUATION_FOLD)


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]


def _is_refusal(sentence: str) -> bool:
    return bool(_REFUSAL_RE.search(sentence))


def _has_source_marker(text: str) -> bool:
    return bool(_SOURCE_RE.search(text.casefold()))


def statement_text(answer: str) -> str:
    """Return the answer with unattributed refusal sentences removed.

    A refusal that also cites a source is kept: the answer is still asserting
    something on the record, so removing it would hide real coverage.
    """
    kept: list[str] = []
    for sentence in split_sentences(normalize_text(answer)):
        lowered = sentence.casefold()
        if _is_refusal(lowered) and not _has_source_marker(lowered):
            continue
        kept.append(sentence)
    return " ".join(kept)


def _count_phrases(text: str, phrases: list[str]) -> int:
    """Count phrase occurrences.

    This is deliberately negation-blind. Any window-based "was that negated?"
    heuristic either excuses a real failure (an answer that did produce the
    forbidden phrase) or penalises a correct answer that had to negate it. Both
    are worse than the alternative: keep forbidden phrases as positive
    assertions that a correct answer never needs to reproduce.
    """
    total = 0
    for phrase in phrases:
        needle = phrase.casefold()
        if not needle:
            continue
        start = 0
        while True:
            index = text.find(needle, start)
            if index == -1:
                break
            total += 1
            start = index + len(needle)
    return total


def source_marker_phrases(text: str) -> list[str]:
    lowered = text.casefold()
    seen: list[str] = []
    for pattern in SOURCE_MARKER_PATTERNS:
        if re.search(pattern, lowered):
            seen.append(pattern)
    return seen


def score_answer(answer: str, case: dict[str, Any]) -> dict[str, Any]:
    text = normalize_text(answer)
    lowered = text.casefold()
    asserted = statement_text(text).casefold()

    expected = [str(item) for item in case.get("expected_facts", [])]
    forbidden = [str(item) for item in case.get("forbidden_facts", [])]

    # Mentions are informational only: they include terms that appear inside a
    # refusal, which is exactly the case the credit-bearing count must exclude.
    expected_mentions = sum(term.casefold() in lowered for term in expected)
    expected_hits = sum(term.casefold() in asserted for term in expected)

    forbidden_hits = _count_phrases(lowered, forbidden)

    return {
        "expected_fact_count": len(expected),
        "expected_fact_hits": expected_hits,
        "expected_fact_mentions": expected_mentions,
        "expected_facts_complete": expected_hits == len(expected),
        "forbidden_fact_hits": forbidden_hits,
        "forbidden_facts_clear": forbidden_hits == 0,
        "declined": bool(_REFUSAL_RE.search(lowered)) and expected_hits == 0,
        "uncertainty_marker_present": bool(_UNCERTAINTY_RE.search(lowered)),
        "source_marker_present": _has_source_marker(lowered),
        "source_marker_phrases": source_marker_phrases(lowered),
        "answer_chars": len(text),
        "asserted_chars": len(asserted),
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
        "baseline_expected_mentions": sum(item["without_context"]["expected_fact_mentions"] for item in results),
        "context_expected_mentions": sum(item["with_context"]["expected_fact_mentions"] for item in results),
        "baseline_forbidden_hits": baseline_forbidden,
        "context_forbidden_hits": context_forbidden,
        "forbidden_hit_delta": context_forbidden - baseline_forbidden,
        "baseline_declined": sum(1 for item in results if item["without_context"]["declined"]),
        "context_declined": sum(1 for item in results if item["with_context"]["declined"]),
        "baseline_source_markers": sum(1 for item in results if item["without_context"]["source_marker_present"]),
        "context_source_markers": sum(1 for item in results if item["with_context"]["source_marker_present"]),
        "results": results,
        "limitation": (
            "rubric-based lexical scoring; coverage excludes refusals, forbidden phrases are matched "
            "verbatim so traps must be positive assertions, and human review is still required for answer quality"
        ),
    }
