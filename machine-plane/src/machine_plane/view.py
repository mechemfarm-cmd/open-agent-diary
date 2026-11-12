"""The view: ranked investigable exceptions, not a log viewer.

Nobody reads logs. A page that shows every event has told the reader nothing —
the value is in what gets ranked to the top and explained in language a person
can act on.

Two choices shape this:

**Exceptions are ranked, not counted.** An unexplained effect outranks a late
one. A single "3 divergences" headline tells you nothing about what kind of
problem you have, so there is no such headline; there is a list, ordered, each
line naming its category.

**Coverage is part of the page, not a footnote.** A record that says "the agent
claims this and we were blind" is only honest if the blindness is visible in the
same breath. A view that shows exceptions without showing the gaps invites the
reader to read silence as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from .correlation import (
    CorrelatedRecord,
    CorrelationReport,
    CorrelationStatus,
    CoverageMap,
)
from .events import EventType

#: Most severe first. This ordering is a judgement, and it is stated rather than
#: implied: an effect nobody claimed is the thing worth looking at, because
#: either the agent did something it did not report or something else did it.
RANKING: tuple[CorrelationStatus, ...] = (
    CorrelationStatus.MACHINE_ONLY,
    CorrelationStatus.AMBIGUOUS_MATCH,
    CorrelationStatus.UNMATCHED_DUE_TO_GAP,
    CorrelationStatus.RECORDER_UNAVAILABLE,
    CorrelationStatus.DELAYED_MATCH,
    CorrelationStatus.AGENT_ONLY,
)

_RANK = {status: index for index, status in enumerate(RANKING)}

_VERBS: dict[EventType, str] = {
    EventType.EXEC: "ran",
    EventType.OPEN: "opened",
    EventType.READ_ATTEMPT: "tried to read",
    EventType.WRITE_ATTEMPT: "wrote to",
    EventType.CREATE: "created",
    EventType.RENAME: "renamed",
    EventType.UNLINK: "deleted",
    EventType.METADATA_CHANGE: "changed permissions on",
    EventType.CONNECT: "connected to",
}

_PLACEHOLDER = "a location the policy did not record"


def _observed_phrase(record: CorrelatedRecord) -> str:
    """The effect, in past tense, as an observed thing."""
    event = record.event
    if event is None:
        return "an unrecorded effect"
    verb = _VERBS.get(event.type, "touched")
    return f"{verb} {event.target or _PLACEHOLDER}"


def _claimed_phrase(record: CorrelatedRecord) -> str:
    """The claim, as a noun phrase.

    Kept separate from the observed phrasing because the two accounts are not
    the same kind of statement: one is what happened, the other is what the
    agent says happened.
    """
    claim = record.claim
    if claim is None:
        return "an unrecorded action"
    return f"a {claim.action} of {claim.target or _PLACEHOLDER}"


def describe(record: CorrelatedRecord) -> str:
    """One sentence per record, in the plainest available language."""
    observed = _observed_phrase(record)
    claimed = _claimed_phrase(record)
    status = record.status

    if status is CorrelationStatus.CONFIRMED_MATCH:
        return f"the agent reports {claimed}, and the machine saw it"
    if status is CorrelationStatus.MACHINE_ONLY:
        return f"something {observed}, and the agent never mentioned it"
    if status is CorrelationStatus.AMBIGUOUS_MATCH:
        return f"something {observed}, and it cannot be tied to a specific claim"
    if status is CorrelationStatus.AGENT_ONLY:
        return f"the agent reports {claimed}, and the machine saw nothing"
    if status is CorrelationStatus.DELAYED_MATCH:
        return f"the machine {observed} after the run had closed"
    if status is CorrelationStatus.UNMATCHED_DUE_TO_GAP:
        return f"the agent reports {claimed}, while the recorder was blind"
    if status is CorrelationStatus.RECORDER_UNAVAILABLE:
        return f"the agent reports {claimed}, while the recorder was not running"
    if status is CorrelationStatus.UNMATCHED_DUE_TO_SCOPE:
        return f"the machine {observed}, outside any registered run"
    return observed


@dataclass(frozen=True)
class ViewEntry:
    rank: int
    status: CorrelationStatus
    sentence: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "status": self.status.value,
            "sentence": self.sentence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class View:
    compared: int
    counts: dict[str, int]
    entries: tuple[ViewEntry, ...]
    coverage_note: str

    @property
    def exceptions(self) -> tuple[ViewEntry, ...]:
        return self.entries

    def to_text(self) -> str:
        lines: list[str] = []
        lines.append("Machine record")
        lines.append(f"  records compared : {self.compared}")
        for status in CorrelationStatus:
            count = self.counts.get(status.value, 0)
            if count:
                lines.append(f"  {status.value:<24} {count}")

        lines.append("")
        if self.entries:
            lines.append("Investigate first")
            for entry in self.entries:
                lines.append(f"  {entry.rank}. {entry.sentence}")
                lines.append(f"     ({entry.status.value}: {entry.reason})")
        else:
            lines.append("Nothing to investigate.")

        lines.append("")
        lines.append("Coverage")
        lines.append(f"  {self.coverage_note}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "compared": self.compared,
            "counts": dict(self.counts),
            "exceptions": [entry.to_dict() for entry in self.entries],
            "coverage": self.coverage_note,
        }


def _coverage_note(coverage: CoverageMap | None) -> str:
    """Describe coverage of the observed period.

    The pre-start blindness that ``CoverageMap`` records is not repeated here:
    it concerns the time before the period began, and per-record honesty about
    it is already carried by the claims that fall into it. Repeating it on every
    page would train the reader to ignore the coverage line.
    """
    if coverage is None:
        return "coverage was not reported for this period"

    running = coverage.running_intervals
    gaps = [
        interval
        for interval in coverage.blind_intervals
        if interval[0] != float("-inf")
    ]

    if not running:
        return "the recorder never reported starting, so nothing is known for this period"
    if not gaps:
        return "the recorder was running throughout the observed period"

    open_ended = any(end is None for _, end in gaps)
    if open_ended:
        return (
            f"the recorder was running, with {len(gaps)} reported coverage gap(s), "
            "the last of which is still open"
        )
    return f"the recorder was running, with {len(gaps)} reported coverage gap(s)"


def build_view(
    report: CorrelationReport,
    *,
    coverage: CoverageMap | None = None,
) -> View:
    """Rank the investigable records and describe them."""
    exceptions = [record for record in report.records if record.investigable]
    exceptions.sort(
        key=lambda record: (_RANK.get(record.status, len(RANKING)), record.reason)
    )

    entries = tuple(
        ViewEntry(
            rank=index + 1,
            status=record.status,
            sentence=describe(record),
            reason=record.reason,
        )
        for index, record in enumerate(exceptions)
    )

    return View(
        compared=len(report.records),
        counts=report.counts(),
        entries=entries,
        coverage_note=_coverage_note(coverage),
    )
