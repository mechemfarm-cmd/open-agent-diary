"""Build reproducible paired prompts for orientation answer evaluation."""

from __future__ import annotations

from typing import Any

from agent_diary.config import Paths
from agent_diary.orientation_context import build_context_pack


def build_answer_eval_prompts(
    paths: Paths,
    cases: list[dict[str, Any]],
    *,
    max_items: int = 20,
    max_chars: int = 8_000,
) -> dict[str, Any]:
    if not cases:
        raise ValueError("evaluation requires at least one case")
    pack = build_context_pack(paths, max_items=max_items, max_chars=max_chars)
    context_text = "\n".join(
        f"- {item['statement']} [source: {item['source']['source_entry_ids']}]"
        for item in pack["items"]
    )
    prompts: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        question = str(case.get("question", "")).strip()
        if not question:
            raise ValueError(f"case {index} requires a question")
        prompts.append({
            "case_id": str(case.get("case_id", f"case-{index}")),
            "question": question,
            "without_context": question,
            "with_context": (
                "Use only the approved orientation context below when it is relevant. "
                "Do not invent facts.\n\n"
                f"Approved orientation context:\n{context_text or '(none)'}\n\n"
                f"Question: {question}"
            ),
            "expected_facts": list(case.get("expected_facts", [])),
            "forbidden_facts": list(case.get("forbidden_facts", [])),
            "rubric": {
                "must_use_expected_facts": True,
                "must_not_invent_forbidden_facts": True,
                "must_acknowledge_unknowns": True,
                "must_preserve_source_uncertainty": True,
            },
        })
    return {
        "evaluation_version": 1,
        "case_count": len(prompts),
        "pack_estimated_chars": pack["estimated_chars"],
        "pack_estimated_tokens": (pack["estimated_chars"] + 3) // 4,
        "prompts": prompts,
        "scoring_note": "Score answers independently; prompt generation does not claim answer quality.",
    }
