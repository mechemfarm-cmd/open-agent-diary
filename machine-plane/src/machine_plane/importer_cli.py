"""``machine-plane-import`` — unprivileged ingestion of sealed segments.

Gate 2 is a **mandatory precondition here, not a report**. A segment is scanned
before it is imported; a segment that fails is refused and recorded as refused,
and never ingested as ordinary machine evidence. A detector an operator can run
after the fact is not a boundary.

Unprivileged by design: it reads segments through the ``mp-read`` group and cannot
write, truncate or delete anything in the spool. It writes only its own store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .importer import Importer
from .privacy_gate import run_gate
from .accounting import Counts
from .privacy_scan import account_segment, gate2
from .spool import Spool, SpoolError

DEFAULT_PROBES = Path("/usr/lib/machine-plane/probes")


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
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        self._keys.add(record["record_key"])


def _producer_module() -> Path:
    import machine_plane

    return Path(machine_plane.__file__).parent / "producers" / "linux_ebpf.py"


def _refuse(refused_dir: Path, segment: Path, scan) -> Path:
    """Record a refusal next to the store, where the importer may write."""
    refused_dir.mkdir(parents=True, exist_ok=True)
    target = refused_dir / f"{segment.name}.refused.json"
    target.write_text(
        json.dumps(
            {
                "segment": segment.name,
                "size": segment.stat().st_size,
                "refused_at": time.time(),
                "findings": [str(finding) for finding in scan.findings],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="machine-plane-import")
    parser.add_argument("--spool", default="/var/lib/machine-plane/spool")
    parser.add_argument("--store", default="/var/lib/machine-plane/store/events.ndjson")
    parser.add_argument("--checkpoint-dir", default="/var/lib/machine-plane/store/checkpoints")
    parser.add_argument("--refused-dir", default="/var/lib/machine-plane/store/refused")
    parser.add_argument("--probes", default=str(DEFAULT_PROBES))
    parser.add_argument("--scope", default=None, help="scope whose events count as attributable")
    parser.add_argument("--recorder", default="machine-plane-recorder")
    args = parser.parse_args(argv)

    spool_dir = Path(args.spool)
    store = FileStore(Path(args.store))
    checkpoints = Path(args.checkpoint_dir)
    checkpoints.mkdir(parents=True, exist_ok=True)

    # Gate 1 first: without a known field set, Gate 2 cannot tell an undefined
    # field from a legitimate one. No probes means no schema, which means no
    # import — failing closed rather than importing on an assumed schema.
    probe_paths = sorted(Path(args.probes).glob("*.bt"))
    gate1 = run_gate(probe_paths, producer_module=_producer_module())
    print(gate1.report())
    if not gate1.passed:
        print("\nrefusing to import: Gate 1 failed, so no schema is trustworthy",
              file=sys.stderr)
        return 1

    segments = sorted(spool_dir.glob("segment-*.ndjson"))
    if not segments:
        print("no sealed segments to import")
        return 0

    totals = Counts(scope_supplied=args.scope is not None)
    imported_total = duplicates_total = refused_records = already_total = 0
    refused: list[str] = []

    def accumulate(total: Counts, part: Counts) -> None:
        total.lifecycle += part.lifecycle
        total.action += part.action
        total.gaps += part.gaps
        total.unreadable += part.unreadable
        total.in_scope_actions += part.in_scope_actions
        total.out_of_scope_actions += part.out_of_scope_actions
        total.unmatched_actions += part.unmatched_actions

    for segment in segments:
        checkpoint = checkpoints / f"{segment.name}.offset"
        offset = 0
        if checkpoint.exists():
            try:
                offset = int(json.loads(checkpoint.read_text()).get("offset", 0))
            except ValueError:
                offset = 0

        try:
            counts, _events, already = account_segment(
                segment,
                recorder_id=args.recorder,
                checkpoint_offset=offset,
                expected_scope=args.scope,
            )
        except ValueError as error:
            print(f"  FAILED {segment.name}: {error}", file=sys.stderr)
            return 1

        # The scan is a precondition, not a report.
        scan = gate2(segment, recorder_id=args.recorder, probe_fields=gate1.fields)
        if not scan.passed:
            path = _refuse(Path(args.refused_dir), segment, scan)
            refused.append(segment.name)
            refused_records += counts.records
            already_total += already
            accumulate(totals, counts)
            print(f"  REFUSED {segment.name} — see {path}")
            for finding in scan.findings[:5]:
                print(f"    {finding}")
            continue

        try:
            result = Importer(
                Spool(segment, args.recorder),
                recorder_id=args.recorder,
                store=store,
                checkpoint_path=checkpoint,
            ).run()
        except (SpoolError, ValueError) as error:
            print(f"  FAILED {segment.name}: {error}", file=sys.stderr)
            return 1

        if not (counts.totals_balance and counts.actions_balance):
            print(f"  ACCOUNTING MISMATCH {segment.name}", file=sys.stderr)
            for line in counts.report_lines():
                print(f"    {line}", file=sys.stderr)
            return 1

        imported_total += result.imported
        duplicates_total += result.duplicates
        already_total += already
        accumulate(totals, counts)
        for line in counts.report_lines():
            print(f"  {line}")

    print()
    for line in totals.report_lines():
        print(line)

    ingested = imported_total + duplicates_total + refused_records + already_total
    print(
        f"ingested  : {imported_total} imported + {duplicates_total} duplicates"
        f" + {refused_records} refused + {already_total} already = {ingested}"
        f"  [{'reconciles' if ingested == totals.records else 'DOES NOT RECONCILE'}]"
    )
    print(f"store     : {store.path}")

    if args.scope is None:
        print("  NOTE: no scope supplied — these records are unattributed and must not")
        print("        be read as evidence of what the workload did.")

    if refused:
        print(f"\nrefused segments: {len(refused)} — not imported as evidence",
              file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
