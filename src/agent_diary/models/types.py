from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


Metadata = dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RawEntry:
    entry_type: str
    source: str
    author_role: str
    content: str
    created_at: str = field(default_factory=now_iso)
    metadata: Metadata = field(default_factory=dict)
    title: str | None = None
    entry_id: str = field(default_factory=lambda: f"entry_{uuid4().hex}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkTraceEvent:
    event_type: str
    summary: str
    created_at: str = field(default_factory=now_iso)
    project: str | None = None
    source_surface: str | None = None
    actor: str | None = None
    session_key: str | None = None
    task_id: str | None = None
    details: Metadata = field(default_factory=dict)
    related_entry_ids: list[str] = field(default_factory=list)
    related_artifact_ids: list[str] = field(default_factory=list)
    related_paths: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: f"work_{uuid4().hex}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Artifact:
    """Secondary interpreted data linked to authoritative raw entries.

    For future derived analysis artifacts, keep lineage/method hints in `metadata`
    (for example: `source_entry_ids`, `schema_version`, `method`, `generated_at`).
    """
    entry_id: str
    artifact_type: str
    producer: str
    content: str
    created_at: str = field(default_factory=now_iso)
    metadata: Metadata = field(default_factory=dict)
    artifact_id: str = field(default_factory=lambda: f"artifact_{uuid4().hex}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Overlay:
    entry_id: str
    overlay_type: str
    author: str
    content: str
    created_at: str = field(default_factory=now_iso)
    metadata: Metadata = field(default_factory=dict)
    overlay_id: str = field(default_factory=lambda: f"overlay_{uuid4().hex}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Knowledge Graph types ─────────────────────────────────────────────

PREDICATE_REGISTRY: dict[str, dict[str, Any]] = {
    "OWNS":           {"display": "Owns",         "cardinality": "multi"},
    "RUNS_ON":        {"display": "Runs on",      "cardinality": "single"},
    "USES":           {"display": "Uses",         "cardinality": "multi"},
    "CONNECTED_TO":   {"display": "Connected to", "cardinality": "multi"},
    "MEMBER_OF":      {"display": "Member of",    "cardinality": "multi"},
    "WORKS_ON":       {"display": "Works on",     "cardinality": "multi"},
    "LOCATED_IN":     {"display": "Located in",   "cardinality": "single"},
    "HAS_RAM":        {"display": "Has RAM",      "cardinality": "value"},
    "HAS_IP_ADDRESS": {"display": "Has IP",       "cardinality": "value"},
    "HAS_OS":         {"display": "Has OS",       "cardinality": "value"},
}


def normalize_name(name: str) -> str:
    """Lowercase, strip, and collapse internal whitespace for matching."""
    return " ".join(name.lower().split())


@dataclass
class GraphEntity:
    entity_id: str = field(default_factory=lambda: f"ge_{uuid4().hex}")
    canonical_name: str = ""
    normalized_name: str = ""
    entity_type: str = "other"
    lifecycle_status: str = "active"
    merged_into_entity_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: Metadata = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.normalized_name and self.canonical_name:
            self.normalized_name = normalize_name(self.canonical_name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphFact:
    fact_id: str = field(default_factory=lambda: f"gf_{uuid4().hex}")
    subject_entity_id: str = ""
    predicate: str = ""
    object_kind: str = "entity"
    object_entity_id: str | None = None
    object_value: str | None = None
    object_value_type: str | None = None
    state: str = "current"
    valid_from: str = field(default_factory=now_iso)
    valid_to: str | None = None
    time_precision: str = "source"
    time_basis: str = "source_timestamp"
    confidence: str = "medium"
    superseded_by_fact_id: str | None = None
    retracted_by_event_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: Metadata = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphFactEvidence:
    evidence_id: str = field(default_factory=lambda: f"gev_{uuid4().hex}")
    fact_id: str = ""
    source_kind: str = ""
    source_id: str = ""
    source_timestamp: str = field(default_factory=now_iso)
    role: str = "establishes"
    weight: float = 1.0
    extractor_method: str | None = None
    extractor_version: str | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphAssertionEvent:
    event_id: str = field(default_factory=lambda: f"ga_{uuid4().hex}")
    event_type: str = ""
    author: str = ""
    reason: str | None = None
    payload: Metadata = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphExtractionJob:
    job_id: str = field(default_factory=lambda: f"gx_{uuid4().hex}")
    source_kind: str = ""
    source_id: str = ""
    source_timestamp: str = field(default_factory=now_iso)
    status: str = "pending"
    attempt_count: int = 0
    lease_owner: str | None = None
    lease_expiry: str | None = None
    last_error: str | None = None
    extractor_method: str | None = None
    result_summary: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
