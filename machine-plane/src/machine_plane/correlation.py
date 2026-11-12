"""Correlation: joining the agent's account to the machine's account.

This is the point of the whole exercise. Everything else is plumbing.

Three rules shape it, and each exists because the obvious alternative is wrong:

**Identity comes from the supervisor, never from the agent.** A claim is linked
to observed effects through a registered run — a capture id with a scope and
process identity assigned outside the agent — not through an environment
variable the agent could set, and not through a PID that could be reused.

**Time never promotes a match on its own.** Two events near in time are two
events near in time. A confirmed match requires identity, action and target to
agree; time only decides whether the effect landed inside the run's window or
arrived late.

**A comparison that cannot be made is not a match.** Redaction can drop a
target, and then the two accounts cannot be compared at all. That is reported as
ambiguous, not as agreement. Silence is not evidence.

The statuses below are the taxonomy the design calls for: a disagreement is
categorised, never reduced to a single "divergence" count.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from .events import (
    EventKind,
    EventType,
    MachineEvent,
)
from .privacy import PrivacyPolicy, coarsen_destination


class CorrelationStatus(str, Enum):
    CONFIRMED_MATCH = "confirmed_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    DELAYED_MATCH = "delayed_match"
    MACHINE_ONLY = "machine_only"
    AGENT_ONLY = "agent_only"
    UNMATCHED_DUE_TO_SCOPE = "unmatched_due_to_scope"
    UNMATCHED_DUE_TO_GAP = "unmatched_due_to_gap"
    RECORDER_UNAVAILABLE = "recorder_unavailable"


#: Statuses worth a human looking at. A confirmed match is the quiet case.
INVESTIGABLE = frozenset(
    status for status in CorrelationStatus if status is not CorrelationStatus.CONFIRMED_MATCH
)


#: How an agent's declared action maps onto an observed effect. Deliberately
#: explicit: an unknown action does not match anything, rather than matching
#: loosely and producing a confident wrong answer.
ACTION_ALIASES: dict[str, EventType] = {
    "exec": EventType.EXEC,
    "run": EventType.EXEC,
    "shell": EventType.EXEC,
    "open": EventType.OPEN,
    "read": EventType.READ_ATTEMPT,
    "read_file": EventType.READ_ATTEMPT,
    "write": EventType.WRITE_ATTEMPT,
    "write_file": EventType.WRITE_ATTEMPT,
    "edit": EventType.WRITE_ATTEMPT,
    "edit_file": EventType.WRITE_ATTEMPT,
    "create": EventType.CREATE,
    "create_file": EventType.CREATE,
    "rename": EventType.RENAME,
    "move": EventType.RENAME,
    "unlink": EventType.UNLINK,
    "delete": EventType.UNLINK,
    "remove": EventType.UNLINK,
    "chmod": EventType.METADATA_CHANGE,
    "metadata_change": EventType.METADATA_CHANGE,
    "connect": EventType.CONNECT,
    "fetch": EventType.CONNECT,
    "request": EventType.CONNECT,
    "http": EventType.CONNECT,
}

#: Gap events that mean the recorder could not see something.
_BLINDING_GAPS = frozenset(
    {
        EventType.BUFFER_OVERFLOW,
        EventType.PERMISSION_GAP,
        EventType.SINK_UNAVAILABLE,
        EventType.UNSUPPORTED_KERNEL_FEATURE,
    }
)


@dataclass(frozen=True)
class AgentClaim:
    """One action the agent says it took.

    Produced by adapting an Agent Diary work trace. The core never imports the
    Diary, so this is the shape a caller must provide.
    """

    capture_id: str
    action: str
    wall_time: float
    target: str | None = None
    monotonic_time: float | None = None
    tool: str | None = None


@dataclass(frozen=True)
class RunRegistration:
    """A run registered by the supervisor, outside the agent."""

    capture_id: str
    started_at: float
    ended_at: float | None = None
    scope: str | None = None
    boot_id: str | None = None
    process_instance_ids: tuple[str, ...] = ()

    def covers(self, timestamp: float) -> bool:
        if timestamp < self.started_at:
            return False
        return self.ended_at is None or timestamp <= self.ended_at


class RunRegistry:
    """Supervisor-issued run registrations."""

    def __init__(self, registrations: Iterable[RunRegistration] = ()) -> None:
        self._registrations: tuple[RunRegistration, ...] = tuple(registrations)

    def add(self, registration: RunRegistration) -> None:
        self._registrations = (*self._registrations, registration)

    def get(self, capture_id: str) -> RunRegistration | None:
        for registration in self._registrations:
            if registration.capture_id == capture_id:
                return registration
        return None

    def for_event(self, event: MachineEvent) -> tuple[RunRegistration, ...]:
        """Registrations that could own this event, by identity — not by time."""
        identity = event.identity
        if identity is None:
            return ()
        scope = identity.isolation_domain.scope if identity.isolation_domain else None
        matches: list[RunRegistration] = []
        for registration in self._registrations:
            if registration.boot_id is not None and identity.boot_id != registration.boot_id:
                continue
            if registration.scope is not None and scope == registration.scope:
                matches.append(registration)
                continue
            if identity.process_instance_id in registration.process_instance_ids:
                matches.append(registration)
        return tuple(matches)


class CoverageMap:
    """When the recorder was running, and when it was blind.

    A missing event has to be distinguishable from nothing happening, and this
    is the structure that makes the distinction. Before the first
    ``recorder_started`` nothing is known, so that interval counts as blind
    rather than as quiet.
    """

    def __init__(
        self,
        running: Iterable[tuple[float, float | None]] = (),
        blind: Iterable[tuple[float, float | None]] = (),
    ) -> None:
        self._running = tuple(running)
        self._blind = tuple(blind)

    @property
    def blind_intervals(self) -> tuple[tuple[float, float | None], ...]:
        return self._blind

    @property
    def running_intervals(self) -> tuple[tuple[float, float | None], ...]:
        return self._running

    @classmethod
    def from_events(cls, events: Iterable[MachineEvent]) -> "CoverageMap":
        ordered = sorted(events, key=lambda item: item.wall_time)
        running: list[tuple[float, float | None]] = []
        blind: list[tuple[float, float | None]] = []
        run_start: float | None = None
        blind_start: float | None = None
        saw_start = False

        for event in ordered:
            moment = event.wall_time
            if event.type is EventType.RECORDER_STARTED:
                if blind_start is not None:
                    blind.append((blind_start, moment))
                    blind_start = None
                if not saw_start:
                    blind.append((float("-inf"), moment))
                saw_start = True
                run_start = moment
            elif event.type is EventType.RECORDER_STOPPED:
                if run_start is not None:
                    running.append((run_start, moment))
                    run_start = None
            elif event.type in _BLINDING_GAPS and blind_start is None:
                blind_start = moment

        if run_start is not None:
            running.append((run_start, None))
        if blind_start is not None:
            blind.append((blind_start, None))
        return cls(running, blind)

    @staticmethod
    def _within(interval: tuple[float, float | None], timestamp: float) -> bool:
        start, end = interval
        if timestamp < start:
            return False
        return end is None or timestamp < end

    def is_blind(self, timestamp: float) -> bool:
        if any(self._within(interval, timestamp) for interval in self._blind):
            return True
        return not self.recorder_running_at(timestamp)

    def recorder_running_at(self, timestamp: float) -> bool:
        return any(self._within(interval, timestamp) for interval in self._running)


@dataclass(frozen=True)
class CorrelatedRecord:
    """One machine event and the agent claim it was compared against."""

    status: CorrelationStatus
    reason: str
    event: MachineEvent | None = None
    claim: AgentClaim | None = None

    @property
    def investigable(self) -> bool:
        return self.status in INVESTIGABLE

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "event_id": self.event.event_id if self.event else None,
            "capture_id": self.claim.capture_id if self.claim else None,
            "target": (
                self.event.target
                if self.event is not None
                else (self.claim.target if self.claim else None)
            ),
        }


@dataclass(frozen=True)
class CorrelationReport:
    records: tuple[CorrelatedRecord, ...]

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for record in self.records:
            tally[record.status.value] = tally.get(record.status.value, 0) + 1
        return tally

    def exceptions(self) -> tuple[CorrelatedRecord, ...]:
        """Only the records worth investigating, in the order they were found."""
        return tuple(record for record in self.records if record.investigable)

    def to_dict(self) -> dict:
        return {
            "counts": self.counts(),
            "records": [record.to_dict() for record in self.records],
        }


def _compare_targets(
    claim_target: str | None,
    event_target: str | None,
    event_type: EventType,
    policy: PrivacyPolicy,
) -> tuple[bool, bool]:
    """Compare an agent's target with an observed one, after identical treatment.

    Returns ``(agree, decidable)``.

    Redaction can drop a target, and when it does the comparison is genuinely
    impossible — *unless* the other side survived. A surviving event target
    proves the path was inside the allowed roots; a dropped claim target proves
    it was not. So one surviving and one dropped is a decidable disagreement,
    not an undecidable one. Only when both are dropped is there nothing to
    compare, and that is reported as undecidable rather than as agreement: a
    comparison that cannot be made is not a match.
    """
    if claim_target is None and event_target is None:
        return True, True
    if claim_target is None or event_target is None:
        return False, False

    if event_type is EventType.CONNECT:
        expected = coarsen_destination(claim_target)
    else:
        redacted = policy.redact_path(claim_target)
        if redacted.value is None:
            # The claim's path was dropped, so it was outside the allowed roots.
            # The event's target survived, so it was inside them. Different.
            return False, True
        expected = redacted.value

    return expected == event_target, True


def correlate(
    events: Sequence[MachineEvent],
    claims: Sequence[AgentClaim],
    registry: RunRegistry,
    *,
    coverage: CoverageMap | None = None,
    policy: PrivacyPolicy | None = None,
) -> CorrelationReport:
    """Compare the two accounts and categorise every record."""
    policy = policy or PrivacyPolicy()
    coverage = coverage if coverage is not None else CoverageMap.from_events(events)

    records: list[CorrelatedRecord] = []
    used_claims: set[int] = set()

    for event in events:
        if event.kind is not EventKind.ACTION:
            continue

        registrations = registry.for_event(event)
        if not registrations:
            records.append(
                CorrelatedRecord(
                    status=CorrelationStatus.UNMATCHED_DUE_TO_SCOPE,
                    reason="no_registered_run_for_identity",
                    event=event,
                )
            )
            continue

        capture_ids = {registration.capture_id for registration in registrations}
        agreed: list[tuple[int, AgentClaim]] = []
        undecidable: list[int] = []

        for index, claim in enumerate(claims):
            if index in used_claims or claim.capture_id not in capture_ids:
                continue
            if ACTION_ALIASES.get(claim.action.lower()) is not event.type:
                continue
            same, decidable = _compare_targets(
                claim.target, event.target, event.type, policy
            )
            if not decidable:
                undecidable.append(index)
            elif same:
                agreed.append((index, claim))

        if len(agreed) == 1:
            index, claim = agreed[0]
            used_claims.add(index)
            registration = registry.get(claim.capture_id)
            if registration is not None and not registration.covers(event.wall_time):
                records.append(
                    CorrelatedRecord(
                        status=CorrelationStatus.DELAYED_MATCH,
                        reason="effect_outside_run_window",
                        event=event,
                        claim=claim,
                    )
                )
            else:
                records.append(
                    CorrelatedRecord(
                        status=CorrelationStatus.CONFIRMED_MATCH,
                        reason="identity_action_target_agree",
                        event=event,
                        claim=claim,
                    )
                )
        elif len(agreed) > 1:
            records.append(
                CorrelatedRecord(
                    status=CorrelationStatus.AMBIGUOUS_MATCH,
                    reason=f"{len(agreed)}_claims_agree",
                    event=event,
                )
            )
        elif undecidable:
            # The claims are deliberately NOT marked consumed. When an event's
            # target was dropped by redaction, every claim of that action type
            # is equally incomparable to it, so consuming them here would let
            # one un-attributable event swallow unrelated claims. Both sides are
            # reported instead: the event as ambiguous, and each claim as having
            # no observed effect. Both statements are true, and the reason codes
            # say why.
            records.append(
                CorrelatedRecord(
                    status=CorrelationStatus.AMBIGUOUS_MATCH,
                    reason="target_not_comparable_after_redaction",
                    event=event,
                )
            )
        else:
            records.append(
                CorrelatedRecord(
                    status=CorrelationStatus.MACHINE_ONLY,
                    reason="no_agent_claim_for_effect",
                    event=event,
                )
            )

    for index, claim in enumerate(claims):
        if index in used_claims:
            continue
        if not coverage.recorder_running_at(claim.wall_time):
            status = CorrelationStatus.RECORDER_UNAVAILABLE
            reason = "recorder_not_running"
        elif coverage.is_blind(claim.wall_time):
            status = CorrelationStatus.UNMATCHED_DUE_TO_GAP
            reason = "recorder_blind_at_claim_time"
        else:
            status = CorrelationStatus.AGENT_ONLY
            reason = "no_observed_effect"
        records.append(
            CorrelatedRecord(status=status, reason=reason, claim=claim)
        )

    return CorrelationReport(records=tuple(records))
