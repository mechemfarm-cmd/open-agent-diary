"""Off-box checkpoints: what turns a hash chain into evidence, or refuses to.

``chain.verify`` can detect that a surviving link was altered. It cannot detect
that links were *deleted*, because a valid prefix of a valid chain is itself a
valid chain. The only thing that closes that hole is a record of the chain's
length and head hash, held somewhere the recorder cannot reach.

Which makes the trust domain the whole question. A checkpoint written to the
same machine as the chain proves nothing at all: whoever can rewrite the chain
can rewrite the checkpoint beside it. So this module refuses to describe a
same-host checkpoint as evidence, and that refusal is a property of the code
rather than a note in a document.

The default trust domain is ``UNKNOWN``, not ``SEPARATE_HOST``. Assuming the
good case would make every downstream claim wrong by default, and the failure
would be invisible — the exact shape of mistake this package exists to avoid.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .chain import Checkpoint, HashChain, IntegrityStatus, verify


class TrustDomain(str, Enum):
    SAME_HOST = "same_host"
    SEPARATE_HOST = "separate_host"
    UNKNOWN = "unknown"


class CheckpointSink(Protocol):
    """Somewhere checkpoints are kept.

    A sink declares its trust domain because only the caller knows where it
    actually lives. Nothing here tries to guess.
    """

    name: str
    trust_domain: TrustDomain

    def append(self, checkpoint: Checkpoint) -> None: ...
    def latest(self) -> Checkpoint | None: ...


class MemoryCheckpointSink:
    """A sink that keeps checkpoints in memory. For tests and dry runs."""

    def __init__(
        self,
        *,
        name: str = "memory",
        trust_domain: TrustDomain = TrustDomain.SEPARATE_HOST,
    ) -> None:
        self.name = name
        self.trust_domain = trust_domain
        self._checkpoints: list[Checkpoint] = []

    def append(self, checkpoint: Checkpoint) -> None:
        self._checkpoints.append(checkpoint)

    def latest(self) -> Checkpoint | None:
        return self._checkpoints[-1] if self._checkpoints else None

    def all(self) -> tuple[Checkpoint, ...]:
        return tuple(self._checkpoints)


class JsonlCheckpointSink:
    """An append-only file of checkpoints.

    Appending rather than overwriting matters: a sink that keeps only the latest
    checkpoint loses the ability to notice that the chain was rolled back to a
    state that happens to match it.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        trust_domain: TrustDomain = TrustDomain.UNKNOWN,
    ) -> None:
        self.path = Path(path)
        self.name = name or str(self.path)
        self.trust_domain = trust_domain

    def append(self, checkpoint: Checkpoint) -> None:
        line = (json.dumps(checkpoint.to_dict(), sort_keys=True) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read(self) -> Iterator[Checkpoint]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield Checkpoint.from_dict(json.loads(line))

    def latest(self) -> Checkpoint | None:
        found = None
        for checkpoint in self._read():
            found = checkpoint
        return found

    def all(self) -> tuple[Checkpoint, ...]:
        return tuple(self._read())


@dataclass(frozen=True)
class PublishedCheckpoint:
    checkpoint: Checkpoint
    sink_name: str
    trust_domain: TrustDomain

    @property
    def off_box(self) -> bool:
        return self.trust_domain is TrustDomain.SEPARATE_HOST

    def to_dict(self) -> dict:
        return {
            "sink": self.sink_name,
            "trust_domain": self.trust_domain.value,
            "off_box": self.off_box,
            "checkpoint": self.checkpoint.to_dict(),
        }


@dataclass(frozen=True)
class IntegrityAssessment:
    status: IntegrityStatus
    off_box: bool
    detail: str

    @property
    def evidence(self) -> bool:
        """Whether this chain may be described as evidence.

        Only when it verifies *and* the checkpoint lives somewhere the recorder
        cannot reach. Anything else is observability with loss reporting, which
        is a perfectly good thing to be — it is just not the same claim.
        """
        return self.status is IntegrityStatus.OK and self.off_box

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "off_box": self.off_box,
            "evidence": self.evidence,
            "detail": self.detail,
        }


def publish(
    chain: HashChain,
    sink: CheckpointSink,
    *,
    taken_at: float | None = None,
) -> PublishedCheckpoint:
    """Record the chain's current length and head in ``sink``."""
    checkpoint = chain.checkpoint(taken_at=taken_at)
    sink.append(checkpoint)
    return PublishedCheckpoint(
        checkpoint=checkpoint,
        sink_name=sink.name,
        trust_domain=sink.trust_domain,
    )


def assess(chain: HashChain, sink: CheckpointSink) -> IntegrityAssessment:
    """Check the chain against the newest checkpoint the sink holds."""
    latest = sink.latest()
    off_box = sink.trust_domain is TrustDomain.SEPARATE_HOST

    if latest is None:
        return IntegrityAssessment(
            status=IntegrityStatus.OK,
            off_box=False,
            detail=(
                "no checkpoint has been published, so deletion and rollback "
                "cannot be detected at all"
            ),
        )

    result = verify(chain, checkpoint=latest)

    if not result.ok:
        return IntegrityAssessment(
            status=result.status,
            off_box=off_box,
            detail=result.detail,
        )

    if not off_box:
        return IntegrityAssessment(
            status=IntegrityStatus.OK,
            off_box=False,
            detail=(
                f"the chain matches its checkpoint, but the checkpoint is held "
                f"by {sink.name!r} in the {sink.trust_domain.value} trust domain, "
                "so it does not rule out deletion or rollback"
            ),
        )

    return IntegrityAssessment(
        status=IntegrityStatus.OK,
        off_box=True,
        detail=(
            f"the chain matches a checkpoint held by {sink.name!r} in the "
            "separate_host trust domain"
        ),
    )
