"""Gate 2 — inspect a sealed segment before any number from it is reported.

Gate 1 inspects the probes before they attach. This inspects what actually landed
afterwards. They are not substitutes: Gate 1 cannot know whether a probe behaved
as written, and Gate 2 cannot un-run a dereference.

Three things are checked, and any failure voids the run rather than producing a
partial result:

1. every record parses — an unreadable line is not a zero;
2. no record carries a field the schema does not define, so a probe that started
   emitting something new shows up as a failure rather than as extra data;
3. no value looks like a path or free text. The one approved pathname
   dereference still has to arrive redacted: approval to *read* a path is not
   approval to *store* one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .events import EventType, MachineEvent, event_kind, EventKind
from .spool import Spool, SpoolError

#: Detail keys the recorder writes on its own lifecycle records. These are ours,
#: not the probes', so they are declared here rather than derived.
LIFECYCLE_KEYS = frozenset(
    {
        "capture_id",
        "cgroup_inode",
        "scope",
        "protection_level",
        "supervisor_mode",
        "records",
        "dropped_this_run",
        "error",
        "sealed_on_recovery",
        "max_results",
    }
)

#: Computed by the producer rather than read from a probe field.
DERIVED_KEYS = frozenset({"outcome"})

#: Short strings that are values rather than identifiers, and legitimately contain
#: no path separators. Listed so the path rule can stay strict elsewhere.
NUMERIC_OR_ENUM_KEYS = frozenset(
    {
        "pid",
        "tid",
        "cgroup",
        "cgroup_inode",
        "rc",
        "write",
        "records",
        "dropped_this_run",
        "protection_level",
        "supervisor_mode",
        "probe",
        "phase",
        "ts",
    }
)


@dataclass
class Find:
    record: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"record {self.record}: {self.rule}: {self.detail}"


@dataclass
class ScanResult:
    records: int = 0
    action_records: int = 0
    findings: list[Find] = field(default_factory=list)
    target_redacted: bool | None = None

    @property
    def passed(self) -> bool:
        return not self.findings

    def report(self) -> str:
        if self.passed:
            state = {
                True: "redacted",
                False: "absent",
                None: "not observed",
            }[self.target_redacted]
            return (
                f"Gate 2: PASSED — {self.records} records "
                f"({self.action_records} action), no undefined fields, "
                f"no path-shaped values; approved pathname: {state}"
            )
        lines = ["Gate 2: REFUSED — the run is void, not partially reported"]
        for finding in self.findings[:20]:
            lines.append(f"  {finding}")
        if len(self.findings) > 20:
            lines.append(f"  ... and {len(self.findings) - 20} more")
        return "\n".join(lines)


def looks_like_path(value: str) -> bool:
    """Whether a string is shaped like a filesystem location.

    Deliberately blunt. A redacted path has already lost its separators, so
    anything still carrying one is either unredacted or a field nobody declared —
    and both are findings.
    """
    if not value:
        return False
    if value.startswith(("/", "~", "./", "../")):
        return True
    if "/" in value:
        return True
    if "\x00" in value or "\n" in value:
        return True
    return False


def _walk_strings(payload: dict, prefix: str = ""):
    for key, value in payload.items():
        name = f"{prefix}{key}"
        if isinstance(value, str):
            yield name, key, value
        elif isinstance(value, dict):
            yield from _walk_strings(value, prefix=f"{name}.")


def scan_segment(
    path: str | Path,
    *,
    recorder_id: str,
    allowed_detail_keys: frozenset[str] | set[str] = frozenset(),
) -> ScanResult:
    segment = Path(path)
    result = ScanResult()
    allowed = set(allowed_detail_keys) | LIFECYCLE_KEYS | DERIVED_KEYS

    spool = Spool(segment, recorder_id)
    try:
        records = list(spool.read_from(0))
    except SpoolError as error:
        result.findings.append(Find(0, "unparseable-record", str(error)))
        return result

    for index, (_start, _end, envelope) in enumerate(records):
        result.records += 1
        try:
            event = MachineEvent.from_dict(envelope.event)
        except Exception as error:  # noqa: BLE001 - any failure voids the run
            result.findings.append(Find(index, "unreadable-event", str(error)))
            continue

        is_action = event_kind(event.type) is EventKind.ACTION
        if is_action:
            result.action_records += 1

        # Rule 2: no field the schema does not define.
        for key in event.platform_details:
            if key not in allowed:
                result.findings.append(
                    Find(index, "undefined-field", f"{key!r} on {event.type.value}")
                )

        # Rule 3: nothing path-shaped, anywhere.
        if event.target is not None:
            if looks_like_path(event.target):
                result.findings.append(
                    Find(index, "unredacted-target", f"{event.target!r} on {event.type.value}")
                )
            else:
                result.target_redacted = True
        if event.result and looks_like_path(event.result):
            result.findings.append(
                Find(index, "path-in-result", f"{event.result!r} on {event.type.value}")
            )
        for name, key, value in _walk_strings(dict(event.platform_details)):
            if key in NUMERIC_OR_ENUM_KEYS:
                continue
            if key == "label":
                # comm, truncated by the kernel. It can contain a slash without
                # being a path (fail2ban renames itself to 'f2b/f.sshd'), so the
                # shape test is wrong here; the length bound is the real check.
                if len(value) > 16:
                    result.findings.append(
                        Find(index, "label-too-long", f"{value!r} is not a truncated comm")
                    )
                continue
            if looks_like_path(value):
                result.findings.append(
                    Find(index, "path-shaped-detail", f"{name}={value!r} on {event.type.value}")
                )

    if result.target_redacted is None:
        result.target_redacted = False
    return result


def gate2(segment: str | Path, *, recorder_id: str, probe_fields: set[str]) -> ScanResult:
    return scan_segment(segment, recorder_id=recorder_id, allowed_detail_keys=probe_fields)


@dataclass
class Accounting:
    """The counters behind a segment, named separately so they reconcile.

    A single "records" number hides which population it describes. These are
    defined so the equation can be checked rather than asserted:

        records  ==  lifecycle + action + gaps
        imported + duplicates + refused  ==  records

    ``lifecycle`` covers ``recorder_started``, ``recorder_stopped`` and the other
    gap-class events, which are the recorder's own bookkeeping rather than
    observations by a probe.
    """

    segment: str
    records: int = 0
    lifecycle: int = 0
    action: int = 0
    gaps: int = 0
    imported: int = 0
    duplicates: int = 0
    #: Records below the checkpoint, imported by an earlier run. Without this
    #: the equation fails on every re-run, which looks like loss and is not.
    already: int = 0

    @property
    def consistent(self) -> bool:
        return self.records == self.lifecycle + self.action + self.gaps

    def line(self) -> str:
        return (
            f"{self.segment}: records {self.records} = "
            f"lifecycle {self.lifecycle} + action {self.action} + gaps {self.gaps}; "
            f"imported {self.imported} + duplicates {self.duplicates}"
        )


def account_segment(
    path: str | Path, *, recorder_id: str, checkpoint_offset: int = 0
) -> tuple[Accounting, list[MachineEvent]]:
    """Count a segment by category, returning the events as well."""
    segment = Path(path)
    counts = Accounting(segment=segment.name)
    events: list[MachineEvent] = []

    for _start, end_offset, envelope in Spool(segment, recorder_id).read_from(0):
        counts.records += 1
        if end_offset <= checkpoint_offset:
            counts.already += 1
        try:
            event = MachineEvent.from_dict(envelope.event)
        except Exception:  # noqa: BLE001 - counted, and the scan reports it
            counts.gaps += 1
            continue
        events.append(event)
        if event.type in {
            EventType.RECORDER_STARTED,
            EventType.RECORDER_STOPPED,
            EventType.BUFFER_OVERFLOW,
            EventType.PERMISSION_GAP,
            EventType.UNSUPPORTED_KERNEL_FEATURE,
            EventType.SINK_UNAVAILABLE,
            EventType.CLOCK_JUMP,
        }:
            counts.lifecycle += 1
        else:
            counts.action += 1

    return counts, events


def attribution(events: list[MachineEvent], *, scope: str | None) -> tuple[int, int]:
    """How many events belong to the named scope, and how many do not.

    The probes are host-wide. Until they are filtered, most of what a capture
    contains belongs to something else on the machine, and a claim about what the
    workload did must not be read from the total.
    """
    matched = 0
    unmatched = 0
    for event in events:
        if event.identity is None or event.identity.isolation_domain is None:
            unmatched += 1
            continue
        if scope is not None and event.identity.isolation_domain.scope == scope:
            matched += 1
        else:
            unmatched += 1
    return matched, unmatched
