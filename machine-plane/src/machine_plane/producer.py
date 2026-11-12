"""The producer interface.

Each platform producer implements the same five steps:

    collect -> normalize -> redact -> annotate capabilities -> emit

The core does not know whether an event came from eBPF, Sysmon, Endpoint
Security, auditd, Falco, Tetragon, or a future sensor. It knows the pipeline and
the record, and nothing else.

Redaction happens *inside* the pipeline, before the event reaches the sink. A
producer that collects command lines and relies on a later filter has already
lost the metadata-only guarantee.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .capabilities import CapabilitySet
from .events import (
    PATH_EVENT_TYPES,
    MachineEvent,
    PrivacyMode,
)
from .identity import IdentityConfidence
from .privacy import PrivacyPolicy


@dataclass(frozen=True)
class RawEvent:
    """What a sensor yields, before normalisation.

    Deliberately loose: platform sensors differ too much for a strict shape, and
    forcing one would push platform detail into the core.
    """

    type: Any
    monotonic_time: float
    wall_time: float
    identity: Any = None
    target: str | None = None
    result: str | None = None
    details: dict | None = None
    identity_confidence: IdentityConfidence = IdentityConfidence.CERTAIN


class Sink(Protocol):
    """Somewhere finished events go. The spool implements this."""

    def append(self, event: MachineEvent) -> None: ...


class Producer(ABC):
    """Base class for platform producers."""

    #: Stable producer name, recorded on every event.
    name: str = "unnamed"
    version: str = "0.0.0"
    platform: str = "unknown"

    def __init__(self, policy: PrivacyPolicy | None = None) -> None:
        self._policy = policy or PrivacyPolicy()
        self._capabilities = CapabilitySet()

    @abstractmethod
    def capabilities(self) -> CapabilitySet:
        """Report what this producer can observe, with coverage intervals."""

    @abstractmethod
    def collect(self) -> Iterable[RawEvent]:
        """Yield raw events from the sensor."""

    @abstractmethod
    def normalize(self, raw: RawEvent) -> MachineEvent:
        """Turn a raw sensor event into a machine-plane event."""

    # -- pipeline stages -------------------------------------------------

    def redact(self, event: MachineEvent) -> MachineEvent:
        """Apply the privacy policy. Never stores contents."""
        if event.privacy_mode is PrivacyMode.CONTENT_BEARING:
            # Content-bearing mode is a separate, opt-in mode and not
            # implemented here. Refusing is safer than silently allowing it.
            raise NotImplementedError(
                "content-bearing mode is not implemented in this build"
            )

        target = event.target
        if event.type in PATH_EVENT_TYPES and target is not None:
            target = self._policy.redact_path(target).value
        elif event.type.value == "connect" and target is not None:
            target = _coarsen_destination(target)

        details, _removed = self._policy.sanitize_details(dict(event.platform_details))

        return replace(event, target=target, platform_details=details)

    def annotate(self, event: MachineEvent) -> MachineEvent:
        """Stamp provenance so no event can masquerade as another platform's."""
        return replace(
            event,
            producer=self.name,
            producer_version=self.version,
            platform=self.platform,
            capabilities_at_capture=self.capabilities().reports,
        )

    def run(self, sink: Sink) -> int:
        """Run the full pipeline, emitting into ``sink``. Returns events emitted."""
        emitted = 0
        for raw in self.collect():
            event = self.normalize(raw)
            event = self.redact(event)
            event = self.annotate(event)
            event.validate_for_storage()
            sink.append(event)
            emitted += 1
        return emitted

    def events(self) -> Iterator[MachineEvent]:
        """The pipeline without a sink, for tests and inspection."""
        for raw in self.collect():
            event = self.annotate(self.redact(self.normalize(raw)))
            event.validate_for_storage()
            yield event


def _coarsen_destination(target: str) -> str:
    """Reduce a network destination to host and port.

    Paths and query strings in a URL routinely carry tokens and identifiers.
    The metadata-only guarantee has to survive a producer that hands us a full
    URL, so we cut it down rather than trusting the sensor.
    """
    if "://" in target:
        scheme, _, remainder = target.partition("://")
        authority = remainder.split("/", 1)[0]
        return f"{scheme}://{authority}"
    return target.split("/", 1)[0]
