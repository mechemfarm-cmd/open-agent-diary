"""Provenance migration for records written before capture modes existed.

The evidence store contains records from segments whose headers carry no
``capture_mode``. Those records cannot be read as workload evidence — nothing in
them says what mode produced them, and at least one such segment is a host-wide
capture. This module builds the manifest that has to exist before any record moves:
per segment, what it is, what it contributed, and where every stored record will go.

Two properties matter more than the mechanics:

* **exactly once** — every record in the store is accounted for in exactly one
  destination, or the migration refuses to proceed;
* **no promotion** — a mode-less segment stays legacy even if its contents look
  filtered. Looking like evidence is not the same as having provenance.

The store record key already names its segment
(``recorder:segment-<name>.ndjson:<offset>``), so attribution needs no heuristics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .accounting import Counts
from .privacy_scan import account_segment

#: Destinations a record can be assigned to.
FILTERED = "filtered"
CONTROL = "control"
LEGACY = "legacy"


@dataclass
class SegmentProvenance:
    name: str
    mode: str
    in_spool: bool
    records_in_store: int = 0
    size_bytes: int | None = None
    sha256: str | None = None
    records_in_segment: int | None = None
    in_scope: int | None = None
    out_of_scope: int | None = None
    note: str = ""

    @property
    def destination(self) -> str:
        if self.mode == "control":
            return CONTROL
        if self.mode == "filtered":
            return FILTERED
        return LEGACY


@dataclass
class Manifest:
    store: str
    store_records: int
    store_sha256: str
    segments: list[SegmentProvenance] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        totals = {FILTERED: 0, CONTROL: 0, LEGACY: 0}
        for segment in self.segments:
            totals[segment.destination] += segment.records_in_store
        return totals

    @property
    def accounted(self) -> int:
        return sum(self.totals.values())

    @property
    def exactly_once(self) -> bool:
        """Every stored record has exactly one destination, and nothing is lost."""
        return self.accounted == self.store_records

    def to_dict(self) -> dict:
        return {
            "store": self.store,
            "store_records": self.store_records,
            "store_sha256": self.store_sha256,
            "totals": self.totals,
            "exactly_once": self.exactly_once,
            "segments": [
                {
                    "name": s.name,
                    "mode": s.mode,
                    "in_spool": s.in_spool,
                    "records_in_store": s.records_in_store,
                    "size_bytes": s.size_bytes,
                    "sha256": s.sha256,
                    "records_in_segment": s.records_in_segment,
                    "in_scope": s.in_scope,
                    "out_of_scope": s.out_of_scope,
                    "destination": s.destination,
                    "note": s.note,
                }
                for s in self.segments
            ],
        }


def _digest(path: Path, chunk: int = 1 << 20) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            hasher.update(block)
    return hasher.hexdigest()


def segment_mode(path: Path) -> tuple[str, str]:
    """The mode a segment records, plus a note when it records none.

    ``unknown`` covers both a header that lacks ``capture_mode`` and a segment with no
    readable header at all. They are different defects with the same consequence:
    nothing in the artifact says what produced it, so neither may be promoted.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line).get("event", {})
                details = event.get("platform_details") or {}
                if "capture_id" not in details and not details.get("scope"):
                    continue
                mode = details.get("capture_mode")
                if mode:
                    return mode, ""
                return "unknown", "header present, capture_mode absent"
    except (OSError, ValueError):
        return "unknown", "header unreadable"
    return "unknown", "no recorder_started header"


def store_attribution(store: Path) -> dict[str, int]:
    """Count store records per segment, using the record key's own segment name."""
    counts: dict[str, int] = {}
    with store.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            key = json.loads(line).get("record_key", "")
            parts = key.split(":", 2)
            name = parts[1] if len(parts) > 2 else "<unattributable>"
            counts[name] = counts.get(name, 0) + 1
    return counts


def build_manifest(store: Path, spool: Path) -> Manifest:
    """Describe where every stored record belongs. Reads only; changes nothing."""
    attribution = store_attribution(store)
    segments: list[SegmentProvenance] = []

    for name, stored in sorted(attribution.items()):
        path = spool / name
        entry = SegmentProvenance(name=name, mode="unknown", in_spool=path.exists(),
                                  records_in_store=stored)
        if path.exists():
            entry.size_bytes = path.stat().st_size
            entry.sha256 = _digest(path)
            mode, note = segment_mode(path)
            entry.mode, entry.note = mode, note
            try:
                counts, _events, _already = account_segment(
                    path, recorder_id="machine-plane-recorder"
                )
                entry.records_in_segment = counts.records
                entry.in_scope = counts.in_scope_actions
                entry.out_of_scope = counts.out_of_scope_actions
            except ValueError as error:
                entry.note = f"{entry.note}; accounting refused: {error}"
        else:
            entry.note = "segment no longer in the spool; provenance cannot be re-derived"
        segments.append(entry)

    return Manifest(
        store=str(store),
        store_records=sum(attribution.values()),
        store_sha256=_digest(store),
        segments=segments,
    )


def verify_partition(manifest: Manifest, original: Path, filtered: Path, legacy: Path) -> list[str]:
    """Every source record present exactly once across the rebuilt stores."""
    problems: list[str] = []

    def keys(path: Path) -> list[str]:
        with path.open("r", encoding="utf-8") as handle:
            return [
                json.loads(line)["record_key"]
                for line in handle
                if line.strip()
            ]

    source = keys(original)
    moved = keys(filtered) + keys(legacy)
    if len(moved) != len(source):
        problems.append(f"record count differs: source {len(source)}, moved {len(moved)}")
    if len(set(moved)) != len(moved):
        problems.append("a record appears more than once across the rebuilt stores")
    missing = set(source) - set(moved)
    if missing:
        problems.append(f"{len(missing)} source records are absent from both stores")
    return problems
