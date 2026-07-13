from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from agent_diary.config import Paths


LEDGER_VERSION = 1
LEDGER_PATH = "ledger.json"
BATCHES_DIR = "batches"
IMPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def ensure_import_dirs(paths: Paths) -> None:
    paths.imports_dir.mkdir(parents=True, exist_ok=True)
    (paths.imports_dir / BATCHES_DIR).mkdir(parents=True, exist_ok=True)


def _ledger_file(paths: Paths) -> Path:
    return paths.imports_dir / LEDGER_PATH


def load_import_ledger(paths: Paths) -> dict[str, Any]:
    ensure_import_dirs(paths)
    ledger_file = _ledger_file(paths)
    if not ledger_file.exists():
        return {"version": LEDGER_VERSION, "items": {}}
    body = json.loads(ledger_file.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError("import ledger is malformed")
    items = body.get("items", {})
    if not isinstance(items, dict):
        raise ValueError("import ledger items are malformed")
    return {
        "version": int(body.get("version", LEDGER_VERSION)),
        "items": items,
    }


def save_import_ledger(paths: Paths, ledger: dict[str, Any]) -> Path:
    ensure_import_dirs(paths)
    ledger_file = _ledger_file(paths)
    ledger_file.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
    return ledger_file


def _canonicalize_source_payload(entry: dict[str, Any]) -> dict[str, Any]:
    metadata = entry.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "entry_type": entry.get("entry_type"),
        "source": entry.get("source"),
        "author_role": entry.get("author_role"),
        "created_at": entry.get("created_at"),
        "title": entry.get("title"),
        "content": entry.get("content"),
        "metadata": metadata,
    }


def build_source_item_key(entry: dict[str, Any]) -> str:
    metadata = entry.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    external_ref = (
        metadata.get("source_item_id")
        or metadata.get("source_message_id")
        or metadata.get("message_id")
        or metadata.get("conversation_item_id")
    )
    source = str(entry.get("source", "")).strip() or "unknown-source"
    if external_ref:
        return f"{source}::external::{external_ref}"

    canonical = json.dumps(_canonicalize_source_payload(entry), sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{source}::fingerprint::{digest}"


def normalize_import_id(import_id: str) -> str:
    normalized = str(import_id).strip()
    if not normalized:
        raise ValueError("import_id is required")
    if not IMPORT_ID_PATTERN.fullmatch(normalized):
        raise ValueError("import_id must be a filename-safe id using letters, numbers, dots, underscores, or hyphens")
    return normalized


def write_import_batch_manifest(
    paths: Paths,
    *,
    import_id: str,
    manifest: dict[str, Any],
) -> Path:
    ensure_import_dirs(paths)
    normalized = normalize_import_id(import_id)
    target = paths.imports_dir / BATCHES_DIR / f"{normalized}.json"
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return target


def list_import_batch_manifests(paths: Paths, limit: int = 20) -> list[dict[str, Any]]:
    ensure_import_dirs(paths)
    manifests: list[dict[str, Any]] = []
    batch_dir = paths.imports_dir / BATCHES_DIR
    for path in sorted(batch_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(body, dict):
            continue
        body = dict(body)
        body["manifest_path"] = str(path)
        body["manifest_file"] = path.name
        manifests.append(body)
        if len(manifests) >= limit:
            break
    return manifests


def load_import_batch_manifest(paths: Paths, import_id: str) -> dict[str, Any]:
    ensure_import_dirs(paths)
    normalized = normalize_import_id(import_id)
    path = paths.imports_dir / BATCHES_DIR / f"{normalized}.json"
    if not path.exists():
        raise FileNotFoundError(f"import batch manifest not found: {normalized}")
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError(f"import batch manifest is malformed: {normalized}")
    return body


def build_import_audit_summary(
    parsed_rows: list[dict[str, Any]],
    *,
    imported: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> dict[str, Any]:
    entry_type_counts = Counter()
    source_counts = Counter()
    author_role_counts = Counter()
    created_at_values: list[str] = []
    for row in parsed_rows:
        entry = row.get("entry", {})
        if not isinstance(entry, dict):
            continue
        entry_type_counts[str(entry.get("entry_type", "")).strip() or "unknown"] += 1
        source_counts[str(entry.get("source", "")).strip() or "unknown"] += 1
        author_role_counts[str(entry.get("author_role", "")).strip() or "unknown"] += 1
        created_at = str(entry.get("created_at", "")).strip()
        if created_at:
            created_at_values.append(created_at)

    duplicate_entry_ids = sorted(
        {
            str(item.get("existing_entry_id", "")).strip()
            for item in skipped
            if isinstance(item, dict) and str(item.get("existing_entry_id", "")).strip()
        }
    )
    imported_entry_ids = [
        str(item.get("entry_id", "")).strip()
        for item in imported
        if isinstance(item, dict) and str(item.get("entry_id", "")).strip()
    ]

    return {
        "line_count": len(parsed_rows),
        "entry_type_counts": dict(sorted(entry_type_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "author_role_counts": dict(sorted(author_role_counts.items())),
        "created_at_range": {
            "first": min(created_at_values) if created_at_values else None,
            "last": max(created_at_values) if created_at_values else None,
        },
        "imported_entry_ids": imported_entry_ids,
        "duplicate_existing_entry_ids": duplicate_entry_ids,
        "duplicate_source_item_count": sum(
            1
            for item in skipped
            if isinstance(item, dict) and str(item.get("reason", "")).strip() == "duplicate_source_item"
        ),
    }
