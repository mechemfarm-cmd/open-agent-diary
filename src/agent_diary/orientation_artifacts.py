"""General source-artifact records for multimodal orientation.

Artifacts are metadata/provenance records. This module does not read the
locator. Derived representations are only attachable after current content
consent is checked against the orientation run.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from agent_diary.config import Paths
from agent_diary.orientation import load_run

ARTIFACT_TYPES = {
    "conversation",
    "document",
    "image",
    "audio",
    "video",
    "project",
    "spreadsheet",
    "work_trace",
}
REPRESENTATION_KINDS = {
    "text",
    "ocr",
    "caption",
    "transcript",
    "structured",
    "summary",
    "embedding",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _artifact_dir(paths: Paths) -> Path:
    directory = paths.data_root / "orientation" / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_json(path: Path, payload: dict[str, Any]) -> None:
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


def _find_source(paths: Paths, run_id: str, source_id: str) -> dict[str, Any]:
    run = load_run(paths, run_id)
    source = next((item for item in run["sources"] if item.get("source_id") == source_id), None)
    if source is None:
        raise KeyError(f"orientation source not found: {source_id}")
    return source


def create_artifact(
    paths: Paths,
    *,
    run_id: str,
    source_id: str,
    artifact_type: str,
    locator: str,
    mime_type: str | None = None,
    size: int | None = None,
    modified_at: str | None = None,
    sha256: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f"unsupported artifact type: {artifact_type}")
    if not str(locator).strip():
        raise ValueError("artifact locator is required")
    if size is not None and int(size) < 0:
        raise ValueError("artifact size must not be negative")
    source = _find_source(paths, run_id, source_id)
    if source.get("consent") == "revoked":
        raise PermissionError("revoked orientation source cannot create artifacts")

    artifact_id = f"artifact_{uuid4().hex}"
    artifact = {
        "orientation_version": 1,
        "artifact_id": artifact_id,
        "run_id": run_id,
        "source_id": source_id,
        "artifact_type": artifact_type,
        "locator": str(locator),
        "mime_type": mime_type,
        "size": int(size) if size is not None else None,
        "modified_at": modified_at,
        "sha256": sha256,
        "consent": source.get("consent"),
        "content_access_granted": bool(source.get("content_access_granted")),
        "metadata": dict(metadata or {}),
        "representations": [],
        "created_at": _now(),
    }
    _write_json(_artifact_dir(paths) / f"{artifact_id}.json", artifact)
    return artifact


def load_artifact(paths: Paths, artifact_id: str) -> dict[str, Any]:
    artifact_id = str(artifact_id).strip()
    path = _artifact_dir(paths) / f"{artifact_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"orientation artifact not found: {artifact_id}")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("representations"), list):
        raise ValueError(f"orientation artifact is malformed: {artifact_id}")
    return body


def add_representation(
    paths: Paths,
    artifact_id: str,
    kind: str,
    content: str,
    *,
    method: str = "unspecified",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in REPRESENTATION_KINDS:
        raise ValueError(f"unsupported representation kind: {kind}")
    if not str(content).strip():
        raise ValueError("representation content is required")
    artifact = load_artifact(paths, artifact_id)
    source = _find_source(paths, artifact["run_id"], artifact["source_id"])
    if source.get("consent") != "approved" or source.get("content_access_granted") is not True:
        raise PermissionError("source content consent is required for derived representations")
    representation = {
        "representation_id": f"representation_{uuid4().hex}",
        "kind": kind,
        "method": str(method).strip() or "unspecified",
        "content": str(content),
        "metadata": dict(metadata or {}),
        "created_at": _now(),
    }
    artifact["representations"].append(representation)
    artifact["updated_at"] = _now()
    _write_json(_artifact_dir(paths) / f"{artifact_id}.json", artifact)
    return artifact
