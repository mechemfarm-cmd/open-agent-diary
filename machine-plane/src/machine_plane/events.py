"""The machine-plane event record.

Every event carries its own provenance — producer, producer version, platform,
capabilities at capture, privacy mode, coverage state and confidence — so that
downstream code can never treat a partial Windows or macOS event as equivalent
to a richer Linux one.

File and network semantics are kept distinct on purpose. A single
``file_changed`` would erase the difference between opening a file for writing
and actually modifying its contents, and that difference is exactly what makes
an effect worth investigating.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .capabilities import CapabilityReport
from .identity import IdentityConfidence, ProcessIdentity
from .privacy import FORBIDDEN_CONTENT_KEYS

SCHEMA_VERSION = 1


class SchemaMismatch(Exception):
    """Raised when a record's schema version is not one we can read.

    Ingestion fails closed. Skipping an unreadable record would make it
    indistinguishable from an event that never happened.
    """


class EventKind(str, Enum):
    ACTION = "action"
    GAP = "gap"


class EventType(str, Enum):
    # Observed effects
    EXEC = "exec"
    OPEN = "open"
    READ_ATTEMPT = "read_attempt"
    WRITE_ATTEMPT = "write_attempt"
    CREATE = "create"
    RENAME = "rename"
    UNLINK = "unlink"
    METADATA_CHANGE = "metadata_change"
    CONNECT = "connect"

    # Recorder lifecycle and coverage gaps
    RECORDER_STARTED = "recorder_started"
    RECORDER_STOPPED = "recorder_stopped"
    BUFFER_OVERFLOW = "buffer_overflow"
    PERMISSION_GAP = "permission_gap"
    UNSUPPORTED_KERNEL_FEATURE = "unsupported_kernel_feature"
    SINK_UNAVAILABLE = "sink_unavailable"
    CLOCK_JUMP = "clock_jump"


ACTION_EVENT_TYPES = frozenset(
    {
        EventType.EXEC,
        EventType.OPEN,
        EventType.READ_ATTEMPT,
        EventType.WRITE_ATTEMPT,
        EventType.CREATE,
        EventType.RENAME,
        EventType.UNLINK,
        EventType.METADATA_CHANGE,
        EventType.CONNECT,
    }
)

GAP_EVENT_TYPES = frozenset(
    {
        EventType.RECORDER_STARTED,
        EventType.RECORDER_STOPPED,
        EventType.BUFFER_OVERFLOW,
        EventType.PERMISSION_GAP,
        EventType.UNSUPPORTED_KERNEL_FEATURE,
        EventType.SINK_UNAVAILABLE,
        EventType.CLOCK_JUMP,
    }
)

#: File event types whose ``target`` is a path.
PATH_EVENT_TYPES = frozenset(
    {
        EventType.OPEN,
        EventType.READ_ATTEMPT,
        EventType.WRITE_ATTEMPT,
        EventType.CREATE,
        EventType.RENAME,
        EventType.UNLINK,
        EventType.METADATA_CHANGE,
    }
)


class PrivacyMode(str, Enum):
    METADATA_ONLY = "metadata_only"
    CONTENT_BEARING = "content_bearing"


class CoverageState(str, Enum):
    COVERED = "covered"
    GAP = "gap"
    UNKNOWN = "unknown"


def event_kind(event_type: EventType) -> EventKind:
    return EventKind.GAP if event_type in GAP_EVENT_TYPES else EventKind.ACTION


@dataclass(frozen=True)
class MachineEvent:
    """One observed machine event, or one explicit gap in coverage."""

    type: EventType
    producer: str
    producer_version: str
    platform: str
    monotonic_time: float
    wall_time: float
    privacy_mode: PrivacyMode = PrivacyMode.METADATA_ONLY
    coverage_state: CoverageState = CoverageState.COVERED
    identity_confidence: IdentityConfidence = IdentityConfidence.CERTAIN
    identity: ProcessIdentity | None = None
    capabilities_at_capture: tuple[CapabilityReport, ...] = ()
    target: str | None = None
    result: str | None = None
    platform_details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SchemaMismatch(
                f"event schema version {self.schema_version} "
                f"is not readable by this build ({SCHEMA_VERSION})"
            )
        if event_kind(self.type) is EventKind.ACTION and self.identity is None:
            raise ValueError(
                f"{self.type.value} is an action event and requires a process identity"
            )

    def content_violations(self) -> tuple[str, ...]:
        """Detail keys that carry content rather than shape."""
        if self.privacy_mode is PrivacyMode.CONTENT_BEARING:
            return ()
        return tuple(
            sorted(
                key
                for key in self.platform_details
                if key.lower() in FORBIDDEN_CONTENT_KEYS
            )
        )

    def validate_for_storage(self) -> None:
        """Guard the boundary, not the constructor.

        Construction happens *before* redaction, so a producer must be able to
        build an event that still carries a command line and then have the
        pipeline strip it. Enforcing the metadata-only guarantee at
        construction would make the redaction stage unreachable — which is the
        bug this method exists to avoid. The guarantee belongs at the point of
        writing.
        """
        violations = self.content_violations()
        if violations:
            raise ValueError(
                "metadata-only event carries content-bearing detail keys: "
                + ", ".join(violations)
            )

    @property
    def kind(self) -> EventKind:
        return event_kind(self.type)

    @property
    def event_id(self) -> str:
        """Content-derived id: identical content yields an identical id."""
        canonical = json.dumps(
            self._payload(include_event_id=False),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _payload(self, *, include_event_id: bool) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "type": self.type.value,
            "producer": self.producer,
            "producer_version": self.producer_version,
            "platform": self.platform,
            "privacy_mode": self.privacy_mode.value,
            "coverage_state": self.coverage_state.value,
            "identity_confidence": self.identity_confidence.value,
            "identity": self.identity.to_dict() if self.identity else None,
            "capabilities_at_capture": [r.to_dict() for r in self.capabilities_at_capture],
            "monotonic_time": self.monotonic_time,
            "wall_time": self.wall_time,
            "target": self.target,
            "result": self.result,
            "platform_details": dict(self.platform_details),
        }
        if include_event_id:
            payload["event_id"] = self.event_id
        return payload

    def to_dict(self) -> dict:
        return self._payload(include_event_id=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: dict) -> "MachineEvent":
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaMismatch(
                f"record schema version {version!r} is not readable by this "
                f"build ({SCHEMA_VERSION})"
            )
        return cls(
            type=EventType(payload["type"]),
            producer=payload["producer"],
            producer_version=payload["producer_version"],
            platform=payload["platform"],
            monotonic_time=payload["monotonic_time"],
            wall_time=payload["wall_time"],
            privacy_mode=PrivacyMode(payload.get("privacy_mode", "metadata_only")),
            coverage_state=CoverageState(payload.get("coverage_state", "covered")),
            identity_confidence=IdentityConfidence(
                payload.get("identity_confidence", "certain")
            ),
            identity=(
                ProcessIdentity.from_dict(payload["identity"])
                if payload.get("identity")
                else None
            ),
            capabilities_at_capture=tuple(
                CapabilityReport.from_dict(item)
                for item in payload.get("capabilities_at_capture", [])
            ),
            target=payload.get("target"),
            result=payload.get("result"),
            platform_details=payload.get("platform_details") or {},
        )

    @classmethod
    def from_json(cls, line: str) -> "MachineEvent":
        return cls.from_dict(json.loads(line))
