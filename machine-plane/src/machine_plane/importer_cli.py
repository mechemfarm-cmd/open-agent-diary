"""``machine-plane-import`` — unprivileged ingestion of sealed segments.

The core :class:`~machine_plane.importer.Importer` reads one spool. A Stage 3 run
produces one segment per run, so this walks the sealed segments in order and
imports each, keeping a per-segment checkpoint.

Unprivileged by design: it reads segments through the ``mp-read`` group and
cannot write, truncate or delete anything in the spool. It writes only its own
store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .importer import Importer
from .spool import Spool, SpoolError


class FileStore:
    """Append-only JSONL store, with record-key deduplication."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._keys: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    self._keys.add(json.loads(line)["record_key"])
                except (ValueError, KeyError):
                    continue

    def has(self, record_key: str) -> bool:
        return record_key in self._keys

    def append(self, record: dict) -> None:
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        self._keys.add(record["record_key"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="machine-plane-import")
    parser.add_argument("--spool", default="/var/lib/machine-plane/spool")
    parser.add_argument("--store", default="/var/lib/machine-plane/store/events.ndjson")
    parser.add_argument("--checkpoint-dir", default="/var/lib/machine-plane/store/checkpoints")
    parser.add_argument("--recorder", default="machine-plane-recorder")
    args = parser.parse_args(argv)

    spool_dir = Path(args.spool)
    store = FileStore(Path(args.store))
    checkpoints = Path(args.checkpoint_dir)
    checkpoints.mkdir(parents=True, exist_ok=True)

    segments = sorted(spool_dir.glob("segment-*.ndjson"))
    if not segments:
        print("no sealed segments to import")
        return 0

    total_imported = 0
    total_duplicates = 0
    failures: list[str] = []

    for segment in segments:
        try:
            result = Importer(
                Spool(segment, args.recorder),
                recorder_id=args.recorder,
                store=store,
                checkpoint_path=checkpoints / f"{segment.name}.offset",
            ).run()
        except (SpoolError, ValueError) as error:
            # Fail closed: a record that cannot be read is not skipped, because a
            # skipped record is indistinguishable from an event that never happened.
            failures.append(f"{segment.name}: {error}")
            break

        total_imported += result.imported
        total_duplicates += result.duplicates
        print(f"  {segment.name}: imported {result.imported}, duplicates {result.duplicates}")

    print(f"imported   : {total_imported}")
    print(f"duplicates : {total_duplicates}")
    print(f"store      : {store.path}")

    if failures:
        for failure in failures:
            print(f"failed     : {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
