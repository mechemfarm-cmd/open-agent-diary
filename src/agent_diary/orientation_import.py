"""Consent-gated Hermes-history orientation import."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

from agent_diary.cli.session_builder import build_session_jsonl
from agent_diary.cli.transcript_adapter import adapt_session_export
from agent_diary.config import Paths
from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.orientation import load_run, update_source_metadata
from agent_diary.orientation_artifacts import create_artifact, load_artifact
from agent_diary.service.handlers import import_session_jsonl
from agent_diary.storage.files import ensure_data_dirs
from agent_diary.storage.imports import ensure_import_dirs, normalize_import_id


def _source(paths: Paths, run_id: str, source_id: str) -> dict[str, Any]:
    run = load_run(paths, run_id)
    source = next((item for item in run["sources"] if item.get("source_id") == source_id), None)
    if source is None:
        raise KeyError(f"orientation source not found: {source_id}")
    return source


def _artifact_for_source(paths: Paths, run_id: str, source_id: str, input_path: Path) -> str:
    source = _source(paths, run_id, source_id)
    metadata = source.get("metadata", {})
    if isinstance(metadata, dict) and metadata.get("artifact_id"):
        load_artifact(paths, str(metadata["artifact_id"]))
        return str(metadata["artifact_id"])
    stat = input_path.stat()
    artifact = create_artifact(
        paths,
        run_id=run_id,
        source_id=source_id,
        artifact_type="conversation",
        locator=str(input_path),
        mime_type="application/jsonl",
        size=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    )
    update_source_metadata(paths, run_id, source_id, {"artifact_id": artifact["artifact_id"]})
    return artifact["artifact_id"]


def import_approved_history(
    paths: Paths,
    *,
    run_id: str,
    source_id: str,
    format_name: str,
    import_id: str | None = None,
    dry_run: bool = False,
    gap_minutes: int = 60,
    max_chars: int = 6000,
    max_messages: int = 80,
) -> dict[str, Any]:
    source = _source(paths, run_id, source_id)
    if source.get("consent") != "approved" or source.get("content_access_granted") is not True:
        raise PermissionError("source content consent is required for history import")
    input_path = Path(str(source.get("locator", "")).strip()).expanduser()
    if not input_path.is_file():
        raise ValueError("Hermes history import requires a readable export file")
    effective_import_id = normalize_import_id(import_id or f"orientation-{run_id}-{source_id}")
    ensure_data_dirs(paths)
    bootstrap_sqlite(paths.sqlite_path)
    ensure_import_dirs(paths)

    with tempfile.TemporaryDirectory(prefix="orientation-import-", dir=str(paths.imports_dir)) as temp_dir:
        temp_root = Path(temp_dir)
        transcript_path = temp_root / "transcript.jsonl"
        session_path = temp_root / "session.jsonl"
        adapter_result = adapt_session_export(
            input_path=input_path,
            output_path=transcript_path,
            format_name=format_name,
        )
        session_result = build_session_jsonl(
            input_path=transcript_path,
            output_path=session_path,
            source=f"orientation:{run_id}:{source_id}",
            gap_minutes=gap_minutes,
            max_chars=max_chars,
            max_messages=max_messages,
        )
        import_result = import_session_jsonl(
            paths,
            {
                "path": str(session_path),
                "import_id": effective_import_id,
                "source_session_id": adapter_result.get("source_session_id"),
                "source_conversation_id": adapter_result.get("source_conversation_id"),
                "dry_run": dry_run,
            },
        )

    artifact_id = None
    if not dry_run:
        artifact_id = _artifact_for_source(paths, run_id, source_id, input_path)
        update_source_metadata(
            paths,
            run_id,
            source_id,
            {
                "last_import_id": effective_import_id,
                "last_imported_at": datetime.now(timezone.utc).isoformat(),
                "last_imported_count": import_result.get("imported_count", 0),
            },
        )

    return {
        "run_id": run_id,
        "source_id": source_id,
        "input_path": str(input_path),
        "format": format_name,
        "import_id": effective_import_id,
        "dry_run": dry_run,
        "imported": not dry_run,
        "artifact_id": artifact_id,
        "transcript_message_count": adapter_result["message_count"],
        "session_chunk_count": session_result["entry_count"],
        "imported_count": import_result.get("imported_count", 0),
        "skipped_count": import_result.get("skipped_count", 0),
        "manifest_path": import_result.get("manifest_path"),
    }
