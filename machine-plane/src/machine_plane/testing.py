"""Test doubles for producer authors.

A producer can be developed and verified against a mock long before it touches a
kernel. That is the point: the record format, the pipeline and the ingestion
boundary are testable without privileges, and they survive whichever sensor
Stage 0 eventually picks.
"""

from __future__ import annotations

from collections.abc import Iterable

from .capabilities import Capability, CapabilityReport, CapabilitySet
from .events import CoverageState, EventType, MachineEvent, PrivacyMode
from .identity import (
    DomainType,
    IdentityConfidence,
    IsolationDomain,
    ProcessIdentity,
)
from .producer import Producer, RawEvent


def make_identity(
    *,
    host_id: str = "host-1",
    boot_id: str = "boot-1",
    process_instance_id: str = "proc-1",
    parent_process_instance_id: str | None = None,
    process_start_time: float = 1000.0,
    domain_type: DomainType | None = DomainType.CGROUP,
    domain_id: str | None = "scope-1",
    parent_domain: IsolationDomain | None = None,
) -> ProcessIdentity:
    domain = None
    if domain_type is not None:
        domain = IsolationDomain(
            type=domain_type,
            id=domain_id,
            scope="agent-run",
            platform="linux",
            parent=parent_domain,
        )
    return ProcessIdentity(
        host_id=host_id,
        boot_id=boot_id,
        process_instance_id=process_instance_id,
        process_start_time=process_start_time,
        parent_process_instance_id=parent_process_instance_id,
        isolation_domain=domain,
    )


def make_capabilities(*, available: bool = True) -> CapabilitySet:
    reports = [
        CapabilityReport(
            capability=capability,
            available=available,
            from_time=0.0,
            to_time=None,
            reason=None if available else "mock_absent",
        )
        for capability in Capability
    ]
    return CapabilitySet(reports)


class MockProducer(Producer):
    """A producer that replays a fixed list of raw events.

    Used to exercise the pipeline, the spool and the importer without any
    privileged access to a real machine.
    """

    name = "mock"
    version = "0.1.0"
    platform = "linux"

    def __init__(self, raws: list[RawEvent] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._raws = list(raws or [])
        self._capabilities = make_capabilities()

    def capabilities(self) -> CapabilitySet:
        return self._capabilities

    def collect(self) -> Iterable[RawEvent]:
        return list(self._raws)

    def normalize(self, raw: RawEvent) -> MachineEvent:
        return MachineEvent(
            type=raw.type,
            producer="unset",
            producer_version="unset",
            platform="unset",
            monotonic_time=raw.monotonic_time,
            wall_time=raw.wall_time,
            privacy_mode=PrivacyMode.METADATA_ONLY,
            coverage_state=CoverageState.COVERED,
            identity_confidence=raw.identity_confidence,
            identity=raw.identity,
            target=raw.target,
            result=raw.result,
            platform_details=dict(raw.details or {}),
        )


def action_event(
    event_type: EventType = EventType.WRITE_ATTEMPT,
    *,
    target: str | None = "/home/x/notes.txt",
    details: dict | None = None,
    monotonic_time: float = 10.0,
    wall_time: float = 1_700_000_000.0,
    identity: ProcessIdentity | None = None,
) -> RawEvent:
    return RawEvent(
        type=event_type,
        monotonic_time=monotonic_time,
        wall_time=wall_time,
        identity=identity if identity is not None else make_identity(),
        target=target,
        result="ok",
        details=details or {},
        identity_confidence=IdentityConfidence.CERTAIN,
    )
