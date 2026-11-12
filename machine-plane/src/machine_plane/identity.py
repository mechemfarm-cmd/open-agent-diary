"""Neutral host and process identity for the machine plane.

The core schema must not name Linux concepts. cgroups do not exist on macOS, and
Windows uses job objects, so identity is expressed as a neutral
``isolation_domain`` with a type and a *nestable chain*, and producer-specific
detail is preserved alongside it rather than flattened into it.

Reducing identity to the lowest common denominator would produce a portable
format that has lost the evidence needed to explain what happened.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DomainType(str, Enum):
    """The kind of isolation boundary a process sits inside."""

    CGROUP = "cgroup"
    JOB_OBJECT = "job_object"
    ENDPOINT_SECURITY_PROCESS_GROUP = "endpoint_security_process_group"
    CONTAINER = "container"
    VM = "vm"
    NONE = "none"
    UNKNOWN = "unknown"


class IdentityConfidence(str, Enum):
    """How well the producer could attribute the event to a process.

    This is the producer's confidence in its own attribution, and is distinct
    from a correlation verdict. An event can be attributed with certainty and
    still never match an agent claim.
    """

    CERTAIN = "certain"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IsolationDomain:
    """One isolation boundary, optionally nested inside another.

    A process may sit inside ``VM -> container -> cgroup -> process`` on Linux,
    or ``host -> job object -> process`` on Windows. ``parent`` expresses that
    nesting so the schema never has to pretend a single flat identifier is
    enough.
    """

    type: DomainType
    id: str | None = None
    scope: str | None = None
    platform: str | None = None
    parent: "IsolationDomain | None" = None

    def chain(self) -> tuple["IsolationDomain", ...]:
        """Return the domain chain, outermost first."""
        nodes: list[IsolationDomain] = []
        node: IsolationDomain | None = self
        while node is not None:
            nodes.append(node)
            node = node.parent
        nodes.reverse()
        return tuple(nodes)

    def to_dict(self) -> dict:
        payload: dict = {
            "type": self.type.value,
            "id": self.id,
            "scope": self.scope,
            "platform": self.platform,
        }
        payload["parent"] = self.parent.to_dict() if self.parent else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict | None) -> "IsolationDomain | None":
        if not payload:
            return None
        parent = payload.get("parent")
        return cls(
            type=DomainType(payload["type"]),
            id=payload.get("id"),
            scope=payload.get("scope"),
            platform=payload.get("platform"),
            parent=cls.from_dict(parent) if parent else None,
        )


@dataclass(frozen=True)
class ProcessIdentity:
    """Neutral process identity, portable across producers.

    ``process_instance_id`` is the neutral field. On Linux a producer might
    derive it from ``boot_id + pid + start_time``; on Windows from a Sysmon
    process GUID; on macOS from an audit token. The core never assumes which.
    """

    host_id: str
    boot_id: str
    process_instance_id: str
    process_start_time: float | None = None
    parent_process_instance_id: str | None = None
    isolation_domain: IsolationDomain | None = None

    def to_dict(self) -> dict:
        return {
            "host_id": self.host_id,
            "boot_id": self.boot_id,
            "process_instance_id": self.process_instance_id,
            "process_start_time": self.process_start_time,
            "parent_process_instance_id": self.parent_process_instance_id,
            "isolation_domain": (
                self.isolation_domain.to_dict() if self.isolation_domain else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ProcessIdentity":
        return cls(
            host_id=payload["host_id"],
            boot_id=payload["boot_id"],
            process_instance_id=payload["process_instance_id"],
            process_start_time=payload.get("process_start_time"),
            parent_process_instance_id=payload.get("parent_process_instance_id"),
            isolation_domain=IsolationDomain.from_dict(
                payload.get("isolation_domain")
            ),
        )
