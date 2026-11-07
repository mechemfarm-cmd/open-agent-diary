"""Approved orientation knowledge kept separate from ordinary Diary memory."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from agent_diary.config import Paths
from agent_diary.orientation import load_run

KNOWLEDGE_STATUSES = {"active", "revoked", "superseded"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _directory(paths: Paths) -> Path:
    directory = paths.data_root / "orientation" / "knowledge"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _path(paths: Paths, candidate_id: str) -> Path:
    return _directory(paths) / f"{candidate_id}.json"


def _write(path: Path, payload: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_knowledge(paths: Paths, candidate_id: str) -> dict[str, Any]:
    path = _path(paths, candidate_id)
    if not path.exists():
        raise FileNotFoundError(f"orientation knowledge not found: {candidate_id}")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or body.get("status") not in KNOWLEDGE_STATUSES:
        raise ValueError(f"orientation knowledge is malformed: {candidate_id}")
    return body


def materialize_candidate(paths: Paths, candidate: dict[str, Any]) -> dict[str, Any]:
    """Turn one approved candidate into an active, source-linked record."""
    if candidate.get("status") != "approved":
        raise ValueError("only approved candidates can become orientation knowledge")
    run = load_run(paths, str(candidate["run_id"]))
    source = next((item for item in run["sources"] if item.get("source_id") == candidate.get("source_id")), None)
    if source is None:
        raise KeyError(f"orientation source not found: {candidate.get('source_id')}")
    if source.get("consent") != "approved" or source.get("content_access_granted") is not True:
        raise PermissionError("approved orientation knowledge requires active content consent")
    candidate_id = str(candidate["candidate_id"])
    existing_path = _path(paths, candidate_id)
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {}
    record = {
        "orientation_version": 1,
        "knowledge_id": existing.get("knowledge_id", f"orientation_{candidate_id}"),
        "candidate_id": candidate_id,
        "run_id": str(candidate["run_id"]),
        "source_id": str(candidate["source_id"]),
        "source_artifact_id": source.get("metadata", {}).get("artifact_id"),
        "kind": str(candidate["kind"]),
        "statement": str(candidate["statement"]),
        "source_entry_ids": list(candidate.get("source_entry_ids", [])),
        "evidence": list(candidate.get("evidence", [])),
        "confidence": candidate.get("confidence", 0.0),
        "method": str(candidate.get("method", "unspecified")),
        "status": existing.get("status", "active") if existing.get("status") in KNOWLEDGE_STATUSES else "active",
        "approved_at": existing.get("approved_at", _now()),
        "updated_at": _now(),
    }
    _write(existing_path, record)
    return record


def set_knowledge_status(paths: Paths, candidate_id: str, status: str) -> dict[str, Any]:
    if status not in KNOWLEDGE_STATUSES - {"active"}:
        raise ValueError("knowledge status must be revoked or superseded")
    record = load_knowledge(paths, candidate_id)
    record["status"] = status
    record["updated_at"] = _now()
    _write(_path(paths, candidate_id), record)
    return record


def list_knowledge(paths: Paths, *, status: str = "active") -> list[dict[str, Any]]:
    if status not in KNOWLEDGE_STATUSES:
        raise ValueError(f"unsupported knowledge status: {status}")
    records: list[dict[str, Any]] = []
    for path in sorted(_directory(paths).glob("candidate_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("status") == status:
            records.append(record)
    return records


def search_knowledge(paths: Paths, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Search active orientation only; this never consults ordinary memory."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    terms = [term for term in re.findall(r"[\w'-]+", query.casefold()) if len(term) > 1]
    if not terms:
        return []
    matches: list[tuple[int, dict[str, Any]]] = []
    for record in list_knowledge(paths, status="active"):
        haystack = f"{record.get('statement', '')} {record.get('kind', '')}".casefold()
        score = sum(haystack.count(term) for term in terms)
        if score:
            matches.append((score, record))
    matches.sort(key=lambda item: (-item[0], item[1].get("approved_at", "")))
    return [record for _, record in matches[:limit]]
