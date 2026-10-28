"""Combined review views for orientation candidates and approved knowledge."""

from __future__ import annotations

from typing import Any

from agent_diary.config import Paths
from agent_diary.orientation import load_run
from agent_diary.orientation_candidates import list_candidates
from agent_diary.orientation_knowledge import load_knowledge


def show_candidate(paths: Paths, run_id: str, candidate_id: str) -> dict[str, Any]:
    candidate = next(
        (item for item in list_candidates(paths, run_id) if item.get("candidate_id") == candidate_id),
        None,
    )
    if candidate is None:
        raise KeyError(f"orientation candidate not found: {candidate_id}")
    run = load_run(paths, run_id)
    source = next((item for item in run["sources"] if item.get("source_id") == candidate.get("source_id")), None)
    if source is None:
        raise KeyError(f"orientation source not found: {candidate.get('source_id')}")
    knowledge = None
    try:
        knowledge = load_knowledge(paths, candidate_id)
    except FileNotFoundError:
        pass
    return {
        "candidate": candidate,
        "source": {
            "source_id": source.get("source_id"),
            "kind": source.get("kind"),
            "locator": source.get("locator"),
            "scope": source.get("scope"),
            "consent": source.get("consent"),
            "content_access_granted": source.get("content_access_granted"),
            "artifact_id": source.get("metadata", {}).get("artifact_id") if isinstance(source.get("metadata"), dict) else None,
        },
        "knowledge": knowledge,
    }
