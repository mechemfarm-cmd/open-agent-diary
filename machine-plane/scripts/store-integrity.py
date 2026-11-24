"""Read-only integrity analysis of the evidence store.

Answers, per record: is this key unique, does its byte offset land on a real line
start in the artifact that still exists, and does the key ever point at more than one
event? Nothing here writes to the store.

Offset semantics matter, and the first version of this tool got them wrong twice over.
``record_key`` carries a **byte offset** into the segment, not a record index, so it
must be compared against byte positions rather than line counts. Comparing against a
line count made every sizeable segment look stale. But byte size alone is also too
weak: a small replacement file can still be longer than a stored offset.

Two tests, the second decisive:

* is the offset below the artifact's byte size?
* does the offset land exactly on a line start?

An offset that is not a line boundary in this file did not come from this file,
whatever its magnitude. That is the test that settles provenance.

Classification, strongest evidence first:

  supported-by-artifact   every offset lands on a line start, mode recorded
  legacy-unique-key       every offset lands on a line start, mode absent
  artifact-mismatch       offsets beyond the file, or not on line starts
  no-artifact             no segment file exists to check against

``artifact-mismatch`` and ``no-artifact`` are both legacy/unverifiable. They must not
be assigned a guessed provenance, because nothing survives that could confirm it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

STORE = Path(sys.argv[1])
OUT = Path(sys.argv[2])
SPOOL = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/var/lib/machine-plane/spool")

SUSPECTS = ("mp-stage3-gate-20261118T134118Z", "mp-stage3-gate2-20261118T134145Z")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def segment_facts(path: Path) -> dict:
    """Record count, byte size, hash, mode, and the byte offset of every line start."""
    starts: set[int] = set()
    offset = 0
    records = 0
    mode = "unknown"
    with path.open("rb") as fh:
        for line in fh:
            starts.add(offset)
            offset += len(line)
            records += 1
            if records == 1:
                details = (
                    json.loads(line.decode("utf-8", "replace"))
                    .get("event", {})
                    .get("platform_details")
                    or {}
                )
                mode = details.get("capture_mode") or "unknown"
    return {
        "records": records,
        "size": offset,
        "sha256": sha256(path),
        "mode": mode,
        "mtime": path.stat().st_mtime,
        "line_starts": starts,
    }


def main() -> int:
    key_counts: Counter[str] = Counter()
    key_events: dict[str, str] = {}
    key_collisions: dict[str, set] = defaultdict(set)
    event_ids: Counter[str] = Counter()
    per_segment: dict[str, dict] = defaultdict(
        lambda: {"records": 0, "offsets": [], "max_offset": -1}
    )
    total = 0
    unparseable = 0

    with STORE.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            total += 1
            record = json.loads(line)
            key = record.get("record_key", "")
            event_id = record.get("event_id") or record.get("event", {}).get("event_id") or ""
            key_counts[key] += 1
            event_ids[event_id] += 1

            parts = key.split(":")
            if len(parts) < 3:
                unparseable += 1
                continue
            segment, offset_text = parts[1], parts[2]
            try:
                offset = int(offset_text)
            except ValueError:
                unparseable += 1
                continue

            if key in key_events and key_events[key] != event_id:
                key_collisions[key].add(key_events[key])
                key_collisions[key].add(event_id)
            key_events.setdefault(key, event_id)

            entry = per_segment[segment]
            entry["records"] += 1
            entry["offsets"].append(offset)
            entry["max_offset"] = max(entry["max_offset"], offset)

    duplicate_keys = [k for k, n in key_counts.items() if n > 1]
    duplicate_event_ids = [e for e, n in event_ids.items() if n > 1]

    # (segment, offset) pairs that appear more than once
    pair_counts: Counter[tuple[str, int]] = Counter()
    with STORE.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            parts = json.loads(line).get("record_key", "").split(":")
            if len(parts) >= 3:
                try:
                    pair_counts[(parts[1], int(parts[2]))] += 1
                except ValueError:
                    pass
    duplicate_pairs = [pair for pair, n in pair_counts.items() if n > 1]

    segments = {}
    classification: Counter[str] = Counter()
    for name, facts in sorted(per_segment.items()):
        path = SPOOL / name
        offsets = facts["offsets"]
        info = {
            "store_records": facts["records"],
            "store_max_offset": facts["max_offset"],
            "store_duplicate_keys": sum(
                1 for k in duplicate_keys if k.split(":")[1:2] == [name]
            ),
            "artifact_present": path.exists(),
        }
        if path.exists():
            art = segment_facts(path)
            starts = art.pop("line_starts")
            info.update(
                {
                    "artifact_records": art["records"],
                    "artifact_size": art["size"],
                    "artifact_sha256": art["sha256"],
                    "artifact_mode": art["mode"],
                    "artifact_mtime": art["mtime"],
                }
            )
            on_line_start = sum(1 for o in offsets if o in starts)
            info["offsets_below_size"] = sum(1 for o in offsets if o < art["size"])
            info["offsets_on_line_start"] = on_line_start
            info["offsets_not_on_line_start"] = facts["records"] - on_line_start

            # The strong test decides. A stored offset that is not a line boundary in
            # this file did not come from this file, however small it happens to be.
            if on_line_start == facts["records"]:
                if art["mode"] == "unknown":
                    info["classification"] = "legacy-unique-key"
                    classification["legacy-unique-key"] += facts["records"]
                else:
                    info["classification"] = "supported-by-artifact"
                    classification["supported-by-artifact"] += facts["records"]
            else:
                info["classification"] = "artifact-mismatch"
                classification["artifact-mismatch"] += facts["records"]
        else:
            info["offsets_below_size"] = 0
            info["offsets_on_line_start"] = 0
            info["offsets_not_on_line_start"] = facts["records"]
            info["classification"] = "no-artifact"
            classification["no-artifact"] += facts["records"]
        segments[name] = info

    classification["duplicate-or-collision"] = len(duplicate_keys)
    classification["same-key-different-event"] = len(key_collisions)

    suspect_detail = {}
    for token in SUSPECTS:
        for name, info in segments.items():
            if token in name:
                offsets = sorted(per_segment[name]["offsets"])
                suspect_detail[name] = {
                    **info,
                    "offsets_min": offsets[0] if offsets else None,
                    "offsets_max": offsets[-1] if offsets else None,
                    "sample_offsets": offsets[:5] + offsets[-5:],
                }

    manifest = {
        "store": str(STORE),
        "store_sha256": sha256(STORE),
        "store_records": total,
        "offset_semantics": "byte offsets, tested against artifact size and exact line starts",
        "unparseable_keys": unparseable,
        "duplicate_record_keys": len(duplicate_keys),
        "duplicate_key_examples": duplicate_keys[:5],
        "duplicate_event_ids": len(duplicate_event_ids),
        "same_key_different_event": len(key_collisions),
        "duplicate_segment_offset_pairs": len(duplicate_pairs),
        "segments": segments,
        "suspects": suspect_detail,
        "classification": dict(classification),
        "legacy_unverifiable_total": (
            classification["artifact-mismatch"] + classification["no-artifact"]
        ),
    }
    OUT.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
