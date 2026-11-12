"""Capability reporting for the machine plane.

A single minimum kernel version must not imply uniform capability. Linux support
depends on kernel configuration, BTF, cgroups, lockdown mode, available BPF
features, and the sensor chosen — so capabilities are reported individually, and
each report carries *when* it was unavailable, not merely that it was.

Without the interval, a missing event is indistinguishable from "nothing
happened".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Capability(str, Enum):
    BPF_AVAILABLE = "bpf_available"
    BTF_AVAILABLE = "btf_available"
    CGROUP_TRACKING = "cgroup_tracking"
    PROCESS_EXEC = "process_exec"
    FILE_ACTIVITY = "file_activity"
    NETWORK_CONNECT = "network_connect"
    KERNEL_LOCKDOWN_MODE = "kernel_lockdown_mode"


@dataclass(frozen=True)
class CapabilityReport:
    """One capability, and the interval over which it held.

    ``to_time`` of ``None`` means the state was still current when the report
    was written. ``reason`` names why a capability was unavailable, so a gap can
    be explained rather than merely counted.
    """

    capability: Capability
    available: bool
    from_time: float
    to_time: float | None = None
    reason: str | None = None

    def covers(self, timestamp: float) -> bool:
        if timestamp < self.from_time:
            return False
        return self.to_time is None or timestamp < self.to_time

    def to_dict(self) -> dict:
        return {
            "capability": self.capability.value,
            "available": self.available,
            "from": self.from_time,
            "to": self.to_time,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "CapabilityReport":
        return cls(
            capability=Capability(payload["capability"]),
            available=bool(payload["available"]),
            from_time=payload["from"],
            to_time=payload.get("to"),
            reason=payload.get("reason"),
        )


class CapabilitySet:
    """A producer's capability reports, queryable at a point in time."""

    def __init__(self, reports: list[CapabilityReport] | None = None) -> None:
        self._reports: tuple[CapabilityReport, ...] = tuple(reports or ())

    @property
    def reports(self) -> tuple[CapabilityReport, ...]:
        return self._reports

    def available_at(self, capability: Capability, timestamp: float) -> bool:
        """True only if a report covers the timestamp *and* says available.

        An unmentioned capability is not available: silence must never be read
        as support.
        """
        for report in self._reports:
            if report.capability is capability and report.covers(timestamp):
                return report.available
        return False

    def missing_at(self, timestamp: float) -> tuple[Capability, ...]:
        """Capabilities that were not available at ``timestamp``."""
        return tuple(
            capability
            for capability in Capability
            if not self.available_at(capability, timestamp)
        )

    def to_list(self) -> list[dict]:
        return [report.to_dict() for report in self._reports]

    @classmethod
    def from_list(cls, payload: list[dict] | None) -> "CapabilitySet":
        return cls([CapabilityReport.from_dict(item) for item in (payload or [])])

    def __len__(self) -> int:
        return len(self._reports)
