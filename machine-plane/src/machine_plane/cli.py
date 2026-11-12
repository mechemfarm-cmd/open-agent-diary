"""Command line entry point.

Kept deliberately small. Its job is to make the pipeline observable without a
sensor: run the mock producer into a spool, import the spool, and print what
happened — including the gap events, because a run that reports only what it saw
is telling half the story.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from .events import GAP_EVENT_TYPES, EventType
from .identity import IdentityConfidence
from .importer import Importer, MemoryStore
from .privacy import PathDisplay, PrivacyPolicy
from .producer import RawEvent
from .spool import Spool
from .testing import MockProducer, action_event


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
        RawEvent(
            type=EventType.BUFFER_OVERFLOW,
            monotonic_time=30.0,
            wall_time=1_700_000_020.0,
            identity=None,
            target=None,
            result="lost_events=17",
            details={"lost": 17},
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="machine-plane")
    parser.add_argument(
        "command",
        choices=["demo"],
        help="demo: run the mock producer through the pipeline and importer",
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

    with tempfile.TemporaryDirectory() as directory:
        spool = Spool(Path(directory) / "machine.spool", recorder="demo-recorder")
        producer = MockProducer(_demo_events(), policy=policy)
        emitted = producer.run(spool)

        store = MemoryStore()
        importer = Importer(spool, "demo-recorder", store)
        result = importer.run()

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
            if record["event"]["type"] in {t.value for t in GAP_EVENT_TYPES}
        ]
        print(f"gap records      : {len(gaps)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
