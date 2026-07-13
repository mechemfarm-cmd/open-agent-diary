from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from agent_diary.cli.openclaw_session_import import discover_openclaw_session_files
from agent_diary.config import Paths
from agent_diary.index.repository import get_work_trace_row
from agent_diary.service.handlers import append_work_trace_event


def _non_empty_str(value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError("value must be a non-empty string")
    return text


def _parse_iso_datetime(value: str) -> datetime:
    text = _non_empty_str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_since(raw: str | None) -> datetime | None:
    if raw in (None, ""):
        return None
    if len(str(raw)) == 10:
        return datetime.fromisoformat(str(raw) + "T00:00:00+00:00").astimezone(timezone.utc)
    return _parse_iso_datetime(str(raw))


def _coerce_until(raw: str | None) -> datetime | None:
    if raw in (None, ""):
        return None
    if len(str(raw)) == 10:
        return datetime.fromisoformat(str(raw) + "T00:00:00+00:00").astimezone(timezone.utc)
    return _parse_iso_datetime(str(raw))


def _stable_event_id(*parts: str) -> str:
    basis = "|".join(part.strip() for part in parts if part and part.strip())
    return f"work_{uuid5(NAMESPACE_URL, basis).hex}"


def _extract_result_payload(message: dict[str, Any]) -> dict[str, Any] | None:
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        candidate = item.get("content")
        if candidate in (None, ""):
            candidate = item.get("text")
        if candidate in (None, ""):
            continue
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, str):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _tool_arguments(item: dict[str, Any]) -> dict[str, Any]:
    arguments = item.get("arguments")
    if not isinstance(arguments, dict):
        arguments = item.get("input")
    return dict(arguments) if isinstance(arguments, dict) else {}


def _command_from_tool(tool_name: str, arguments: dict[str, Any]) -> str | None:
    if tool_name == "bash":
        return str(arguments.get("command", "")).strip() or None
    if tool_name == "exec_command":
        return str(arguments.get("cmd", "")).strip() or None
    if tool_name == "write_stdin":
        session_id = str(arguments.get("session_id", "")).strip()
        chars = str(arguments.get("chars", "")).strip()
        if session_id or chars:
            return f"stdin session={session_id} chars={chars[:80]}".strip()
    return None


def _is_test_command(command: str) -> bool:
    lowered = command.lower()
    return any(
        token in lowered
        for token in (
            "pytest",
            "unittest",
            "npm test",
            "pnpm test",
            "yarn test",
            "cargo test",
            "go test",
            "vitest",
            "jest",
        )
    )


def _summarize_command(command: str, *, test_run: bool) -> str:
    compact = " ".join(command.split())
    if len(compact) > 120:
        compact = compact[:117] + "..."
    prefix = "Ran test command" if test_run else "Ran command"
    return f"{prefix}: {compact}"


def _tool_to_event_payload(
    *,
    session_file: Path,
    session_id: str | None,
    session_key: str | None,
    call_record_id: str,
    call_timestamp: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    result_message: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_name == "message":
        if str(arguments.get("action", "")).strip() == "send":
            return None

    result_payload = _extract_result_payload(result_message)
    is_error = bool(result_message.get("isError", False))
    result_record_id = str(result_message.get("__record_id", "")).strip() or call_record_id
    event_id = _stable_event_id(str(session_file), call_record_id, tool_call_id, tool_name, result_record_id)

    common = {
        "event_id": event_id,
        "created_at": call_timestamp,
        "project": "external-openclaw-work",
        "source_surface": "openclaw-session",
        "actor": "assistant",
        "session_key": session_key,
        "details": {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "session_file": str(session_file),
            "source_session_id": session_id,
            "call_record_id": call_record_id,
            "result_record_id": result_record_id,
            "arguments": arguments,
            "result_payload": result_payload,
            "is_error": is_error,
        },
        "related_paths": [str(session_file)],
        "tags": ["auto", "openclaw", "session_import", tool_name],
    }

    command = _command_from_tool(tool_name, arguments)
    if command:
        test_run = _is_test_command(command)
        return {
            **common,
            "event_type": "test_run" if test_run else "command",
            "summary": _summarize_command(command, test_run=test_run),
            "tags": common["tags"] + (["test_run"] if test_run else ["command"]),
        }

    action_name = str(arguments.get("action", "")).strip() or str(arguments.get("kind", "")).strip()
    summary = f"Ran tool {tool_name}"
    if action_name:
        summary += f" ({action_name})"
    summary += "."
    return {
        **common,
        "event_type": "action",
        "summary": summary,
        "tags": common["tags"] + ["action"],
    }


def extract_openclaw_work_trace_events(
    *,
    input_path: Path,
    session_key: str | None = None,
) -> dict[str, Any]:
    input_path = Path(input_path).expanduser().resolve()
    pending: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    session_id: str | None = None
    session_file = input_path

    with input_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError(f"{input_path}: line {line_number}: session log record must be an object")

            record_type = obj.get("type")
            if record_type == "session":
                if obj.get("id") not in (None, ""):
                    session_id = _non_empty_str(obj.get("id"))
                continue
            if record_type != "message":
                continue

            message = obj.get("message")
            if not isinstance(message, dict):
                continue

            role = message.get("role")
            record_id = _non_empty_str(obj.get("id"))
            timestamp = _non_empty_str(obj.get("timestamp") or message.get("timestamp"))
            if role == "assistant":
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "toolCall":
                        continue
                    tool_name = str(item.get("name", "")).strip()
                    tool_call_id = str(item.get("id", "")).strip()
                    if not tool_name or not tool_call_id:
                        continue
                    pending[tool_call_id] = {
                        "record_id": record_id,
                        "timestamp": timestamp,
                        "tool_name": tool_name,
                        "arguments": _tool_arguments(item),
                    }
                continue

            if role != "toolResult":
                continue
            tool_call_id = str(message.get("toolCallId", "")).strip()
            if not tool_call_id:
                continue
            pending_item = pending.pop(tool_call_id, None)
            if pending_item is None:
                continue
            message["__record_id"] = record_id
            payload = _tool_to_event_payload(
                session_file=session_file,
                session_id=session_id,
                session_key=session_key,
                call_record_id=str(pending_item["record_id"]),
                call_timestamp=str(pending_item["timestamp"]),
                tool_call_id=tool_call_id,
                tool_name=str(pending_item["tool_name"]),
                arguments=dict(pending_item["arguments"]),
                result_message=message,
            )
            if payload is not None:
                events.append(payload)

    return {
        "input_path": str(input_path),
        "source_session_id": session_id,
        "source_session_key": session_key,
        "event_count": len(events),
        "events": events,
    }


def import_openclaw_work_trace(
    paths: Paths,
    *,
    input_path: Path,
    session_key: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    extracted = extract_openclaw_work_trace_events(input_path=input_path, session_key=session_key)
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for event in extracted["events"]:
        event_id = str(event["event_id"])
        if get_work_trace_row(paths.sqlite_path, event_id) is not None:
            skipped.append({"event_id": event_id, "reason": "duplicate_work_trace_event"})
            continue
        if dry_run:
            imported.append({"event_id": event_id, "dry_run": True, "event_type": event["event_type"]})
            continue
        out = append_work_trace_event(paths, event)
        imported.append({"event_id": out["event_id"], "work_file": out["work_file"], "event_type": event["event_type"]})
    return {
        "input_path": extracted["input_path"],
        "source_session_id": extracted["source_session_id"],
        "source_session_key": extracted["source_session_key"],
        "dry_run": dry_run,
        "discovered_event_count": extracted["event_count"],
        "imported_count": len(imported),
        "skipped_duplicate_count": len(skipped),
        "imported": imported,
        "skipped": skipped,
    }


def backfill_openclaw_work_trace_session_key(
    paths: Paths,
    *,
    trajectories_root: Path,
    session_key: str,
    since: str | None = None,
    until: str | None = None,
    days_back: int | None = None,
    dry_run: bool = False,
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
    missing_files: list[str] = []
    total_discovered_events = 0
    total_imported = 0
    total_skipped = 0
    for item in discovered:
        input_path = Path(item["session_file"])
        if not input_path.exists():
            missing_files.append(str(input_path))
            continue
        imported = import_openclaw_work_trace(
            paths,
            input_path=input_path,
            session_key=session_key,
            dry_run=dry_run,
        )
        total_discovered_events += int(imported.get("discovered_event_count", 0))
        total_imported += int(imported.get("imported_count", 0))
        total_skipped += int(imported.get("skipped_duplicate_count", 0))
        results.append(
            {
                "started_at": item["started_at"],
                "session_file": item["session_file"],
                "trajectory_path": item["trajectory_path"],
                "session_id": item.get("session_id"),
                "thread_id": item.get("thread_id"),
                "discovered_event_count": imported.get("discovered_event_count", 0),
                "imported_count": imported.get("imported_count", 0),
                "skipped_duplicate_count": imported.get("skipped_duplicate_count", 0),
            }
        )

    return {
        "session_key": session_key,
        "trajectories_root": str(trajectories_root),
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
        "days_back": days_back,
        "dry_run": dry_run,
        "discovered_session_file_count": len(discovered),
        "processed_session_file_count": len(results),
        "missing_session_files": missing_files,
        "discovered_event_count": total_discovered_events,
        "imported_count": total_imported,
        "skipped_duplicate_count": total_skipped,
        "items": results,
    }
