from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from agent_diary.analytics.common import collect_source_rows, entry_has_active_artifact_type
from agent_diary.analytics.conversation_briefs import build_conversation_brief_text
from agent_diary.analytics.compressed_memory import build_compressed_memory_text
from agent_diary.analytics.open_loops import build_open_loops_payload
from agent_diary.config import Paths
from agent_diary.index.repository import (
    get_entry_row,
    get_work_trace_row,
    insert_artifact,
    insert_entry,
    insert_memory_index_row,
    insert_work_trace_event,
    list_entry_rows,
    count_entry_rows,
    list_work_trace_rows,
    list_work_trace_counts_for_entries,
    search_memory as search_index,
    search_work_trace as search_work_trace_index,
)
from agent_diary.models.types import Artifact, Overlay, RawEntry, WorkTraceEvent
from agent_diary.storage.entry_reader import fetch_raw_entry as fetch_entry_from_files
from agent_diary.storage.files import (
    append_artifact,
    append_overlay as append_overlay_file,
    append_raw_entry,
    append_work_trace_event as append_work_trace_event_file,
)
from agent_diary.storage.imports import (
    build_import_audit_summary,
    build_source_item_key,
    load_import_batch_manifest,
    list_import_batch_manifests,
    load_import_ledger,
    normalize_import_id,
    save_import_ledger,
    write_import_batch_manifest,
)


def _require_fields(payload: dict[str, Any], fields: list[str]) -> None:
    missing = [name for name in fields if name not in payload or payload[name] in (None, "")]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")


def append_entry(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(
        payload,
        ["entry_type", "source", "author_role", "content", "created_at"],
    )
    entry = RawEntry(**payload)
    raw_path = append_raw_entry(paths, entry)
    insert_entry(paths.sqlite_path, entry, str(raw_path))
    result = {"entry_id": entry.entry_id, "raw_file": str(raw_path)}
    _append_work_trace_best_effort(
        paths,
        {
            "event_type": "file_change",
            "summary": f"Appended raw entry {entry.entry_id}.",
            "project": "agent-diary",
            "source_surface": "agent-diary",
            "details": {
                "change_kind": "create",
                "entry_type": entry.entry_type,
                "source": entry.source,
                "author_role": entry.author_role,
            },
            "related_entry_ids": [entry.entry_id],
            "related_paths": [str(raw_path)],
            "tags": ["auto", "file_change", "raw_entry"],
        },
    )
    return result


def _normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_string_list(value: Any, *, field_name: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return [str(item).strip() for item in value if str(item).strip()]


def _append_work_trace_best_effort(paths: Paths, payload: dict[str, Any]) -> None:
    try:
        append_work_trace_event(paths, payload)
    except Exception as exc:
        print(f"[agent-diary] WARN: work trace append failed: {exc}")


def append_work_trace_event(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(payload, ["event_type", "summary"])
    details = payload.get("details", {})
    if details in (None, ""):
        details = {}
    if not isinstance(details, dict):
        raise ValueError("details must be an object")

    event_payload = {
        "event_type": str(payload["event_type"]).strip(),
        "summary": str(payload["summary"]).strip(),
        "details": details,
        "project": _normalize_optional_text(payload.get("project")),
        "source_surface": _normalize_optional_text(payload.get("source_surface")),
        "actor": _normalize_optional_text(payload.get("actor")),
        "session_key": _normalize_optional_text(payload.get("session_key")),
        "task_id": _normalize_optional_text(payload.get("task_id")),
        "related_entry_ids": _normalize_string_list(payload.get("related_entry_ids"), field_name="related_entry_ids"),
        "related_artifact_ids": _normalize_string_list(payload.get("related_artifact_ids"), field_name="related_artifact_ids"),
        "related_paths": _normalize_string_list(payload.get("related_paths"), field_name="related_paths"),
        "tags": _normalize_string_list(payload.get("tags"), field_name="tags"),
    }
    event_id = _normalize_optional_text(payload.get("event_id"))
    if event_id:
        event_payload["event_id"] = event_id
    created_at = _normalize_optional_text(payload.get("created_at"))
    if created_at:
        event_payload["created_at"] = created_at

    event = WorkTraceEvent(**event_payload)
    event_path = append_work_trace_event_file(paths, event)
    insert_work_trace_event(paths.sqlite_path, event, str(event_path))
    return {"event_id": event.event_id, "work_file": str(event_path)}


def fetch_work_trace_event(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(payload, ["event_id"])
    event_id = str(payload["event_id"]).strip()
    row = get_work_trace_row(paths.sqlite_path, event_id)
    if row is None:
        raise FileNotFoundError(f"work trace event not found: {event_id}")
    work_file = Path(str(row["work_file_path"]))
    if not work_file.exists():
        raise FileNotFoundError(f"work trace file missing: {work_file}")
    body = json.loads(work_file.read_text(encoding="utf-8"))
    return {"event": body}


def list_work_trace(paths: Paths, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    limit = int(payload.get("limit", 20))
    offset = int(payload.get("offset", 0))
    event_type = _normalize_optional_text(payload.get("event_type"))
    project = _normalize_optional_text(payload.get("project"))
    rows = list_work_trace_rows(
        paths.sqlite_path,
        limit=limit,
        offset=offset,
        event_type=event_type,
        project=project,
    )
    items = [
        {
            "event_id": row["event_id"],
            "created_at": row["created_at"],
            "event_type": row["event_type"],
            "summary": row["summary"],
            "project": row.get("project"),
            "source_surface": row.get("source_surface"),
            "actor": row.get("actor"),
            "session_key": row.get("session_key"),
            "task_id": row.get("task_id"),
            "work_file_path": row.get("work_file_path"),
        }
        for row in rows
    ]
    return {
        "limit": limit,
        "offset": offset,
        "filters": {"event_type": event_type, "project": project},
        "count": len(items),
        "items": items,
    }


def _build_work_trace_snippet(text: str, query: str, size: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= size:
        return compact
    terms = [t for t in re.findall(r"\w+", query.lower()) if t]
    lowered = compact.lower()
    pivot = -1
    for term in terms:
        idx = lowered.find(term)
        if idx != -1 and (pivot == -1 or idx < pivot):
            pivot = idx
    if pivot == -1:
        return compact[: size - 3] + "..."
    start = max(0, pivot - (size // 3))
    end = min(len(compact), start + size)
    snippet = compact[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(compact):
        snippet = snippet + "..."
    return snippet


def search_work_trace(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    limit = int(payload.get("limit", 20))
    event_type = _normalize_optional_text(payload.get("event_type"))
    project = _normalize_optional_text(payload.get("project"))
    matches = search_work_trace_index(
        paths.sqlite_path,
        query=query,
        limit=limit,
        event_type=event_type,
        project=project,
    )
    items = [
        {
            "event_id": row["event_id"],
            "created_at": row["created_at"],
            "event_type": row["event_type"],
            "summary": row["summary"],
            "project": row.get("project"),
            "source_surface": row.get("source_surface"),
            "actor": row.get("actor"),
            "session_key": row.get("session_key"),
            "task_id": row.get("task_id"),
            "match_text": _build_work_trace_snippet(
                " ".join(
                    part
                    for part in [str(row.get("summary", "")), str(row.get("project", "")), str(row.get("work_file_path", ""))]
                    if part and part != "None"
                ),
                query,
            ),
            "fetch_work_trace": {"event_id": row["event_id"]},
        }
        for row in matches
    ]
    return {
        "query": query,
        "limit": limit,
        "filters": {"event_type": event_type, "project": project},
        "count": len(items),
        "matches": items,
    }


def _entry_search_context(paths: Paths, entry_id: str) -> dict[str, Any]:
    row = get_entry_row(paths.sqlite_path, entry_id)
    if row is None:
        return {}
    raw_file = Path(str(row["raw_file_path"]))
    if not raw_file.exists():
        return {}
    body = json.loads(raw_file.read_text(encoding="utf-8"))
    return {
        "created_at": body.get("created_at"),
        "entry_type": body.get("entry_type"),
        "source": body.get("source"),
        "author_role": body.get("author_role"),
        "title": body.get("title"),
    }


def search_all(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    limit = int(payload.get("limit", 20))

    memory_result = search_memory(paths, payload)
    work_trace_result = search_work_trace(
        paths,
        {
            "query": query,
            "limit": limit,
            "event_type": payload.get("event_type"),
            "project": payload.get("project"),
        },
    )

    combined: list[dict[str, Any]] = []

    for match in memory_result.get("matches", []):
        entry_id = str(match.get("entry_id", "")).strip()
        context = _entry_search_context(paths, entry_id) if entry_id else {}
        combined.append(
            {
                "search_domain": "conversation",
                "created_at": context.get("created_at"),
                "entry_id": entry_id or None,
                "entry_type": context.get("entry_type") or match.get("entry_type"),
                "source": context.get("source") or match.get("source"),
                "author_role": context.get("author_role") or match.get("author_role"),
                "title": context.get("title"),
                "match_layer": match.get("match_layer"),
                "supporting_layers": match.get("supporting_layers", []),
                "match_text": match.get("match_text"),
                "fetch_raw_entry": match.get("fetch_raw_entry"),
            }
        )

    for match in work_trace_result.get("matches", []):
        combined.append(
            {
                "search_domain": "work_trace",
                "created_at": match.get("created_at"),
                "event_id": match.get("event_id"),
                "event_type": match.get("event_type"),
                "summary": match.get("summary"),
                "project": match.get("project"),
                "source_surface": match.get("source_surface"),
                "actor": match.get("actor"),
                "session_key": match.get("session_key"),
                "task_id": match.get("task_id"),
                "match_text": match.get("match_text"),
                "fetch_work_trace": match.get("fetch_work_trace"),
            }
        )

    combined.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    combined = combined[:limit]

    return {
        "query": query,
        "limit": limit,
        "filters": {
            "source_conversation_id": memory_result.get("filters", {}).get("source_conversation_id"),
            "source_session_id": memory_result.get("filters", {}).get("source_session_id"),
            "import_id": memory_result.get("filters", {}).get("import_id"),
            "truthful_only": memory_result.get("filters", {}).get("truthful_only", False),
            "event_type": work_trace_result.get("filters", {}).get("event_type"),
            "project": work_trace_result.get("filters", {}).get("project"),
        },
        "counts": {
            "conversation_matches": len(memory_result.get("matches", [])),
            "work_trace_matches": len(work_trace_result.get("matches", [])),
            "combined_matches": len(combined),
            "compressed_memory_hits": memory_result.get("match_summary", {}).get("compressed_memory_hits", 0),
            "raw_entry_hits": memory_result.get("match_summary", {}).get("raw_entry_hits", 0),
        },
        "matches": combined,
        "note": "Combined search spans authoritative conversation truth, derived memory artifacts, and recorded work-trace activity.",
    }


def append_overlay(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(payload, ["entry_id", "overlay_type", "author", "content"])
    entry_id = str(payload["entry_id"]).strip()
    if not entry_id:
        raise ValueError("entry_id is required")
    if get_entry_row(paths.sqlite_path, entry_id) is None:
        raise FileNotFoundError(f"entry not found: {entry_id}")

    overlay = Overlay(**payload)
    overlay_path = append_overlay_file(paths, overlay)
    result = {
        "entry_id": overlay.entry_id,
        "overlay_id": overlay.overlay_id,
        "overlay_file": str(overlay_path),
        "overlay_type": overlay.overlay_type,
    }
    _append_work_trace_best_effort(
        paths,
        {
            "event_type": "file_change",
            "summary": f"Appended {overlay.overlay_type} overlay for entry {overlay.entry_id}.",
            "project": "agent-diary",
            "source_surface": "agent-diary",
            "details": {
                "change_kind": "create",
                "overlay_type": overlay.overlay_type,
                "author": overlay.author,
            },
            "related_entry_ids": [overlay.entry_id],
            "related_paths": [str(overlay_path)],
            "tags": ["auto", "file_change", "overlay"],
        },
    )
    return result


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_import_id() -> str:
    return f"import_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"


def import_session_jsonl(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(payload, ["path"])
    import_path = Path(str(payload["path"])).expanduser().resolve()
    if not import_path.exists():
        raise FileNotFoundError(f"import file not found: {import_path}")

    import_id = normalize_import_id(str(payload.get("import_id") or _make_import_id()))
    imported_at = _utc_now_iso()
    source_session_id = str(payload.get("source_session_id", "")).strip() or None
    source_conversation_id = str(payload.get("source_conversation_id", "")).strip() or None
    dry_run = bool(payload.get("dry_run", False))

    parsed_rows: list[dict[str, Any]] = []
    with import_path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            _require_fields(obj, ["entry_type", "source", "author_role", "content", "created_at"])
            parsed_rows.append({"line": idx, "entry": obj})

    ledger = load_import_ledger(paths)
    ledger_items = ledger["items"]
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in parsed_rows:
        obj = dict(row["entry"])
        metadata = obj.get("metadata", {})
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError(f"metadata must be an object on line {row['line']}")

        if source_session_id and "source_session_id" not in metadata:
            metadata["source_session_id"] = source_session_id
        if source_conversation_id and "source_conversation_id" not in metadata:
            metadata["source_conversation_id"] = source_conversation_id

        source_item_key = build_source_item_key({**obj, "metadata": metadata})
        if source_item_key in ledger_items:
            existing = ledger_items[source_item_key]
            skipped.append(
                {
                    "line": row["line"],
                    "reason": "duplicate_source_item",
                    "source_item_key": source_item_key,
                    "existing_entry_id": existing["entry_id"],
                }
            )
            continue

        ingestion_meta = {
            "truthful_source": True,
            "import_mode": "session_jsonl",
            "import_id": import_id,
            "imported_at": imported_at,
            "source_item_key": source_item_key,
        }
        if source_session_id:
            ingestion_meta["source_session_id"] = source_session_id
        if source_conversation_id:
            ingestion_meta["source_conversation_id"] = source_conversation_id
        metadata["ingestion"] = ingestion_meta

        entry_payload = {
            "entry_type": obj["entry_type"],
            "source": obj["source"],
            "author_role": obj["author_role"],
            "content": obj["content"],
            "created_at": obj["created_at"],
            "metadata": metadata,
        }
        if "title" in obj:
            entry_payload["title"] = obj["title"]

        if dry_run:
            imported.append({"line": row["line"], "source_item_key": source_item_key, "dry_run": True})
            continue

        result = append_entry(paths, entry_payload)
        imported.append(
            {
                "line": row["line"],
                "entry_id": result["entry_id"],
                "raw_file": result["raw_file"],
                "source_item_key": source_item_key,
            }
        )
        ledger_items[source_item_key] = {
            "entry_id": result["entry_id"],
            "import_id": import_id,
            "imported_at": imported_at,
            "source": obj["source"],
            "created_at": obj["created_at"],
            "line": row["line"],
        }

    manifest = {
        "import_id": import_id,
        "imported_at": imported_at,
        "import_mode": "session_jsonl",
        "input_path": str(import_path),
        "source_session_id": source_session_id,
        "source_conversation_id": source_conversation_id,
        "dry_run": dry_run,
        "imported_count": len(imported),
        "skipped_count": len(skipped),
        "imported": imported,
        "skipped": skipped,
        "audit": build_import_audit_summary(parsed_rows, imported=imported, skipped=skipped),
    }

    if not dry_run:
        ledger_path = save_import_ledger(paths, ledger)
        manifest["ledger_path"] = str(ledger_path)
    manifest_path = write_import_batch_manifest(paths, import_id=import_id, manifest=manifest)
    manifest["manifest_path"] = str(manifest_path)
    _append_work_trace_best_effort(
        paths,
        {
            "event_type": "action",
            "summary": f"Imported session JSONL batch {import_id}.",
            "project": "agent-diary",
            "source_surface": "agent-diary",
            "details": {
                "import_mode": "session_jsonl",
                "import_id": import_id,
                "dry_run": dry_run,
                "imported_count": len(imported),
                "skipped_count": len(skipped),
                "source_session_id": source_session_id,
                "source_conversation_id": source_conversation_id,
            },
            "related_entry_ids": [
                str(item.get("entry_id", "")).strip()
                for item in imported
                if isinstance(item, dict) and str(item.get("entry_id", "")).strip()
            ],
            "related_paths": [
                str(import_path),
                str(manifest_path),
                str(manifest.get("ledger_path", "")) if manifest.get("ledger_path") else "",
            ],
            "tags": ["auto", "action", "import"],
        },
    )
    return manifest


def import_session_and_refresh_derived(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    import_result = import_session_jsonl(paths, payload)
    imported_entry_ids = [
        str(item.get("entry_id", "")).strip()
        for item in import_result.get("imported", [])
        if str(item.get("entry_id", "")).strip()
    ]
    dry_run = bool(payload.get("dry_run", False))
    derived: dict[str, Any] = {}
    if dry_run:
        derived = {
            "conversation_briefs": {"status": "skipped_dry_run", "produced_count": 0, "skipped_count": 0},
            "compressed_memory": {"status": "skipped_dry_run", "produced_count": 0, "skipped_count": 0},
            "open_loops": {"status": "skipped_dry_run", "loop_count": 0},
        }
    elif not imported_entry_ids:
        derived = {
            "conversation_briefs": {"status": "skipped_no_imports", "produced_count": 0, "skipped_count": 0},
            "compressed_memory": {"status": "skipped_no_imports", "produced_count": 0, "skipped_count": 0},
            "open_loops": {"status": "skipped_no_imports", "loop_count": 0},
        }
    else:
        brief_result = produce_conversation_briefs(paths, {"entry_ids": imported_entry_ids, "limit": len(imported_entry_ids)})
        memory_result = produce_compressed_memory(paths, {"entry_ids": imported_entry_ids, "limit": len(imported_entry_ids)})
        open_loops_result = produce_open_loops(paths, {"entry_ids": imported_entry_ids, "limit": len(imported_entry_ids)})
        derived = {
            "conversation_briefs": {
                "status": "ok",
                "produced_count": int(brief_result.get("produced_count", 0)),
                "skipped_count": int(brief_result.get("skipped_count", 0)),
            },
            "compressed_memory": {
                "status": "ok",
                "produced_count": int(memory_result.get("produced_count", 0)),
                "skipped_count": int(memory_result.get("skipped_count", 0)),
            },
            "open_loops": {
                "status": "ok",
                "loop_count": int(open_loops_result.get("loop_count", 0)),
                "artifact_id": open_loops_result.get("artifact_id"),
            },
        }

    result = {
        "import_id": import_result.get("import_id"),
        "imported_count": int(import_result.get("imported_count", 0)),
        "skipped_count": int(import_result.get("skipped_count", 0)),
        "imported_entry_ids": imported_entry_ids,
        "import_result": import_result,
        "derived": derived,
    }
    _append_work_trace_best_effort(
        paths,
        {
            "event_type": "action",
            "summary": f"Imported session batch and refreshed derived layers for {result['import_id']}.",
            "project": "agent-diary",
            "source_surface": "agent-diary",
            "details": {
                "import_id": result["import_id"],
                "imported_count": result["imported_count"],
                "skipped_count": result["skipped_count"],
                "derived": derived,
            },
            "related_entry_ids": imported_entry_ids,
            "tags": ["auto", "action", "import", "derived_refresh"],
        },
    )
    return result


def refresh_derived_for_import(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(payload, ["import_id"])
    import_id = normalize_import_id(str(payload["import_id"]))
    dry_run = bool(payload.get("dry_run", False))
    force = bool(payload.get("force", True))

    manifest = load_import_batch_manifest(paths, import_id=import_id)
    imported_entry_ids = [
        str(item.get("entry_id", "")).strip()
        for item in manifest.get("imported", [])
        if isinstance(item, dict) and str(item.get("entry_id", "")).strip()
    ]

    if dry_run:
        derived = {
            "conversation_briefs": {"status": "skipped_dry_run", "produced_count": 0, "skipped_count": 0},
            "compressed_memory": {"status": "skipped_dry_run", "produced_count": 0, "skipped_count": 0},
            "open_loops": {"status": "skipped_dry_run", "loop_count": 0},
        }
    elif not imported_entry_ids:
        derived = {
            "conversation_briefs": {"status": "skipped_no_imported_entries", "produced_count": 0, "skipped_count": 0},
            "compressed_memory": {"status": "skipped_no_imported_entries", "produced_count": 0, "skipped_count": 0},
            "open_loops": {"status": "skipped_no_imported_entries", "loop_count": 0},
        }
    else:
        brief_result = produce_conversation_briefs(
            paths, {"entry_ids": imported_entry_ids, "limit": len(imported_entry_ids), "force": force}
        )
        memory_result = produce_compressed_memory(
            paths, {"entry_ids": imported_entry_ids, "limit": len(imported_entry_ids), "force": force}
        )
        open_loops_result = produce_open_loops(paths, {"entry_ids": imported_entry_ids, "limit": len(imported_entry_ids)})
        derived = {
            "conversation_briefs": {
                "status": "ok",
                "produced_count": int(brief_result.get("produced_count", 0)),
                "skipped_count": int(brief_result.get("skipped_count", 0)),
            },
            "compressed_memory": {
                "status": "ok",
                "produced_count": int(memory_result.get("produced_count", 0)),
                "skipped_count": int(memory_result.get("skipped_count", 0)),
            },
            "open_loops": {
                "status": "ok",
                "loop_count": int(open_loops_result.get("loop_count", 0)),
                "artifact_id": open_loops_result.get("artifact_id"),
            },
        }

    result = {
        "import_id": import_id,
        "imported_entry_count": len(imported_entry_ids),
        "imported_entry_ids": imported_entry_ids,
        "manifest_path": str(paths.imports_dir / "batches" / f"{import_id}.json"),
        "force": force,
        "dry_run": dry_run,
        "derived": derived,
    }
    _append_work_trace_best_effort(
        paths,
        {
            "event_type": "action",
            "summary": f"Refreshed derived layers for import {import_id}.",
            "project": "agent-diary",
            "source_surface": "agent-diary",
            "details": {
                "import_id": import_id,
                "dry_run": dry_run,
                "force": force,
                "imported_entry_count": len(imported_entry_ids),
                "derived": derived,
            },
            "related_entry_ids": imported_entry_ids,
            "related_paths": [str(paths.imports_dir / "batches" / f"{import_id}.json")],
            "tags": ["auto", "action", "derived_refresh"],
        },
    )
    return result


def list_imports(paths: Paths, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    limit = int(payload.get("limit", 20))
    manifests = list_import_batch_manifests(paths, limit=limit)
    items: list[dict[str, Any]] = []
    for manifest in manifests:
        items.append(
            {
                "import_id": manifest.get("import_id"),
                "imported_at": manifest.get("imported_at"),
                "imported_count": manifest.get("imported_count"),
                "skipped_duplicate_count": manifest.get("skipped_count"),
                "source_session_id": manifest.get("source_session_id"),
                "source_conversation_id": manifest.get("source_conversation_id"),
                "batch_manifest_path": manifest.get("manifest_path"),
                "manifest_file": manifest.get("manifest_file"),
                "dry_run": manifest.get("dry_run"),
                "audit": manifest.get("audit"),
            }
        )
    return {"limit": limit, "count": len(items), "items": items}


def _is_compressed_memory_artifact(artifact_type: str) -> bool:
    normalized = artifact_type.strip().lower().replace("_", "-")
    return normalized in {"memory", "compressed-memory"}


def _build_snippet(text: str, query: str, size: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= size:
        return compact
    terms = [t for t in re.findall(r"\w+", query.lower()) if t]
    lowered = compact.lower()
    pivot = -1
    for term in terms:
        idx = lowered.find(term)
        if idx != -1 and (pivot == -1 or idx < pivot):
            pivot = idx
    if pivot == -1:
        return compact[: size - 3] + "..."
    start = max(0, pivot - (size // 3))
    end = min(len(compact), start + size)
    snippet = compact[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(compact):
        snippet = snippet + "..."
    return snippet


def _build_preview(text: str, size: int = 140) -> str:
    compact = " ".join(text.split())
    if len(compact) <= size:
        return compact
    return compact[: size - 3] + "..."


def _query_terms(query: str) -> list[str]:
    return [t for t in re.findall(r"\w+", query.lower()) if t]


def _parse_date_only_query(query: str) -> str | None:
    """Return YYYY-MM-DD for a date-only human query, or None.

    Text search treats punctuation as token separators, so a query like
    ``7/19/26`` becomes loose tokens (7, 19, 26) and matches unrelated logs.
    Date-looking queries should instead mean "entries from that date".
    """
    text = query.strip()
    if not text:
        return None

    iso_match = re.fullmatch(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if iso_match:
        year, month, day = (int(part) for part in iso_match.groups())
    else:
        slash_match = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", text)
        if not slash_match:
            return None
        month, day, raw_year = slash_match.groups()
        month = int(month)
        day = int(day)
        year = int(raw_year)
        if year < 100:
            year += 2000

    try:
        return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
    except ValueError:
        return None


def _score_match_text(text: str, query: str) -> dict[str, int]:
    lowered = text.lower()
    terms = _query_terms(query)
    if not terms:
        return {"phrase_bonus": 0, "coverage": 0, "frequency": 0, "score": 0}
    phrase_bonus = 100 if query.lower() in lowered else 0
    coverage = sum(1 for term in terms if term in lowered)
    frequency = sum(lowered.count(term) for term in terms)
    return {
        "phrase_bonus": phrase_bonus,
        "coverage": coverage,
        "frequency": frequency,
        "score": phrase_bonus + (coverage * 10) + frequency,
    }


def _normalize_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_provenance_filters(payload: dict[str, Any]) -> dict[str, Any]:
    raw_filters = payload.get("filters")
    filters = raw_filters if isinstance(raw_filters, dict) else {}
    return {
        "source_conversation_id": _normalize_optional_str(
            filters.get("source_conversation_id", payload.get("source_conversation_id"))
        ),
        "source_session_id": _normalize_optional_str(
            filters.get("source_session_id", payload.get("source_session_id"))
        ),
        "import_id": _normalize_optional_str(filters.get("import_id", payload.get("import_id"))),
        "truthful_only": bool(filters.get("truthful_only", payload.get("truthful_only", False))),
    }


def _resolve_producer_entry_ids(
    paths: Paths,
    *,
    payload: dict[str, Any],
    limit: int,
) -> tuple[list[str] | None, dict[str, Any]]:
    entry_ids = payload.get("entry_ids")
    if entry_ids:
        if not isinstance(entry_ids, list):
            raise ValueError("entry_ids must be a list when provided")
        normalized = [str(e).strip() for e in entry_ids if str(e).strip()]
        return normalized, {"selection_mode": "entry_ids", "filters": None}

    filters = _resolve_provenance_filters(payload)
    if not any([filters["source_conversation_id"], filters["source_session_id"], filters["import_id"], filters["truthful_only"]]):
        return None, {"selection_mode": "unscoped", "filters": filters}

    scoped = list_entries(
        paths,
        {
            "limit": limit,
            "offset": 0,
            "filters": {
                key: value
                for key, value in {
                    "source_conversation_id": filters["source_conversation_id"],
                    "source_session_id": filters["source_session_id"],
                    "import_id": filters["import_id"],
                    "truthful_only": filters["truthful_only"],
                }.items()
                if value
            },
        },
    )
    scoped_entry_ids = [str(item.get("entry_id", "")).strip() for item in scoped.get("items", []) if str(item.get("entry_id", "")).strip()]
    return scoped_entry_ids, {"selection_mode": "provenance_scope", "filters": filters}


def _canonical_source_entry_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(entry_id).strip() for entry_id in value if str(entry_id).strip()})


def _artifact_scope(artifact_body: dict[str, Any]) -> dict[str, Any]:
    artifact_type = str(artifact_body.get("artifact_type", "")).strip()
    metadata = artifact_body.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if artifact_type == "analysis:open-loop":
        source_entry_ids = _canonical_source_entry_ids(metadata.get("source_entry_ids"))
        if not source_entry_ids:
            anchor_id = str(artifact_body.get("entry_id", "")).strip()
            if anchor_id:
                source_entry_ids = [anchor_id]
        return {
            "artifact_type": artifact_type,
            "scope_type": "lineage_source_entry_ids",
            "source_entry_ids": source_entry_ids,
        }
    source_entry_id = str(metadata.get("source_entry_id", "")).strip()
    if source_entry_id:
        return {
            "artifact_type": artifact_type,
            "scope_type": "source_entry_id",
            "source_entry_id": source_entry_id,
        }
    return {
        "artifact_type": artifact_type,
        "scope_type": "entry_id",
        "entry_id": str(artifact_body.get("entry_id", "")).strip(),
    }


def _artifact_generation_key(artifact_body: dict[str, Any]) -> str:
    return json.dumps(_artifact_scope(artifact_body), sort_keys=True, separators=(",", ":"))


def _artifact_lifecycle_status(artifact_body: dict[str, Any]) -> str:
    metadata = artifact_body.get("metadata")
    if not isinstance(metadata, dict):
        return "active"
    status = str(metadata.get("lifecycle_status", "")).strip().lower()
    if status in {"active", "superseded"}:
        return status
    return "active"


def _is_artifact_active(artifact_body: dict[str, Any]) -> bool:
    return _artifact_lifecycle_status(artifact_body) == "active"


def _write_artifact_json(path: Path, body: dict[str, Any]) -> None:
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def _candidate_artifact_paths(paths: Paths, artifact_body: dict[str, Any]) -> list[Path]:
    scope = _artifact_scope(artifact_body)
    if scope.get("scope_type") == "lineage_source_entry_ids":
        return list(paths.artifacts_dir.glob("*/artifact_*.json"))
    entry_id = str(artifact_body.get("entry_id", "")).strip()
    if not entry_id:
        return []
    artifact_dir = paths.artifacts_dir / entry_id
    if not artifact_dir.exists():
        return []
    return list(artifact_dir.glob("artifact_*.json"))


def _mark_prior_artifacts_superseded(paths: Paths, new_artifact_body: dict[str, Any]) -> None:
    new_scope = _artifact_scope(new_artifact_body)
    new_artifact_id = str(new_artifact_body.get("artifact_id", "")).strip()
    superseded_at = str(new_artifact_body.get("created_at", "")).strip()
    for artifact_path in _candidate_artifact_paths(paths, new_artifact_body):
        try:
            existing = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(existing.get("artifact_id", "")).strip() == new_artifact_id:
            continue
        if _artifact_scope(existing) != new_scope:
            continue
        if not _is_artifact_active(existing):
            continue
        metadata = existing.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["lifecycle_status"] = "superseded"
        metadata["superseded_at"] = superseded_at
        metadata["superseded_by_artifact_id"] = new_artifact_id
        metadata.setdefault("generation_key", _artifact_generation_key(existing))
        existing["metadata"] = metadata
        _write_artifact_json(artifact_path, existing)


def normalize_derived_artifact_lifecycle(paths: Paths, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    dry_run = bool(payload.get("dry_run", False))
    entry_id_filter = str(payload.get("entry_id", "")).strip() or None
    artifact_type_filter = str(payload.get("artifact_type", "")).strip() or None

    all_records: list[dict[str, Any]] = []
    for artifact_path in paths.artifacts_dir.glob("*/artifact_*.json"):
        try:
            artifact_body = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        artifact_type = str(artifact_body.get("artifact_type", "")).strip()
        artifact_id = str(artifact_body.get("artifact_id", "")).strip()
        created_at = str(artifact_body.get("created_at", "")).strip()
        if not artifact_type or not artifact_id:
            continue
        all_records.append(
            {
                "path": artifact_path,
                "body": artifact_body,
                "scope_key": _artifact_generation_key(artifact_body),
                "sort_key": (created_at, artifact_id),
                "artifact_id": artifact_id,
            }
        )

    by_scope: dict[str, list[dict[str, Any]]] = {}
    for record in all_records:
        by_scope.setdefault(str(record["scope_key"]), []).append(record)

    def _record_matches_filter(record: dict[str, Any]) -> bool:
        body = record["body"]
        if entry_id_filter and str(body.get("entry_id", "")).strip() != entry_id_filter:
            return False
        if artifact_type_filter and str(body.get("artifact_type", "")).strip() != artifact_type_filter:
            return False
        return True

    touched_scopes = {
        scope_key for scope_key, records in by_scope.items() if any(_record_matches_filter(record) for record in records)
    }

    changes: list[dict[str, Any]] = []
    normalized_scope_count = 0
    for scope_key in sorted(touched_scopes):
        records = by_scope.get(scope_key, [])
        if not records:
            continue
        normalized_scope_count += 1
        sorted_records = sorted(records, key=lambda record: record["sort_key"], reverse=True)
        active = sorted_records[0]
        active_artifact_id = str(active["artifact_id"])
        active_created_at = str(active["sort_key"][0])
        for record in sorted_records:
            body = record["body"]
            metadata = body.get("metadata")
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            desired_metadata = dict(metadata)
            desired_metadata["generation_key"] = str(scope_key)
            if str(record["artifact_id"]) == active_artifact_id:
                desired_metadata["lifecycle_status"] = "active"
                desired_metadata.pop("superseded_at", None)
                desired_metadata.pop("superseded_by_artifact_id", None)
            else:
                desired_metadata["lifecycle_status"] = "superseded"
                desired_metadata["superseded_at"] = active_created_at
                desired_metadata["superseded_by_artifact_id"] = active_artifact_id
            if desired_metadata == metadata:
                continue
            changes.append(
                {
                    "artifact_id": str(record["artifact_id"]),
                    "artifact_file": str(record["path"]),
                    "scope_key": str(scope_key),
                    "from": {
                        "lifecycle_status": metadata.get("lifecycle_status"),
                        "superseded_at": metadata.get("superseded_at"),
                        "superseded_by_artifact_id": metadata.get("superseded_by_artifact_id"),
                        "generation_key": metadata.get("generation_key"),
                    },
                    "to": {
                        "lifecycle_status": desired_metadata.get("lifecycle_status"),
                        "superseded_at": desired_metadata.get("superseded_at"),
                        "superseded_by_artifact_id": desired_metadata.get("superseded_by_artifact_id"),
                        "generation_key": desired_metadata.get("generation_key"),
                    },
                }
            )
            if dry_run:
                continue
            body["metadata"] = desired_metadata
            _write_artifact_json(record["path"], body)

    return {
        "dry_run": dry_run,
        "filters": {
            "entry_id": entry_id_filter,
            "artifact_type": artifact_type_filter,
        },
        "scanned_artifact_count": len(all_records),
        "normalized_scope_count": normalized_scope_count,
        "changed_artifact_count": len(changes),
        "changes": changes,
    }


def _build_open_loop_participation(paths: Paths) -> dict[str, dict[str, Any]]:
    participation: dict[str, dict[str, Any]] = {}
    for artifact_file in paths.artifacts_dir.glob("*/artifact_*.json"):
        try:
            artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(artifact.get("artifact_type", "")).strip() != "analysis:open-loop":
            continue
        if not _is_artifact_active(artifact):
            continue
        metadata = artifact.get("metadata")
        source_entry_ids: list[str] = []
        if isinstance(metadata, dict):
            source_entry_ids = [str(e).strip() for e in metadata.get("source_entry_ids", []) if str(e).strip()]
        if not source_entry_ids:
            anchor_id = str(artifact.get("entry_id", "")).strip()
            if anchor_id:
                source_entry_ids = [anchor_id]
        loops: list[dict[str, Any]] = []
        try:
            raw_loops = json.loads(str(artifact.get("content", "{}"))).get("loops", [])
            if isinstance(raw_loops, list):
                loops = [loop for loop in raw_loops if isinstance(loop, dict)]
        except json.JSONDecodeError:
            loops = []
        representative_title = None
        if loops:
            candidate = str(loops[0].get("title", "")).strip()
            if candidate:
                representative_title = candidate
        created_at = str(artifact.get("created_at", "")).strip()
        artifact_id = str(artifact.get("artifact_id", "")).strip()
        key = (created_at, artifact_id)
        for entry_id in source_entry_ids:
            current = participation.get(entry_id)
            current_key = (
                str(current.get("latest_created_at", "")),
                str(current.get("latest_artifact_id", "")),
            ) if isinstance(current, dict) else ("", "")
            if current is None or key >= current_key:
                participation[entry_id] = {
                    "count": len(loops),
                    "latest_created_at": created_at,
                    "latest_artifact_id": artifact_id,
                    "representative_title": representative_title,
                    "last_seen_at": created_at or None,
                }
    return participation


def _search_raw_entries(paths: Paths, query: str, limit: int) -> list[dict[str, Any]]:
    return _search_raw_entries_in_rows(
        paths,
        query=query,
        limit=limit,
        rows=list_entry_rows(paths.sqlite_path, limit=max(limit * 10, 200), offset=0),
    )


def _search_entries_by_date(
    paths: Paths,
    *,
    iso_date: str,
    limit: int,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    candidate_rows = rows if rows is not None else list_entry_rows(paths.sqlite_path, limit=1000000, offset=0)
    matches: list[dict[str, Any]] = []
    for row in candidate_rows:
        created_at = str(row.get("created_at", ""))
        if not created_at.startswith(iso_date):
            continue
        body = _effective_entry_body_from_row(paths, row)
        content = str(body.get("content", ""))
        matches.append(
            {
                "entry_id": row["entry_id"],
                "artifact_id": None,
                "indexed_at": created_at,
                "match_text": _build_preview(content, size=240),
                "match_layer": "date_filter",
                "supporting_layers": ["raw_entry"],
                "entry_type": body.get("entry_type", "unknown"),
                "source": row["source"],
                "author_role": row["author_role"],
                "fetch_raw_entry": {"entry_id": row["entry_id"]},
            }
        )
    matches.sort(key=lambda item: str(item.get("indexed_at", "")), reverse=True)
    return matches[:limit]


def _search_raw_entries_in_rows(
    paths: Paths,
    *,
    query: str,
    limit: int,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return []

    scored: list[dict[str, Any]] = []
    for row in rows:
        body = _effective_entry_body_from_row(paths, row)
        content = str(body.get("content", ""))
        lowered = content.lower()
        if not any(term in lowered for term in terms):
            continue
        score = _score_match_text(content, query)
        scored.append(
            {
                "entry_id": row["entry_id"],
                "artifact_id": None,
                "indexed_at": row["created_at"],
                "match_text": content,
                "match_layer": "raw_entry_fallback",
                "entry_type": body.get("entry_type", "unknown"),
                "source": row["source"],
                "author_role": row["author_role"],
                "_score": score["score"],
            }
        )

    scored.sort(key=lambda item: (item["_score"], item["indexed_at"]), reverse=True)
    return [{k: v for k, v in item.items() if k != "_score"} for item in scored[:limit]]


def _rows_matching_provenance_scope(paths: Paths, filters: dict[str, Any]) -> list[dict[str, Any]]:
    return list_entry_rows(
        paths.sqlite_path,
        limit=1000000,
        offset=0,
        source_conversation_id=_normalize_optional_str(filters.get("source_conversation_id")),
        source_session_id=_normalize_optional_str(filters.get("source_session_id")),
        import_id=_normalize_optional_str(filters.get("import_id")),
        truthful_only=bool(filters.get("truthful_only", False)),
    )


def _search_compressed_entries_in_rows(
    paths: Paths,
    *,
    query: str,
    limit: int,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return []

    scored: list[dict[str, Any]] = []
    for row in rows:
        entry_id = str(row.get("entry_id", "")).strip()
        if not entry_id:
            continue
        artifact_dir = paths.artifacts_dir / entry_id
        if not artifact_dir.exists():
            continue
        for artifact_file in sorted(artifact_dir.glob("*.json")):
            artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
            if not _is_artifact_active(artifact):
                continue
            if not _is_compressed_memory_artifact(str(artifact.get("artifact_type", ""))):
                continue
            content = str(artifact.get("content", ""))
            lowered = content.lower()
            if not any(term in lowered for term in terms):
                continue
            score = _score_match_text(content, query)
            scored.append(
                {
                    "entry_id": entry_id,
                    "artifact_id": str(artifact.get("artifact_id", "")).strip() or artifact.get("artifact_id"),
                    "indexed_at": str(artifact.get("created_at", "")).strip() or row["created_at"],
                    "match_text": _build_snippet(content, query),
                    "match_layer": "compressed_memory",
                    "supporting_layers": ["compressed_memory"],
                    "fetch_raw_entry": {"entry_id": entry_id},
                    "_score": score["score"],
                }
            )

    scored.sort(key=lambda item: (item["_score"], item["indexed_at"]), reverse=True)
    return scored[:limit]


def _format_overlay_for_effective_content(overlay: dict[str, Any]) -> str:
    overlay_type = str(overlay.get("overlay_type", "")).strip() or "overlay"
    author = str(overlay.get("author", "")).strip() or "unknown"
    created_at = str(overlay.get("created_at", "")).strip() or "unknown-time"
    content = str(overlay.get("content", "")).strip()
    return f"- [{overlay_type}] by {author} at {created_at}: {content}"


def _compose_effective_content(raw_content: str, overlays: list[dict[str, Any]]) -> str:
    if not overlays:
        return raw_content
    overlay_lines = [_format_overlay_for_effective_content(o) for o in overlays if str(o.get("content", "")).strip()]
    if not overlay_lines:
        return raw_content
    return (
        f"{raw_content}\n\n"
        "Overlay layer (annotations/corrections; raw entry remains unchanged):\n"
        + "\n".join(overlay_lines)
    )


def _effective_entry_body_from_row(paths: Paths, row: dict[str, Any]) -> dict[str, Any]:
    raw_file = Path(str(row["raw_file_path"]))
    if not raw_file.exists():
        return {"content": ""}
    raw_body = json.loads(raw_file.read_text(encoding="utf-8"))
    entry_id = str(raw_body.get("entry_id") or row.get("entry_id") or "").strip()
    overlay_dir = paths.overlays_dir / entry_id if entry_id else None
    overlay_files = sorted(overlay_dir.glob("*.json")) if overlay_dir and overlay_dir.exists() else []
    overlays = [json.loads(path.read_text(encoding="utf-8")) for path in overlay_files]
    effective = dict(raw_body)
    effective["content"] = _compose_effective_content(str(raw_body.get("content", "")), overlays)
    effective["_overlay_count"] = len(overlays)
    return effective


def _parse_iso_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _artifact_generated_at(artifact: dict[str, Any]) -> str | None:
    metadata = artifact.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    generated_at = str(metadata.get("generated_at", "")).strip()
    if generated_at:
        return generated_at
    created_at = str(artifact.get("created_at", "")).strip()
    return created_at or None


def _overlay_staleness_for_artifact(
    artifact: dict[str, Any],
    latest_overlay_at: str | None,
    latest_overlay_dt: datetime | None,
) -> dict[str, Any]:
    artifact_generated_at = _artifact_generated_at(artifact)
    artifact_generated_dt = _parse_iso_timestamp(artifact_generated_at)
    stale = bool(
        latest_overlay_dt is not None
        and artifact_generated_dt is not None
        and latest_overlay_dt > artifact_generated_dt
    )
    result = {
        "overlay_stale": stale,
        "artifact_generated_at": artifact_generated_at,
        "latest_overlay_at": latest_overlay_at,
    }
    if stale:
        result["overlay_stale_reason"] = "overlay_added_after_artifact_generation"
    return result


def _artifact_provenance_summary(
    artifact_body: dict[str, Any],
    *,
    fallback_entry_id: str,
    lineage_source_entry_ids: list[str] | None = None,
) -> dict[str, Any]:
    metadata = artifact_body.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    source_entry_ids = [str(e).strip() for e in metadata.get("source_entry_ids", []) if str(e).strip()]
    source_entry_id = str(metadata.get("source_entry_id", "")).strip()
    if source_entry_id and source_entry_id not in source_entry_ids:
        source_entry_ids.append(source_entry_id)
    if not source_entry_ids and lineage_source_entry_ids:
        source_entry_ids = [str(e).strip() for e in lineage_source_entry_ids if str(e).strip()]
    if not source_entry_ids:
        source_entry_ids = [fallback_entry_id]
    return {
        "schema_version": str(metadata.get("schema_version", "")).strip() or None,
        "method": str(metadata.get("method", "")).strip() or None,
        "method_version": str(metadata.get("method_version", "")).strip() or None,
        "generated_at": str(metadata.get("generated_at", "")).strip() or None,
        "analysis_window": metadata.get("analysis_window") if isinstance(metadata.get("analysis_window"), dict) else None,
        "source_entry_ids": source_entry_ids,
    }


def _resolve_entry_provenance_from_body(body: dict[str, Any]) -> dict[str, Any]:
    metadata = body.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    ingestion = metadata.get("ingestion")
    ingestion = ingestion if isinstance(ingestion, dict) else {}
    return {
        "source_session_id": (
            str(ingestion.get("source_session_id", "")).strip()
            or str(metadata.get("source_session_id", "")).strip()
            or None
        ),
        "source_conversation_id": (
            str(ingestion.get("source_conversation_id", "")).strip()
            or str(metadata.get("source_conversation_id", "")).strip()
            or None
        ),
        "import_id": str(ingestion.get("import_id", "")).strip() or None,
        "truthful_source": bool(ingestion.get("truthful_source", False)),
    }


def _entry_matches_provenance_scope(provenance: dict[str, Any], filters: dict[str, Any]) -> bool:
    source_conversation_id = filters.get("source_conversation_id")
    source_session_id = filters.get("source_session_id")
    import_id = filters.get("import_id")
    truthful_only = bool(filters.get("truthful_only", False))
    if source_conversation_id and provenance.get("source_conversation_id") != source_conversation_id:
        return False
    if source_session_id and provenance.get("source_session_id") != source_session_id:
        return False
    if import_id and provenance.get("import_id") != import_id:
        return False
    if truthful_only and not bool(provenance.get("truthful_source", False)):
        return False
    return True


def attach_artifact(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(
        payload,
        ["entry_id", "artifact_type", "producer", "content"],
    )
    if get_entry_row(paths.sqlite_path, str(payload["entry_id"])) is None:
        raise FileNotFoundError(f"entry not found: {payload['entry_id']}")

    artifact = Artifact(**payload)
    artifact.metadata = dict(artifact.metadata) if isinstance(artifact.metadata, dict) else {}
    artifact.metadata["lifecycle_status"] = "active"
    artifact.metadata["generation_key"] = _artifact_generation_key(artifact.to_dict())
    _mark_prior_artifacts_superseded(paths, artifact.to_dict())
    artifact_path = append_artifact(paths, artifact)
    insert_artifact(paths.sqlite_path, artifact)
    indexed = False
    if _is_compressed_memory_artifact(artifact.artifact_type):
        insert_memory_index_row(
            paths.sqlite_path,
            entry_id=artifact.entry_id,
            artifact_id=artifact.artifact_id,
            created_at=artifact.created_at,
            memory_text=artifact.content,
        )
        indexed = True
    result = {
        "artifact_id": artifact.artifact_id,
        "artifact_file": str(artifact_path),
        "indexed_in_memory": indexed,
    }
    _append_work_trace_best_effort(
        paths,
        {
            "event_type": "file_change",
            "summary": f"Attached {artifact.artifact_type} artifact to entry {artifact.entry_id}.",
            "project": "agent-diary",
            "source_surface": "agent-diary",
            "details": {
                "change_kind": "create",
                "artifact_type": artifact.artifact_type,
                "producer": artifact.producer,
                "indexed_in_memory": indexed,
            },
            "related_entry_ids": [artifact.entry_id],
            "related_artifact_ids": [artifact.artifact_id],
            "related_paths": [str(artifact_path)],
            "tags": ["auto", "file_change", "artifact", artifact.artifact_type],
        },
    )
    return result


def search_memory(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    limit = int(payload.get("limit", 20))
    filters = _resolve_provenance_filters(payload)
    scoped_rows = _rows_matching_provenance_scope(paths, filters) if any(filters.values()) else []

    date_filter = _parse_date_only_query(query)
    if date_filter:
        date_matches = _search_entries_by_date(
            paths,
            iso_date=date_filter,
            limit=limit,
            rows=scoped_rows if scoped_rows else None,
        )
        return {
            "query": query,
            "limit": limit,
            "filters": {
                "source_conversation_id": filters.get("source_conversation_id"),
                "source_session_id": filters.get("source_session_id"),
                "import_id": filters.get("import_id"),
                "truthful_only": bool(filters.get("truthful_only", False)),
                "created_date": date_filter,
            },
            "matches": date_matches,
            "match_summary": {
                "compressed_memory_hits": 0,
                "raw_entry_hits": len(date_matches),
                "entry_matches": len(date_matches),
                "using_raw_layer": bool(date_matches),
                "date_filter": date_filter,
            },
            "note": "Date-only search detected; returning entries whose created_at date matches the requested day.",
        }

    compressed_matches = search_index(paths.sqlite_path, query=query, limit=max(limit * 10, 200))
    linked_matches: list[dict[str, Any]] = []
    for row in compressed_matches:
        entry_id = str(row["entry_id"])
        entry_row = get_entry_row(paths.sqlite_path, entry_id)
        if entry_row is None:
            continue
        raw_file = Path(str(entry_row["raw_file_path"]))
        if not raw_file.exists():
            continue
        body = json.loads(raw_file.read_text(encoding="utf-8"))
        provenance = _resolve_entry_provenance_from_body(body)
        if not _entry_matches_provenance_scope(provenance, filters):
            continue
        linked_matches.append(
            {
                "entry_id": row["entry_id"],
                "artifact_id": row["artifact_id"],
                "indexed_at": row["indexed_at"],
                "match_text": _build_snippet(str(row["match_text"]), query),
                "match_layer": "compressed_memory",
                "supporting_layers": ["compressed_memory"],
                "fetch_raw_entry": {"entry_id": row["entry_id"]},
                "_score": _score_match_text(str(row["match_text"]), query)["score"],
            }
        )
        if len(linked_matches) >= max(limit * 10, 200):
            break
    if scoped_rows:
        scoped_entry_ids = {str(row.get("entry_id", "")).strip() for row in scoped_rows if str(row.get("entry_id", "")).strip()}
        seen_pairs = {
            (str(match.get("entry_id", "")).strip(), str(match.get("artifact_id", "")).strip())
            for match in linked_matches
        }
        for match in _search_compressed_entries_in_rows(paths, query=query, limit=max(limit * 10, 200), rows=scoped_rows):
            pair = (str(match.get("entry_id", "")).strip(), str(match.get("artifact_id", "")).strip())
            if str(match.get("entry_id", "")).strip() not in scoped_entry_ids:
                continue
            if pair in seen_pairs:
                continue
            linked_matches.append(match)
            seen_pairs.add(pair)
    raw_matches: list[dict[str, Any]] = []
    raw_hits = (
        _search_raw_entries_in_rows(paths, query=query, limit=max(limit * 10, 200), rows=scoped_rows)
        if scoped_rows
        else _search_raw_entries(paths, query=query, limit=max(limit * 10, 200))
    )
    for row in raw_hits:
        entry_id = str(row["entry_id"])
        entry_row = get_entry_row(paths.sqlite_path, entry_id)
        if entry_row is None:
            continue
        raw_file = Path(str(entry_row["raw_file_path"]))
        if not raw_file.exists():
            continue
        body = json.loads(raw_file.read_text(encoding="utf-8"))
        provenance = _resolve_entry_provenance_from_body(body)
        if not _entry_matches_provenance_scope(provenance, filters):
            continue
        raw_matches.append(
            {
                "entry_id": row["entry_id"],
                "artifact_id": row["artifact_id"],
                "indexed_at": row["indexed_at"],
                "match_text": _build_snippet(str(row["match_text"]), query),
                "match_layer": row["match_layer"],
                "supporting_layers": ["raw_entry"],
                "entry_type": row["entry_type"],
                "source": row["source"],
                "author_role": row["author_role"],
                "fetch_raw_entry": {"entry_id": row["entry_id"]},
                "_score": _score_match_text(str(row["match_text"]), query)["score"] + 2,
            }
        )
        if len(raw_matches) >= max(limit * 10, 200):
            break

    combined_by_entry: dict[str, dict[str, Any]] = {}
    compressed_count = 0
    raw_count = 0
    for match in linked_matches + raw_matches:
        entry_id = str(match["entry_id"])
        existing = combined_by_entry.get(entry_id)
        if "compressed_memory" in match.get("supporting_layers", []):
            compressed_count += 1
        if "raw_entry" in match.get("supporting_layers", []):
            raw_count += 1
        if existing is None:
            combined_by_entry[entry_id] = dict(match)
            continue

        merged_layers = sorted({*existing.get("supporting_layers", []), *match.get("supporting_layers", [])})
        replacement = dict(existing)
        prefer_match = False
        if int(match.get("_score", 0)) > int(existing.get("_score", 0)):
            prefer_match = True
        elif int(match.get("_score", 0)) == int(existing.get("_score", 0)) and match.get("match_layer") != "compressed_memory":
            prefer_match = True

        if prefer_match:
            replacement = dict(match)
            for carry_key in ("entry_type", "source", "author_role"):
                if carry_key not in replacement and carry_key in existing:
                    replacement[carry_key] = existing[carry_key]
        else:
            for carry_key in ("entry_type", "source", "author_role"):
                if carry_key in match and carry_key not in replacement:
                    replacement[carry_key] = match[carry_key]
        replacement["supporting_layers"] = merged_layers
        combined_by_entry[entry_id] = replacement

    ranked_matches = sorted(
        combined_by_entry.values(),
        key=lambda item: (int(item.get("_score", 0)), str(item.get("indexed_at", ""))),
        reverse=True,
    )
    matches = [
        {k: v for k, v in item.items() if k != "_score"}
        for item in ranked_matches[:limit]
    ]
    return {
        "query": query,
        "limit": limit,
        "filters": {
            "source_conversation_id": filters.get("source_conversation_id"),
            "source_session_id": filters.get("source_session_id"),
            "import_id": filters.get("import_id"),
            "truthful_only": bool(filters.get("truthful_only", False)),
        },
        "matches": matches,
        "match_summary": {
            "compressed_memory_hits": compressed_count,
            "raw_entry_hits": raw_count,
            "entry_matches": len(matches),
            "using_raw_layer": bool(raw_count),
        },
        "note": "Search merges derived compressed-memory hits with authoritative raw-entry matches and ranks them per entry.",
    }


def fetch_raw_entry(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    entry_id = str(payload["entry_id"])
    include_overlays = bool(payload.get("include_overlays", False))
    include_artifacts = bool(payload.get("include_artifacts", False))
    return fetch_entry_from_files(
        paths,
        entry_id=entry_id,
        include_overlays=include_overlays,
        include_artifacts=include_artifacts,
    )


def list_entries(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    limit = int(payload.get("limit", 20))
    offset = int(payload.get("offset", 0))
    only_with_open_loops = bool(payload.get("only_with_open_loops", False))
    filters = _resolve_provenance_filters(payload)
    source_conversation_id = filters["source_conversation_id"]
    source_session_id = filters["source_session_id"]
    import_id = filters["import_id"]
    truthful_only = bool(filters["truthful_only"])
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    row_offset = offset
    total: int
    rows: list[dict[str, Any]]
    open_loop_participation = _build_open_loop_participation(paths)
    needs_provenance_filter = any([source_conversation_id, source_session_id, import_id, truthful_only])
    if only_with_open_loops or needs_provenance_filter:
        all_rows = list_entry_rows(
            paths.sqlite_path,
            limit=1000000,
            offset=0,
            source_conversation_id=source_conversation_id,
            source_session_id=source_session_id,
            import_id=import_id,
            truthful_only=truthful_only,
        )
        filtered_rows: list[dict[str, Any]] = []
        for row in all_rows:
            loop_info = open_loop_participation.get(str(row["entry_id"]))
            if only_with_open_loops and not (loop_info and int(loop_info.get("count", 0)) > 0):
                continue
            filtered_rows.append(row)

        if only_with_open_loops:
            filtered_rows.sort(
                key=lambda r: (
                    str(open_loop_participation.get(str(r["entry_id"]), {}).get("last_seen_at", "")),
                    str(r.get("created_at", "")),
                    str(r.get("entry_id", "")),
                ),
                reverse=True,
            )
        total = len(filtered_rows)
        rows = filtered_rows[offset : offset + limit]
        row_offset = offset
    else:
        rows = list_entry_rows(paths.sqlite_path, limit=limit, offset=offset)
        total = count_entry_rows(paths.sqlite_path)
    items: list[dict[str, Any]] = []
    for row in rows:
        raw_file = Path(row["raw_file_path"])
        body = json.loads(raw_file.read_text(encoding="utf-8"))
        provenance = _resolve_entry_provenance_from_body(body)
        brief = None
        latest_brief_key: tuple[str, str] | None = None
        artifact_dir = paths.artifacts_dir / row["entry_id"]
        if artifact_dir.exists():
            for artifact_file in sorted(artifact_dir.glob("*.json")):
                artifact_body = json.loads(artifact_file.read_text(encoding="utf-8"))
                if artifact_body.get("artifact_type") == "conversation-brief":
                    if not _is_artifact_active(artifact_body):
                        continue
                    key = (
                        str(artifact_body.get("created_at", "")).strip(),
                        str(artifact_body.get("artifact_id", "")).strip(),
                    )
                    if latest_brief_key is None or key > latest_brief_key:
                        latest_brief_key = key
                        brief = str(artifact_body.get("content", "")).strip() or None
        item = {
            "entry_id": row["entry_id"],
            "created_at": row["created_at"],
            "entry_type": body.get("entry_type", "unknown"),
            "source": row["source"],
            "author_role": row["author_role"],
            "brief": brief,
            "preview": _build_preview(str(body.get("content", ""))),
            "provenance": provenance,
        }
        loop_info = open_loop_participation.get(str(row["entry_id"]))
        if loop_info and int(loop_info.get("count", 0)) > 0:
            item["open_loop"] = {
                "has_open_loops": True,
                "count": int(loop_info.get("count", 0)),
                "representative_title": str(loop_info.get("representative_title") or "").strip() or None,
                "last_seen_at": str(loop_info.get("last_seen_at") or "").strip() or None,
            }
        items.append(item)

    # Batch query work trace counts for all returned items
    try:
        from agent_diary.index.repository import list_work_trace_counts_for_entries as _wt_counts
        entry_ids = [item["entry_id"] for item in items]
        wt_map = _wt_counts(paths.sqlite_path, entry_ids)
        for item in items:
            item["work_trace_count"] = wt_map.get(item["entry_id"], 0)
    except Exception:
        pass  # non-critical; timeline will show no badge

    return {
        "limit": limit,
        "offset": row_offset,
        "total": total,
        "filters": {
            "source_conversation_id": source_conversation_id,
            "source_session_id": source_session_id,
            "import_id": import_id,
            "truthful_only": truthful_only,
            "only_with_open_loops": only_with_open_loops,
        },
        "items": items,
    }


def fetch_entry_detail(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    _require_fields(payload, ["entry_id"])
    fetched = fetch_entry_from_files(
        paths,
        entry_id=str(payload["entry_id"]),
        include_overlays=True,
        include_artifacts=True,
    )
    entry = fetched["entry"]
    overlays = []
    for o in fetched.get("overlays", []):
        overlays.append(
            {
                "overlay_id": str(o.get("overlay_id", "")).strip() or o.get("overlay_id"),
                "overlay_type": str(o.get("overlay_type", "")).strip() or o.get("overlay_type"),
                "author": str(o.get("author", "")).strip() or o.get("author"),
                "content": o.get("content", ""),
                "created_at": o.get("created_at"),
                "metadata": o.get("metadata", {}),
            }
        )
    overlays.sort(key=lambda o: (str(o.get("created_at", "")), str(o.get("overlay_id", ""))), reverse=True)
    latest_overlay_at = str(overlays[0].get("created_at", "")).strip() if overlays else None
    latest_overlay_dt = _parse_iso_timestamp(latest_overlay_at)
    artifacts = []
    seen_artifact_ids: set[str] = set()
    for a in fetched.get("artifacts", []):
        artifact_id = str(a.get("artifact_id", "")).strip()
        if artifact_id:
            seen_artifact_ids.add(artifact_id)
        artifact = {
            "artifact_id": artifact_id or a.get("artifact_id"),
            "artifact_type": a.get("artifact_type"),
            "producer": a.get("producer"),
            "created_at": a.get("created_at"),
            "lifecycle_status": _artifact_lifecycle_status(a),
            "provenance": _artifact_provenance_summary(a, fallback_entry_id=str(entry["entry_id"])),
        }
        if a.get("artifact_type") in {"memory", "compressed-memory", "conversation-brief"}:
            artifact["content"] = a.get("content", "")
        if a.get("artifact_type") == "analysis:open-loop":
            metadata = a.get("metadata") if isinstance(a.get("metadata"), dict) else {}
            artifact["lineage"] = {
                "link_mode": "direct",
                "anchor_entry_id": str(a.get("entry_id", entry["entry_id"])),
                "source_entry_ids": [str(e) for e in metadata.get("source_entry_ids", []) if str(e).strip()],
            }
            try:
                artifact["open_loops"] = json.loads(str(a.get("content", "{}"))).get("loops", [])
            except json.JSONDecodeError:
                artifact["open_loops"] = []
        artifact.update(_overlay_staleness_for_artifact(a, latest_overlay_at, latest_overlay_dt))
        artifacts.append(artifact)

    for linked in _find_linked_open_loop_artifacts(paths, entry_id=str(entry["entry_id"]), exclude_artifact_ids=seen_artifact_ids):
        linked.update(_overlay_staleness_for_artifact(linked, latest_overlay_at, latest_overlay_dt))
        artifacts.append(linked)

    artifacts.sort(key=lambda a: (str(a.get("created_at", "")), str(a.get("artifact_id", ""))), reverse=True)
    latest_by_type: dict[str, tuple[str, str]] = {}
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type", "")).strip()
        if not artifact_type:
            continue
        if str(artifact.get("lifecycle_status", "active")).strip() == "superseded":
            continue
        key = (str(artifact.get("created_at", "")), str(artifact.get("artifact_id", "")))
        current = latest_by_type.get(artifact_type)
        if current is None or key > current:
            latest_by_type[artifact_type] = key
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type", "")).strip()
        if not artifact_type:
            continue
        if str(artifact.get("lifecycle_status", "active")).strip() == "superseded":
            artifact["is_current"] = False
            continue
        key = (str(artifact.get("created_at", "")), str(artifact.get("artifact_id", "")))
        selected = latest_by_type.get(artifact_type)
        artifact["is_current"] = key == selected if selected is not None else True

    artifact_types = sorted(
        {
            str(artifact.get("artifact_type", "")).strip()
            for artifact in artifacts
            if str(artifact.get("artifact_type", "")).strip()
        }
    )
    current_by_type: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type", "")).strip()
        if not artifact_type or not artifact.get("is_current"):
            continue
        current_by_type[artifact_type] = {
            "artifact_id": artifact.get("artifact_id"),
            "created_at": artifact.get("created_at"),
            "producer": artifact.get("producer"),
            "overlay_stale": bool(artifact.get("overlay_stale", False)),
            "lifecycle_status": artifact.get("lifecycle_status"),
        }

    work_trace_events = _find_work_trace_events_for_entry(paths, entry_id=str(entry["entry_id"]))

    return {
        "entry_id": entry["entry_id"],
        "raw_entry": entry,
        "entry_provenance": _resolve_entry_provenance_from_body(entry),
        "overlays": overlays,
        "artifacts": artifacts,
        "work_trace": {
            "events": work_trace_events,
            "summary": {
                "total_count": len(work_trace_events),
                "event_types": sorted(
                    {
                        str(event.get("event_type", "")).strip()
                        for event in work_trace_events
                        if str(event.get("event_type", "")).strip()
                    }
                ),
            },
        },
        "artifact_summary": {
            "total_count": len(artifacts),
            "current_count": sum(1 for artifact in artifacts if artifact.get("is_current")),
            "stale_count": sum(1 for artifact in artifacts if artifact.get("overlay_stale")),
            "artifact_types": artifact_types,
            "current_by_type": current_by_type,
        },
        "truth_model": {
            "primary": "raw_entry",
            "secondary": "artifacts",
            "overlay_layer": "overlays",
        },
    }


def _find_linked_open_loop_artifacts(paths: Paths, *, entry_id: str, exclude_artifact_ids: set[str]) -> list[dict[str, Any]]:
    linked: list[dict[str, Any]] = []
    for artifact_file in paths.artifacts_dir.glob("*/artifact_*.json"):
        try:
            body = json.loads(artifact_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(body.get("artifact_type", "")).strip() != "analysis:open-loop":
            continue
        artifact_id = str(body.get("artifact_id", "")).strip()
        if not artifact_id or artifact_id in exclude_artifact_ids:
            continue
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            continue
        source_entry_ids = [str(e).strip() for e in metadata.get("source_entry_ids", []) if str(e).strip()]
        if entry_id not in source_entry_ids:
            continue
        try:
            open_loops = json.loads(str(body.get("content", "{}"))).get("loops", [])
        except json.JSONDecodeError:
            open_loops = []
        linked.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": "analysis:open-loop",
                "producer": body.get("producer"),
                "created_at": body.get("created_at"),
                "open_loops": open_loops,
                "provenance": _artifact_provenance_summary(
                    body,
                    fallback_entry_id=entry_id,
                    lineage_source_entry_ids=source_entry_ids,
                ),
                "lineage": {
                    "link_mode": "lineage",
                    "anchor_entry_id": str(body.get("entry_id", "")),
                    "source_entry_ids": source_entry_ids,
                },
                "lifecycle_status": _artifact_lifecycle_status(body),
            }
        )
    linked.sort(key=lambda a: (str(a.get("created_at", "")), str(a.get("artifact_id", ""))), reverse=True)
    return linked


def _find_work_trace_events_for_entry(paths: Paths, *, entry_id: str) -> list[dict[str, Any]]:
    import json as _json
    from agent_diary.index.repository import list_work_trace_events_for_entry as _list_wt

    # Primary path: indexed SQLite lookup (fast)
    indexed_rows = _list_wt(paths.sqlite_path, entry_id)
    if indexed_rows:
        events: list[dict[str, Any]] = []
        for row in indexed_rows:
            wf = Path(row["work_file_path"])
            try:
                body = _json.loads(wf.read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                body = {}
            details = body.get("details")
            events.append(
                {
                    "event_id": body.get("event_id") or row["event_id"],
                    "event_type": body.get("event_type") or row["event_type"],
                    "summary": body.get("summary") or row["summary"],
                    "created_at": body.get("created_at") or row["created_at"],
                    "project": body.get("project"),
                    "source_surface": body.get("source_surface"),
                    "actor": body.get("actor"),
                    "session_key": body.get("session_key"),
                    "task_id": body.get("task_id"),
                    "details": details if isinstance(details, dict) else {},
                    "related_entry_ids": [str(item).strip() for item in body.get("related_entry_ids", []) if str(item).strip()],
                    "related_artifact_ids": [str(item).strip() for item in body.get("related_artifact_ids", []) if str(item).strip()],
                    "related_paths": [str(item).strip() for item in body.get("related_paths", []) if str(item).strip()],
                    "tags": [str(item).strip() for item in body.get("tags", []) if str(item).strip()],
                    "work_file_path": str(wf),
                }
            )
        events.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("event_id", ""))), reverse=True)
        return events

    # Fallback: full filesystem scan (for legacy data before migration)
    events = []
    for work_file in paths.work_trace_dir.rglob("*.json"):
        try:
            body = _json.loads(work_file.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            continue
        related_entry_ids = [str(item).strip() for item in body.get("related_entry_ids", []) if str(item).strip()]
        if entry_id not in related_entry_ids:
            continue
        details = body.get("details")
        events.append(
            {
                "event_id": body.get("event_id"),
                "event_type": body.get("event_type"),
                "summary": body.get("summary"),
                "created_at": body.get("created_at"),
                "project": body.get("project"),
                "source_surface": body.get("source_surface"),
                "actor": body.get("actor"),
                "session_key": body.get("session_key"),
                "task_id": body.get("task_id"),
                "details": details if isinstance(details, dict) else {},
                "related_entry_ids": related_entry_ids,
                "related_artifact_ids": [str(item).strip() for item in body.get("related_artifact_ids", []) if str(item).strip()],
                "related_paths": [str(item).strip() for item in body.get("related_paths", []) if str(item).strip()],
                "tags": [str(item).strip() for item in body.get("tags", []) if str(item).strip()],
                "work_file_path": str(work_file),
            }
        )
    events.sort(key=lambda item: (str(item.get("created_at", "")), str(item.get("event_id", ""))), reverse=True)
    return events


def produce_open_loops(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    limit = int(payload.get("limit", 20))
    if limit < 1:
        raise ValueError("limit must be >= 1")
    normalized_entry_ids, selection = _resolve_producer_entry_ids(paths, payload=payload, limit=limit)

    source_rows = collect_source_rows(paths, limit=limit, entry_ids=normalized_entry_ids)
    if not source_rows:
        raise FileNotFoundError("no source entries found for open-loop analysis")

    source_entry_ids = [str(r["entry_id"]) for r in source_rows]
    source_rows_with_effective = [
        {
            **row,
            "_effective_body": _effective_entry_body_from_row(paths, row),
        }
        for row in source_rows
    ]
    payload_content = build_open_loops_payload(source_entries=source_rows_with_effective)
    newest_row = max(source_rows, key=lambda r: (r["created_at"], r["entry_id"]))
    generated_at = datetime.now(timezone.utc).isoformat()

    artifact_payload = {
        "entry_id": str(newest_row["entry_id"]),
        "artifact_type": "analysis:open-loop",
        "producer": "open-loop.v1",
        "content": json.dumps(payload_content, sort_keys=True),
        "created_at": generated_at,
        "metadata": {
            "schema_version": "open-loop.v1",
            "source_entry_ids": source_entry_ids,
            "analysis_window": {
                "start": min(r["created_at"] for r in source_rows),
                "end": max(r["created_at"] for r in source_rows),
            },
            "method": "keyword-window-v1",
            "method_version": "1",
            "generated_at": generated_at,
        },
    }
    attached = attach_artifact(paths, artifact_payload)
    result = {
        "artifact_id": attached["artifact_id"],
        "artifact_file": attached["artifact_file"],
        "loop_count": len(payload_content["loops"]),
        "source_entry_ids": source_entry_ids,
        "selection_mode": selection["selection_mode"],
        "filters": selection["filters"],
    }
    _append_work_trace_best_effort(
        paths,
        {
            "event_type": "action",
            "summary": f"Produced open-loop analysis for {len(source_entry_ids)} source entries.",
            "project": "agent-diary",
            "source_surface": "agent-diary",
            "details": {
                "artifact_id": result["artifact_id"],
                "loop_count": result["loop_count"],
                "selection_mode": result["selection_mode"],
                "filters": result["filters"],
            },
            "related_entry_ids": source_entry_ids,
            "related_artifact_ids": [result["artifact_id"]],
            "related_paths": [result["artifact_file"]],
            "tags": ["auto", "action", "producer", "open_loops"],
        },
    )
    return result


def produce_conversation_briefs(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    limit = int(payload.get("limit", 20))
    if limit < 1:
        raise ValueError("limit must be >= 1")
    normalized_entry_ids, selection = _resolve_producer_entry_ids(paths, payload=payload, limit=limit)
    force = bool(payload.get("force", False))

    source_rows = collect_source_rows(paths, limit=limit, entry_ids=normalized_entry_ids)
    if not source_rows:
        raise FileNotFoundError("no source entries found for conversation-brief analysis")

    produced: list[dict[str, Any]] = []
    skipped: list[str] = []
    for row in source_rows:
        entry_id = str(row["entry_id"])
        if not force and entry_has_active_artifact_type(paths, entry_id=entry_id, artifact_type="conversation-brief"):
            skipped.append(entry_id)
            continue
        body = _effective_entry_body_from_row(paths, row)
        brief = build_conversation_brief_text(body)
        attached = attach_artifact(
            paths,
            {
                "entry_id": entry_id,
                "artifact_type": "conversation-brief",
                "producer": "conversation-brief.v1",
                "content": brief,
                "metadata": {
                    "schema_version": "conversation-brief.v1",
                    "method": "deterministic-dialogue-brief-v1",
                    "source_entry_id": entry_id,
                },
            },
        )
        produced.append(
            {
                "entry_id": entry_id,
                "artifact_id": attached["artifact_id"],
                "artifact_file": attached["artifact_file"],
                "brief": brief,
            }
        )
    result = {
        "produced_count": len(produced),
        "skipped_count": len(skipped),
        "produced": produced,
        "skipped": skipped,
        "selection_mode": selection["selection_mode"],
        "filters": selection["filters"],
    }
    if produced or skipped:
        _append_work_trace_best_effort(
            paths,
            {
                "event_type": "action",
                "summary": f"Produced conversation briefs for {len(produced)} entries and skipped {len(skipped)}.",
                "project": "agent-diary",
                "source_surface": "agent-diary",
                "details": {
                    "produced_count": len(produced),
                    "skipped_count": len(skipped),
                    "selection_mode": selection["selection_mode"],
                    "filters": selection["filters"],
                },
                "related_entry_ids": [str(item["entry_id"]) for item in produced] + skipped,
                "related_artifact_ids": [str(item["artifact_id"]) for item in produced],
                "related_paths": [str(item["artifact_file"]) for item in produced],
                "tags": ["auto", "action", "producer", "conversation_brief"],
            },
        )
    return result


def produce_compressed_memory(paths: Paths, payload: dict[str, Any]) -> dict[str, Any]:
    limit = int(payload.get("limit", 20))
    if limit < 1:
        raise ValueError("limit must be >= 1")
    normalized_entry_ids, selection = _resolve_producer_entry_ids(paths, payload=payload, limit=limit)
    force = bool(payload.get("force", False))

    source_rows = collect_source_rows(paths, limit=limit, entry_ids=normalized_entry_ids)
    if not source_rows:
        raise FileNotFoundError("no source entries found for compressed-memory analysis")

    produced: list[dict[str, Any]] = []
    skipped: list[str] = []
    for row in source_rows:
        entry_id = str(row["entry_id"])
        if not force and entry_has_active_artifact_type(paths, entry_id=entry_id, artifact_type="compressed-memory"):
            skipped.append(entry_id)
            continue
        body = _effective_entry_body_from_row(paths, row)
        memory_text = build_compressed_memory_text(body)
        attached = attach_artifact(
            paths,
            {
                "entry_id": entry_id,
                "artifact_type": "compressed-memory",
                "producer": "compressed-memory.v2",
                "content": memory_text,
                "metadata": {
                    "schema_version": "compressed-memory.v2",
                    "method": "deterministic-dialogue-compression-v2",
                    "source_entry_id": entry_id,
                },
            },
        )
        produced.append(
            {
                "entry_id": entry_id,
                "artifact_id": attached["artifact_id"],
                "artifact_file": attached["artifact_file"],
                "indexed_in_memory": attached["indexed_in_memory"],
            }
        )
    result = {
        "produced_count": len(produced),
        "skipped_count": len(skipped),
        "produced": produced,
        "skipped": skipped,
        "selection_mode": selection["selection_mode"],
        "filters": selection["filters"],
    }
    if produced or skipped:
        _append_work_trace_best_effort(
            paths,
            {
                "event_type": "action",
                "summary": f"Produced compressed memory for {len(produced)} entries and skipped {len(skipped)}.",
                "project": "agent-diary",
                "source_surface": "agent-diary",
                "details": {
                    "produced_count": len(produced),
                    "skipped_count": len(skipped),
                    "selection_mode": selection["selection_mode"],
                    "filters": selection["filters"],
                },
                "related_entry_ids": [str(item["entry_id"]) for item in produced] + skipped,
                "related_artifact_ids": [str(item["artifact_id"]) for item in produced],
                "related_paths": [str(item["artifact_file"]) for item in produced],
                "tags": ["auto", "action", "producer", "compressed_memory"],
            },
        )
    return result


def status(paths: Paths) -> dict[str, Any]:
    return {
        "ok": True,
        "data_root": str(paths.data_root),
        "sqlite_path": str(paths.sqlite_path),
    }
