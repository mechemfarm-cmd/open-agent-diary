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

**The reason a target is missing decides what follows.** ``None`` alone is
ambiguous — it can mean the action had no target, that a target was redacted, or
that nobody recorded one. The record carries a ``TargetState`` so the comparator
can tell those apart instead of inferring from absence.

**Time.** Comparison runs on wall time, which is the only clock the two sides
share — an export carries wall times, not monotonic ones. That is a real
limitation, so it is handled rather than assumed away: a ``clock_jump`` marks
the interval as unreliable and claims inside it are reported as gaps, because a
silent step can move a claim across a start, stop or gap boundary without
anything else noticing.

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
    TargetState,
    target_is_consistent,
)
from .privacy import PrivacyPolicy, coarsen_destination


class CorrelationStatus(str, Enum):
    CONFIRMED_MATCH = "confirmed_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    DELAYED_MATCH = "delayed_match"
    MATCH_NOT_PROVABLE = "match_not_provable"
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
    #: Why the target is what it is. Defaults to UNKNOWN, because a claim that
    #: simply does not mention a target has not told us there wasn't one.
    target_state: TargetState = TargetState.UNKNOWN

    def __post_init__(self) -> None:
        # The same invariant the event enforces. A claim is not exempt just
        # because it came from the agent's side of the comparison: a claim
        # asserting ABSENT while carrying a path would agree with an absent
        # event without its target ever being looked at.
        if not target_is_consistent(self.target, self.target_state):
            raise ValueError(
                f"target_state {self.target_state.value} contradicts the claim's "
                f"target: PRESENT requires a target, and ABSENT, REDACTED and "
                f"UNKNOWN all require none"
            )


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


@dataclass(frozen=True)
class BlindInterval:
    """A period the recorder could not see, and what it could not see.

    Scope matters as much as time. Losing file tracing does not invalidate an
    exec observation, so an unscoped gap would over-report blindness — and
    over-reporting it turns real evidence into "unmatched due to gap".
    """

    start: float
    end: float | None
    affected: tuple[str, ...] = ()
    reason: str = ""

    @property
    def scoped(self) -> bool:
        """Whether the gap names what it affects, rather than claiming everything."""
        return bool(self.affected)

    def covers(self, timestamp: float) -> bool:
        if timestamp < self.start:
            return False
        return self.end is None or timestamp < self.end

    def applies_to(self, subject: str | None) -> bool:
        if not self.affected:
            return True
        if subject is None:
            return True
        return subject in self.affected

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "affected": list(self.affected),
            "reason": self.reason,
        }


class CoverageMap:
    """When the recorder was running, when it was blind, and to what.

    A missing event has to be distinguishable from nothing happening, and this
    is the structure that makes the distinction. Before the first
    ``recorder_started`` nothing is known, so that interval counts as blind
    rather than as quiet.
    """

    def __init__(
        self,
        running: Iterable[tuple[float, float | None]] = (),
        blind: Iterable[BlindInterval] = (),
        clock_unreliable: Iterable[tuple[float, float | None]] = (),
    ) -> None:
        self._running = tuple(running)
        self._blind = tuple(blind)
        self._clock = tuple(clock_unreliable)

    @property
    def blind_intervals(self) -> tuple[BlindInterval, ...]:
        return self._blind

    @property
    def running_intervals(self) -> tuple[tuple[float, float | None], ...]:
        return self._running

    @property
    def clock_unreliable_intervals(self) -> tuple[tuple[float, float | None], ...]:
        return self._clock

    @classmethod
    def from_events(cls, events: Iterable[MachineEvent]) -> "CoverageMap":
        ordered = sorted(events, key=lambda item: item.wall_time)
        starts = [
            event.wall_time
            for event in ordered
            if event.type is EventType.RECORDER_STARTED
        ]

        def next_start_after(moment: float) -> float | None:
            for start in starts:
                if start > moment:
                    return start
            return None

        running: list[tuple[float, float | None]] = []
        blind: list[BlindInterval] = []
        clock: list[tuple[float, float | None]] = []
        run_start: float | None = None
        saw_start = False

        for event in ordered:
            moment = event.wall_time

            if event.type is EventType.RECORDER_STARTED:
                if not saw_start:
                    blind.append(
                        BlindInterval(
                            float("-inf"),
                            moment,
                            (),
                            "before_recorder_started",
                        )
                    )
                saw_start = True
                run_start = moment
            elif event.type is EventType.RECORDER_STOPPED:
                if run_start is not None:
                    running.append((run_start, moment))
                    run_start = None
            elif event.type is EventType.CLOCK_JUMP:
                # Wall-clock ordering is not trustworthy after a step, so the
                # interval is marked rather than ignored: correlation compares
                # on wall time, and a silent step can move a claim across a
                # start/stop boundary without any gap being reported.
                clock.append((moment, next_start_after(moment)))
            elif event.type in _BLINDING_GAPS:
                note = event.coverage_note
                end = note.recovered_at if note else None
                if end is None:
                    # Without a stated recovery, the gap runs until the recorder
                    # says it started again — never "forever" by assumption.
                    end = next_start_after(moment)
                blind.append(
                    BlindInterval(
                        moment,
                        end,
                        note.affected if note else (),
                        event.type.value,
                    )
                )

        if run_start is not None:
            running.append((run_start, None))
        return cls(running, blind, clock)

    @staticmethod
    def _within(interval: tuple[float, float | None], timestamp: float) -> bool:
        start, end = interval
        if timestamp < start:
            return False
        return end is None or timestamp < end

    def is_blind(self, timestamp: float, *, affected: str | None = None) -> bool:
        """Whether the recorder was blind at ``timestamp``, optionally scoped.

        ``affected`` is a capability or event-type name. A gap that names what
        it affects only covers that subject; an unscoped gap covers everything,
        which is what an unscoped producer is entitled to claim.
        """
        for interval in self._blind:
            if interval.covers(timestamp) and interval.applies_to(affected):
                return True
        return not self.recorder_running_at(timestamp)

    def recorder_running_at(self, timestamp: float) -> bool:
        return any(self._within(interval, timestamp) for interval in self._running)

    def clock_unreliable_at(self, timestamp: float) -> bool:
        return any(self._within(interval, timestamp) for interval in self._clock)


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


def _coverage_objection(
    coverage: CoverageMap, claim: AgentClaim, event: MachineEvent
) -> str | None:
    """Why a matched pair cannot be confirmed, or ``None`` if it can.

    A gap is not only the reason an unmatched claim is unexplained; it limits
    what *any* comparison inside it can prove. A match found in an interval the
    recorder could not see is a match nobody observed, and reporting it as
    confirmed would place the coverage model's own caveats behind the result
    instead of in front of it.

    This is checked on both times because either side can fall in the unreliable
    interval, and a confirmation needs both halves to be sound.
    """
    subject = event.type.value
    if not coverage.recorder_running_at(claim.wall_time):
        return "recorder_not_running"
    if coverage.clock_unreliable_at(claim.wall_time) or coverage.clock_unreliable_at(
        event.wall_time
    ):
        return "wall_clock_unreliable_at_match_time"
    if coverage.is_blind(claim.wall_time, affected=subject):
        return "recorder_blind_at_claim_time"
    if coverage.is_blind(event.wall_time, affected=subject):
        return "recorder_blind_at_effect_time"
    return None


def _claim_subject(claim: AgentClaim) -> str | None:
    """What an event-type name for this claim's action, for scoped coverage."""
    event_type = ACTION_ALIASES.get(claim.action.lower())
    return event_type.value if event_type else None


def _compare_targets(
    claim: AgentClaim,
    event: MachineEvent,
    policy: PrivacyPolicy,
) -> tuple[bool, bool]:
    """Compare an agent's target with an observed one, after identical treatment.

    Returns ``(agree, decidable)``.

    The reason a target is missing decides what can be concluded, so this works
    from ``target_state`` rather than from ``None``:

    - UNKNOWN on either side — nobody recorded why, so nothing follows. This is
      the case that used to return agreement, and it produced a confirmed match
      between a redacted private path and a claim that never had a target.
    - ABSENT on both sides — both accounts explicitly say there was no target.
      That is agreement about the absence, stated rather than inferred.
    - ABSENT on one side only — one had a target and the other did not.
    - REDACTED on the event side with the claim's target surviving the same
      policy — the event's path was outside the allowed roots and the claim's
      was inside, so they differ.
    - REDACTED on both sides — nothing left to compare.
    """
    claim_state = claim.target_state
    event_state = event.target_state

    if TargetState.UNKNOWN in (claim_state, event_state):
        return False, False

    if claim_state is TargetState.ABSENT and event_state is TargetState.ABSENT:
        return True, True

    if TargetState.ABSENT in (claim_state, event_state):
        return False, True

    # Both sides had a target. Resolve the claim's through the same policy the
    # producer applied to the event, so a basename is not compared to a path.
    if event.type is EventType.CONNECT:
        expected = coarsen_destination(claim.target) if claim.target else None
    else:
        redacted = policy.redact_path(claim.target) if claim.target else None
        expected = redacted.value if redacted else None

    if event_state is TargetState.REDACTED:
        if expected is None:
            return False, False
        return False, True

    if expected is None:
        # The claim's target was dropped while the event's survived, so the
        # event's was inside the allowed roots and the claim's was not.
        return False, True

    return expected == event.target, True


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
            same, decidable = _compare_targets(claim, event, policy)
            if not decidable:
                undecidable.append(index)
            elif same:
                agreed.append((index, claim))

        if len(agreed) == 1:
            index, claim = agreed[0]
            used_claims.add(index)
            registration = registry.get(claim.capture_id)
            objection = _coverage_objection(coverage, claim, event)
            if objection is not None:
                # Matched, but the interval cannot support a confirmation. The
                # pair is reported rather than silently promoted: the agreement
                # is real, and so is the reason it proves nothing.
                records.append(
                    CorrelatedRecord(
                        status=CorrelationStatus.MATCH_NOT_PROVABLE,
                        reason=objection,
                        event=event,
                        claim=claim,
                    )
                )
            elif registration is not None and not registration.covers(event.wall_time):
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
        # Scoped to what this claim is about: losing file tracing does not make
        # an exec claim unobservable.
        affected = _claim_subject(claim)
        if not coverage.recorder_running_at(claim.wall_time):
            status = CorrelationStatus.RECORDER_UNAVAILABLE
            reason = "recorder_not_running"
        elif coverage.clock_unreliable_at(claim.wall_time):
            # Wall time is what this comparison runs on, so a clock step makes
            # the comparison unreliable rather than merely inconvenient. Marking
            # it beats silently placing the claim on the wrong side of a
            # boundary.
            status = CorrelationStatus.UNMATCHED_DUE_TO_GAP
            reason = "wall_clock_unreliable_at_claim_time"
        elif coverage.is_blind(claim.wall_time, affected=affected):
            status = CorrelationStatus.UNMATCHED_DUE_TO_GAP
            reason = "recorder_blind_at_claim_time"
        else:
            status = CorrelationStatus.AGENT_ONLY
            reason = "no_observed_effect"
        records.append(
            CorrelatedRecord(status=status, reason=reason, claim=claim)
        )

    return CorrelationReport(records=tuple(records))
