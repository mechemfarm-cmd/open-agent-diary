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

from .accounting import (
    Counts,
    EventClass,
    classify,
    is_in_scope,
)
from .events import EventType, MachineEvent
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
        "capture_mode",
        "producer_out_of_scope",
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
        "capture_mode",
        "producer_out_of_scope",
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

        is_action = classify(event.type) is EventClass.ACTION
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


def account_segment(
    path: str | Path,
    *,
    recorder_id: str,
    checkpoint_offset: int = 0,
    expected_scope: str | None = None,
) -> tuple[Counts, list[MachineEvent], int]:
    """Classify a segment with the disjoint partition, and attribute its actions.

    Returns ``(counts, events, already)``. ``already`` is the number of records
    below a resumed checkpoint — a separate population, because otherwise a
    re-import reads as loss.

    **The scope inode comes from the segment, not from the caller.** The recorder
    measured it and stamped it into the ``RECORDER_STARTED`` header, so it is the
    authoritative value and the same one the kernel predicate was given. An
    operator-supplied number would be a second source of truth that could disagree
    with what actually ran.

    ``expected_scope`` is therefore a *check*, not an input: if supplied, the
    segment's own scope name must match it, and a mismatch is refused rather than
    attributed against the wrong run.

    Classification uses :func:`machine_plane.accounting.classify`, **not** the
    schema's ``event_kind()``. The schema treats every recorder and coverage event
    as a gap; this partition separates recorder bookkeeping from coverage loss, and
    counting with the broader notion would both break the equation and hide which
    of the two occurred.
    """
    segment = Path(path)
    parsed: list[MachineEvent] = []
    already = 0
    unreadable = 0

    for _start, end_offset, envelope in Spool(segment, recorder_id).read_from(0):
        if end_offset <= checkpoint_offset:
            already += 1
        try:
            parsed.append(MachineEvent.from_dict(envelope.event))
        except Exception:  # noqa: BLE001 - counted as a gap, and Gate 2 reports it
            unreadable += 1

    # The run's own header carries the join key.
    scope_inode: str | None = None
    header_scope: str | None = None
    for event in parsed:
        if event.type is EventType.RECORDER_STARTED:
            details = dict(event.platform_details)
            raw_inode = details.get("cgroup_inode")
            if raw_inode is not None:
                scope_inode = str(raw_inode)
            header_scope = details.get("scope")
            header_mode = details.get("capture_mode")
            break

    if expected_scope is not None and header_scope is not None and header_scope != expected_scope:
        raise ValueError(
            f"segment belongs to scope {header_scope!r}, not the expected "
            f"{expected_scope!r}"
        )

    counts = Counts(
        scope_in_segment=scope_inode,
        scope_requested=expected_scope is not None,
    )
    counts.gaps += unreadable
    counts.unreadable += unreadable

    for event in parsed:
        event_class = classify(event.type)
        counts.add_record(event_class)
        if event_class is EventClass.ACTION:
            # Unmatched means the record cannot be attributed at all: no identity,
            # or an identity with no isolation domain. Both are different from an
            # identity that simply belongs to another scope.
            domain = event.identity.isolation_domain if event.identity else None
            if event.identity is None or domain is None:
                counts.add_attribution(None)
            else:
                counts.add_attribution(is_in_scope(event.identity, scope_inode))

    return counts, parsed, already


def attribution(events: list[MachineEvent], *, scope: str | int | None) -> tuple[int, int]:
    """In-scope and out-of-scope action counts, using the normative rule.

    Kept for callers that already hold the events; it delegates to ``is_in_scope``
    so there is one scope rule in Python rather than two.
    """
    in_scope = 0
    out_of_scope = 0
    for event in events:
        if classify(event.type) is not EventClass.ACTION:
            continue
        if is_in_scope(event.identity, scope):
            in_scope += 1
        else:
            out_of_scope += 1
    return in_scope, out_of_scope
