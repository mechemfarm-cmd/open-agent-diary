"""The recorder: one bounded run, one segment, then exit.

This is the privileged half. It creates a scope, launches a named workload inside
it, measures the cgroup that will join probe events to that run, attaches the
probes, normalises what they emit into the spool, and seals.

Every decision that could be made wrongly is delegated to a module that can be
tested without root:

- **what the run may do at all** — :mod:`machine_plane.containment`;
- **what gets dropped when the spool is full** — :mod:`machine_plane.retention`;
- **what the probes emit** — :mod:`machine_plane.producers.linux_ebpf`.

What is left here is ordering, which is the part that has to be right and is
therefore written once, plainly.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from .containment import Level, StartupDecision, config_fingerprint, decide_startup
from .events import EventType, MachineEvent
from .retention import (
    RetentionError,
    RetentionJournal,
    Segment,
    choose_victim,
    rotate,
    sealed_segments,
)
from .spool import Spool

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_JOURNAL_MAX_BYTES = 1024 * 1024
RECORDER_NAME = "machine-plane-recorder"
RECORDER_VERSION = "1"


class Scope(Protocol):
    """A launched workload's isolation domain."""

    @property
    def name(self) -> str: ...

    @property
    def cgroup_inode(self) -> str: ...

    def wait(self, timeout: float) -> int: ...

    def stop(self) -> None: ...


class Supervisor(Protocol):
    def start(self, workload: Sequence[str]) -> Scope: ...


class Producer(Protocol):
    def collect(self) -> Iterable[object]: ...

    def normalize(self, raw: object) -> MachineEvent: ...


@dataclass(frozen=True)
class RecorderConfig:
    capture_id: str
    spool_dir: Path
    probes: tuple[Path, ...]
    workload: tuple[str, ...]
    duration_s: float
    attach_timeout_s: float = 15.0
    max_bytes: int = DEFAULT_MAX_BYTES
    journal_max_bytes: int = DEFAULT_JOURNAL_MAX_BYTES
    recorder_id: str = RECORDER_NAME
    #: ``filtered`` (the workload-evidence path) or ``control`` (the paired
    #: unfiltered measurement). Never inferred: the recorder is told which one it
    #: is, because a silent switch between them would make a control run look like
    #: evidence about the workload.
    capture_mode: str = "filtered"

    def __post_init__(self) -> None:
        if not self.capture_id:
            raise ValueError("capture_id is required")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if not self.probes:
            raise ValueError("at least one probe is required")
        if self.capture_mode not in {"filtered", "control"}:
            raise ValueError(
                f"capture_mode must be filtered or control, got {self.capture_mode!r}"
            )

    def fingerprint_payload(self) -> dict:
        """What was agreed: the recording configuration, not the machine."""
        return {
            "probes": sorted(path.name for path in self.probes),
            "spool_dir": str(self.spool_dir),
            "max_bytes": self.max_bytes,
            "journal_max_bytes": self.journal_max_bytes,
        }

    def config_fingerprint(self) -> str:
        return config_fingerprint(self.fingerprint_payload())


class SegmentWriter:
    """One run's segment: written as ``.partial``, sealed by rename.

    The rename is what makes a segment readable. Readers scan ``*.ndjson`` only,
    so they can never observe a segment still being appended to — no lock, no
    torn line, no "is it finished yet" question to get wrong. It also means a
    leftover ``.partial`` is unambiguously a run that did not seal.
    """

    def __init__(
        self,
        spool_dir: Path,
        *,
        capture_id: str,
        boot_id: str,
        sequence: int,
        recorder_id: str,
        mode: int = 0o640,
    ) -> None:
        self.spool_dir = Path(spool_dir)
        stem = f"segment-{boot_id[:8] or 'noboot'}-{sequence:06d}-{capture_id}"
        self.partial = self.spool_dir / f"{stem}.ndjson.partial"
        self.sealed = self.spool_dir / f"{stem}.ndjson"
        self._spool = Spool(self.partial, recorder_id, mode=mode)
        self.records = 0

    def append(self, event: MachineEvent) -> None:
        self._spool.append(event)
        self.records += 1

    def size(self) -> int:
        return self._spool.size()

    def seal(self, terminal: MachineEvent) -> Path:
        """Write the terminal record, fsync, then publish by rename."""
        self.append(terminal)
        descriptor = os.open(self.partial, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(self.partial, self.sealed)
        _fsync_dir(self.spool_dir)
        return self.sealed


@dataclass
class RunResult:
    capture_id: str
    ok: bool
    exit_code: int
    segment: Path | None = None
    records: int = 0
    dropped: tuple[str, ...] = field(default_factory=tuple)
    end_reason: str | None = None
    refusal: StartupDecision | None = None
    error: str | None = None


class Recorder:
    def __init__(
        self,
        config: RecorderConfig,
        *,
        supervisor: Supervisor,
        producer: Producer | None = None,
        producer_factory=None,
        boot_id: str = "",
        sequence: int = 0,
        clock=time.monotonic,
        wall=time.time,
        detection=None,
        detection_error: str | None = None,
        consent=None,
    ) -> None:
        self.config = config
        self.supervisor = supervisor
        self.producer = producer
        # The producer needs the scope's cgroup inode to fill scope_by_cgroup, and
        # that does not exist until the scope is created — so it is built after
        # the start, not before.
        self.producer_factory = producer_factory
        self.boot_id = boot_id
        self.sequence = sequence
        self.clock = clock
        self.wall = wall
        self.detection = detection
        self.detection_error = detection_error
        self.consent = consent

    # -- the run ---------------------------------------------------------

    def run(self) -> RunResult:
        decision = decide_startup(
            detection=self.detection,
            detection_error=self.detection_error,
            consent=self.consent,
            current_config=self.config.config_fingerprint(),
        )
        if not decision.allowed:
            return RunResult(
                capture_id=self.config.capture_id,
                ok=False,
                exit_code=decision.exit_code,
                refusal=decision,
                end_reason="refused",
            )

        spool_dir = Path(self.config.spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)
        journal = RetentionJournal(
            spool_dir / "retention.journal", self.config.journal_max_bytes
        )
        journal.recover_interrupted_compaction()

        level = decision.detection.level if decision.detection else Level.ADVISORY
        end_reason = "workload_exit"
        dropped: tuple[str, ...] = ()
        scope = None
        writer = None

        try:
            dropped = self._rotate(journal, spool_dir)

            scope = self.supervisor.start(self.config.workload)
            writer = SegmentWriter(
                spool_dir,
                capture_id=self.config.capture_id,
                boot_id=self.boot_id,
                sequence=self.sequence,
                recorder_id=self.config.recorder_id,
            )

            writer.append(
                self._lifecycle(
                    EventType.RECORDER_STARTED,
                    result=level.value,
                    details={
                        "capture_id": self.config.capture_id,
                        "cgroup_inode": scope.cgroup_inode,
                        "scope": scope.name,
                        "protection_level": level.value,
                        "supervisor_mode": "supervisor",
                        "capture_mode": self.config.capture_mode,
                    },
                )
            )

            producer = self._producer_for(scope)
            # Collection runs for the length of the window, not until the probes
            # exit on their own — a live bpftrace does not exit by itself, so
            # draining it inline would mean the duration was never reached.
            errors: list[BaseException] = []

            redact = getattr(producer, "redact", None)

            def _collect() -> None:
                try:
                    for raw in producer.collect():
                        event = producer.normalize(raw)
                        # The pipeline's redaction stage is not optional. Skipping
                        # it stored absolute paths from the one approved pathname
                        # dereference: approval to read a path is not approval to
                        # store one.
                        if callable(redact):
                            event = redact(event)
                        writer.append(event)
                except BaseException as collect_error:  # noqa: BLE001
                    errors.append(collect_error)

            collector = threading.Thread(target=_collect, daemon=True)
            collector.start()

            # Hold the workload until every probe is listening. Without this the
            # workload can finish before attachment completes, and the capture
            # contains only the machine's own noise — which looks like success
            # because the record count is high.
            attached = getattr(producer, "attached", None)
            if attached is not None:
                if not attached.wait(timeout=self.config.attach_timeout_s):
                    raise RuntimeError(
                        "probes did not report attachment within "
                        f"{self.config.attach_timeout_s}s"
                    )
            release = getattr(scope, "release", None)
            if callable(release):
                release()

            window_started = self.clock()
            scope.wait(self.config.duration_s)

            # The window is the duration, not the workload's lifetime. A workload
            # that exits early must not shorten the measurement — otherwise the
            # recorded span varies with how fast the workload happened to run,
            # and two runs are no longer comparable.
            remaining = self.config.duration_s - (self.clock() - window_started)
            if remaining > 0:
                time.sleep(remaining)

            stop = getattr(producer, "stop", None)
            if callable(stop):
                stop()
            collector.join(timeout=15)
            if collector.is_alive():
                raise RuntimeError("probe collection did not stop after the window")
            if errors:
                raise errors[0]

        except RetentionError as error:
            end_reason = "retention_unrecordable"
            return RunResult(
                capture_id=self.config.capture_id,
                ok=False,
                exit_code=6,
                dropped=dropped,
                end_reason=end_reason,
                error=str(error),
            )
        except Exception as error:  # noqa: BLE001 - sealed below, then re-raised as a result
            end_reason = "probe_error"
            if writer is not None:
                writer.seal(
                    self._lifecycle(
                        EventType.RECORDER_STOPPED,
                        result=end_reason,
                        details={
                            "records": writer.records,
                            "protection_level": level.value,
                            "error": type(error).__name__,
                        },
                    )
                )
            return RunResult(
                capture_id=self.config.capture_id,
                ok=False,
                exit_code=7,
                segment=writer.sealed if writer is not None else None,
                records=writer.records if writer is not None else 0,
                dropped=dropped,
                end_reason=end_reason,
                error=f"{type(error).__name__}: {error}",
            )
        finally:
            if scope is not None:
                try:
                    scope.stop()
                except Exception:  # noqa: BLE001 - teardown must not mask the run
                    pass

        assert writer is not None
        sealed = writer.seal(
            self._lifecycle(
                EventType.RECORDER_STOPPED,
                result=end_reason,
                details={
                    "records": writer.records,
                    "protection_level": level.value,
                    "capture_id": self.config.capture_id,
                    "capture_mode": self.config.capture_mode,
                    "dropped_this_run": len(dropped),
                    # The producer counts what the kernel let through; the importer
                    # counts what arrived. Two layers, two numbers, reconciled
                    # rather than assumed equal.
                    "producer_out_of_scope": getattr(producer, "out_of_scope", None),
                },
            )
        )
        dropped = dropped + tuple(self._rotate(journal, spool_dir))

        return RunResult(
            capture_id=self.config.capture_id,
            ok=True,
            exit_code=0,
            segment=sealed,
            records=writer.records,
            dropped=dropped,
            end_reason=end_reason,
        )

    # -- helpers ---------------------------------------------------------

    def _producer_for(self, scope: Scope) -> Producer:
        if self.producer_factory is not None:
            return self.producer_factory(scope)
        if self.producer is None:
            raise ValueError("a producer or producer_factory is required")
        return self.producer

    def _rotate(self, journal: RetentionJournal, spool_dir: Path) -> tuple[str, ...]:
        paths = sorted(spool_dir.glob("segment-*.ndjson"))
        segments = sealed_segments(paths)
        total = sum(segment.size for segment in segments)
        total += sum(
            path.stat().st_size for path in spool_dir.glob("segment-*.ndjson.partial")
        )
        if total <= self.config.max_bytes:
            return ()
        dropped = rotate(
            segments=segments,
            journal=journal,
            spool_dir=spool_dir,
            now=self.wall(),
            total_bytes=total,
            max_bytes=self.config.max_bytes,
        )
        return tuple(dropped)

    def _lifecycle(
        self, event_type: EventType, *, result: str, details: dict
    ) -> MachineEvent:
        return MachineEvent(
            type=event_type,
            producer=self.config.recorder_id,
            producer_version=RECORDER_VERSION,
            platform="linux",
            monotonic_time=self.clock(),
            wall_time=self.wall(),
            result=result,
            platform_details=details,
        )


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_torn_tails(spool_dir: Path, *, recorder_id: str = RECORDER_NAME) -> list[str]:
    """Seal any ``.partial`` left by a run that did not finish.

    A torn tail is reported, not deleted: the records it holds are real
    observations of a run that stopped unexpectedly, and the gap is part of the
    evidence.
    """
    sealed: list[str] = []
    for partial in sorted(Path(spool_dir).glob("segment-*.ndjson.partial")):
        stem = partial.name[: -len(".partial")]
        writer_partial = partial
        spool = Spool(writer_partial, recorder_id)
        spool.append(
            MachineEvent(
                type=EventType.RECORDER_STOPPED,
                producer=recorder_id,
                producer_version=RECORDER_VERSION,
                platform="linux",
                monotonic_time=0.0,
                wall_time=time.time(),
                result="torn_tail",
                platform_details={"sealed_on_recovery": True},
            )
        )
        os.replace(writer_partial, Path(spool_dir) / stem)
        sealed.append(stem)
    return sealed
