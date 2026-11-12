"""Turning an exported work trace into agent claims.

The comparison in ``correlation`` needs the agent's side of the story. This
module produces it — from a **neutral export file**, never from a live store.

That constraint is structural, not a promise. This module opens exactly one kind
of thing: a text file of JSON lines. It does not import a database driver, does
not know where any Diary lives, and cannot be pointed at one. Whoever runs the
export decides what leaves their machine; whoever runs the adapter only sees
what they were handed.

Two rules carried over from the rest of the package:

**Nothing is dropped silently.** A record that cannot be adapted — no run
identity, no timestamp, an action nobody recognises — is reported with its line
number and a reason. Silently skipping it would make an unadaptable claim look
like a claim that never existed, which is the same failure the gap events exist
to prevent on the machine side.

**Identity is required.** A claim without a run identity cannot be linked to an
observed effect, so it is reported rather than guessed at. ``default_capture_id``
exists for single-run exports, and using it is counted and surfaced, because
attributing a whole file to one run is a real assumption and should not be
invisible.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .correlation import AgentClaim
from .events import TargetState

#: Tool and action names mapped to the canonical verbs ``correlation`` knows.
#: An unrecognised name is reported rather than matched loosely: a claim that
#: matches the wrong thing is worse than a claim that matches nothing.
TOOL_ACTIONS: dict[str, str] = {
    "write_file": "write",
    "write": "write",
    "edit": "write",
    "edit_file": "write",
    "create_file": "create",
    "create": "create",
    "read_file": "read",
    "read": "read",
    "cat": "read",
    "delete_file": "unlink",
    "unlink": "unlink",
    "remove": "unlink",
    "delete": "unlink",
    "move": "rename",
    "rename": "rename",
    "chmod": "metadata_change",
    "terminal": "exec",
    "bash": "exec",
    "shell": "exec",
    "run_command": "exec",
    "exec": "exec",
    "run": "exec",
    "web_fetch": "connect",
    "fetch": "connect",
    "http_request": "connect",
    "connect": "connect",
}

_CAPTURE_KEYS = ("capture_id", "run_id", "run")
_ACTION_KEYS = ("action", "tool", "kind", "name")
_TARGET_KEYS = ("target", "path", "url", "destination", "file")
_TIME_KEYS = ("wall_time", "timestamp", "time", "at", "when")


@dataclass(frozen=True)
class AdaptProblem:
    """One record that could not become a claim, and why."""

    line_number: int
    reason: str

    def to_dict(self) -> dict:
        return {"line": self.line_number, "reason": self.reason}


@dataclass
class AdaptResult:
    claims: list[AgentClaim] = field(default_factory=list)
    problems: list[AdaptProblem] = field(default_factory=list)
    used_default_capture_id: int = 0
    read: int = 0

    @property
    def adapted(self) -> int:
        return len(self.claims)

    @property
    def skipped(self) -> int:
        return len(self.problems)

    def to_dict(self) -> dict:
        return {
            "read": self.read,
            "adapted": self.adapted,
            "skipped": self.skipped,
            "used_default_capture_id": self.used_default_capture_id,
            "problems": [problem.to_dict() for problem in self.problems],
        }


def _first(payload: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def adapt_record(
    payload: Any,
    *,
    line_number: int = 0,
    default_capture_id: str | None = None,
) -> tuple[AgentClaim | None, AdaptProblem | None, bool]:
    """Adapt one record.

    Returns ``(claim, problem, used_default)``. Exactly one of claim and problem
    is set.
    """
    if not isinstance(payload, dict):
        return None, AdaptProblem(line_number, "record_is_not_an_object"), False

    capture_id = _first(payload, _CAPTURE_KEYS)
    used_default = False
    if capture_id is None and default_capture_id is not None:
        capture_id = default_capture_id
        used_default = True
    if capture_id is None:
        return None, AdaptProblem(line_number, "missing_run_identity"), False

    raw_action = _first(payload, _ACTION_KEYS)
    if raw_action is None:
        return None, AdaptProblem(line_number, "missing_action"), used_default

    action = TOOL_ACTIONS.get(str(raw_action).strip().lower())
    if action is None:
        return (
            None,
            AdaptProblem(line_number, f"unrecognised_action:{raw_action}"),
            used_default,
        )

    wall_time = _first(payload, _TIME_KEYS)
    if wall_time is None:
        return None, AdaptProblem(line_number, "missing_timestamp"), used_default
    try:
        wall_time = float(wall_time)
    except (TypeError, ValueError):
        return None, AdaptProblem(line_number, "timestamp_is_not_a_number"), used_default

    target = _first(payload, _TARGET_KEYS)
    if target is not None:
        target = str(target)

    # A record that carries a target states it. A record that does not has not
    # said there was none, so the claim stays UNKNOWN rather than asserting an
    # absence nobody claimed. A record may say so explicitly.
    declared_state = payload.get("target_state")
    if declared_state is not None:
        try:
            target_state = TargetState(str(declared_state))
        except ValueError:
            return None, AdaptProblem(line_number, "unknown_target_state"), used_default
    else:
        target_state = (
            TargetState.PRESENT if target is not None else TargetState.UNKNOWN
        )

    monotonic = payload.get("monotonic_time")
    tool = payload.get("tool")

    return (
        AgentClaim(
            capture_id=str(capture_id),
            action=action,
            wall_time=wall_time,
            target=target,
            monotonic_time=float(monotonic) if monotonic is not None else None,
            tool=str(tool) if tool is not None else None,
            target_state=target_state,
        ),
        None,
        used_default,
    )


def iter_jsonl(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, line)`` for non-empty lines, 1-indexed."""
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            yield number, line


def adapt_jsonl(
    text: str, *, default_capture_id: str | None = None
) -> AdaptResult:
    """Adapt a JSON-lines export."""
    result = AdaptResult()
    for number, line in iter_jsonl(text):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            result.read += 1
            result.problems.append(AdaptProblem(number, "unreadable_json"))
            continue

        result.read += 1
        claim, problem, used_default = adapt_record(
            payload, line_number=number, default_capture_id=default_capture_id
        )
        if claim is not None:
            result.claims.append(claim)
        if problem is not None:
            result.problems.append(problem)
        if used_default and claim is not None:
            result.used_default_capture_id += 1

    return result


def adapt_file(
    path: str | os.PathLike[str], *, default_capture_id: str | None = None
) -> AdaptResult:
    """Adapt an exported file.

    The only thing this module ever opens is a text file of JSON lines. There is
    no database path, no connection string, and no import that could reach one.
    """
    text = Path(path).read_text(encoding="utf-8")
    return adapt_jsonl(text, default_capture_id=default_capture_id)
