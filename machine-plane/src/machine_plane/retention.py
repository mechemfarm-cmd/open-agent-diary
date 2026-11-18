"""Retention: a bounded spool, and an accounting of what it dropped.

An unbounded recorder is an availability failure waiting for a date. So the spool
has a byte cap, and the oldest sealed segment goes first when it is exceeded.

The accounting is the part that has to be right. A retention record stored in a
segment is destroyed by the very mechanism it describes — rotate enough times and
the history of drops becomes a history of the most recent few. So every retention
event is written to :class:`RetentionJournal`, and that file is authoritative.

Because the journal must therefore outlive the segments it describes, it cannot
grow without limit either: an unbounded journal is just a second spool. Past its
cap it compacts to a single summary record, and the summary says so — a shortened
history is visible rather than looking like a complete one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

JOURNAL_VERSION = 1


class RetentionError(Exception):
    """The journal could not be written.

    Never swallowed: the run stops, because a spool that cannot be written and
    cannot be pruned cannot be accounted for.
    """


@dataclass(frozen=True)
class Totals:
    events: int = 0
    compactions: int = 0
    bytes_freed: int = 0
    first_dropped: float | None = None
    last_dropped: float | None = None
    detail_from: float | None = None

    def plus_event(self, at: float, size: int) -> "Totals":
        return Totals(
            events=self.events + 1,
            compactions=self.compactions,
            bytes_freed=self.bytes_freed + size,
            first_dropped=(
                at if self.first_dropped is None else min(self.first_dropped, at)
            ),
            last_dropped=(
                at if self.last_dropped is None else max(self.last_dropped, at)
            ),
            detail_from=self.detail_from,
        )


@dataclass
class DropRecord:
    kind: str
    at: float
    victim: str | None = None
    victim_oldest: float | None = None
    size: int = 0
    reason: str | None = None

    def to_json(self) -> str:
        payload = {"version": JOURNAL_VERSION, "kind": self.kind, "at": self.at}
        if self.victim is not None:
            payload["victim"] = self.victim
        if self.victim_oldest is not None:
            payload["victim_oldest"] = self.victim_oldest
        if self.kind == "event":
            payload["bytes"] = self.size
        if self.reason is not None:
            payload["reason"] = self.reason
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class RetentionJournal:
    """Append-only accounting for retention, bounded by compaction."""

    def __init__(self, path: str | os.PathLike[str], max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("journal max_bytes must be positive")
        self.path = Path(path)
        self.max_bytes = max_bytes

    # -- reading ---------------------------------------------------------

    def records(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # A torn last line is possible after a crash. It is not a
                # record, so it contributes no count — but it is also not a
                # reason to fail the whole journal.
                continue

    def totals(self) -> Totals:
        totals = Totals()
        for record in self.records():
            kind = record.get("kind")
            if kind == "event":
                totals = totals.plus_event(
                    float(record["at"]), int(record.get("bytes", 0))
                )
            elif kind == "summary":
                totals = Totals(
                    events=totals.events + int(record.get("events", 0)),
                    compactions=int(record.get("compactions", 1)),
                    bytes_freed=totals.bytes_freed + int(record.get("bytes_freed", 0)),
                    first_dropped=_min_opt(
                        totals.first_dropped, record.get("first_dropped")
                    ),
                    last_dropped=_max_opt(
                        totals.last_dropped, record.get("last_dropped")
                    ),
                    detail_from=record.get("detail_from"),
                )
        return totals

    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    # -- writing ---------------------------------------------------------

    def record_drop(
        self, *, victim: str, victim_oldest: float | None, size: int, at: float
    ) -> None:
        """Record a retention *before* the victim is deleted.

        Ordering is the whole safety argument: a crash between this write and the
        unlink leaves a claim of a drop that has not happened, which the next
        start reconciles. The reverse order leaves a deletion nobody recorded.
        """
        self._append(
            DropRecord(
                kind="event", at=at, victim=victim, victim_oldest=victim_oldest, size=size
            )
        )
        self.compact_if_needed(now=at)

    def record_aborted(self, *, victim: str, at: float, reason: str) -> None:
        self._append(DropRecord(kind="aborted", at=at, victim=victim, reason=reason))

    def _append(self, record: DropRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640
            )
            try:
                os.write(descriptor, (record.to_json() + "\n").encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise RetentionError(f"cannot write retention journal: {error}") from error

    # -- compaction ------------------------------------------------------

    def compact_if_needed(self, *, now: float) -> bool:
        if self.size() <= self.max_bytes:
            return False
        self.compact(now=now)
        return True

    def compact(self, *, now: float) -> None:
        """Replace the journal with one summary record carrying its totals.

        Per-event detail before ``detail_from`` is gone, and the summary says so.
        Recomputation is idempotent because the summary is derived from the
        journal it replaces.
        """
        totals = self.totals()
        summary = {
            "version": JOURNAL_VERSION,
            "kind": "summary",
            "at": now,
            "events": totals.events,
            "compactions": totals.compactions + 1,
            "bytes_freed": totals.bytes_freed,
            "first_dropped": totals.first_dropped,
            "last_dropped": totals.last_dropped,
            "detail_from": now,
        }
        temporary = self.path.with_suffix(".journal.new")
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640
            )
            try:
                os.write(
                    descriptor,
                    (json.dumps(summary, sort_keys=True) + "\n").encode("utf-8"),
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.path)
            _fsync_dir(self.path.parent)
        except OSError as error:
            raise RetentionError(f"cannot compact retention journal: {error}") from error

    def recover_interrupted_compaction(self) -> bool:
        """Discard a half-written compaction. The rename is the commit point."""
        temporary = self.path.with_suffix(".journal.new")
        if not temporary.exists():
            return False
        temporary.unlink()
        return True


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _min_opt(current: float | None, other: object) -> float | None:
    if other is None:
        return current
    value = float(other)  # type: ignore[arg-type]
    return value if current is None else min(current, value)


def _max_opt(current: float | None, other: object) -> float | None:
    if other is None:
        return current
    value = float(other)  # type: ignore[arg-type]
    return value if current is None else max(current, value)


# -- rotation --------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One sealed segment. Only sealed segments are rotation candidates."""

    name: str
    size: int
    oldest_record_at: float | None = None


def sealed_segments(paths: Iterable[Path]) -> list[Segment]:
    segments = []
    for path in paths:
        if path.suffix != ".ndjson":
            continue  # a .partial is the active run; never a candidate
        segments.append(Segment(name=path.name, size=path.stat().st_size))
    return segments


def choose_victim(segments: Iterable[Segment]) -> Segment | None:
    """The oldest sealed segment, or None.

    Ordered by each segment's own oldest record where known, then by name, which
    encodes boot id and sequence. Name ordering alone would be wrong across a
    reboot, and record time alone would be wrong when the clock stepped.
    """
    candidates = list(segments)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda s: (s.oldest_record_at if s.oldest_record_at is not None else float("inf"), s.name),
    )


def rotate(
    *,
    segments: Iterable[Segment],
    journal: RetentionJournal,
    spool_dir: Path,
    now: float,
    total_bytes: int,
    max_bytes: int,
) -> list[str]:
    """Drop oldest sealed segments until the spool is under its cap.

    Returns the names dropped. Raises :class:`RetentionError` if the journal
    cannot record — in which case nothing is deleted, because a deletion nobody
    recorded is worse than a full disk.
    """
    available = list(segments)
    dropped: list[str] = []
    remaining = total_bytes

    while remaining > max_bytes:
        victim = choose_victim(available)
        if victim is None:
            break
        journal.record_drop(
            victim=victim.name,
            victim_oldest=victim.oldest_record_at,
            size=victim.size,
            at=now,
        )
        try:
            (spool_dir / victim.name).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RetentionError(f"cannot delete {victim.name}: {error}") from error
        _fsync_dir(spool_dir)
        available.remove(victim)
        dropped.append(victim.name)
        remaining -= victim.size

    return dropped
