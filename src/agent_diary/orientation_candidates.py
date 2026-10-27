"""Reviewable orientation candidates derived from imported source entries."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from agent_diary.analytics.common import entry_row_to_body
from agent_diary.analytics.open_loops import build_open_loops_payload
from agent_diary.config import Paths
from agent_diary.index.repository import list_entry_rows
from agent_diary.orientation import load_run, update_source_metadata

CANDIDATE_STATUSES = {"pending", "approved", "rejected", "stale"}
_EXPLICIT_HINTS = (
    "i prefer ",
    "i like ",
    "i use ",
    "i want ",
    "my preference",
    "we decided ",
    "we agreed ",
    "the decision is ",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_dir(paths: Paths) -> Path:
    directory = paths.data_root / "orientation" / "candidates"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write(path: Path, body: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_candidates(paths: Paths, run_id: str) -> dict[str, Any]:
    path = _candidate_dir(paths) / f"{run_id}.json"
    if not path.exists():
        return {"orientation_version": 1, "run_id": run_id, "generated_at": None, "candidates": []}
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("candidates"), list):
        raise ValueError(f"orientation candidates are malformed: {run_id}")
    return body


def _candidate_id(run_id: str, kind: str, text: str, source_ids: list[str]) -> str:
    basis = "|".join([run_id, kind, text, *sorted(source_ids)])
    return f"candidate_{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:24]}"


def _sentence_candidates(body: dict[str, Any]) -> list[dict[str, Any]]:
    role = str(body.get("author_role", "")).lower()
    if role not in {"human", "user"}:
        return []
    entry_id = str(body.get("entry_id", ""))
    content = " ".join(str(body.get("content", "")).split())
    output: list[dict[str, Any]] = []
    for part in re.split(r"(?<=[.!?])\s+", content):
        text = part.strip()
        lowered = text.lower()
        if len(text) < 12 or not any(hint in lowered for hint in _EXPLICIT_HINTS):
            continue
        output.append({
            "kind": "user_statement",
            "statement": text[:300],
            "source_entry_ids": [entry_id],
            "evidence": [{"entry_id": entry_id, "quote": text[:300]}],
            "confidence": 0.55,
            "method": "explicit-user-statement-v1",
        })
    return output


def generate_candidates(
    paths: Paths,
    *,
    run_id: str,
    source_id: str,
    limit: int = 10_000,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    run = load_run(paths, run_id)
    source = next((item for item in run["sources"] if item.get("source_id") == source_id), None)
    if source is None:
        raise KeyError(f"orientation source not found: {source_id}")
    if source.get("consent") != "approved":
        raise PermissionError("orientation candidates require approved source consent")
    import_id = source.get("metadata", {}).get("last_import_id") if isinstance(source.get("metadata"), dict) else None
    if not import_id:
        raise ValueError("source has no completed import to analyze")

    rows = list_entry_rows(paths.sqlite_path, limit=limit, offset=0, import_id=str(import_id))
    bodies = [entry_row_to_body(row) for row in rows]
    proposed: list[dict[str, Any]] = []
    for body in bodies:
        proposed.extend(_sentence_candidates(body))
    loops = build_open_loops_payload(source_entries=rows).get("loops", [])
    for loop in loops:
        proposed.append({
            "kind": "open_loop",
            "statement": str(loop.get("title", "")),
            "source_entry_ids": list(loop.get("supporting_entry_ids", [])),
            "evidence": list(loop.get("evidence_snippets", [])),
            "confidence": float(loop.get("signals", {}).get("confidence", 0.0)),
            "method": "open-loop-detector-v1",
        })

    existing = _load_candidates(paths, run_id)
    prior = {str(item.get("candidate_id")): item for item in existing["candidates"] if isinstance(item, dict)}
    candidates: dict[str, dict[str, Any]] = {}
    for item in proposed:
        statement = str(item.get("statement", "")).strip()
        source_ids = [str(value) for value in item.get("source_entry_ids", []) if str(value).strip()]
        if not statement or not source_ids:
            continue
        candidate_id = _candidate_id(run_id, str(item["kind"]), statement, source_ids)
        old = prior.get(candidate_id, {})
        candidates[candidate_id] = {
            "candidate_id": candidate_id,
            "run_id": run_id,
            "source_id": source_id,
            "kind": item["kind"],
            "statement": statement,
            "source_entry_ids": source_ids,
            "evidence": item.get("evidence", []),
            "confidence": item.get("confidence", 0.0),
            "method": item["method"],
            "status": old.get("status", "pending"),
            "created_at": old.get("created_at", _now()),
            "updated_at": _now(),
        }

    result = {
        "orientation_version": 1,
        "run_id": run_id,
        "source_id": source_id,
        "import_id": str(import_id),
        "generated_at": _now(),
        "candidate_count": len(candidates),
        "candidates": sorted(candidates.values(), key=lambda item: (item["status"], item["kind"], item["candidate_id"])),
    }
    _write(_candidate_dir(paths) / f"{run_id}.json", result)
    update_source_metadata(paths, run_id, source_id, {"candidate_count": len(candidates), "candidates_generated_at": result["generated_at"]})
    return result


def list_candidates(paths: Paths, run_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
    if status is not None and status not in CANDIDATE_STATUSES:
        raise ValueError(f"unsupported candidate status: {status}")
    candidates = _load_candidates(paths, run_id)["candidates"]
    return [item for item in candidates if status is None or item.get("status") == status]


def set_candidate_status(paths: Paths, run_id: str, candidate_id: str, status: str) -> dict[str, Any]:
    if status not in CANDIDATE_STATUSES - {"pending"}:
        raise ValueError("status must be approved, rejected, or stale")
    body = _load_candidates(paths, run_id)
    item = next((candidate for candidate in body["candidates"] if candidate.get("candidate_id") == candidate_id), None)
    if item is None:
        raise KeyError(f"orientation candidate not found: {candidate_id}")
    if item.get("status") == "rejected" and status == "approved":
        raise ValueError("rejected candidates require regeneration before approval")
    item["status"] = status
    item["updated_at"] = _now()
    _write(_candidate_dir(paths) / f"{run_id}.json", body)
    return item
