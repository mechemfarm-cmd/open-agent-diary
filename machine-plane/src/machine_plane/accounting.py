"""Accounting: which population a record belongs to, and whether it is in scope.

Two questions live here, and both are seams where a small disagreement turns into
a large wrong number:

1. **Which class is this record?** Three classes, disjoint and exhaustive. This is
   *not* the schema's ``event_kind()``, which classifies every recorder and
   coverage event as ``EventKind.GAP``. Counting with that broader notion puts
   buffer overflows, clock jumps and recorder start/stop in the same bucket, which
   both breaks the equation and hides which of them happened.

2. **Is this event in scope?** The rule is normative and has two representations:
   a bpftrace predicate in the kernel and this function in Python. They cannot be
   the same code, so they are reconciled by comparing what they produce rather than
   by sharing a name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .events import EventType
from .identity import ProcessIdentity

#: Recorder bookkeeping. Deliberately narrow: a coverage-loss event is *not*
#: lifecycle, however much it resembles one.
LIFECYCLE_TYPES = frozenset(
    {
        EventType.RECORDER_STARTED,
        EventType.RECORDER_STOPPED,
    }
)

#: Everything the workload itself did. Enumerated rather than "anything not in the
#: other two sets", because a fallthrough default is how a new event type enters the
#: action population unnoticed: both equations still balance, the totals still add up,
#: and the error is invisible. An unclassified type raises instead.
ACTION_TYPES = frozenset(
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

#: Everything the recorder reports because it could not see. Each of these is a
#: gap in coverage, and none of them belongs in ``lifecycle``.
GAP_TYPES = frozenset(
    {
        EventType.BUFFER_OVERFLOW,
        EventType.PERMISSION_GAP,
        EventType.UNSUPPORTED_KERNEL_FEATURE,
        EventType.SINK_UNAVAILABLE,
        EventType.CLOCK_JUMP,
    }
)


class EventClass(str, Enum):
    LIFECYCLE = "lifecycle"
    ACTION = "action"
    GAP = "gap"


def classify(event_type: EventType) -> EventClass:
    """Exactly one class per event type, or an error.

    Three explicit sets, no default. A type that belongs to none of them is a
    schema change this module has not been taught about, and the honest response is
    to stop rather than to file it under ``action`` and let the equations balance
    over a population nobody chose.
    """
    if event_type in LIFECYCLE_TYPES:
        return EventClass.LIFECYCLE
    if event_type in ACTION_TYPES:
        return EventClass.ACTION
    if event_type in GAP_TYPES:
        return EventClass.GAP
    raise ValueError(
        f"event type {event_type!r} belongs to no accounting class — add it to "
        "LIFECYCLE_TYPES, ACTION_TYPES or GAP_TYPES in accounting.py"
    )


def is_in_scope(identity: ProcessIdentity | None, scope_inode: str | int | None) -> bool:
    """The normative scope rule, in Python.

    An event is in scope if and only if its cgroup identity is present **and**
    numerically equal to the measured scope cgroup inode. Numeric equality matters:
    the kernel predicate compares integers, so a string comparison here would
    disagree with it on leading zeros or whitespace.
    """
    if scope_inode is None or identity is None:
        return False
    if identity.isolation_domain is None:
        return False

    domain = identity.isolation_domain
    if domain.type.value != "cgroup" or domain.id is None:
        return False

    try:
        return int(domain.id) == int(scope_inode)
    except (TypeError, ValueError):
        # A non-numeric cgroup cannot have come from the kernel predicate's
        # operand, so it is not in scope. Returning False rather than raising
        # keeps one malformed record from stopping an accounting pass.
        return False


@dataclass
class Counts:
    """The two populations, with their equations checked rather than asserted."""

    lifecycle: int = 0
    action: int = 0
    gaps: int = 0
    in_scope_actions: int = 0
    out_of_scope_actions: int = 0
    unmatched_actions: int = 0
    #: The inode the segment records in its own header — what the kernel predicate
    #: was given. Present means attribution is computable; absent means it is not.
    scope_in_segment: str | None = None
    #: Whether the importer was asked to check against a particular scope. This is a
    #: separate fact from the above: a segment can carry a scope the caller never
    #: named, and reporting those as "unattributed" contradicts the per-segment counts.
    scope_requested: bool = False
    #: ``filtered``, ``control``, or ``unknown``. ``unknown`` is what a segment gets
    #: when its header carries no mode at all — a capture from before modes existed.
    #: It is NOT filtered: absence of provenance is not evidence of a filter, and
    #: defaulting it to "filtered" would relabel a host-wide capture as a workload
    #: measurement.
    capture_mode: str = "unknown"
    #: Records that could not be parsed at all. Counted as gaps, listed separately
    #: so a corrupt segment is visible as its own number.
    unreadable: int = 0

    @property
    def records(self) -> int:
        return self.lifecycle + self.action + self.gaps

    @property
    def totals_balance(self) -> bool:
        return self.records == self.lifecycle + self.action + self.gaps

    @property
    def actions_balance(self) -> bool:
        return self.action == (
            self.in_scope_actions + self.out_of_scope_actions + self.unmatched_actions
        )

    @property
    def filtered_clean(self) -> bool:
        """Acceptance for a filtered run: no out-of-scope and no unmatched action."""
        return self.out_of_scope_actions == 0 and self.unmatched_actions == 0

    def add_record(self, event_class: EventClass) -> None:
        if event_class is EventClass.LIFECYCLE:
            self.lifecycle += 1
        elif event_class is EventClass.GAP:
            self.gaps += 1
        else:
            self.action += 1

    def add_attribution(self, in_scope: bool | None) -> None:
        """``None`` means the action carried no identity at all."""
        if in_scope is None:
            self.unmatched_actions += 1
        elif in_scope:
            self.in_scope_actions += 1
        else:
            self.out_of_scope_actions += 1

    def attribution_label(self) -> str:
        """What may honestly be said about this population, and on what basis.

        Three distinct facts, previously collapsed into one flag:

        * the segment may record a scope (so attribution is computable),
        * the caller may or may not have asked for a particular scope,
        * and the counts may or may not support a claim about the workload.

        Reporting "unattributed" while the per-segment line prints in-scope counts
        was the contradiction: both were true statements about different things,
        printed as if they were the same thing.
        """
        if self.scope_in_segment is None:
            return (
                f"unattributed ({self.action} action records; the segment records no "
                "scope cgroup, so nothing can be claimed as the workload's)"
            )
        basis = (
            f"scope {self.scope_in_segment} from the segment header; --scope not "
            "requested, so this is what the kernel was told to capture"
            if not self.scope_requested
            else f"scope {self.scope_in_segment}, matches --scope"
        )
        return (
            f"in_scope {self.in_scope_actions} / out_of_scope {self.out_of_scope_actions}"
            f" / unmatched {self.unmatched_actions} — {basis}"
        )

    def report_lines(self) -> list[str]:
        return [
            f"records   : {self.records} = lifecycle {self.lifecycle}"
            f" + action {self.action} + gaps {self.gaps}"
            f"  [{'balances' if self.totals_balance else 'DOES NOT BALANCE'}]",
            f"actions   : {self.action} = in_scope {self.in_scope_actions}"
            f" + out_of_scope {self.out_of_scope_actions}"
            f" + unmatched {self.unmatched_actions}"
            f"  [{'balances' if self.actions_balance else 'DOES NOT BALANCE'}]",
            f"attribution: {self.attribution_label()}",
        ]
