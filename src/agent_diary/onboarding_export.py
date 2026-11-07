"""Export a Hermes database into a transcript the diary can read.

Why this exists: consent is recorded against where the history *lives* — a
SQLite database. Both ``preview_approved_source`` and ``import_approved_history``
require the run's locator to be a *readable export file*. This module is the
bridge, and it is the first and only place content is read.

Three properties matter, and each has a test:

- **Consent is re-checked here.** Not merely at preview. This is the moment
  bytes leave the database, so a state file edited by hand cannot get around it.
- **Reading is read-only.** The source database is opened ``mode=ro`` and is
  never mutated.
- **Export is idempotent per session.** A marker file per session means a
  re-run picks up new sessions only, and a session deleted from Hermes does not
  break it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import tempfile

from agent_diary.cli.transcript_adapter import adapt_session_export
from agent_diary.orientation import load_run, set_source_locator

EXPORT_FORMAT = "generic-message-jsonl"

# session_meta rows are internal bookkeeping, not conversation.
EXPORTED_ROLES = ("user", "assistant", "tool")

# The adapter requires message_id, created_at, role and content. A message with
# no text has nothing to contribute to a text transcript, so it is skipped and
# counted rather than written as an invalid row.
_LIVE_FILTER = (
    "m.active = 1 AND m.compacted = 0 AND TRIM(COALESCE(m.content, '')) <> '' "
    "AND m.role IN ('user', 'assistant', 'tool')"
)


class ExportRefused(PermissionError):
    """Raised when consent does not permit reading content."""


def _iso(epoch: float | int | None) -> str:
    if epoch is None:
        raise ValueError("message has no timestamp")
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()


def _marker_dir(paths, source_id: str) -> Path:
    return paths.imports_dir / ".export-markers" / source_id


def _already_exported(marker_dir: Path) -> set[str]:
    if not marker_dir.is_dir():
        return set()
    return {p.stem for p in marker_dir.glob("*.imported")}


def export_hermes_history(
    paths,
    run_id: str,
    source_id: str,
    *,
    db_path: Path | str | None = None,
    keep_canonical: bool = False,
) -> dict:
    """Write approved Hermes history to a canonical JSONL transcript.

    Returns a summary. ``status`` is one of ``exported`` (a new file was
    written), ``nothing_new`` (every session was already exported), or it raises.
    """
    run = load_run(paths, run_id)
    source = next((s for s in run["sources"] if s.get("source_id") == source_id), None)
    if source is None:
        raise KeyError(f"orientation source not found: {source_id}")

    if source.get("consent") != "approved" or source.get("content_access_granted") is not True:
        raise ExportRefused(
            "export reads content, so it requires approved consent with content scope; "
            f"source {source_id!r} is {source.get('consent')!r} "
            f"with content_access_granted={source.get('content_access_granted')!r}"
        )

    # The locator is repointed at the transcript once an export succeeds, so it
    # stops pointing at the database. Prefer the remembered origin, which is why
    # a re-run works at all.
    existing = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    remembered = existing.get("source_database") if existing else None
    candidate = db_path or remembered or source.get("locator", "")
    origin = Path(str(candidate).strip()).expanduser()
    if not origin.is_file():
        raise ValueError(f"Hermes database not found at {origin}")

    paths.imports_dir.mkdir(parents=True, exist_ok=True)
    marker_dir = _marker_dir(paths, source_id)
    marker_dir.mkdir(parents=True, exist_ok=True)
    seen = _already_exported(marker_dir)

    connection = sqlite3.connect(f"file:{origin}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        session_ids = [
            str(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT m.session_id FROM messages m "
                f"JOIN sessions s ON s.id = m.session_id WHERE {_LIVE_FILTER}"
            )
        ]
        new_sessions = sorted(sid for sid in session_ids if sid not in seen)
        skipped_empty = int(
            connection.execute(
                "SELECT COUNT(*) FROM messages m WHERE m.active = 1 AND m.compacted = 0 "
                "AND (m.content IS NULL OR TRIM(m.content) = '')"
            ).fetchone()[0]
        )

        rows: list[dict] = []
        for session_id in new_sessions:
            for message in connection.execute(
                f"SELECT m.id AS id, m.role AS role, m.content AS content, "
                f"m.timestamp AS timestamp FROM messages m "
                f"WHERE m.session_id = ? AND {_LIVE_FILTER} "
                f"ORDER BY m.timestamp, m.id",
                (session_id,),
            ):
                rows.append(
                    {
                        "message_id": f"hermes:{session_id}:{message['id']}",
                        "created_at": _iso(message["timestamp"]),
                        "role": str(message["role"]),
                        "content": str(message["content"]),
                    }
                )
    finally:
        connection.close()

    if not new_sessions:
        return {
            "status": "nothing_new",
            "run_id": run_id,
            "source_id": source_id,
            "exported_path": source.get("locator"),
            "format_name": (source.get("metadata") or {}).get("format_name", EXPORT_FORMAT),
            "sessions_exported": 0,
            "messages_exported": 0,
            "sessions_already_exported": len(seen),
            "messages_skipped_empty": skipped_empty,
        }

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    export_path = paths.imports_dir / f"hermes-export-{stamp}.jsonl"
    with export_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Prove the file is readable before the locator is trusted: adapt it into a
    # temp dir, so a bad export fails here rather than during preview.
    with tempfile.TemporaryDirectory(prefix="onboarding-export-", dir=str(paths.imports_dir)) as tmp:
        canonical = Path(tmp) / "canonical.jsonl"
        adapted = adapt_session_export(
            input_path=export_path,
            output_path=canonical,
            format_name=EXPORT_FORMAT,
        )
        if keep_canonical:
            keep = export_path.with_suffix(".canonical.jsonl")
            keep.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")

    set_source_locator(
        paths,
        run_id,
        source_id,
        str(export_path),
        format_name=EXPORT_FORMAT,
        metadata={"source_database": str(origin)},
    )

    for session_id in new_sessions:
        (marker_dir / f"{session_id}.imported").write_text(
            datetime.now(tz=timezone.utc).isoformat(), encoding="utf-8"
        )

    return {
        "status": "exported",
        "run_id": run_id,
        "source_id": source_id,
        "exported_path": str(export_path),
        "format_name": EXPORT_FORMAT,
        "sessions_exported": len(new_sessions),
        "messages_exported": adapted["message_count"],
        "sessions_already_exported": len(seen),
        "messages_skipped_empty": skipped_empty,
        "validated": True,
    }
