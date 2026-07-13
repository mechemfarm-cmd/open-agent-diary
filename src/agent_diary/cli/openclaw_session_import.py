from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any

from agent_diary.cli.session_builder import build_session_jsonl
from agent_diary.cli.transcript_adapter import adapt_session_export, build_openclaw_telegram_direct_transcript
from agent_diary.config import Paths
from agent_diary.service.handlers import import_session_jsonl
from agent_diary.storage.imports import ensure_import_dirs


def _slugify_identifier(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    return slug or "unknown"


def _make_readable_import_id(source: str, resolved_session_id: str | None, input_path: Path) -> str:
    session_hint = resolved_session_id or input_path.stem
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"import-{_slugify_identifier(source)}-{_slugify_identifier(session_hint)}-{stamp}"


def import_openclaw_session(
    paths: Paths,
    *,
    input_path: Path,
    format_name: str = "openclaw-session-jsonl",
    source: str = "openclaw-session-import",
    import_id: str | None = None,
    source_session_id: str | None = None,
    source_conversation_id: str | None = None,
    dry_run: bool = False,
    gap_minutes: int = 60,
    max_chars: int = 6000,
    max_messages: int = 80,
    min_messages_before_gap_split: int = 4,
    min_chars_before_gap_split: int = 400,
) -> dict[str, Any]:
    ensure_import_dirs(paths)
    input_path = Path(input_path).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="openclaw-import-", dir=str(paths.imports_dir)) as temp_dir:
        temp_root = Path(temp_dir)
        transcript_path = temp_root / "transcript.jsonl"
        session_path = temp_root / "session.jsonl"

        adapter_result = adapt_session_export(
            input_path=input_path,
            output_path=transcript_path,
            format_name=format_name,
            source_session_id=source_session_id,
            source_conversation_id=source_conversation_id,
        )
        effective_session_id = source_session_id or adapter_result.get("source_session_id")
        effective_conversation_id = source_conversation_id or adapter_result.get("source_conversation_id")
        effective_import_id = import_id or _make_readable_import_id(source, effective_session_id, input_path)
        import_label = (
            f"{source} | session={effective_session_id or 'unknown'} | "
            f"conversation={effective_conversation_id or 'unknown'} | import={effective_import_id}"
        )

        session_result = build_session_jsonl(
            input_path=transcript_path,
            output_path=session_path,
            source=source,
            gap_minutes=gap_minutes,
            max_chars=max_chars,
            max_messages=max_messages,
            min_messages_before_gap_split=min_messages_before_gap_split,
            min_chars_before_gap_split=min_chars_before_gap_split,
        )
        import_result = import_session_jsonl(
            paths,
            {
                "path": str(session_path),
                "import_id": effective_import_id,
                "source_session_id": effective_session_id,
                "source_conversation_id": effective_conversation_id,
                "dry_run": dry_run,
            },
        )

    batch_manifest_path = import_result.get("manifest_path")
    summary = {
        "input_path": str(input_path),
        "format": format_name,
        "source": source,
        "resolved_source_session_id": effective_session_id,
        "resolved_source_conversation_id": effective_conversation_id,
        "source_session_id": effective_session_id,
        "source_conversation_id": effective_conversation_id,
        "dry_run": dry_run,
        "import_id": import_result.get("import_id"),
        "import_label": import_label,
        "transcript_message_count": adapter_result["message_count"],
        "session_chunk_count": session_result["entry_count"],
        "imported_count": import_result.get("imported_count", 0),
        "skipped_duplicate_count": import_result.get("skipped_count", 0),
        "batch_manifest_path": batch_manifest_path,
        "adapter": {
            "format": adapter_result["format"],
            "message_count": adapter_result["message_count"],
            "schema_fields": adapter_result["schema_fields"],
        },
        "session_builder": {
            "message_count": session_result["message_count"],
            "entry_count": session_result["entry_count"],
            "source": session_result["source"],
            "gap_minutes": session_result["gap_minutes"],
            "max_chars": session_result["max_chars"],
            "max_messages": session_result["max_messages"],
            "min_messages_before_gap_split": session_result["min_messages_before_gap_split"],
            "min_chars_before_gap_split": session_result["min_chars_before_gap_split"],
        },
        "import_result": import_result,
    }
    return summary


def import_telegram_direct(
    paths: Paths,
    *,
    inbound_path: Path,
    sessions_root: Path,
    chat_id: str,
    source: str = "telegram-direct-import",
    import_id: str | None = None,
    source_session_id: str | None = None,
    source_conversation_id: str | None = None,
    dry_run: bool = False,
    gap_minutes: int = 60,
    max_chars: int = 6000,
    max_messages: int = 80,
    min_messages_before_gap_split: int = 4,
    min_chars_before_gap_split: int = 400,
    session_files: list[Path] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    ensure_import_dirs(paths)
    inbound_path = Path(inbound_path).expanduser().resolve()
    sessions_root = Path(sessions_root).expanduser().resolve()
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise ValueError("chat_id is required")

    with tempfile.TemporaryDirectory(prefix="telegram-direct-import-", dir=str(paths.imports_dir)) as temp_dir:
        temp_root = Path(temp_dir)
        transcript_path = temp_root / "transcript.jsonl"
        session_path = temp_root / "session.jsonl"

        transcript_result = build_openclaw_telegram_direct_transcript(
            inbound_path=inbound_path,
            sessions_root=sessions_root,
            output_path=transcript_path,
            chat_id=chat_id,
            source_session_id=source_session_id,
            source_conversation_id=source_conversation_id,
            session_files=session_files,
            since=since,
            until=until,
        )
        effective_session_id = source_session_id or transcript_result.get("source_session_id")
        effective_conversation_id = source_conversation_id or transcript_result.get("source_conversation_id")
        effective_import_id = import_id or _make_readable_import_id(source, effective_session_id, inbound_path)
        import_label = (
            f"{source} | session={effective_session_id or 'unknown'} | "
            f"conversation={effective_conversation_id or 'unknown'} | import={effective_import_id}"
        )

        session_result = build_session_jsonl(
            input_path=transcript_path,
            output_path=session_path,
            source=source,
            gap_minutes=gap_minutes,
            max_chars=max_chars,
            max_messages=max_messages,
            min_messages_before_gap_split=min_messages_before_gap_split,
            min_chars_before_gap_split=min_chars_before_gap_split,
        )
        import_result = import_session_jsonl(
            paths,
            {
                "path": str(session_path),
                "import_id": effective_import_id,
                "source_session_id": effective_session_id,
                "source_conversation_id": effective_conversation_id,
                "dry_run": dry_run,
            },
        )

    batch_manifest_path = import_result.get("manifest_path")
    return {
        "inbound_path": str(inbound_path),
        "sessions_root": str(sessions_root),
        "chat_id": chat_id,
        "format": "openclaw-telegram-direct",
        "source": source,
        "resolved_source_session_id": effective_session_id,
        "resolved_source_conversation_id": effective_conversation_id,
        "source_session_id": effective_session_id,
        "source_conversation_id": effective_conversation_id,
        "dry_run": dry_run,
        "import_id": import_result.get("import_id"),
        "import_label": import_label,
        "transcript_message_count": transcript_result["message_count"],
        "session_chunk_count": session_result["entry_count"],
        "imported_count": import_result.get("imported_count", 0),
        "skipped_duplicate_count": import_result.get("skipped_count", 0),
        "batch_manifest_path": batch_manifest_path,
        "transcript": {
            "format": transcript_result["format"],
            "message_count": transcript_result["message_count"],
            "inbound_count": transcript_result["inbound_count"],
            "outbound_count": transcript_result["outbound_count"],
            "schema_fields": transcript_result["schema_fields"],
        },
        "session_builder": {
            "message_count": session_result["message_count"],
            "entry_count": session_result["entry_count"],
            "source": session_result["source"],
            "gap_minutes": session_result["gap_minutes"],
            "max_chars": session_result["max_chars"],
            "max_messages": session_result["max_messages"],
            "min_messages_before_gap_split": session_result["min_messages_before_gap_split"],
            "min_chars_before_gap_split": session_result["min_chars_before_gap_split"],
        },
        "import_result": import_result,
    }


def backfill_telegram_direct(
    paths: Paths,
    *,
    inbound_path: Path,
    sessions_root: Path,
    trajectories_root: Path,
    session_key: str,
    chat_id: str,
    source: str = "telegram-direct-backfill",
    since: str | None = None,
    until: str | None = None,
    days_back: int | None = None,
    dry_run: bool = False,
    gap_minutes: int = 60,
    max_chars: int = 6000,
    max_messages: int = 80,
    min_messages_before_gap_split: int = 4,
    min_chars_before_gap_split: int = 400,
) -> dict[str, Any]:
    if days_back is not None and days_back < 1:
        raise ValueError("days_back must be >= 1")
    since_dt = _coerce_since(since)
    until_dt = _coerce_until(until)
    if days_back is not None and since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    if since_dt and until_dt and since_dt >= until_dt:
        raise ValueError("since must be earlier than until")

    inbound_path = Path(inbound_path).expanduser().resolve()
    sessions_root = Path(sessions_root).expanduser().resolve()
    trajectories_root = Path(trajectories_root).expanduser().resolve()

    discovered = discover_openclaw_session_files(
        trajectories_root=trajectories_root,
        session_key=session_key,
        since=since_dt,
        until=until_dt,
    )
    missing_files: list[str] = []
    selected_files: list[Path] = []
    selected_items: list[dict[str, Any]] = []
    for item in discovered:
        file_path = Path(str(item["session_file"])).expanduser().resolve()
        if not file_path.exists():
            missing_files.append(str(file_path))
            continue
        selected_files.append(file_path)
        selected_items.append(item)

    import_summary: dict[str, Any] | None = None
    if selected_files:
        import_summary = import_telegram_direct(
            paths,
            inbound_path=inbound_path,
            sessions_root=sessions_root,
            chat_id=chat_id,
            source=source,
            dry_run=dry_run,
            gap_minutes=gap_minutes,
            max_chars=max_chars,
            max_messages=max_messages,
            min_messages_before_gap_split=min_messages_before_gap_split,
            min_chars_before_gap_split=min_chars_before_gap_split,
            session_files=selected_files,
            since=since_dt,
            until=until_dt,
        )

    return {
        "session_key": session_key,
        "chat_id": str(chat_id),
        "source": source,
        "inbound_path": str(inbound_path),
        "sessions_root": str(sessions_root),
        "trajectories_root": str(trajectories_root),
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
        "days_back": days_back,
        "dry_run": dry_run,
        "discovered_session_file_count": len(discovered),
        "processed_session_file_count": len(selected_files),
        "missing_session_files": missing_files,
        "transcript_message_count": int(import_summary.get("transcript_message_count", 0)) if import_summary else 0,
        "session_chunk_count": int(import_summary.get("session_chunk_count", 0)) if import_summary else 0,
        "imported_count": int(import_summary.get("imported_count", 0)) if import_summary else 0,
        "skipped_duplicate_count": int(import_summary.get("skipped_duplicate_count", 0)) if import_summary else 0,
        "items": [
            {
                "started_at": item["started_at"],
                "session_file": item["session_file"],
                "trajectory_path": item["trajectory_path"],
                "session_id": item.get("session_id"),
                "thread_id": item.get("thread_id"),
            }
            for item in selected_items
        ],
        "import_summary": import_summary,
    }


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _coerce_since(value: str | None) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if len(raw) == 10:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    dt = _parse_iso_datetime(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_until(value: str | None) -> datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if len(raw) == 10:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc) + timedelta(days=1)
    dt = _parse_iso_datetime(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def discover_openclaw_session_files(
    *,
    trajectories_root: Path,
    session_key: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for path in sorted(trajectories_root.glob("*.trajectory.jsonl")):
        session_started: dict[str, Any] | None = None
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError(f"{path}: line {line_number}: trajectory record must be an object")
                if obj.get("type") == "session.started":
                    session_started = obj
                    break
        if session_started is None:
            continue
        if session_started.get("sessionKey") != session_key:
            continue

        started_at_raw = session_started.get("ts")
        if started_at_raw in (None, ""):
            continue
        started_at = _parse_iso_datetime(str(started_at_raw)).astimezone(timezone.utc)
        if since and started_at < since:
            continue
        if until and started_at >= until:
            continue

        data = session_started.get("data", {})
        if not isinstance(data, dict):
            data = {}
        session_file_raw = data.get("sessionFile")
        if session_file_raw in (None, ""):
            continue
        session_file = str(Path(str(session_file_raw)).expanduser().resolve())
        if session_file in seen_files:
            continue
        seen_files.add(session_file)
        items.append(
            {
                "session_key": session_key,
                "started_at": started_at.isoformat(),
                "trajectory_path": str(path.resolve()),
                "session_id": session_started.get("sessionId"),
                "session_file": session_file,
                "thread_id": data.get("threadId"),
            }
        )

    items.sort(key=lambda item: (item["started_at"], item["session_file"]))
    return items


def backfill_openclaw_session_key(
    paths: Paths,
    *,
    trajectories_root: Path,
    session_key: str,
    source: str = "openclaw-session-backfill",
    since: str | None = None,
    until: str | None = None,
    days_back: int | None = None,
    dry_run: bool = False,
    gap_minutes: int = 60,
    max_chars: int = 6000,
    max_messages: int = 80,
    min_messages_before_gap_split: int = 4,
    min_chars_before_gap_split: int = 400,
) -> dict[str, Any]:
    if days_back is not None and days_back < 1:
        raise ValueError("days_back must be >= 1")
    since_dt = _coerce_since(since)
    until_dt = _coerce_until(until)
    if days_back is not None and since_dt is None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    if since_dt and until_dt and since_dt >= until_dt:
        raise ValueError("since must be earlier than until")

    trajectories_root = Path(trajectories_root).expanduser().resolve()
    discovered = discover_openclaw_session_files(
        trajectories_root=trajectories_root,
        session_key=session_key,
        since=since_dt,
        until=until_dt,
    )

    results: list[dict[str, Any]] = []
    total_transcript_messages = 0
    total_session_chunks = 0
    total_imported = 0
    total_skipped = 0
    missing_files: list[str] = []

    for item in discovered:
        input_path = Path(item["session_file"])
        if not input_path.exists():
            missing_files.append(str(input_path))
            continue
        imported = import_openclaw_session(
            paths,
            input_path=input_path,
            source=source,
            dry_run=dry_run,
            gap_minutes=gap_minutes,
            max_chars=max_chars,
            max_messages=max_messages,
            min_messages_before_gap_split=min_messages_before_gap_split,
            min_chars_before_gap_split=min_chars_before_gap_split,
        )
        total_transcript_messages += int(imported.get("transcript_message_count", 0))
        total_session_chunks += int(imported.get("session_chunk_count", 0))
        total_imported += int(imported.get("imported_count", 0))
        total_skipped += int(imported.get("skipped_duplicate_count", 0))
        results.append(
            {
                "started_at": item["started_at"],
                "session_file": item["session_file"],
                "trajectory_path": item["trajectory_path"],
                "session_id": item.get("session_id"),
                "thread_id": item.get("thread_id"),
                "transcript_message_count": imported.get("transcript_message_count", 0),
                "session_chunk_count": imported.get("session_chunk_count", 0),
                "imported_count": imported.get("imported_count", 0),
                "skipped_duplicate_count": imported.get("skipped_duplicate_count", 0),
                "import_id": imported.get("import_id"),
                "batch_manifest_path": imported.get("batch_manifest_path"),
            }
        )

    return {
        "session_key": session_key,
        "source": source,
        "trajectories_root": str(trajectories_root),
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
        "days_back": days_back,
        "dry_run": dry_run,
        "discovered_session_file_count": len(discovered),
        "processed_session_file_count": len(results),
        "missing_session_files": missing_files,
        "transcript_message_count": total_transcript_messages,
        "session_chunk_count": total_session_chunks,
        "imported_count": total_imported,
        "skipped_duplicate_count": total_skipped,
        "items": results,
    }
