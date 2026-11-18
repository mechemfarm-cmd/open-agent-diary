"""The neutral export format: how a work trace is written down.

The adapter consumes this format. Something has to produce it, and this module
is that — and only that.

**It cannot read anyone's history.** It writes already-neutral records to a file
and validates files someone else produced. It knows no database, imports no
driver, and has no concept of where a Diary lives. The piece that opens a real
store belongs outside this package, on the far side of an export step someone
chose to run. That is what lets this software be installed without granting it
access to anybody's memory.

The format is deliberately small — three required fields and three optional
ones. Every field not written is a field that cannot leak.

    {"capture_id": "...", "action": "write", "wall_time": 1700000000.0,
     "target": "/home/x/work/report.txt", "tool": "write_file"}

- ``capture_id`` — required. The supervisor-issued run identity. Without it a
  claim cannot be linked to anything, so it is not optional by accident.
- ``action`` — required. A canonical verb, or a tool name the adapter maps one
  from. An unrecognised value is reported downstream rather than guessed at.
- ``wall_time`` — required. When the agent says it acted.
- ``target`` — optional. A path or destination, and the field most likely to
  carry something private. Omit it when it is not needed.
- ``monotonic_time`` — optional, for ordering.
- ``tool`` — optional, the original tool name, kept for the reader's benefit.

Unknown extra fields are reported by the validator rather than rejected: a
producer may legitimately add its own, and a validator that refuses them would
be obeyed by being bypassed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .claims import TOOL_ACTIONS, iter_jsonl
from .correlation import AgentClaim
from .events import TargetState, target_is_consistent

EXPORT_VERSION = 2

REQUIRED_FIELDS = ("capture_id", "action", "wall_time")
OPTIONAL_FIELDS = ("target", "monotonic_time", "tool", "target_state")


@dataclass(frozen=True)
class ExportProblem:
    line_number: int
    reason: str

    def to_dict(self) -> dict:
        return {"line": self.line_number, "reason": self.reason}


@dataclass
class ExportSummary:
    path: str
    written: int
    bytes_written: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "written": self.written,
            "bytes_written": self.bytes_written,
        }


@dataclass
class ExportValidation:
    records: int = 0
    problems: list[ExportProblem] = field(default_factory=list)
    field_counts: dict[str, int] = field(default_factory=dict)
    unknown_fields: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "records": self.records,
            "ok": self.ok,
            "field_counts": dict(self.field_counts),
            "unknown_fields": dict(self.unknown_fields),
            "problems": [problem.to_dict() for problem in self.problems],
        }


def to_record(claim: AgentClaim) -> dict:
    """Render a claim in the export format, omitting what is not set.

    Empty fields are left out rather than written as nulls, so a reader cannot
    mistake "no target recorded" for "target recorded as nothing".

    ``target_state`` is written whenever it is not already implied by the
    target's presence, so an explicit absence or a redaction survives the
    boundary. Without it the round trip turned every ABSENT claim back into
    UNKNOWN, and the agreement the core had just learned to make soundly could
    never reach it through the real path.
    """
    record: dict = {
        "capture_id": claim.capture_id,
        "action": claim.action,
        "wall_time": claim.wall_time,
    }
    if claim.target is not None:
        record["target"] = claim.target
    if claim.monotonic_time is not None:
        record["monotonic_time"] = claim.monotonic_time
    if claim.tool is not None:
        record["tool"] = claim.tool

    implied = (
        TargetState.PRESENT if claim.target is not None else TargetState.UNKNOWN
    )
    if claim.target_state is not implied:
        record["target_state"] = claim.target_state.value
    return record


def write_export(
    records: Iterable[Mapping], path: str | os.PathLike[str]
) -> ExportSummary:
    """Write records as JSON lines, atomically."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")

    written = 0
    with open(temporary, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(dict(record), sort_keys=True) + "\n")
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)

    return ExportSummary(
        path=str(target), written=written, bytes_written=target.stat().st_size
    )


def write_claims(claims: Iterable[AgentClaim], path: str | os.PathLike[str]) -> ExportSummary:
    """Write claims as an export file."""
    return write_export((to_record(claim) for claim in claims), path)


def validate_export(text: str) -> ExportValidation:
    """Check that a file is a usable export, and say what is in it.

    Validating someone else's export matters as much as producing a good one: a
    file that is quietly missing run identities would otherwise show up later as
    a pile of unmatched claims, with nothing pointing at the real cause.
    """
    result = ExportValidation()

    for number, line in iter_jsonl(text):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            result.problems.append(ExportProblem(number, "unreadable_json"))
            continue

        if not isinstance(payload, dict):
            result.problems.append(ExportProblem(number, "record_is_not_an_object"))
            continue

        result.records += 1
        for key in payload:
            result.field_counts[key] = result.field_counts.get(key, 0) + 1
            if key not in REQUIRED_FIELDS and key not in OPTIONAL_FIELDS:
                result.unknown_fields[key] = result.unknown_fields.get(key, 0) + 1

        for key in REQUIRED_FIELDS:
            if key not in payload or payload[key] in (None, ""):
                result.problems.append(ExportProblem(number, f"missing_{key}"))

        time_value = payload.get("wall_time")
        if time_value is not None:
            try:
                float(time_value)
            except (TypeError, ValueError):
                result.problems.append(ExportProblem(number, "wall_time_not_a_number"))

        action = payload.get("action")
        if action is not None:
            normalised = str(action).strip().lower()
            if normalised not in TOOL_ACTIONS:
                result.problems.append(
                    ExportProblem(number, f"unrecognised_action:{action}")
                )

        # The state is validated here as well as in the adapter. The adapter is
        # what actually refuses a bad record, so this is not the only defence —
        # but a function called validate_export should not hand back "usable"
        # for a file whose states are unreadable or contradict their targets.
        declared = payload.get("target_state")
        if declared is not None:
            try:
                state = TargetState(str(declared).strip().lower())
            except ValueError:
                result.problems.append(
                    ExportProblem(number, f"unrecognised_target_state:{declared}")
                )
            else:
                if not target_is_consistent(payload.get("target"), state):
                    result.problems.append(
                        ExportProblem(number, "target_state_contradicts_target")
                    )

    return result


def validate_file(path: str | os.PathLike[str]) -> ExportValidation:
    """Validate an export file on disk."""
    return validate_export(Path(path).read_text(encoding="utf-8"))
