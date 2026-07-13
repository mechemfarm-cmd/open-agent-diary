from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import re
from pathlib import Path
from typing import Any

from agent_diary.models.types import Artifact, RawEntry, WorkTraceEvent


def connect_sqlite(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with release-safe local concurrency defaults."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _query_terms(query: str) -> list[str]:
    return [t for t in re.findall(r"\w+", query.lower()) if t]


def _fts_match_query(terms: list[str]) -> str:
    # Quote terms so punctuation in user queries cannot become FTS syntax.
    return " OR ".join(f'"{term.replace(chr(34), chr(34) + chr(34))}"' for term in terms)


def _entry_index_values(entry: RawEntry) -> tuple[str | None, str | None, str | None, int]:
    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    ingestion = metadata.get("ingestion") if isinstance(metadata.get("ingestion"), dict) else {}
    source_session_id = ingestion.get("source_session_id") or metadata.get("source_session_id")
    source_conversation_id = ingestion.get("source_conversation_id") or metadata.get("source_conversation_id")
    import_id = ingestion.get("import_id")
    truthful_source = bool(ingestion.get("truthful_source", False))
    return (
        str(source_session_id).strip() if source_session_id not in (None, "") else None,
        str(source_conversation_id).strip() if source_conversation_id not in (None, "") else None,
        str(import_id).strip() if import_id not in (None, "") else None,
        1 if truthful_source else 0,
    )


def insert_entry(db_path: Path, entry: RawEntry, raw_file_path: str) -> None:
    source_session_id, source_conversation_id, import_id, truthful_source = _entry_index_values(entry)
    with closing(connect_sqlite(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO entries(
              entry_id, created_at, title, source, author_role, raw_file_path,
              entry_type, source_session_id, source_conversation_id, import_id, truthful_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id,
                entry.created_at,
                entry.title,
                entry.source,
                entry.author_role,
                raw_file_path,
                entry.entry_type,
                source_session_id,
                source_conversation_id,
                import_id,
                truthful_source,
            ),
        )
        conn.commit()


def _work_trace_searchable_text(event: WorkTraceEvent) -> str:
    parts: list[str] = [
        event.event_type,
        event.summary,
        event.project or "",
        event.source_surface or "",
        event.actor or "",
        event.session_key or "",
        event.task_id or "",
        " ".join(event.related_entry_ids),
        " ".join(event.related_artifact_ids),
        " ".join(event.related_paths),
        " ".join(event.tags),
        json.dumps(event.details, sort_keys=True) if isinstance(event.details, dict) else "",
    ]
    return "\n".join(part for part in parts if part)


def insert_work_trace_event(db_path: Path, event: WorkTraceEvent, work_file_path: str) -> None:
    with closing(connect_sqlite(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO work_trace_events(
              event_id, created_at, event_type, summary, project, source_surface, actor, session_key, task_id, searchable_text, work_file_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.created_at,
                event.event_type,
                event.summary,
                event.project,
                event.source_surface,
                event.actor,
                event.session_key,
                event.task_id,
                _work_trace_searchable_text(event),
                work_file_path,
            ),
        )
        searchable_text = _work_trace_searchable_text(event)
        conn.execute(
            "INSERT INTO work_trace_fts(event_id, searchable_text) VALUES (?, ?)",
            (event.event_id, searchable_text),
        )
        # Index entry links for efficient lookup
        for entry_id in event.related_entry_ids:
            conn.execute(
                "INSERT OR IGNORE INTO work_trace_entry_links(event_id, entry_id) VALUES (?, ?)",
                (event.event_id, entry_id),
            )
        conn.commit()


def insert_artifact(db_path: Path, artifact: Artifact) -> None:
    with closing(connect_sqlite(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO artifacts(artifact_id, entry_id, created_at, artifact_type, producer, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.artifact_id,
                artifact.entry_id,
                artifact.created_at,
                artifact.artifact_type,
                artifact.producer,
                artifact.content,
            ),
        )
        conn.commit()


def get_work_trace_row(db_path: Path, event_id: str) -> dict[str, Any] | None:
    with closing(connect_sqlite(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT event_id, created_at, event_type, summary, project, source_surface, actor, session_key, task_id, work_file_path
            FROM work_trace_events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
    return dict(row) if row else None


def list_work_trace_rows(
    db_path: Path,
    *,
    limit: int = 20,
    offset: int = 0,
    event_type: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if project:
        clauses.append("project = ?")
        params.append(project)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(connect_sqlite(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT event_id, created_at, event_type, summary, project, source_surface, actor, session_key, task_id, work_file_path
            FROM work_trace_events
            {where_sql}
            ORDER BY created_at DESC, event_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def search_work_trace(
    db_path: Path,
    *,
    query: str,
    limit: int = 20,
    event_type: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return []

    clauses: list[str] = ["work_trace_fts MATCH ?"]
    params: list[Any] = [_fts_match_query(terms)]
    if event_type:
        clauses.append("wte.event_type = ?")
        params.append(event_type)
    if project:
        clauses.append("wte.project = ?")
        params.append(project)
    where_sql = " AND ".join(clauses)

    with closing(connect_sqlite(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT wte.event_id, wte.created_at, wte.event_type, wte.summary, wte.project,
                   wte.source_surface, wte.actor, wte.session_key, wte.task_id,
                   wte.work_file_path, wte.searchable_text
            FROM work_trace_fts
            JOIN work_trace_events wte ON wte.event_id = work_trace_fts.event_id
            WHERE {where_sql}
            ORDER BY bm25(work_trace_fts), wte.created_at DESC, wte.event_id DESC
            LIMIT ?
            """,
            (*params, max(limit * 5, limit)),
        ).fetchall()

    scored: list[dict[str, Any]] = []
    lowered_query = query.lower()
    for row in rows:
        item = dict(row)
        text = str(item["searchable_text"]).lower()
        phrase_bonus = 100 if lowered_query in text else 0
        coverage = sum(1 for term in terms if term in text)
        frequency = sum(text.count(term) for term in terms)
        item["_score"] = phrase_bonus + (coverage * 10) + frequency
        scored.append(item)

    scored.sort(key=lambda r: (r["_score"], r["created_at"], r["event_id"]), reverse=True)
    return [{k: v for k, v in row.items() if k not in {"_score", "searchable_text"}} for row in scored[:limit]]

def list_work_trace_events_for_entry(db_path: Path, entry_id: str) -> list[dict[str, Any]]:
    """Fast lookup of work trace events linked to an entry via the junction index."""
    with closing(connect_sqlite(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT wte.event_id, wte.created_at, wte.event_type, wte.summary,
                   wte.project, wte.source_surface, wte.actor, wte.session_key,
                   wte.task_id, wte.work_file_path
            FROM work_trace_events wte
            JOIN work_trace_entry_links link ON link.event_id = wte.event_id
            WHERE link.entry_id = ?
            ORDER BY wte.created_at DESC, wte.event_id DESC
            """,
            (entry_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def backfill_work_trace_entry_links(db_path: Path) -> int:
    """Populate the junction table from existing work trace files. Returns count of links added."""
    import json
    from pathlib import Path as P
    count = 0
    with closing(connect_sqlite(db_path)) as conn:
        rows = conn.execute(
            "SELECT event_id, work_file_path FROM work_trace_events"
        ).fetchall()
        for event_id, work_file_path in rows:
            fp = P(work_file_path)
            if not fp.exists():
                continue
            try:
                body = json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                continue
            for entry_id in body.get("related_entry_ids", []):
                conn.execute(
                    "INSERT OR IGNORE INTO work_trace_entry_links(event_id, entry_id) VALUES (?, ?)",
                    (event_id, entry_id),
                )
                count += 1
        conn.commit()
    return count


def list_work_trace_counts_for_entries(db_path: Path, entry_ids: list[str]) -> dict[str, int]:
    """Return a map of entry_id -> work trace event count for the given entry IDs."""
    if not entry_ids:
        return {}
    with closing(connect_sqlite(db_path)) as conn:
        placeholders = ",".join(["?"] * len(entry_ids))
        rows = conn.execute(
            f"""
            SELECT link.entry_id, COUNT(link.event_id) AS cnt
            FROM work_trace_entry_links link
            WHERE link.entry_id IN ({placeholders})
            GROUP BY link.entry_id
            """,
            entry_ids,
        ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def search_memory(db_path: Path, query: str, limit: int = 20) -> list[dict[str, Any]]:
    # Search only the compressed memory index and return lightweight links.
    terms = _query_terms(query)
    if not terms:
        return []

    with closing(connect_sqlite(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
              mi.entry_id,
              mi.artifact_id,
              mi.created_at AS indexed_at,
              mi.memory_text AS match_text
            FROM memory_index_fts
            JOIN memory_index mi ON mi.id = memory_index_fts.rowid
            WHERE memory_index_fts MATCH ?
              AND NOT EXISTS (
                SELECT 1
                FROM memory_index newer
                WHERE newer.entry_id = mi.entry_id
                  AND (
                    newer.created_at > mi.created_at
                    OR (
                      newer.created_at = mi.created_at
                      AND COALESCE(newer.artifact_id, '') > COALESCE(mi.artifact_id, '')
                    )
                  )
              )
            ORDER BY bm25(memory_index_fts), mi.created_at DESC
            LIMIT ?
            """,
            (_fts_match_query(terms), max(limit * 5, limit)),
        ).fetchall()

    scored: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        text = str(item["match_text"]).lower()
        # Deterministic ranking: exact phrase > term coverage > frequency.
        phrase_bonus = 100 if query.lower() in text else 0
        coverage = sum(1 for term in terms if term in text)
        frequency = sum(text.count(term) for term in terms)
        item["_score"] = phrase_bonus + (coverage * 10) + frequency
        scored.append(item)

    scored.sort(key=lambda r: (r["_score"], r["indexed_at"]), reverse=True)
    return [{k: v for k, v in row.items() if k != "_score"} for row in scored[:limit]]

def get_entry_row(db_path: Path, entry_id: str) -> dict[str, Any] | None:
    with closing(connect_sqlite(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT entry_id, created_at, title, source, author_role, raw_file_path, entry_type, source_session_id, source_conversation_id, import_id, truthful_source
            FROM entries
            WHERE entry_id = ?
            """,
            (entry_id,),
        ).fetchone()
    return dict(row) if row else None


def _entry_filter_sql(
    *,
    source_conversation_id: str | None = None,
    source_session_id: str | None = None,
    import_id: str | None = None,
    truthful_only: bool = False,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if source_conversation_id:
        clauses.append("source_conversation_id = ?")
        params.append(source_conversation_id)
    if source_session_id:
        clauses.append("source_session_id = ?")
        params.append(source_session_id)
    if import_id:
        clauses.append("import_id = ?")
        params.append(import_id)
    if truthful_only:
        clauses.append("truthful_source = 1")
    return ("WHERE " + " AND ".join(clauses) if clauses else "", params)


def list_entry_rows(
    db_path: Path,
    *,
    limit: int = 20,
    offset: int = 0,
    source_conversation_id: str | None = None,
    source_session_id: str | None = None,
    import_id: str | None = None,
    truthful_only: bool = False,
) -> list[dict[str, Any]]:
    where_sql, params = _entry_filter_sql(
        source_conversation_id=source_conversation_id,
        source_session_id=source_session_id,
        import_id=import_id,
        truthful_only=truthful_only,
    )
    with closing(connect_sqlite(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT entry_id, created_at, source, author_role, raw_file_path, entry_type, source_session_id, source_conversation_id, import_id, truthful_source
            FROM entries
            {where_sql}
            ORDER BY created_at DESC, entry_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_entry_rows(
    db_path: Path,
    *,
    source_conversation_id: str | None = None,
    source_session_id: str | None = None,
    import_id: str | None = None,
    truthful_only: bool = False,
) -> int:
    where_sql, params = _entry_filter_sql(
        source_conversation_id=source_conversation_id,
        source_session_id=source_session_id,
        import_id=import_id,
        truthful_only=truthful_only,
    )
    with closing(connect_sqlite(db_path)) as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM entries {where_sql}", params).fetchone()
        return row[0] if row else 0


def insert_memory_index_row(
    db_path: Path,
    *,
    entry_id: str,
    artifact_id: str,
    created_at: str,
    memory_text: str,
) -> None:
    with closing(connect_sqlite(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO memory_index(entry_id, artifact_id, created_at, memory_text, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry_id, artifact_id, created_at, memory_text, None),
        )
        conn.execute(
            """
            INSERT INTO memory_index_fts(rowid, entry_id, artifact_id, created_at, memory_text)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cursor.lastrowid, entry_id, artifact_id, created_at, memory_text),
        )
        conn.commit()
