"""The unprivileged importer.

The importer is the only component that writes machine events into Agent Diary,
and it cannot manufacture one. The ``plane`` field is stamped here from the
spool envelope, never read from the event payload — and the event schema has no
``plane`` field at all, so a forged payload has nowhere to put the claim.

Ingestion fails closed. An unreadable record stops the import rather than being
skipped, because a skipped record is indistinguishable from an event that never
happened. The checkpoint is advanced only after a record is safely stored, so a
crash mid-import resumes rather than losing or duplicating work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .events import MachineEvent
from .spool import Spool, SpoolError


class Store(Protocol):
    def has(self, record_key: str) -> bool: ...
    def append(self, record: dict) -> None: ...


class MemoryStore:
    """A minimal store, used by tests and for dry runs."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self._keys: set[str] = set()

    def has(self, record_key: str) -> bool:
        return record_key in self._keys

    def append(self, record: dict) -> None:
        self.records.append(record)
        self._keys.add(record["record_key"])


@dataclass
class ImportResult:
    imported: int = 0
    duplicates: int = 0
    offset: int = 0
    plane: str = "machine"
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.imported + self.duplicates


class Importer:
    """Reads the spool and writes machine events into a store."""

    def __init__(
        self,
        spool: Spool,
        recorder_id: str,
        store: Store,
        checkpoint_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.spool = spool
        self.recorder_id = recorder_id
        self.store = store
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None

    # -- checkpoint ------------------------------------------------------

    def load_offset(self) -> int:
        if not self.checkpoint_path or not self.checkpoint_path.exists():
            return 0
        try:
            payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(payload.get("offset", 0))

    def save_offset(self, offset: int) -> None:
        if not self.checkpoint_path:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"offset": offset}), encoding="utf-8")
        os.replace(temporary, self.checkpoint_path)

    # -- import ----------------------------------------------------------

    def run(self) -> ImportResult:
        """Import everything new. Raises rather than skipping bad records.

        The checkpoint advances after *each* record, not once at the end, so an
        interruption resumes at the last record actually stored rather than
        re-reading the whole spool. Re-reading would be harmless — dedupe is by
        content-derived event id — but it is slower and makes the stored offset
        a worse description of reality.
        """
        offset = self.load_offset()
        result = ImportResult(offset=offset)

        for start_offset, end_offset, envelope in self.spool.read_from(offset):
            if envelope.recorder != self.recorder_id:
                raise SpoolError(
                    f"spool record at offset {end_offset} claims recorder "
                    f"{envelope.recorder!r}, expected {self.recorder_id!r}"
                )

            # Raises SchemaMismatch on an unreadable record: fail closed, and
            # do not advance the checkpoint past it.
            event = MachineEvent.from_dict(envelope.event)

            # Identity is the record's position in the spool, not its content.
            # Two genuinely distinct events can redact to identical content —
            # two different paths both dropped by policy — and deduplicating on
            # content would silently merge them into one. A wrong merge is the
            # same class of error as a wrong match.
            record_key = f"{envelope.recorder}:{start_offset}"

            if self.store.has(record_key):
                result.duplicates += 1
                result.offset = end_offset
                self.save_offset(end_offset)
                continue

            self.store.append(
                {
                    # Stamped by the importer, never taken from the payload.
                    "plane": "machine",
                    "record_key": record_key,
                    "event_id": event.event_id,
                    "recorder": envelope.recorder,
                    "recorded_at": envelope.recorded_at,
                    "event": event.to_dict(),
                }
            )
            result.imported += 1
            result.offset = end_offset
            self.save_offset(end_offset)

        self.save_offset(result.offset)
        return result
