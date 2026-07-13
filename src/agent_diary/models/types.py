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
