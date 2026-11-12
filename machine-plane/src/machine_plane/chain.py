"""Tamper evidence for the machine record.

A local hash chain detects *modification* of the links that survive. It cannot
detect that an earlier segment was **deleted**, because a valid prefix of a
valid chain is itself a valid chain. This module keeps those two capabilities
apart instead of blurring them into one word, "integrity":

- ``verify(chain)`` answers: has any surviving link been altered or reordered?
- ``verify(chain, checkpoint=...)`` answers: has anything been removed, or has
  the log been rolled back to an earlier state?

The second answer requires a checkpoint that lives somewhere the recorder does
not control. That is why the design says off-box checkpoints are required before
the words *evidence*, *integrity* or *flight recorder* are used — and the test
suite pins that a truncated chain passes the first check and fails the second,
so nobody mistakes one for the other.

The chain stores event ids, not event bodies. That is sufficient, because an
event id is content-derived: altering an event changes its id, which breaks the
chain.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .events import MachineEvent

#: The previous-hash value of the first link.
GENESIS = "0" * 64

CHAIN_VERSION = 1


class IntegrityStatus(str, Enum):
    OK = "ok"
    MODIFIED = "modified"
    BROKEN_LINK = "broken_link"
    TRUNCATED = "truncated"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class ChainLink:
    sequence: int
    prev_hash: str
    hash: str
    recorded_at: float
    event_id: str
    kind: str = "event"

    def to_dict(self) -> dict:
        return {
            "chain_version": CHAIN_VERSION,
            "sequence": self.sequence,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "recorded_at": self.recorded_at,
            "event_id": self.event_id,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ChainLink":
        version = payload.get("chain_version")
        if version != CHAIN_VERSION:
            raise ValueError(
                f"chain link version {version!r} is not readable by this build "
                f"({CHAIN_VERSION})"
            )
        return cls(
            sequence=payload["sequence"],
            prev_hash=payload["prev_hash"],
            hash=payload["hash"],
            recorded_at=payload["recorded_at"],
            event_id=payload["event_id"],
            kind=payload.get("kind", "event"),
        )


def compute_hash(sequence: int, prev_hash: str, recorded_at: float, event_id: str) -> str:
    """The link's hash, over its position and its content."""
    material = json.dumps(
        {
            "sequence": sequence,
            "prev_hash": prev_hash,
            "recorded_at": recorded_at,
            "event_id": event_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    """The chain's length and head hash, at a moment in time.

    Only useful when it is stored somewhere the recorder cannot reach. Held on
    the same machine as the chain, it proves nothing: whoever can rewrite the
    chain can rewrite the checkpoint too.
    """

    length: int
    head_hash: str
    taken_at: float

    def to_dict(self) -> dict:
        return {
            "length": self.length,
            "head_hash": self.head_hash,
            "taken_at": self.taken_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Checkpoint":
        return cls(
            length=int(payload["length"]),
            head_hash=payload["head_hash"],
            taken_at=payload["taken_at"],
        )


@dataclass(frozen=True)
class VerifyResult:
    status: IntegrityStatus
    index: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is IntegrityStatus.OK


class HashChain:
    """An append-only hash chain over machine events."""

    def __init__(self, links: Iterable[ChainLink] = ()) -> None:
        self._links: list[ChainLink] = list(links)

    # -- reading ---------------------------------------------------------

    @property
    def links(self) -> tuple[ChainLink, ...]:
        return tuple(self._links)

    def __len__(self) -> int:
        return len(self._links)

    def head(self) -> ChainLink | None:
        return self._links[-1] if self._links else None

    def checkpoint(self, *, taken_at: float | None = None) -> Checkpoint:
        head = self.head()
        return Checkpoint(
            length=len(self._links),
            head_hash=head.hash if head else GENESIS,
            taken_at=taken_at if taken_at is not None else (
                head.recorded_at if head else 0.0
            ),
        )

    # -- writing ---------------------------------------------------------

    def append(self, event: MachineEvent, *, recorded_at: float | None = None) -> ChainLink:
        head = self.head()
        link = ChainLink(
            sequence=head.sequence + 1 if head else 0,
            prev_hash=head.hash if head else GENESIS,
            hash="",
            recorded_at=recorded_at if recorded_at is not None else event.wall_time,
            event_id=event.event_id,
        )
        link = ChainLink(
            sequence=link.sequence,
            prev_hash=link.prev_hash,
            hash=compute_hash(
                link.sequence, link.prev_hash, link.recorded_at, link.event_id
            ),
            recorded_at=link.recorded_at,
            event_id=link.event_id,
        )
        self._links.append(link)
        return link

    def seal(self, *, recorded_at: float) -> ChainLink:
        """Close the chain with a terminal marker.

        Called on shutdown and after a crash recovery, so an interrupted tail is
        explicit rather than looking like the end of the log.
        """
        head = self.head()
        link = ChainLink(
            sequence=head.sequence + 1 if head else 0,
            prev_hash=head.hash if head else GENESIS,
            hash="",
            recorded_at=recorded_at,
            event_id="",
            kind="seal",
        )
        link = ChainLink(
            sequence=link.sequence,
            prev_hash=link.prev_hash,
            hash=compute_hash(
                link.sequence, link.prev_hash, link.recorded_at, link.event_id
            ),
            recorded_at=link.recorded_at,
            event_id=link.event_id,
            kind="seal",
        )
        self._links.append(link)
        return link

    # -- persistence -----------------------------------------------------

    def save(self, path: str | os.PathLike[str]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            for link in self._links:
                handle.write(json.dumps(link.to_dict(), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "HashChain":
        target = Path(path)
        if not target.exists():
            return cls()
        links: list[ChainLink] = []
        with open(target, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    links.append(ChainLink.from_dict(json.loads(line)))
        return cls(links)


def verify(
    chain: HashChain | Iterable[ChainLink],
    *,
    checkpoint: Checkpoint | None = None,
) -> VerifyResult:
    """Check the chain, and optionally check it against a checkpoint.

    Without a checkpoint this detects modification and reordering of the links
    that are present. It deliberately cannot detect deletion: a valid prefix of
    a valid chain verifies. That is what the checkpoint is for.
    """
    links = tuple(chain.links if isinstance(chain, HashChain) else chain)

    previous_hash = GENESIS
    for index, link in enumerate(links):
        if link.sequence != index:
            return VerifyResult(
                IntegrityStatus.BROKEN_LINK,
                index,
                f"expected sequence {index}, found {link.sequence}",
            )
        if link.prev_hash != previous_hash:
            return VerifyResult(
                IntegrityStatus.BROKEN_LINK,
                index,
                "previous-hash does not match the preceding link",
            )
        expected = compute_hash(
            link.sequence, link.prev_hash, link.recorded_at, link.event_id
        )
        if link.hash != expected:
            return VerifyResult(
                IntegrityStatus.MODIFIED,
                index,
                "link hash does not match its content",
            )
        previous_hash = link.hash

    if checkpoint is None:
        return VerifyResult(IntegrityStatus.OK)

    if len(links) < checkpoint.length:
        return VerifyResult(
            IntegrityStatus.TRUNCATED,
            len(links),
            f"chain holds {len(links)} links, checkpoint recorded "
            f"{checkpoint.length}",
        )

    if checkpoint.length > 0:
        at_index = links[checkpoint.length - 1]
        if at_index.hash != checkpoint.head_hash:
            return VerifyResult(
                IntegrityStatus.ROLLED_BACK,
                checkpoint.length - 1,
                "the link at the checkpointed length does not match the "
                "checkpointed hash",
            )

    return VerifyResult(IntegrityStatus.OK)


def build(
    events: Iterable[MachineEvent], *, seal_at: float | None = None
) -> HashChain:
    """Build a chain from events, optionally sealing it."""
    chain = HashChain()
    for event in events:
        chain.append(event)
    if seal_at is not None:
        chain.seal(recorded_at=seal_at)
    return chain
