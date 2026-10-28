"""Synthetic, model-free evaluation of bounded orientation context."""

from __future__ import annotations

from typing import Any

from agent_diary.config import Paths
from agent_diary.orientation_context import build_context_pack


def evaluate_context_pack(
    paths: Paths,
    cases: list[dict[str, Any]],
    *,
    max_items: int = 20,
    max_chars: int = 8_000,
) -> dict[str, Any]:
    """Evaluate fact coverage against a no-context constant baseline.

    This measures whether required approved facts are present in the pack. It does
    not measure generated answer correctness or model reasoning quality.
    """
    if not cases:
        raise ValueError("evaluation requires at least one case")
    pack = build_context_pack(paths, max_items=max_items, max_chars=max_chars)
    items = pack["items"]
    item_text = {
        str(item.get("knowledge_id")): str(item.get("statement", "")).casefold()
        for item in items
    }
    results: list[dict[str, Any]] = []
    for case in cases:
        expected_id = str(case.get("expected_knowledge_id", ""))
        terms = [str(term).casefold() for term in case.get("expected_terms", [])]
        statement = item_text.get(expected_id, "")
        covered = bool(statement) and all(term in statement for term in terms)
        results.append({
            "case_id": str(case.get("case_id", f"case-{len(results) + 1}")),
            "covered_by_pack": covered,
            "baseline_without_context": False,
            "expected_knowledge_id": expected_id,
        })
    pack_hits = sum(1 for result in results if result["covered_by_pack"])
    total = len(results)
    return {
        "evaluation_version": 1,
        "case_count": total,
        "baseline_hits": 0,
        "pack_hits": pack_hits,
        "baseline_rate": 0.0,
        "pack_rate": pack_hits / total,
        "delta": pack_hits / total,
        "pack_estimated_chars": pack["estimated_chars"],
        "pack_estimated_tokens": (pack["estimated_chars"] + 3) // 4,
        "budget_pass": pack["estimated_chars"] <= max_chars and pack["item_count"] <= max_items,
        "results": results,
        "limitation": "coverage only; not a model answer-quality evaluation",
    }
