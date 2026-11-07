"""Build a bounded, read-only startup context from active orientation knowledge."""

from __future__ import annotations

from typing import Any

from agent_diary.config import Paths
from agent_diary.orientation_knowledge import list_knowledge


def build_context_pack(
    paths: Paths,
    *,
    max_items: int = 20,
    max_chars: int = 8_000,
) -> dict[str, Any]:
    if max_items < 1:
        raise ValueError("max_items must be >= 1")
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")

    active = list_knowledge(paths, status="active")
    selected: list[dict[str, Any]] = []
    used_chars = 0
    omitted = 0
    for record in active:
        if len(selected) >= max_items:
            omitted += 1
            continue
        item = {
            "knowledge_id": record.get("knowledge_id"),
            "candidate_id": record.get("candidate_id"),
            "kind": record.get("kind"),
            "statement": record.get("statement"),
            "confidence": record.get("confidence"),
            "source": {
                "run_id": record.get("run_id"),
                "source_id": record.get("source_id"),
                "source_artifact_id": record.get("source_artifact_id"),
                "source_entry_ids": record.get("source_entry_ids", []),
                "method": record.get("method"),
                "approved_at": record.get("approved_at"),
            },
        }
        item_chars = len(str(item))
        if used_chars + item_chars > max_chars:
            omitted += 1
            continue
        selected.append(item)
        used_chars += item_chars

    return {
        "orientation_context_version": 1,
        "items": selected,
        "item_count": len(selected),
        "active_count": len(active),
        "omitted_count": omitted,
        "estimated_chars": used_chars,
        "limits": {"max_items": max_items, "max_chars": max_chars},
        "note": "Read-only orientation context; not ordinary memory retrieval.",
    }
