"""Reversible rebuild of the evidence store.

The store accumulated records while the recorder's segment allocator could reuse a
path. When a path was reused, publication overwrote the artifact while the store kept
every record imported from the larger original. The allocator is fixed; this repairs
the corpus.

The design is a **partition, not a rebuild**. The store is not corrupt — there are no
duplicate keys and no collisions — it is unverifiable: for most records nothing survives
that can confirm what produced them. So records that can be supported become the
evidence store, and the rest are preserved in a legacy store where they are kept but
never presented as evidence.

Two invariants govern everything here:

* **nothing is promoted** — evidence requires an artifact that exists, every byte offset
  landing on a line start in it, and an explicit capture mode. Looking filtered is not
  provenance;
* **recovery rolls back, never forward** — the previous generation is only ever renamed,
  never modified, so rollback is always available. Finishing a partial swap would mean
  judging which generation is real, which is the guess this protocol exists to avoid.

Nothing moves unless the caller asks for it explicitly. ``plan`` and ``stage`` are
read-only with respect to the live store.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Destination names, and the classification each accepts.
EVIDENCE = "evidence"
CONTROL = "control"
LEGACY = "legacy"

DESTINATIONS = (EVIDENCE, CONTROL, LEGACY)

#: Which classifications may enter which destination. `legacy-unique-key` is legacy and
#: not evidence: matching bytes and offsets prove artifact identity, not capture mode.
ROUTING = {
    "supported-by-artifact": EVIDENCE,
    "control": CONTROL,
    "legacy-unique-key": LEGACY,
    "artifact-mismatch": LEGACY,
    "no-artifact": LEGACY,
}

#: The two classifications that may never be promoted, whatever they look like.
NEVER_PROMOTED = frozenset({"artifact-mismatch", "no-artifact"})

HEADROOM = 0.10


class RebuildRefused(RuntimeError):
    """A precondition failed. The rebuild stops rather than proceeding on a guess."""


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durably(path: Path, text: str) -> None:
    """Write, flush to disk, then make the directory entry durable.

    A rename is only durable once the directory entry is, so every manifest and progress
    write fsyncs both.
    """
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(path.parent)


@dataclass
class SegmentRoute:
    name: str
    classification: str
    destination: str
    store_records: int
    artifact_sha256: str | None = None
    artifact_present: bool = False


@dataclass
class Plan:
    store: Path
    snapshot: Path
    snapshot_sha256: str
    manifest_path: Path
    manifest_sha256: str
    routes: dict[str, SegmentRoute] = field(default_factory=dict)
    expected_counts: dict[str, int] = field(default_factory=dict)
    source_records: int = 0

    def destination_for(self, segment: str) -> str:
        route = self.routes.get(segment)
        if route is None:
            # A record whose segment the reviewed manifest does not mention cannot be
            # classified, and inventing a destination would be the guess the whole
            # protocol exists to avoid.
            raise RebuildRefused(
                f"segment {segment!r} appears in the store but not in the manifest — "
                "refusing to classify it"
            )
        return route.destination


def load_plan(
    *,
    store: Path,
    snapshot: Path,
    manifest_path: Path,
    spool: Path,
    verify_artifacts: bool = True,
) -> Plan:
    """Read the reviewed manifest and check the inputs are the ones that were reviewed."""
    if not snapshot.exists():
        raise RebuildRefused(f"snapshot not found: {snapshot}")
    if not manifest_path.exists():
        raise RebuildRefused(f"manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = Plan(
        store=store,
        snapshot=snapshot,
        snapshot_sha256=sha256_file(snapshot),
        manifest_path=manifest_path,
        manifest_sha256=sha256_file(manifest_path),
        source_records=manifest["store_records"],
    )

    for name, info in manifest["segments"].items():
        classification = info["classification"]
        destination = ROUTING.get(classification)
        if destination is None:
            raise RebuildRefused(f"unknown classification {classification!r} for {name}")
        route = SegmentRoute(
            name=name,
            classification=classification,
            destination=destination,
            store_records=info["store_records"],
            artifact_sha256=info.get("artifact_sha256"),
            artifact_present=bool(info.get("artifact_present")),
        )
        if verify_artifacts and route.destination == EVIDENCE:
            artifact = spool / name
            if not artifact.exists():
                raise RebuildRefused(
                    f"{name} is classified as evidence but its artifact is gone — the "
                    "manifest and the spool disagree, so neither can be trusted"
                )
            if route.artifact_sha256 and sha256_file(artifact) != route.artifact_sha256:
                raise RebuildRefused(
                    f"{name} artifact hash does not match the manifest — it changed "
                    "after review"
                )
        plan.routes[name] = route

    for destination in DESTINATIONS:
        plan.expected_counts[destination] = sum(
            route.store_records
            for route in plan.routes.values()
            if route.destination == destination
        )
    if sum(plan.expected_counts.values()) != plan.source_records:
        raise RebuildRefused(
            f"manifest does not account for every record: "
            f"{plan.expected_counts} vs {plan.source_records}"
        )
    return plan


# -- quiescence ---------------------------------------------------------------


@dataclass
class Quiescence:
    lock_path: Path
    evidence: dict
    _held: bool = False

    def release(self) -> None:
        if self._held:
            self.lock_path.unlink(missing_ok=True)
            self._held = False


def prove_quiescence(store: Path, spool: Path) -> Quiescence:
    """Establish that no writer is active, and record the evidence.

    Every check is recorded, not just passed: the transaction manifest states what was
    true at the time rather than asserting that something was quiet.
    """
    lock = store / ".rebuild.lock"
    store.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError as error:
        raise RebuildRefused(
            f"{lock} exists — another rebuild holds it, or one did not finish"
        ) from error

    evidence: dict = {"lock_acquired_at": time.time(), "lock_path": str(lock)}
    try:
        partials = sorted(str(p) for p in spool.glob("segment-*.ndjson.partial"))
        evidence["partials"] = partials
        if partials:
            # No age rule. A partial is a run that did not seal, and an unsealed run is
            # exactly what leaves artifacts that do not match their records. Age says
            # nothing about relevance.
            raise RebuildRefused(
                f"{len(partials)} unsealed .partial file(s) in the spool — resolve them "
                "with `machine-plane-recorder recover` first: " + ", ".join(partials[:3])
            )

        running = []
        for name in ("machine-plane-recorder", "machine-plane-import"):
            try:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
                    except OSError:
                        continue
                    if name in cmdline:
                        running.append(f"{name}:{entry.name}")
            except OSError:
                pass
        evidence["running_writers"] = running
        if running:
            raise RebuildRefused(f"writer processes are running: {', '.join(running)}")
    except Exception:
        lock.unlink(missing_ok=True)
        raise

    return Quiescence(lock_path=lock, evidence=evidence, _held=True)


# -- disk space ---------------------------------------------------------------


def preflight_space(store: Path, snapshot: Path) -> dict:
    """Peak usage is the previous generation plus the staged one, not either alone."""
    current = sum(
        path.stat().st_size for path in store.rglob("*") if path.is_file()
    )
    staged = snapshot.stat().st_size
    required = int((current + staged) * (1 + HEADROOM))
    free = shutil.disk_usage(store).free
    facts = {
        "current_store_bytes": current,
        "staged_bytes": staged,
        "required_bytes": required,
        "free_bytes": free,
    }
    if free < required:
        raise RebuildRefused(
            f"insufficient space: need {required} bytes, have {free} "
            f"(short by {required - free})"
        )
    return facts


# -- staging ------------------------------------------------------------------


@dataclass
class StagedStore:
    destination: str
    path: Path
    records: int
    bytes: int
    sha256: str


def stage(snapshot: Path, plan: Plan, staging_dir: Path) -> tuple[list[StagedStore], dict]:
    """Write the three destination stores in source order. Nothing live is touched.

    Records are written in source order, so each destination is a stable subsequence of
    the source and relative order within a store is preserved and verifiable.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    handles = {}
    for destination in DESTINATIONS:
        path = staging_dir / f"{destination}.ndjson"
        handles[destination] = (path, path.open("w", encoding="utf-8"))

    counts = {destination: 0 for destination in DESTINATIONS}
    per_key: dict[str, str] = {}
    try:
        with snapshot.open("r", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                record = json.loads(line)
                key = record["record_key"]
                if key in per_key:
                    raise RebuildRefused(f"duplicate record_key in the source: {key}")
                segment = key.split(":", 2)[1]
                destination = plan.destination_for(segment)

                if destination == EVIDENCE and plan.routes[segment].classification in NEVER_PROMOTED:
                    raise RebuildRefused(
                        f"{segment} is {plan.routes[segment].classification} and must "
                        "never reach the evidence store"
                    )

                path, handle = handles[destination]
                handle.write(line if line.endswith("\n") else line + "\n")
                counts[destination] += 1
                # Payload bytes as they appear in the source, for key-based comparison.
                per_key[key] = hashlib.sha256(
                    json.dumps(record.get("event"), sort_keys=True).encode("utf-8")
                ).hexdigest()
    finally:
        for path, handle in handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        _fsync_dir(staging_dir)

    staged: list[StagedStore] = []
    for destination in DESTINATIONS:
        path = staging_dir / f"{destination}.ndjson"
        staged.append(
            StagedStore(
                destination=destination,
                path=path,
                records=counts[destination],
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
            )
        )
    return staged, {"per_key": per_key, "counts": counts}


# -- verification -------------------------------------------------------------


def verify(
    snapshot: Path,
    staged: list[StagedStore],
    plan: Plan,
    staging_meta: dict,
    store: Path,
    spool: Path,
) -> list[str]:
    """Every check the proposal specifies, run against the staged stores.

    Returns the problems found. An empty list means the staging may be swapped in.
    """
    problems: list[str] = []
    by_destination = {s.destination: s for s in staged}

    # Counts, and that the three account for the source exactly.
    for destination in DESTINATIONS:
        expected = plan.expected_counts[destination]
        actual = by_destination[destination].records
        if actual != expected:
            problems.append(f"{destination}: {actual} records, manifest says {expected}")
    total = sum(s.records for s in staged)
    if total != plan.source_records:
        problems.append(f"staged {total} records, source has {plan.source_records}")

    # Exactly once, by key, across the destinations — compared as sets, not counts.
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for staged_store in staged:
        with staged_store.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                key = record["record_key"]
                if key in seen:
                    duplicates.append(key)
                seen[key] = hashlib.sha256(
                    json.dumps(record.get("event"), sort_keys=True).encode("utf-8")
                ).hexdigest()
    if duplicates:
        problems.append(f"{len(duplicates)} record_key(s) appear in more than one store")
    missing = set(staging_meta["per_key"]) - set(seen)
    if missing:
        problems.append(f"{len(missing)} source record(s) absent from every store")
    extra = set(seen) - set(staging_meta["per_key"])
    if extra:
        problems.append(f"{len(extra)} staged record(s) not present in the source")

    # Payload bytes identical for the same key.
    mismatched = [
        key
        for key, digest in seen.items()
        if key in staging_meta["per_key"] and staging_meta["per_key"][key] != digest
    ]
    if mismatched:
        problems.append(f"{len(mismatched)} record(s) changed payload")

    # No promotion: checked by joining staged evidence records back to the manifest.
    evidence = by_destination[EVIDENCE]
    with evidence.path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            segment = json.loads(line)["record_key"].split(":", 2)[1]
            route = plan.routes.get(segment)
            if route is None:
                problems.append(f"evidence record from unclassified segment {segment}")
                break
            if route.classification in NEVER_PROMOTED:
                problems.append(
                    f"{segment} is {route.classification} but reached the evidence store"
                )
                break
            if route.classification != "supported-by-artifact":
                problems.append(
                    f"{segment} is {route.classification}, not supported-by-artifact"
                )
                break

    # Provenance: every evidence segment's artifact still hashes as reviewed.
    for route in plan.routes.values():
        if route.destination != EVIDENCE:
            continue
        artifact = spool / route.name
        if not artifact.exists():
            problems.append(f"{route.name}: artifact vanished during the rebuild")
        elif route.artifact_sha256 and sha256_file(artifact) != route.artifact_sha256:
            problems.append(f"{route.name}: artifact changed during the rebuild")

    # Destination integrity against what the transaction manifest will record.
    for staged_store in staged:
        if staged_store.bytes != staged_store.path.stat().st_size:
            problems.append(f"{staged_store.destination}: byte count changed")
        if staged_store.sha256 != sha256_file(staged_store.path):
            problems.append(f"{staged_store.destination}: hash changed after staging")

    return problems


def ordering_is_subsequence(snapshot: Path, staged: list[StagedStore]) -> list[str]:
    """Each destination must be a stable subsequence of the source, in source order."""
    problems: list[str] = []
    source_keys: list[str] = []
    with snapshot.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                source_keys.append(json.loads(line)["record_key"])
    position = {key: index for index, key in enumerate(source_keys)}

    for staged_store in staged:
        last = -1
        with staged_store.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                index = position[json.loads(line)["record_key"]]
                if index <= last:
                    problems.append(f"{staged_store.destination}: ordering broken")
                    break
                last = index
    return problems


# -- transaction --------------------------------------------------------------


def write_transaction(
    store: Path, plan: Plan, staged: list[StagedStore], quiescence: Quiescence, space: dict
) -> Path:
    txn_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    txn_dir = store / ".rebuild-txn" / txn_id
    txn_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "txn_id": txn_id,
        "started_at": time.time(),
        "source": {"path": str(plan.snapshot), "sha256": plan.snapshot_sha256},
        "reviewed_manifest": {
            "path": str(plan.manifest_path),
            "sha256": plan.manifest_sha256,
        },
        "stores": {
            s.destination: {
                "records": s.records,
                "bytes": s.bytes,
                "sha256": s.sha256,
                "source": str(s.path),
                "destination": str(store / s.destination / "events.ndjson"),
            }
            for s in staged
        },
        "rename_plan": [
            step
            for s in staged
            for step in (f"moved-aside {s.destination}", f"installed {s.destination}")
        ],
        "quiescence": quiescence.evidence,
        "disk_space": space,
    }
    _write_durably(txn_dir / "txn.json", json.dumps(manifest, indent=2))
    _write_durably(txn_dir / "progress.log", "")
    return txn_dir


def swap(store: Path, txn_dir: Path, staged: list[StagedStore], txn_id: str) -> None:
    """Rename each store aside, then install the staged one, recording each step.

    Progress is fsynced after every rename and before the next begins, so an interrupted
    swap is always explainable from the log.
    """
    progress = txn_dir / "progress.log"
    superseded = store / f"superseded-{txn_id}"
    superseded.mkdir(parents=True, exist_ok=True)

    def record(line: str) -> None:
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(txn_dir)

    for staged_store in staged:
        live = store / staged_store.destination
        live_dir = store / staged_store.destination
        live_file = live_dir / "events.ndjson"

        if live_file.exists():
            aside = superseded / staged_store.destination
            aside.parent.mkdir(parents=True, exist_ok=True)
            os.rename(live_file, aside)
            _fsync_dir(live_file.parent)
            record(f"moved-aside   {staged_store.destination}")

        live_dir.mkdir(parents=True, exist_ok=True)
        os.rename(staged_store.path, live_file)
        _fsync_dir(live_file.parent)
        record(f"installed     {staged_store.destination}")

    # Checkpoints are keyed by segment; the rebuild changes which segments the evidence
    # store holds, so a stale checkpoint must not be carried forward.
    checkpoints = store / "checkpoints"
    if checkpoints.exists():
        os.rename(checkpoints, superseded / "checkpoints")
        _fsync_dir(store)
        record("moved-aside   checkpoints")

    _write_durably(txn_dir / "COMPLETE", txn_id)


def incomplete_transaction(store: Path) -> Path | None:
    """The directory of an open transaction, if one exists."""
    root = store / ".rebuild-txn"
    if not root.is_dir():
        return None
    for candidate in sorted(root.iterdir()):
        if candidate.is_dir() and not (candidate / "COMPLETE").exists():
            return candidate
    return None


def recover(store: Path) -> str:
    """Roll back an interrupted swap. Never rolls forward.

    Roll-forward is deliberately absent: completing a partial swap means trusting staged
    files whose installation was interrupted, and choosing to finish is a judgement about
    which generation is real. The previous generation is only ever renamed, so rollback
    is always available and is always the safe direction.
    """
    txn_dir = incomplete_transaction(store)
    if txn_dir is None:
        return "nothing to recover"
    txn_id = txn_dir.name
    superseded = store / f"superseded-{txn_id}"
    restored: list[str] = []

    for destination in DESTINATIONS:
        live = store / destination / "events.ndjson"
        aside = superseded / destination
        if aside.exists():
            live.parent.mkdir(parents=True, exist_ok=True)
            if live.exists():
                # Both present with no progress entry explaining it: two candidate
                # generations for one identity. Refuse rather than pick one.
                raise RebuildRefused(
                    f"{destination}: both a live store and a moved-aside copy exist — "
                    "refusing to choose between them; resolve by hand"
                )
            os.rename(aside, live)
            _fsync_dir(live.parent)
            restored.append(destination)

    if (superseded / "checkpoints").exists():
        target = store / "checkpoints"
        if not target.exists():
            os.rename(superseded / "checkpoints", target)
            _fsync_dir(store)

    _write_durably(txn_dir / "ROLLED_BACK", txn_id)
    return f"rolled back {txn_id}: restored {', '.join(restored) or 'nothing'}"
