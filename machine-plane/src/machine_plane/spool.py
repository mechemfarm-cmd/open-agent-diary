"""The protected spool.

The spool is the ingestion boundary, and it is the *only* place a machine-plane
event can originate. The recorder writes; the importer reads. Because the agent
cannot write here — that is a permissions question, not a code question — it
cannot submit an event claiming to be machine-observed.

Each record is wrapped in an envelope carrying the recorder's identity, so the
importer can stamp ``plane=machine`` from the envelope rather than trusting
anything inside the event payload.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .events import MachineEvent

SPOOL_VERSION = 1


class SpoolError(Exception):
    pass


@dataclass(frozen=True)
class SpoolEnvelope:
    spool_version: int
    recorder: str
    recorded_at: float
    event: dict

    def to_json(self) -> str:
        return json.dumps(
            {
                "spool_version": self.spool_version,
                "recorder": self.recorder,
                "recorded_at": self.recorded_at,
                "event": self.event,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "SpoolEnvelope":
        version = payload.get("spool_version")
        if version != SPOOL_VERSION:
            raise SpoolError(
                f"spool record version {version!r} is not readable by this "
                f"build ({SPOOL_VERSION})"
            )
        return cls(
            spool_version=version,
            recorder=payload["recorder"],
            recorded_at=payload["recorded_at"],
            event=payload["event"],
        )


class Spool:
    """An append-only, recorder-owned spool of machine events."""

    def __init__(self, path: str | os.PathLike[str], recorder: str) -> None:
        self.path = Path(path)
        self.recorder = recorder

    def append(self, event: MachineEvent) -> None:
        """Append one record.

        The line is written with a single ``write`` on a descriptor opened
        ``O_APPEND``, so concurrent appends cannot interleave and a reader never
        observes a half-written record as complete.
        """
        # Last gate before disk. The producer pipeline already redacts, but the
        # spool is the boundary and refuses to write content in metadata-only
        # mode even if a producer skipped that stage.
        event.validate_for_storage()

        envelope = SpoolEnvelope(
            spool_version=SPOOL_VERSION,
            recorder=self.recorder,
            recorded_at=event.wall_time,
            event=event.to_dict(),
        )
        line = (envelope.to_json() + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)

    def size(self) -> int:
        if not self.path.exists():
            return 0
        return self.path.stat().st_size

    def read_from(self, offset: int = 0) -> Iterator[tuple[int, int, SpoolEnvelope]]:
        """Yield ``(start_offset, end_offset, envelope)`` from ``offset``.

        A trailing partial line is deliberately *not* consumed: the returned
        end offset stops before it, so a restart resumes at the same place
        instead of losing or half-reading a record.

        The start offset is yielded because it is the only thing that makes two
        records distinguishable when their content is identical — which happens
        legitimately whenever redaction maps two different paths to the same
        redacted value.
        """
        if not self.path.exists():
            return

        position = offset
        buffer = b""
        with open(self.path, "rb") as handle:
            handle.seek(offset)
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    index = buffer.find(b"\n")
                    if index < 0:
                        break
                    line = buffer[:index]
                    buffer = buffer[index + 1 :]
                    start = position
                    position += index + 1
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise SpoolError(
                            f"unreadable spool record at offset {start}: {error}"
                        ) from error
                    yield start, position, SpoolEnvelope.from_dict(payload)
