"""Consent-scoped orientation manifests.

This module records what a user has allowed an orientation run to inspect. It
never reads a source locator and never creates diary entries. Content access is
false unless the user explicitly approves a source whose scope is ``content``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import uuid4

from agent_diary.config import Paths

ORIENTATION_VERSION = 1
ORIENTATION_DIR = "orientation"
RUNS_DIR = "runs"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ALLOWED_KINDS = {"hermes_history", "filesystem_root", "project"}
ALLOWED_SCOPES = {"metadata", "content"}
CONSENT_PENDING = "pending"
CONSENT_APPROVED = "approved"
CONSENT_REJECTED = "rejected"
CONSENT_REVOKED = "revoked"
CONSENTS = {CONSENT_PENDING, CONSENT_APPROVED, CONSENT_REJECTED, CONSENT_REVOKED}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(paths: Paths) -> Path:
    return paths.data_root / ORIENTATION_DIR


def _runs_dir(paths: Paths) -> Path:
    path = _root(paths) / RUNS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_run_id(value: str) -> str:
    run_id = str(value).strip()
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must be a filename-safe identifier")
    return run_id


def _validate_source(source: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source, dict):
        raise ValueError("orientation source must be an object")
    source_id = str(source.get("source_id", "")).strip()
    if not source_id or not RUN_ID_PATTERN.fullmatch(source_id):
        raise ValueError("source_id must be a filename-safe identifier")
    kind = str(source.get("kind", "")).strip()
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"unsupported orientation source kind: {kind}")
    locator = str(source.get("locator", "")).strip()
    if not locator:
        raise ValueError("orientation source locator is required")
    scope = str(source.get("scope", "metadata")).strip()
    if scope not in ALLOWED_SCOPES:
        raise ValueError("orientation source scope must be metadata or content")
    return {
        "source_id": source_id,
        "kind": kind,
        "locator": locator,
        "scope": scope,
        "consent": CONSENT_PENDING,
        "content_access_granted": False,
        "requested_at": _now(),
        "decided_at": None,
        "decision_reason": None,
    }


def _run_status(sources: list[dict[str, Any]]) -> str:
    consents = {str(source["consent"]) for source in sources}
    if CONSENT_PENDING in consents:
        return "awaiting_consent"
    if consents == {CONSENT_REVOKED}:
        return "revoked"
    if consents == {CONSENT_REJECTED}:
        return "rejected"
    return "approved"


def _write_run(paths: Paths, run: dict[str, Any]) -> Path:
    target = _runs_dir(paths) / f"{run['run_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{run['run_id']}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(run, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def create_run(paths: Paths, sources: list[dict[str, Any]], *, run_id: str | None = None) -> dict[str, Any]:
    if not sources:
        raise ValueError("orientation run requires at least one source")
    normalized = [_validate_source(source) for source in sources]
    ids = [source["source_id"] for source in normalized]
    if len(set(ids)) != len(ids):
        raise ValueError("orientation source_id values must be unique")
    actual_run_id = _normalize_run_id(run_id or f"orientation_{uuid4().hex}")
    target = _runs_dir(paths) / f"{actual_run_id}.json"
    if target.exists():
        raise FileExistsError(f"orientation run already exists: {actual_run_id}")
    timestamp = _now()
    run = {
        "orientation_version": ORIENTATION_VERSION,
        "run_id": actual_run_id,
        "status": _run_status(normalized),
        "created_at": timestamp,
        "updated_at": timestamp,
        "sources": normalized,
    }
    _write_run(paths, run)
    return run


def load_run(paths: Paths, run_id: str) -> dict[str, Any]:
    normalized = _normalize_run_id(run_id)
    path = _runs_dir(paths) / f"{normalized}.json"
    if not path.exists():
        raise FileNotFoundError(f"orientation run not found: {normalized}")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("sources"), list):
        raise ValueError(f"orientation run is malformed: {normalized}")
    return body


def list_runs(paths: Paths, *, limit: int = 20) -> list[dict[str, Any]]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    directory = _runs_dir(paths)
    runs: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(body, dict) and isinstance(body.get("sources"), list):
            runs.append(body)
        if len(runs) >= limit:
            break
    return runs


def set_consent(
    paths: Paths,
    run_id: str,
    source_id: str,
    decision: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    if decision not in {CONSENT_APPROVED, CONSENT_REJECTED, CONSENT_REVOKED}:
        raise ValueError("decision must be approved, rejected, or revoked")
    run = load_run(paths, run_id)
    source = next((item for item in run["sources"] if item.get("source_id") == source_id), None)
    if source is None:
        raise KeyError(f"orientation source not found: {source_id}")
    current = str(source.get("consent", CONSENT_PENDING))
    if current == CONSENT_REVOKED:
        raise ValueError("revoked orientation consent is terminal")
    if current in {CONSENT_APPROVED, CONSENT_REJECTED} and decision != CONSENT_REVOKED:
        raise ValueError("an already decided source can only be revoked")
    source["consent"] = decision
    source["decided_at"] = _now()
    source["decision_reason"] = reason
    source["content_access_granted"] = decision == CONSENT_APPROVED and source.get("scope") == "content"
    run["status"] = _run_status(run["sources"])
    run["updated_at"] = _now()
    _write_run(paths, run)
    return run
