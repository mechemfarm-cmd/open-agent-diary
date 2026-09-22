from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import json
import sqlite3
from typing import Any

from agent_diary.config import Paths


@dataclass(frozen=True)
class EvidenceRef:
    source_kind: str
    source_id: str
    timestamp: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "timestamp": self.timestamp,
            "role": self.role,
        }


@dataclass(frozen=True)
class SemanticCandidate:
    source_kind: str
    source_id: str
    subject: str
    text: str
    timestamp: str
    status: str
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    author_role: str | None = None
    confidence: str | None = None
    volatility: str | None = None
    provisional: bool = False
    superseded_by: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "subject": self.subject,
            "text": self.text,
            "timestamp": self.timestamp,
            "status": self.status,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "author_role": self.author_role,
            "confidence": self.confidence,
            "volatility": self.volatility,
            "provisional": self.provisional,
            "superseded_by": self.superseded_by,
            "predicate": self.predicate,
            "object_value": self.object_value,
            "metadata": self.metadata,
        }


def collect_semantic_candidates(paths: Paths, *, topic: str, limit: int = 20) -> list[SemanticCandidate]:
    """Return bounded read-only semantic candidates from existing derived layers."""
    bounded_limit = max(0, int(limit))
    if bounded_limit == 0:
        return []
    candidates = [
        *_graph_fact_candidates(paths.sqlite_path, topic=topic, limit=bounded_limit),
        *_work_trace_candidates(paths.sqlite_path, topic=topic, limit=bounded_limit),
        *_raw_entry_candidates(paths.sqlite_path, topic=topic, limit=bounded_limit),
    ]
    candidates.sort(key=lambda c: (c.timestamp, c.source_kind, c.source_id), reverse=True)
    return candidates[:bounded_limit]


def _topic_match(topic: str, *values: Any) -> bool:
    needle = " ".join(str(topic).lower().split())
    if not needle:
        return True
    haystack = "\n".join(str(v or "") for v in values).lower()
    return needle in haystack


def _load_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        loaded = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _graph_fact_candidates(db_path, *, topic: str, limit: int) -> list[SemanticCandidate]:
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT f.fact_id, f.predicate, f.object_kind, f.object_value, f.state,
                   f.valid_from, f.valid_to, f.confidence, f.superseded_by_fact_id,
                   f.metadata, sub.canonical_name AS subject_name,
                   obj.canonical_name AS object_name
            FROM graph_facts f
            LEFT JOIN graph_entities sub ON sub.entity_id = f.subject_entity_id
            LEFT JOIN graph_entities obj ON obj.entity_id = f.object_entity_id
            WHERE f.state IN ('current', 'historical', 'planned', 'retracted')
            ORDER BY f.valid_from DESC, f.fact_id DESC
            LIMIT ?
            """,
            (max(limit * 4, limit),),
        ).fetchall()
        evidence_rows = conn.execute(
            """
            SELECT fact_id, source_kind, source_id, source_timestamp, role
            FROM graph_fact_evidence
            ORDER BY source_timestamp, evidence_id
            """
        ).fetchall()

    evidence_by_fact: dict[str, list[EvidenceRef]] = {}
    for row in evidence_rows:
        evidence_by_fact.setdefault(str(row["fact_id"]), []).append(
            EvidenceRef(
                source_kind=str(row["source_kind"]),
                source_id=str(row["source_id"]),
                timestamp=str(row["source_timestamp"]) if row["source_timestamp"] is not None else None,
                role=str(row["role"]) if row["role"] is not None else None,
            )
        )

    out: list[SemanticCandidate] = []
    for row in rows:
        subject = str(row["subject_name"] or row["fact_id"])
        obj = str(row["object_name"] or row["object_value"] or "")
        predicate_text = str(row["predicate"] or "").lower().replace("_", " ")
        text = " ".join(part for part in [subject, predicate_text, obj] if part).strip()
        if not _topic_match(topic, subject, obj, text, row["predicate"]):
            continue
        meta = _load_json_object(row["metadata"])
        belief = meta.get("belief") if isinstance(meta.get("belief"), dict) else {}
        semantic_value = meta.get("semantic")
        semantic_meta = semantic_value if isinstance(semantic_value, dict) else {}
        semantic_role = semantic_meta.get("role") or meta.get("semantic_role") or meta.get("role")
        candidate_meta: dict[str, Any] = {"belief": belief} if belief else {}
        if semantic_role:
            candidate_meta["semantic_role"] = str(semantic_role)
        out.append(
            SemanticCandidate(
                source_kind="graph_fact",
                source_id=str(row["fact_id"]),
                subject=subject,
                text=text,
                timestamp=str(row["valid_from"]),
                status=str(row["state"]),
                evidence_refs=evidence_by_fact.get(str(row["fact_id"]), []),
                confidence=str(row["confidence"]) if row["confidence"] is not None else None,
                provisional=bool(belief.get("provisional", False)),
                superseded_by=str(row["superseded_by_fact_id"]) if row["superseded_by_fact_id"] else None,
                predicate=str(row["predicate"] or ""),
                object_value=obj,
                metadata=candidate_meta,
            )
        )
        if len(out) >= limit:
            break
    return out


def _work_trace_candidates(db_path, *, topic: str, limit: int) -> list[SemanticCandidate]:
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT event_id, created_at, event_type, summary, project,
                   source_surface, actor, session_key, task_id
            FROM work_trace_events
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            (max(limit * 4, limit),),
        ).fetchall()
    out: list[SemanticCandidate] = []
    for row in rows:
        subject = str(row["project"] or row["event_type"] or "work trace")
        text = str(row["summary"] or "")
        if not _topic_match(topic, subject, text, row["event_type"], row["actor"], row["session_key"], row["task_id"]):
            continue
        out.append(
            SemanticCandidate(
                source_kind="work_trace",
                source_id=str(row["event_id"]),
                subject=subject,
                text=text,
                timestamp=str(row["created_at"]),
                status="event",
                evidence_refs=[
                    EvidenceRef(
                        source_kind="work_trace",
                        source_id=str(row["event_id"]),
                        timestamp=str(row["created_at"]),
                        role=str(row["event_type"] or "event"),
                    )
                ],
                author_role=str(row["actor"] or row["source_surface"] or "unknown"),
                confidence="observed",
                metadata={"event_type": str(row["event_type"] or ""), "source_surface": str(row["source_surface"] or "")},
            )
        )
        if len(out) >= limit:
            break
    return out


def _raw_entry_candidates(db_path, *, topic: str, limit: int) -> list[SemanticCandidate]:
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT entry_id, created_at, title, source, author_role, raw_file_path,
                   entry_type, source_session_id, source_conversation_id, import_id, truthful_source
            FROM entries
            ORDER BY created_at DESC, entry_id DESC
            LIMIT ?
            """,
            (max(limit * 4, limit),),
        ).fetchall()
    out: list[SemanticCandidate] = []
    for row in rows:
        body = _read_json_file(row["raw_file_path"])
        metadata_value = body.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        semantic_value = metadata.get("semantic")
        semantic = semantic_value if isinstance(semantic_value, dict) else {}
        title = str(body.get("title") or row["title"] or "").strip()
        content = str(body.get("content") or "").strip()
        subject = str(semantic.get("subject") or title or row["source_conversation_id"] or row["source_session_id"] or row["source"] or "raw entry")
        if not _topic_match(topic, subject, title, content, row["source"], row["entry_type"], row["source_session_id"], row["source_conversation_id"]):
            continue
        semantic_role = semantic.get("role") or metadata.get("semantic_role") or metadata.get("role")
        candidate_meta = {
            "source": str(row["source"] or ""),
            "entry_type": str(row["entry_type"] or ""),
            "truthful_source": bool(row["truthful_source"]),
        }
        if semantic_role:
            candidate_meta["semantic_role"] = str(semantic_role)
        out.append(
            SemanticCandidate(
                source_kind="raw_entry",
                source_id=str(row["entry_id"]),
                subject=subject,
                text=content or title or str(row["entry_id"]),
                timestamp=str(row["created_at"]),
                status="current",
                evidence_refs=[
                    EvidenceRef(
                        source_kind="raw_entry",
                        source_id=str(row["entry_id"]),
                        timestamp=str(row["created_at"]),
                        role=str(semantic_role or row["author_role"] or "source"),
                    )
                ],
                author_role=str(row["author_role"] or "unknown"),
                confidence="source-linked" if row["truthful_source"] else None,
                metadata=candidate_meta,
            )
        )
        if len(out) >= limit:
            break
    return out


def _read_json_file(path_value: Any) -> dict[str, Any]:
    if not path_value:
        return {}
    try:
        with open(str(path_value), "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
