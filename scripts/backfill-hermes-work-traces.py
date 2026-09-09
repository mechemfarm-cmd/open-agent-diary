#!/usr/bin/env python3
"""backfill-hermes-work-traces.py

Parse Hermes session data from ~/.hermes/state.db and import tool-call
evidence as work trace events into Agent Diary.

Run as a second pass after the daily session import (hermes-to-diary.sh).
Only processes sessions whose diary entries already exist (skips sessions
not yet imported into the diary).

Usage:
    cd /path/to/agent-diary
    python3 scripts/backfill-hermes-work-traces.py \\
        --diary-db data/index/memory.db \\
        --hermes-db ~/.hermes/state.db \\
        --data-dir data \\
        [--dry-run]
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

# ── helpers ──────────────────────────────────────────────────────────────

def _stable_event_id(*parts: str) -> str:
    """Deterministic event ID so re-runs are idempotent."""
    basis = "|".join(p.strip() for p in parts if p and p.strip())
    return f"work_{uuid5(NAMESPACE_URL, basis).hex}"


def _find_entries_for_session(diary_db: str, session_id: str) -> list[str]:
    """Return diary entry IDs whose metadata.ingestion.source_session_id matches."""
    found: list[str] = []
    conn = sqlite3.connect(diary_db)
    try:
        rows = conn.execute(
            "SELECT entry_id, raw_file_path FROM entries ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()

    for entry_id, raw_path in rows:
        path = Path(raw_path)
        if not path.exists():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        meta = body.get("metadata", {})
        if isinstance(meta, dict):
            ingestion = meta.get("ingestion", {})
            if isinstance(ingestion, dict):
                sid = ingestion.get("source_session_id", "")
                if sid == session_id:
                    found.append(entry_id)
    return found


def _extract_tool_calls(
    hermes_db: str, session_id: str
) -> list[dict]:
    """Extract assistant tool-call messages + their tool-result replies."""
    conn = sqlite3.connect(hermes_db)
    conn.row_factory = sqlite3.Row
    try:
        # Get assistant messages with tool_calls, ordered by timestamp
        assistant_rows = conn.execute(
            """SELECT id, timestamp, content, tool_calls
               FROM messages
               WHERE session_id = ? AND role = 'assistant'
                 AND tool_calls IS NOT NULL AND tool_calls != '[]'
               ORDER BY id ASC""",
            (session_id,),
        ).fetchall()

        # Get tool result messages, indexed by their record id or order
        tool_rows = conn.execute(
            """SELECT id, timestamp, tool_name, content
               FROM messages
               WHERE session_id = ? AND role = 'tool'
                 AND tool_name IS NOT NULL
               ORDER BY id ASC""",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    # Map tool results by tool_name and approximate timestamp proximity
    # Hermes sends tool calls and receives tool results in the same order,
    # so we can zip them when the counts match per-assistant-message.
    events: list[dict] = []
    tool_idx = 0

    for assist in assistant_rows:
        try:
            calls = json.loads(assist["tool_calls"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(calls, list):
            continue

        for tc in calls:
            func = tc.get("function", {})
            if not isinstance(func, dict):
                continue
            tool_name = func.get("name", "")
            try:
                arguments = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            tool_call_id = tc.get("id") or tc.get("call_id", "")
            call_ts = assist["timestamp"] or 0

            # Find matching tool result
            result_content = None
            result_tool_name = None
            while tool_idx < len(tool_rows):
                tr = tool_rows[tool_idx]
                # Find the next tool result whose tool_name matches
                if tr["tool_name"] == tool_name:
                    result_content = tr["content"]
                    result_tool_name = tr["tool_name"]
                    tool_idx += 1
                    break
                # Skip results that don't match (tool calls without preceding assistant)
                # This handles cases where tool results arrive out of order
                tool_idx += 1

            iso_ts = datetime.fromtimestamp(call_ts, tz=timezone.utc).isoformat() if call_ts else ""

            events.append({
                "tool_name": tool_name,
                "arguments": arguments,
                "tool_call_id": tool_call_id,
                "timestamp": iso_ts,
                "result_content": result_content,
                "result_tool_name": result_tool_name,
            })

    return events


def _tool_to_work_trace_event(
    tc: dict,
    session_id: str,
    entry_ids: list[str],
) -> dict | None:
    """Convert a raw tool call + result into a work trace event payload."""
    tool_name = tc["tool_name"]
    arguments = tc["arguments"]
    tool_call_id = tc["tool_call_id"]
    ts = tc["timestamp"]
    result_content = tc["result_content"]

    event_id = _stable_event_id(session_id, tool_call_id, tool_name, str(arguments))

    is_error = False
    if result_content:
        try:
            parsed = json.loads(result_content)
            if isinstance(parsed, dict):
                is_error = bool(parsed.get("error")) or parsed.get("exit_code", 0) != 0
        except (json.JSONDecodeError, TypeError):
            pass

    common = {
        "event_id": event_id,
        "created_at": ts,
        "project": "hermes-session",
        "source_surface": "hermes-agent",
        "actor": "assistant",
        "session_key": session_id,
        "related_entry_ids": entry_ids,
        "tags": ["auto", "hermes", "session_import", tool_name],
        "details": {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "source_session_id": session_id,
            "arguments": arguments,
            "is_error": is_error,
        },
    }

    # ── tool-specific mapping ──

    if tool_name == "terminal":
        command = str(arguments.get("command", "")).strip()
        if not command:
            return None
        test_run = any(
            token in command.lower()
            for token in ("pytest", "unittest", "npm test", "pnpm test",
                          "yarn test", "cargo test", "go test", "vitest", "jest")
        )
        compact = " ".join(command.split())
        if len(compact) > 120:
            compact = compact[:117] + "..."
        prefix = "Ran test command" if test_run else "Ran command"
        # Extract touched paths from command
        touched = [str(p) for p in _paths_in_command(command)]
        return {
            **common,
            "event_type": "test_run" if test_run else "command",
            "summary": f"{prefix}: {compact}",
            "tags": common["tags"] + (["test_run"] if test_run else ["command"]),
            "related_paths": touched,
        }

    elif tool_name in ("write_file",):
        path = str(arguments.get("path", "")).strip()
        if not path:
            return None
        return {
            **common,
            "event_type": "file_edit",
            "summary": f"Wrote file: {path}",
            "tags": common["tags"] + ["file_edit"],
            "related_paths": [path],
        }

    elif tool_name in ("patch",):
        path = str(arguments.get("path", "")).strip()
        if not path:
            return None
        return {
            **common,
            "event_type": "file_edit",
            "summary": f"Patched file: {path}",
            "tags": common["tags"] + ["file_edit"],
            "related_paths": [path],
        }

    elif tool_name in ("search_files", "read_file"):
        path = str(arguments.get("path", "")).strip() or "."
        pattern = str(arguments.get("pattern", "")).strip()
        summary_parts = []
        if tool_name == "search_files":
            summary_parts.append(f"Searched files for: {pattern}" if pattern else "Searched files")
        else:
            summary_parts.append(f"Read file: {path}")
        return {
            **common,
            "event_type": "read",
            "summary": " ".join(summary_parts),
            "tags": common["tags"] + ["read"],
            "related_paths": [path] if path != "." else [],
        }

    elif tool_name in ("memory",):
        action = str(arguments.get("action", "")).strip()
        return {
            **common,
            "event_type": "memory_op",
            "summary": f"Memory operation: {action}" if action else "Memory operation",
            "tags": common["tags"] + ["memory"],
        }

    elif tool_name in ("browser_navigate", "browser_click", "browser_snapshot",
                       "browser_scroll", "browser_console"):
        return {
            **common,
            "event_type": "browser",
            "summary": f"Browser: {tool_name}",
            "tags": common["tags"] + ["browser"],
        }

    # Everything else — generic tool call
    action_name = str(arguments.get("action", "")).strip() or str(arguments.get("kind", "")).strip()
    summary = f"Tool: {tool_name}"
    if action_name:
        summary += f" ({action_name})"
    return {
        **common,
        "event_type": "action",
        "summary": summary,
        "tags": common["tags"] + ["action"],
    }


def _paths_in_command(command: str) -> set[str]:
    """Heuristic: extract likely file/dir paths from a shell command."""
    paths: set[str] = set()
    tokens = command.split()
    for token in tokens:
        token = token.strip("'\"")
        if token.startswith(("~/", "/", "./", "../")):
            paths.add(token)
        elif token.endswith((".py", ".js", ".ts", ".json", ".yaml", ".yml",
                             ".assistantl", ".md", ".sh", ".txt", ".css", ".html")):
            paths.add(token)
    return paths


# ── main ─────────────────────────────────────────────────────────────────

def backfill_hermes_work_traces(
    diary_db: str,
    hermes_db: str,
    data_dir: str,
    dry_run: bool = False,
    session_filter: list[str] | None = None,
) -> dict:
    """Main entry point: find Hermes sessions, extract tool calls, import as work traces."""
    from agent_diary.config import default_paths
    from agent_diary.index.repository import get_work_trace_row
    from agent_diary.service.handlers import append_work_trace_event

    paths = default_paths(Path(data_dir).parent)
    results: list[dict] = []
    total_events = 0
    total_imported = 0
    total_skipped = 0

    # Get all sessions from Hermes DB that have tool calls
    conn = sqlite3.connect(hermes_db)
    conn.row_factory = sqlite3.Row
    try:
        if session_filter:
            placeholders = ",".join("?" for _ in session_filter)
            sessions = conn.execute(
                f"""SELECT DISTINCT s.id, s.title, s.started_at
                   FROM sessions s
                   JOIN messages m ON m.session_id = s.id
                   WHERE m.tool_calls IS NOT NULL AND m.tool_calls != '[]'
                     AND m.role = 'assistant'
                     AND s.id IN ({placeholders})
                   ORDER BY s.started_at ASC""",
                session_filter,
            ).fetchall()
        else:
            sessions = conn.execute(
                """SELECT DISTINCT s.id, s.title, s.started_at
               FROM sessions s
               JOIN messages m ON m.session_id = s.id
               WHERE m.tool_calls IS NOT NULL AND m.tool_calls != '[]'
                 AND m.role = 'assistant'
               ORDER BY s.started_at ASC"""
            ).fetchall()
    finally:
        conn.close()

    for sess in sessions:
        session_id = sess["id"]
        title = sess["title"] or session_id

        # Find diary entries for this session
        entry_ids = _find_entries_for_session(diary_db, session_id)
        if not entry_ids:
            print(f"  ⏭  {session_id[:20]}... — no diary entries found, skipping")
            continue

        tool_calls = _extract_tool_calls(hermes_db, session_id)
        if not tool_calls:
            print(f"  ⏭  {session_id[:20]}... — no tool calls found")
            continue

        print(f"  → {session_id[:20]}... | {len(entry_ids)} entries | {len(tool_calls)} tool calls")

        session_imported = 0
        session_skipped = 0

        for tc in tool_calls:
            event = _tool_to_work_trace_event(tc, session_id, entry_ids)
            if event is None:
                session_skipped += 1
                continue

            total_events += 1

            # Deduplicate
            if get_work_trace_row(paths.sqlite_path, event["event_id"]) is not None:
                session_skipped += 1
                continue

            if dry_run:
                session_imported += 1
                continue

            try:
                out = append_work_trace_event(paths, event)
                session_imported += 1
            except Exception as e:
                print(f"    ✗ error importing event {event['event_id'][:20]}: {e}")
                session_skipped += 1

        total_imported += session_imported
        total_skipped += session_skipped

        results.append({
            "session_id": session_id,
            "title": title,
            "diary_entry_count": len(entry_ids),
            "tool_call_count": len(tool_calls),
            "imported": session_imported,
            "skipped": session_skipped,
        })

    return {
        "dry_run": dry_run,
        "sessions_processed": len(results),
        "total_discovered_events": total_events,
        "total_imported": total_imported,
        "total_skipped": total_skipped,
        "items": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill Hermes work traces into Agent Diary"
    )
    parser.add_argument("--diary-db", required=True,
                        help="Path to diary SQLite DB (data/index/memory.db)")
    parser.add_argument("--hermes-db", required=True,
                        help="Path to Hermes state.db")
    parser.add_argument("--data-dir", required=True,
                        help="Agent Diary data root directory")
    parser.add_argument("--session-ids", default=None,
                        help="Comma-separated list of session IDs to process (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and report, but don't import")
    args = parser.parse_args()

    session_filter = None
    if args.session_ids:
        session_filter = [s.strip() for s in args.session_ids.split(",") if s.strip()]
        print(f"Backfill filter: {len(session_filter)} session(s) specified")

    print(f"Hermes work trace backfill")
    print(f"  Diary DB:  {args.diary_db}")
    print(f"  Hermes DB: {args.hermes_db}")
    print(f"  Data dir:  {args.data_dir}")
    print(f"  Dry run:   {args.dry_run}")
    print()

    result = backfill_hermes_work_traces(
        diary_db=args.diary_db,
        hermes_db=args.hermes_db,
        data_dir=args.data_dir,
        dry_run=args.dry_run,
        session_filter=session_filter,
    )

    print()
    print(f"Sessions processed: {result['sessions_processed']}")
    print(f"Events discovered:  {result['total_discovered_events']}")
    print(f"Imported:           {result['total_imported']}")
    print(f"Skipped:            {result['total_skipped']}")
    if result["items"]:
        print()
        print("Per-session detail:")
        for item in result["items"]:
            print(f"  {item['session_id'][:24]}... "
                  f"| {item['diary_entry_count']} entries "
                  f"| {item['tool_call_count']} calls "
                  f"| +{item['imported']} wt events")