from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_diary.models.types import (
    GraphEntity,
    GraphFact,
    GraphFactEvidence,
    GraphAssertionEvent,
    GraphExtractionJob,
    normalize_name,
    PREDICATE_REGISTRY,
)


# ── Entity operations ──────────────────────────────────────────────────


def insert_entity(db_path: Path, entity: GraphEntity) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO graph_entities(
                entity_id, canonical_name, normalized_name, entity_type,
                lifecycle_status, merged_into_entity_id,
                created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity.entity_id,
                entity.canonical_name,
                entity.normalized_name,
                entity.entity_type,
                entity.lifecycle_status,
                entity.merged_into_entity_id,
                entity.created_at,
                entity.updated_at,
                json.dumps(entity.metadata),
            ),
        )
        conn.commit()


def get_entity(db_path: Path, entity_id: str) -> dict[str, Any] | None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM graph_entities WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        return _row_to_entity(row) if row else None


def find_entity(db_path: Path, query_text: str, entity_type: str | None = None) -> list[dict[str, Any]]:
    """Search by canonical_name, normalized_name, or alias. Returns entity rows."""
    normalized = normalize_name(query_text)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # Direct name matches
        rows = conn.execute(
            """
            SELECT DISTINCT ge.* FROM graph_entities ge
            LEFT JOIN graph_entity_aliases a ON a.entity_id = ge.entity_id
            WHERE ge.normalized_name = ?
               OR ge.canonical_name LIKE ?
               OR a.normalized_alias = ?
            ORDER BY ge.canonical_name
            LIMIT 20
            """,
            (normalized, f"%{query_text}%", normalized),
        ).fetchall()
        results = [_row_to_entity(r) for r in rows]
        if entity_type:
            results = [r for r in results if r["entity_type"] == entity_type]
        return results


def update_entity(db_path: Path, entity_id: str, updates: dict[str, Any]) -> None:
    set_clauses: list[str] = []
    params: list[Any] = []
    for key, value in updates.items():
        if key == "metadata":
            set_clauses.append(f"{key} = ?")
            params.append(json.dumps(value))
        elif key in ("canonical_name", "entity_type", "lifecycle_status", "merged_into_entity_id"):
            set_clauses.append(f"{key} = ?")
            params.append(value)
    if not set_clauses:
        return
    set_clauses.append("updated_at = ?")
    params.append(_now())
    params.append(entity_id)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            f"UPDATE graph_entities SET {', '.join(set_clauses)} WHERE entity_id = ?",
            params,
        )
        conn.commit()


def merge_entity(db_path: Path, entity_id: str, target_entity_id: str) -> None:
    """Mark entity as merged into target."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        now = _now()
        conn.execute(
            "UPDATE graph_entities SET lifecycle_status='merged', merged_into_entity_id=?, updated_at=? WHERE entity_id=?",
            (target_entity_id, now, entity_id),
        )
        conn.commit()


def list_entities(db_path: Path, entity_type: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if entity_type:
            rows = conn.execute(
                "SELECT * FROM graph_entities WHERE entity_type = ? AND lifecycle_status = 'active' ORDER BY canonical_name LIMIT ?",
                (entity_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM graph_entities WHERE lifecycle_status = 'active' ORDER BY canonical_name LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_entity(r) for r in rows]


def _row_to_entity(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["metadata"] = json.loads(d["metadata"]) if isinstance(d.get("metadata"), str) else d.get("metadata", {})
    except (json.JSONDecodeError, TypeError):
        d["metadata"] = {}
    return d


# ── Alias operations ───────────────────────────────────────────────────


def insert_alias(db_path: Path, alias_id: str, entity_id: str, alias: str, source_kind: str, source_id: str, created_at: str | None = None) -> None:
    normalized = normalize_name(alias)
    now = created_at or _now()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT OR IGNORE INTO graph_entity_aliases(alias_id, entity_id, alias, normalized_alias, source_kind, source_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (alias_id, entity_id, alias, normalized, source_kind, source_id, now),
        )
        conn.commit()


def list_aliases(db_path: Path, entity_id: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM graph_entity_aliases WHERE entity_id = ? ORDER BY created_at", (entity_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_obvious_alias(db_path: Path, alias_text: str) -> str | None:
    """If normalized alias maps to exactly one entity, return its id. Otherwise None."""
    normalized = normalize_name(alias_text)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # Check direct canonical match first
        row = conn.execute(
            "SELECT entity_id FROM graph_entities WHERE normalized_name = ? AND lifecycle_status = 'active' LIMIT 2",
            (normalized,),
        ).fetchall()
        if len(row) == 1:
            return row[0][0]

        # Check alias table
        alias_rows = conn.execute(
            """
            SELECT DISTINCT e.entity_id FROM graph_entity_aliases a
            JOIN graph_entities e ON e.entity_id = a.entity_id
            WHERE a.normalized_alias = ? AND e.lifecycle_status = 'active'
            LIMIT 2
            """,
            (normalized,),
        ).fetchall()
        if len(alias_rows) == 1:
            return alias_rows[0][0]
    return None


# ── Fact operations ────────────────────────────────────────────────────


def insert_fact(db_path: Path, fact: GraphFact) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO graph_facts(
                fact_id, subject_entity_id, predicate, object_kind,
                object_entity_id, object_value, object_value_type,
                state, valid_from, valid_to, time_precision, time_basis, confidence,
                superseded_by_fact_id, retracted_by_event_id,
                created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.fact_id, fact.subject_entity_id, fact.predicate, fact.object_kind,
                fact.object_entity_id, fact.object_value, fact.object_value_type,
                fact.state, fact.valid_from, fact.valid_to,
                fact.time_precision, fact.time_basis, fact.confidence,
                fact.superseded_by_fact_id, fact.retracted_by_event_id,
                fact.created_at, fact.updated_at, json.dumps(fact.metadata),
            ),
        )
        conn.commit()
    return fact.fact_id


def get_fact(db_path: Path, fact_id: str) -> dict[str, Any] | None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM graph_facts WHERE fact_id = ?", (fact_id,)).fetchone()
        return _row_to_fact(row) if row else None


def get_current_facts_for_subject(db_path: Path, subject_entity_id: str, predicate: str | None = None) -> list[dict[str, Any]]:
    """Get current facts for a subject. Single-value predicates return at most one."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if predicate:
            rows = conn.execute(
                "SELECT * FROM graph_facts WHERE subject_entity_id = ? AND predicate = ? AND state = 'current' ORDER BY valid_from DESC",
                (subject_entity_id, predicate),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM graph_facts WHERE subject_entity_id = ? AND state = 'current' ORDER BY predicate, valid_from DESC",
                (subject_entity_id,),
            ).fetchall()
        return [_row_to_fact(r) for r in rows]


def get_facts_for_entity(
    db_path: Path, entity_id: str,
    states: list[str] | None = None, include_history: bool = False,
) -> list[dict[str, Any]]:
    """Facts where entity is subject or object."""
    if not states:
        states = ["current"]
    placeholders = ",".join("?" for _ in states)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if include_history:
            rows = conn.execute(
                f"""
                SELECT * FROM graph_facts
                WHERE (subject_entity_id = ? OR object_entity_id = ?)
                  AND state IN ({placeholders})
                ORDER BY predicate, valid_from DESC
                """,
                (entity_id, entity_id, *states),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT * FROM graph_facts
                WHERE (subject_entity_id = ? OR object_entity_id = ?)
                  AND state IN ({placeholders})
                  AND valid_to IS NULL
                ORDER BY predicate, valid_from DESC
                """,
                (entity_id, entity_id, *states),
            ).fetchall()
        return [_row_to_fact(r) for r in rows]


def close_current_fact(db_path: Path, subject_entity_id: str, predicate: str, valid_to: str | None = None) -> str | None:
    """Close the current fact for a single-value predicate. Returns old fact_id or None."""
    now = valid_to or _now()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT fact_id FROM graph_facts WHERE subject_entity_id = ? AND predicate = ? AND state = 'current' LIMIT 1",
            (subject_entity_id, predicate),
        )
        row = cur.fetchone()
        if row is None:
            return None
        old_id = row[0]
        conn.execute(
            "UPDATE graph_facts SET state='historical', valid_to=?, updated_at=? WHERE fact_id=?",
            (now, now, old_id),
        )
        conn.commit()
        return old_id


def supersede_fact(db_path: Path, old_fact_id: str, new_fact_id: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        now = _now()
        conn.execute(
            "UPDATE graph_facts SET superseded_by_fact_id=?, updated_at=? WHERE fact_id=?",
            (new_fact_id, now, old_fact_id),
        )
        conn.commit()


def retract_fact(db_path: Path, fact_id: str, retracted_by_event_id: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        now = _now()
        conn.execute(
            "UPDATE graph_facts SET state='retracted', retracted_by_event_id=?, updated_at=? WHERE fact_id=?",
            (retracted_by_event_id, now, fact_id),
        )
        conn.commit()


def search_facts(db_path: Path, query_text: str, states: list[str] | None = None) -> list[dict[str, Any]]:
    """Full-text-like search across facts by predicate name or neighbor entity name."""
    if not states:
        states = ["current"]
    placeholders = ",".join("?" for _ in states)
    normalized = normalize_name(query_text)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT DISTINCT f.* FROM graph_facts f
            LEFT JOIN graph_entities sub ON sub.entity_id = f.subject_entity_id
            LEFT JOIN graph_entities obj ON obj.entity_id = f.object_entity_id
            WHERE f.state IN ({placeholders})
              AND (
                  f.predicate LIKE ?
                  OR sub.normalized_name LIKE ?
                  OR obj.normalized_name LIKE ?
                  OR f.object_value LIKE ?
              )
            ORDER BY f.valid_from DESC
            LIMIT 20
            """,
            (*states, f"%{query_text}%", f"%{normalized}%", f"%{normalized}%", f"%{query_text}%"),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]


def _row_to_fact(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["metadata"] = json.loads(d["metadata"]) if isinstance(d.get("metadata"), str) else d.get("metadata", {})
    except (json.JSONDecodeError, TypeError):
        d["metadata"] = {}
    return d


# ── Evidence operations ────────────────────────────────────────────────


def insert_evidence(db_path: Path, evidence: GraphFactEvidence) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO graph_fact_evidence(
                evidence_id, fact_id, source_kind, source_id, source_timestamp,
                role, weight, extractor_method, extractor_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.evidence_id, evidence.fact_id,
                evidence.source_kind, evidence.source_id, evidence.source_timestamp,
                evidence.role, evidence.weight,
                evidence.extractor_method, evidence.extractor_version,
                evidence.created_at,
            ),
        )
        conn.commit()


def get_evidence_for_fact(db_path: Path, fact_id: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM graph_fact_evidence WHERE fact_id = ? ORDER BY source_timestamp",
            (fact_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Assertion event operations ─────────────────────────────────────────


def insert_assertion_event(db_path: Path, event: GraphAssertionEvent) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO graph_assertion_events(event_id, event_type, author, reason, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id, event.event_type, event.author,
                event.reason, json.dumps(event.payload),
                event.created_at,
            ),
        )
        conn.commit()


def get_entity_assertion_history(db_path: Path, entity_id: str) -> list[dict[str, Any]]:
    """Get assertion events related to an entity via fact payloads."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM graph_assertion_events
            WHERE payload LIKE ?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (f"%{entity_id}%",),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Extraction job operations ──────────────────────────────────────────


def insert_extraction_job(db_path: Path, job: GraphExtractionJob) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                INSERT INTO graph_extraction_jobs(
                    job_id, source_kind, source_id, source_timestamp,
                    status, attempt_count, lease_owner, lease_expiry,
                    last_error, extractor_method, result_summary,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id, job.source_kind, job.source_id, job.source_timestamp,
                    job.status, job.attempt_count,
                    job.lease_owner, job.lease_expiry,
                    job.last_error, job.extractor_method, job.result_summary,
                    job.created_at, job.updated_at,
                ),
            )
            conn.commit()
            return job.job_id
        except sqlite3.IntegrityError:
            # UNIQUE(source_kind, source_id) violation -> already enqueued
            return _get_existing_job_id(db_path, job.source_kind, job.source_id)


def _get_existing_job_id(db_path: Path, source_kind: str, source_id: str) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT job_id FROM graph_extraction_jobs WHERE source_kind = ? AND source_id = ?",
            (source_kind, source_id),
        ).fetchone()
    return row[0] if row else ""


def claim_extraction_jobs(
    db_path: Path, limit: int, worker_id: str, lease_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Claim pending extraction jobs atomically."""
    expiry = _now_offset(lease_seconds)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM graph_extraction_jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            conn.execute("ROLLBACK")
            return []
        job_ids = [r[0] for r in rows]
        conn.execute(
            f"""
            UPDATE graph_extraction_jobs
            SET status='claimed', lease_owner=?, lease_expiry=?, attempt_count=attempt_count+1, updated_at=?
            WHERE job_id IN ({','.join('?' for _ in job_ids)})
            """,
            (worker_id, expiry, _now(), *job_ids),
        )
        conn.commit()
        return [_row_to_extraction_job(r) for r in rows]


def update_extraction_job(db_path: Path, job_id: str, status: str, last_error: str | None = None, result_summary: str | None = None) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            UPDATE graph_extraction_jobs
            SET status=?, last_error=?, result_summary=?, lease_owner=NULL, lease_expiry=NULL, updated_at=?
            WHERE job_id=?
            """,
            (status, last_error, result_summary, _now(), job_id),
        )
        conn.commit()


def fail_extraction_job(db_path: Path, job_id: str, error: str, retryable: bool = True) -> None:
    new_status = "pending" if retryable else "failed"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            UPDATE graph_extraction_jobs
            SET status=?, last_error=?, lease_owner=NULL, lease_expiry=NULL, updated_at=?
            WHERE job_id=?
            """,
            (new_status, error, _now(), job_id),
        )
        conn.commit()


def release_stale_leases(db_path: Path) -> int:
    """Reset expired claims back to pending. Returns count of released jobs."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "UPDATE graph_extraction_jobs SET status='pending', lease_owner=NULL, lease_expiry=NULL, updated_at=? WHERE status='claimed' AND lease_expiry < ?",
            (now, now),
        )
        conn.commit()
        return cur.rowcount


def get_extraction_queue_status(db_path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, count(*) as cnt FROM graph_extraction_jobs GROUP BY status",
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        for s in ("pending", "claimed", "succeeded", "no_facts", "failed"):
            counts.setdefault(s, 0)
        return counts


def enqueue_source_if_missing(db_path: Path, source_kind: str, source_id: str, source_timestamp: str) -> str:
    """Enqueue a source for extraction only if not already enqueued. Returns job_id."""
    # Check existing
    existing = _get_existing_job_id(db_path, source_kind, source_id)
    if existing:
        return existing
    job = GraphExtractionJob(
        source_kind=source_kind,
        source_id=source_id,
        source_timestamp=source_timestamp,
    )
    return insert_extraction_job(db_path, job)


def _row_to_extraction_job(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ── Neighborhood queries ───────────────────────────────────────────────


def get_neighbors(
    db_path: Path, entity_id: str, states: list[str] | None = None, max_distance: int = 1,
) -> dict[str, Any]:
    """Return adjacent entities + facts. Distance >1 not implemented in v1."""
    if not states:
        states = ["current"]
    placeholders = ",".join("?" for _ in states)

    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # Facts where entity is subject
        subject_facts = conn.execute(
            f"""
            SELECT f.*, obj.canonical_name as object_name, obj.entity_type as object_type
            FROM graph_facts f
            LEFT JOIN graph_entities obj ON obj.entity_id = f.object_entity_id
            WHERE f.subject_entity_id = ? AND f.state IN ({placeholders})
            ORDER BY f.predicate
            """,
            (entity_id, *states),
        ).fetchall()

        # Facts where entity is object
        object_facts = conn.execute(
            f"""
            SELECT f.*, sub.canonical_name as subject_name, sub.entity_type as subject_type
            FROM graph_facts f
            JOIN graph_entities sub ON sub.entity_id = f.subject_entity_id
            WHERE f.object_entity_id = ? AND f.state IN ({placeholders})
            ORDER BY f.predicate
            """,
            (entity_id, *states),
        ).fetchall()

    return {
        "center_entity_id": entity_id,
        "outgoing": [_row_to_fact(r) for r in subject_facts],
        "incoming": [_row_to_fact(r) for r in object_facts],
    }


# ── Helpers ────────────────────────────────────────────────────────────


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _now_offset(seconds: int) -> str:
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def valid_predicate(predicate: str) -> bool:
    return predicate in PREDICATE_REGISTRY