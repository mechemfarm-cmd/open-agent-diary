"""Command line entry point.

Kept deliberately small. Its job is to make the system observable without a
sensor, because none has been chosen yet:

``demo``       runs the mock producer through the pipeline and the importer, and
               prints what survived redaction — including the gap records,
               because a run that reports only what it saw is telling half the
               story.

``correlate``  runs the same pipeline, then joins the machine's account to a set
               of agent claims and prints the result by category. Categories,
               not a divergence count: a disagreement is only useful if you know
               what kind it is.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .correlation import CorrelationStatus, RunRegistry, correlate
from .events import GAP_EVENT_TYPES, EventType
from .importer import Importer, MemoryStore
from .privacy import PathDisplay, PrivacyPolicy
from .producer import RawEvent
from .spool import Spool
from .testing import (
    MockProducer,
    action_event,
    gap_event,
    make_claim,
    make_registration,
)

WALL = 1_700_000_000.0


def _demo_events() -> list[RawEvent]:
    return [
        action_event(EventType.EXEC, target="/usr/bin/python3"),
        action_event(
            EventType.WRITE_ATTEMPT,
            target="/home/x/Documents/journal/day.txt",
        ),
        action_event(
            EventType.WRITE_ATTEMPT,
            target="/home/x/.ssh/id_ed25519",
        ),
        action_event(
            EventType.CONNECT,
            target="https://api.example.com/v1/complete?token=secret",
        ),
        action_event(
            EventType.WRITE_ATTEMPT,
            target="/home/x/notes.txt",
            details={"command_line": "echo hello > notes.txt", "pid": 4821},
        ),
        gap_event(
            EventType.BUFFER_OVERFLOW,
            wall_time=WALL + 20,
            details={"lost": 17},
            result="lost_events=17",
        ),
    ]


def _correlate_events() -> list[RawEvent]:
    return [
        gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
        # The agent said it would write this, and the machine saw it.
        action_event(
            EventType.WRITE_ATTEMPT,
            target="/home/x/work/report.txt",
            wall_time=WALL,
        ),
        # The agent said it would write this, but policy dropped the target, so
        # the two accounts cannot be compared at all.
        action_event(
            EventType.WRITE_ATTEMPT,
            target="/home/x/private/notes.txt",
            wall_time=WALL,
        ),
        # Nothing claimed this.
        action_event(
            EventType.WRITE_ATTEMPT,
            target="/home/x/work/secret.txt",
            wall_time=WALL,
        ),
        # After this, the recorder was blind for a while.
        gap_event(EventType.BUFFER_OVERFLOW, wall_time=WALL + 10),
    ]


def _correlate_claims() -> list:
    return [
        make_claim("write", target="/home/x/work/report.txt", wall_time=WALL),
        make_claim("write", target="/home/x/private/notes.txt", wall_time=WALL),
        make_claim("write", target="/home/x/work/missing.txt", wall_time=WALL),
        make_claim("write", target="/home/x/work/blind.txt", wall_time=WALL + 50),
    ]


def _run_demo(policy: PrivacyPolicy) -> int:
    with tempfile.TemporaryDirectory() as directory:
        spool = Spool(Path(directory) / "machine.spool", recorder="demo-recorder")
        emitted = MockProducer(_demo_events(), policy=policy).run(spool)

        store = MemoryStore()
        result = Importer(spool, "demo-recorder", store).run()

        print(f"emitted to spool : {emitted}")
        print(f"imported         : {result.imported}")
        print(f"duplicates       : {result.duplicates}")
        print()
        for record in store.records:
            event = record["event"]
            print(
                f"  {event['type']:<18} "
                f"target={event['target']!r:<34} "
                f"details={event['platform_details']}"
            )
        print()
        gaps = [
            record
            for record in store.records
            if record["event"]["type"] in {item.value for item in GAP_EVENT_TYPES}
        ]
        print(f"gap records      : {len(gaps)}")
    return 0


def _run_correlate(policy: PrivacyPolicy) -> int:
    events = list(MockProducer(_correlate_events(), policy=policy).events())
    registry = RunRegistry([make_registration()])
    report = correlate(
        events, _correlate_claims(), registry, policy=policy
    )

    print("machine events   :", len(events))
    print("agent claims     :", len(_correlate_claims()))
    print()
    print("by category:")
    for status in CorrelationStatus:
        count = report.counts().get(status.value, 0)
        if count:
            print(f"  {status.value:<24} {count}")
    print()
    print("worth investigating:")
    for record in report.exceptions():
        target = record.event.target if record.event else None
        if target is None and record.claim is not None:
            target = record.claim.target
        print(f"  {record.status.value:<24} {record.reason:<34} {target!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="machine-plane")
    parser.add_argument(
        "command",
        choices=["demo", "correlate"],
        help="demo: pipeline and importer; correlate: join the two accounts",
    )
    parser.add_argument(
        "--allowed-root",
        action="append",
        default=[],
        help="path root inside which full paths may survive redaction",
    )
    parser.add_argument(
        "--path-display",
        choices=[item.value for item in PathDisplay],
        default=PathDisplay.BASENAME.value,
    )
    args = parser.parse_args(argv)

    policy = PrivacyPolicy(
        allowed_roots=tuple(args.allowed_root),
        path_display=PathDisplay(args.path_display),
    )

    if args.command == "correlate":
        return _run_correlate(policy)
    return _run_demo(policy)


if __name__ == "__main__":
    sys.exit(main())
