"""Read-only integrity analysis of the evidence store.

Answers, per record: is this key unique, does its offset exist in the artifact that
still remains, and does the key ever point at more than one event? Nothing here
writes to the store.

Classification, strongest to weakest evidence:

  supported-by-artifact   unique key, offset inside the current segment, mode recorded
  legacy-unique-key       unique key, offset inside the current segment, mode absent
  stale-key-beyond-file   unique key whose offset exceeds the current segment's records
  duplicate-or-collision  the key repeats, or one key names more than one event
  no-artifact             no segment file exists to check against
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

STORE = Path(sys.argv[1])
SPOOL = Path("/var/lib/machine-plane/spool")
OUT = Path(sys.argv[2])

SUSPECTS = ("mp-stage3-gate-20261118T134118Z", "mp-stage3-gate2-20261118T134145Z")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def segment_facts(path: Path) -> dict:
    lines = 0
    mode = "unknown"
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            lines += 1
            if lines == 1:
                details = json.loads(line).get("event", {}).get("platform_details") or {}
                mode = details.get("capture_mode") or "unknown"
    return {
        "records": lines,
        "size": path.stat().st_size,
        "sha256": sha256(path),
        "mode": mode,
        "mtime": path.stat().st_mtime,
    }


def main() -> int:
    key_counts: Counter[str] = Counter()
    key_events: dict[str, str] = {}
    key_collisions: dict[str, set] = defaultdict(set)
    event_ids: Counter[str] = Counter()
    per_segment: dict[str, dict] = defaultdict(
        lambda: {"records": 0, "offsets": [], "max_offset": -1, "duplicate_keys": 0}
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
    for key in duplicate_keys:
        parts = key.split(":")
        if len(parts) >= 3:
            per_segment[parts[1]]["duplicate_keys"] += 1

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
    classification = Counter()
    for name, facts in sorted(per_segment.items()):
        path = SPOOL / name
        present = path.exists()
        info = {
            "store_records": facts["records"],
            "store_max_offset": facts["max_offset"],
            "store_duplicate_keys": facts["duplicate_keys"],
            "artifact_present": present,
        }
        if present:
            file_facts = segment_facts(path)
            info.update(
                {
                    "artifact_records": file_facts["records"],
                    "artifact_size": file_facts["size"],
                    "artifact_sha256": file_facts["sha256"],
                    "artifact_mode": file_facts["mode"],
                    "artifact_mtime": file_facts["mtime"],
                }
            )
            # Offsets are BYTE offsets into the segment, not record indices, so the
            # bound is the artifact's size. Comparing them with its record count is
            # a unit error that makes every large segment look stale.
            inside = sum(1 for o in facts["offsets"] if o < file_facts["size"])
            info["offsets_inside_artifact"] = inside
            info["offsets_beyond_artifact"] = facts["records"] - inside
            if file_facts["mode"] == "unknown":
                classification["legacy-unique-key"] += inside
            else:
                classification["supported-by-artifact"] += inside
            classification["stale-key-beyond-file"] += facts["records"] - inside
        else:
            info["offsets_inside_artifact"] = 0
            info["offsets_beyond_artifact"] = facts["records"]
            classification["no-artifact"] += facts["records"]
        segments[name] = info

    classification["duplicate-or-collision"] = len(duplicate_keys)
    classification["same-key-different-event"] = len(key_collisions)

    suspect_detail = {}
    for token in SUSPECTS:
        for name, info in segments.items():
            if token in name:
                path = SPOOL / name
                offsets = sorted(per_segment[name]["offsets"])
                suspect_detail[name] = {
                    **info,
                    "offsets_min": offsets[0] if offsets else None,
                    "offsets_max": offsets[-1] if offsets else None,
                    "offsets_inside_artifact_size": sum(
                        1 for o in offsets if o < info.get("artifact_size", 0)
                    ),
                    "sample_offsets": offsets[:5] + offsets[-5:],
                    "artifact": str(path) if path.exists() else None,
                }

    manifest = {
        "store": str(STORE),
        "store_sha256": sha256(STORE),
        "store_records": total,
        "unparseable_keys": unparseable,
        "duplicate_record_keys": len(duplicate_keys),
        "duplicate_key_examples": duplicate_keys[:5],
        "duplicate_event_ids": len(duplicate_event_ids),
        "duplicate_event_id_examples": duplicate_event_ids[:5],
        "same_key_different_event": len(key_collisions),
        "same_key_different_event_examples": list(key_collisions.items())[:5],
        "duplicate_segment_offset_pairs": len(duplicate_pairs),
        "duplicate_pair_examples": [
            {"segment": s, "offset": o} for s, o in duplicate_pairs[:5]
        ],
        "segments": segments,
        "suspects": suspect_detail,
        "classification": dict(classification),
    }
    OUT.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
