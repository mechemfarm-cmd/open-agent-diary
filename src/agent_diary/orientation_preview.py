"""Consent-gated, read-only previews of orientation sources."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any

from agent_diary.cli.transcript_adapter import adapt_session_export
from agent_diary.config import Paths
from agent_diary.orientation import load_run


def _parse_timestamp(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def preview_approved_source(
    paths: Paths,
    run_id: str,
    source_id: str,
    *,
    format_name: str,
    max_messages: int = 20,
    sample_chars: int = 240,
) -> dict[str, Any]:
    if max_messages < 1:
        raise ValueError("max_messages must be >= 1")
    if sample_chars < 1:
        raise ValueError("sample_chars must be >= 1")
    run = load_run(paths, run_id)
    source = next((item for item in run["sources"] if item.get("source_id") == source_id), None)
    if source is None:
        raise KeyError(f"orientation source not found: {source_id}")
    if source.get("consent") != "approved" or source.get("content_access_granted") is not True:
        raise PermissionError("orientation source requires approved content consent")
    if source.get("kind") != "hermes_history":
        raise ValueError("this preview currently supports hermes_history sources only")

    input_path = Path(str(source.get("locator", "")).strip()).expanduser()
    if not input_path.is_file():
        raise ValueError("Hermes history preview requires a readable export file")

    with tempfile.TemporaryDirectory(prefix="orientation-preview-", dir=str(paths.data_root)) as temp_dir:
        adapted_path = Path(temp_dir) / "canonical.jsonl"
        adapter = adapt_session_export(
            input_path=input_path,
            output_path=adapted_path,
            format_name=format_name,
        )
        rows: list[dict[str, Any]] = []
        with adapted_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))

    roles = Counter(str(row.get("author_role", "unknown")) for row in rows)
    timestamps = [
        _parse_timestamp(str(row["created_at"]))
        for row in rows
        if row.get("created_at") not in (None, "")
    ]
    samples = []
    for row in rows[:max_messages]:
        content = str(row.get("content", ""))
        samples.append(
            {
                "message_id": row.get("message_id"),
                "created_at": row.get("created_at"),
                "author_role": row.get("author_role"),
                "speaker": row.get("speaker"),
                "content": content[:sample_chars],
                "content_truncated": len(content) > sample_chars,
            }
        )

    return {
        "run_id": run_id,
        "source_id": source_id,
        "source_kind": source["kind"],
        "input_path": str(input_path),
        "format": format_name,
        "content_access_granted": True,
        "imported": False,
        "message_count": len(rows),
        "sample_count": len(samples),
        "author_role_counts": dict(sorted(roles.items())),
        "created_at_range": {
            "first": min(timestamps).isoformat() if timestamps else None,
            "last": max(timestamps).isoformat() if timestamps else None,
        },
        "adapter": adapter,
        "samples": samples,
    }
