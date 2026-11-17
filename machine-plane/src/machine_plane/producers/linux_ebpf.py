"""Linux eBPF producer backed by the reviewed Stage 1 bpftrace probes.

The wire format parsed here is the one the Stage 1 experiment measured:
``MARK``/``EV`` lines from ``probes/{exec,file,net}.bt``. Parsing that exact
format is deliberate — the sensor that was evaluated is the sensor that ships,
so the Stage 1 coverage numbers describe this producer rather than a similar one.

Three things this producer refuses to do, each because the Stage 1 result showed
what happens when they are done casually:

- **It does not invent a target.** The file and net probes never dereference a
  filename or a ``sockaddr``, so an ``open`` or ``connect`` event has a target the
  producer genuinely does not know. That is ``TargetState.UNKNOWN`` — "nobody
  recorded one" — and never ``ABSENT``, which claims the action had no target at
  all. Conflating the two is the exact ambiguity the state model exists to
  prevent.
- **It does not treat a kernel monotonic timestamp as a wall clock.** The probes
  emit ``nsecs``, which is seconds since boot. Used directly as ``wall_time`` that
  is a date in 1970.
- **It does not let an unparsed line be silence.** A line that looks like an event
  but cannot be read becomes a gap event, because a dropped line and an event that
  never happened are indistinguishable otherwise.

Identity is a known weakness and is reported as one. The probes carry ``pid``,
``tid`` and ``cgroup`` but **no process start time**, so a reused PID cannot be
distinguished from the original process. Identity is therefore a composite of
host, boot, isolation domain and pid, and confidence is ``AMBIGUOUS`` rather than
``CERTAIN``. ``PROCESS_START_TIME`` is declared unavailable in the capability set
for the same reason.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import IO

from ..capabilities import Capability, CapabilityReport, CapabilitySet
from ..events import (
    CoverageNote,
    CoverageState,
    EventType,
    MachineEvent,
    PrivacyMode,
    TargetState,
    exec_outcome,
)
from ..identity import DomainType, IdentityConfidence, IsolationDomain, ProcessIdentity
from ..producer import Producer, RawEvent

#: Sensor event names this producer understands. Anything else that presents as an
#: event is reported as a gap rather than skipped.
KNOWN_EVENT_NAMES = frozenset({"exec_attempt", "exec_started", "open", "connect"})

#: Fields that must be present and well-formed on every event line.
REQUIRED_FIELDS = ("ts", "pid", "tid", "cgroup")


class LinuxEBPFProducer(Producer):
    """Collect metadata-only events from the Linux Stage 1 bpftrace probes.

    Two modes, and they are not equivalent:

    - **live** — ``probe_paths`` are attached with ``bpftrace``, one process per
      probe script, and stdout is parsed as it arrives. This is what a recorder
      runs.
    - **replay** — ``captured_log_path`` reads an already-captured log. This is
      what makes the producer testable without root, and it is the mode the test
      suite uses.

    One ``bpftrace`` process per probe is not an implementation detail: passing
    several scripts to a single invocation silently runs only the first, so the
    file and net probes would never attach.
    """

    name = "linux-ebpf"
    version = "0.2.0"
    platform = "linux"

    def __init__(
        self,
        probe_paths: Sequence[str | Path] | None = None,
        *,
        captured_log_path: str | Path | None = None,
        bpftrace: str = "bpftrace",
        host_id: str = "unknown-host",
        boot_id: str = "unknown-boot",
        wall_clock_origin: float | None = None,
        policy=None,
    ) -> None:
        super().__init__(policy=policy)
        self._probe_paths = tuple(str(path) for path in (probe_paths or ()))
        self._captured_log_path = (
            str(captured_log_path) if captured_log_path is not None else None
        )
        self._bpftrace = bpftrace
        self._host_id = host_id
        self._boot_id = boot_id
        #: Wall clock paired with the first monotonic timestamp seen, used to give
        #: events a real wall time without pretending the kernel emitted one.
        self._wall_clock_origin = (
            wall_clock_origin if wall_clock_origin is not None else time.time()
        )
        self._monotonic_origin: float | None = None
        self._unparsed = 0
        self._capabilities = self._build_capabilities()

    # -- capabilities ----------------------------------------------------

    def _build_capabilities(self) -> CapabilitySet:
        """Report observed support and deliberate blind spots together.

        The unavailable reports are the Stage 1 findings. They are capabilities
        rather than comments because a missing event must be explainable: without
        them, "no rename happened" and "rename cannot be seen" look identical.
        """
        supported = {
            Capability.BPF_AVAILABLE: "bpftrace is the configured sensor",
            Capability.BTF_AVAILABLE: "the reviewed probes attach to tracepoints",
            Capability.CGROUP_TRACKING: "every Stage 1 event carries cgroup",
            Capability.PROCESS_EXEC: (
                "exec probe records attempts (sys_exit_execve) and successful "
                "image transitions (sched_process_exec)"
            ),
            Capability.FILE_ACTIVITY: "file probe records openat outcomes",
            Capability.NETWORK_CONNECT: "net probe records connect syscall returns",
        }
        unavailable = {
            Capability.FILE_RENAME: "no probe attaches to a rename event",
            Capability.FILE_UNLINK: "no probe attaches to an unlink event",
            Capability.PROCESS_EXIT: "no probe attaches to a process-exit tracepoint",
            Capability.FILE_PATHS: (
                "file probe never dereferences args->filename, so it cannot say "
                "which file was opened"
            ),
            Capability.NETWORK_DESTINATIONS: (
                "net probe never dereferences sockaddr, so it cannot say where a "
                "connection went"
            ),
            Capability.NETWORK_CONNECT_OUTCOMES: (
                "non-blocking sockets return EINPROGRESS (-115), so success and "
                "failure are indistinguishable from the syscall return alone"
            ),
            Capability.PROCESS_START_TIME: (
                "the probes emit pid, tid and cgroup but no start ticks, so a "
                "reused PID cannot be distinguished from the original process"
            ),
            Capability.KERNEL_LOCKDOWN_MODE: (
                "lockdown state is a host property these probes do not measure"
            ),
        }
        reports = [
            CapabilityReport(capability, True, 0.0, reason=reason)
            for capability, reason in supported.items()
        ]
        reports += [
            CapabilityReport(capability, False, 0.0, reason=reason)
            for capability, reason in unavailable.items()
        ]
        return CapabilitySet(reports)

    def capabilities(self) -> CapabilitySet:
        """Return the producer's complete capability report."""
        return self._capabilities

    # -- collect ---------------------------------------------------------

    def collect(self) -> Iterable[RawEvent]:
        """Yield parsed events from a captured log, or from live probes."""
        if self._captured_log_path is not None:
            with open(self._captured_log_path, encoding="utf-8") as stream:
                yield from self._parse_lines(stream)
            return

        if not self._probe_paths:
            raise ValueError("probe_paths are required unless captured_log_path is set")

        # One process per probe script. A single invocation given several scripts
        # runs only the first and silently ignores the rest.
        processes: list[subprocess.Popen[str]] = []
        try:
            for path in self._probe_paths:
                processes.append(
                    subprocess.Popen(
                        [self._bpftrace, path],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            # Interleave the probes as they arrive rather than draining one to
            # completion: the whole point of a shared monotonic timeline is that
            # the event order across probes is meaningful.
            streams = [p.stdout for p in processes if p.stdout is not None]
            for stream in _interleave(streams):
                yield from self._parse_lines([stream])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            failures = []
            for path, process in zip(self._probe_paths, processes):
                process.wait()
                if process.returncode:
                    stderr = process.stderr.read() if process.stderr else ""
                    failures.append(f"{path}: {stderr.strip()[:200]}")
            if failures:
                raise RuntimeError("bpftrace exited non-zero: " + "; ".join(failures))

    # -- parsing ---------------------------------------------------------

    def _wall_time_for(self, monotonic_seconds: float) -> float:
        """Pair a kernel monotonic timestamp with a real wall clock.

        The probes emit ``nsecs`` — seconds since boot — and nothing else. Used
        directly as ``wall_time`` that is 1970. Instead the first timestamp seen
        is paired with the wall clock at capture, and later events are offset from
        it, which preserves inter-event deltas without inventing per-event wall
        precision the sensor never provided.
        """
        if self._monotonic_origin is None:
            self._monotonic_origin = monotonic_seconds
        return self._wall_clock_origin + (monotonic_seconds - self._monotonic_origin)

    def _gap(self, reason: str, affected: str, lost: int = 1) -> RawEvent:
        """Build a gap event. An unreadable line is not silence."""
        self._unparsed += lost
        return RawEvent(
            EventType.UNSUPPORTED_KERNEL_FEATURE,
            self._wall_clock_origin,
            self._wall_clock_origin,
            result=reason,
            details={"reason": reason},
            coverage_note=CoverageNote(affected=(affected,), lost=lost),
        )

    def _parse_lines(self, lines: Iterable[str]) -> Iterator[RawEvent]:
        """Parse the reviewed MARK/EV wire format.

        A ``MARK`` line is a phase marker and carries no event. An ``EV`` line
        that cannot be read becomes a gap event rather than being dropped: a
        silently skipped line and an event that never happened are the same thing
        to every consumer downstream.
        """
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                fields = shlex.split(stripped)
            except ValueError:
                # Unbalanced quoting: shlex cannot tokenise it at all.
                yield self._gap("unparseable line", "unparsed_event_line")
                continue
            if not fields or fields[0] != "EV":
                # MARK lines and the "Attaching N probes..." banner are expected.
                continue

            if len(fields) < 2:
                yield self._gap("event line has no event name", "unparsed_event_line")
                continue
            event_name = fields[1]

            values: dict[str, str] = {}
            for field in fields[2:]:
                if "=" not in field:
                    continue
                key, value = field.split("=", 1)
                values[key] = value

            if event_name not in KNOWN_EVENT_NAMES:
                yield self._gap(
                    f"unrecognised sensor event {event_name!r}", "unrecognised_event"
                )
                continue

            missing = [name for name in REQUIRED_FIELDS if name not in values]
            if missing:
                yield self._gap(
                    f"event {event_name!r} missing {','.join(missing)}",
                    "malformed_event",
                )
                continue

            try:
                timestamp = float(values["ts"]) / 1_000_000_000
                pid = int(values["pid"])
                tid = int(values["tid"])
            except ValueError:
                yield self._gap(
                    f"event {event_name!r} has non-numeric identity fields",
                    "malformed_event",
                )
                continue

            wall_time = self._wall_time_for(timestamp)
            details: dict = {
                "pid": pid,
                "tid": tid,
                "cgroup": values["cgroup"],
                "label": values.get("label"),
            }
            result = values.get("rc")

            if event_name == "exec_attempt":
                # The attempt has a target — the binary it tried to execute — but
                # sys_exit_execve does not carry it. UNKNOWN, not ABSENT.
                yield RawEvent(
                    "exec_attempt",
                    timestamp,
                    wall_time,
                    result=result,
                    details=details,
                    target_state=TargetState.UNKNOWN,
                )
            elif event_name == "exec_started":
                # sched_process_exec fires only on a successful image transition,
                # and carries the pathname. This is the one event whose target is
                # genuinely known.
                path = values.get("path")
                yield RawEvent(
                    "exec_started",
                    timestamp,
                    wall_time,
                    target=path,
                    result="0",
                    details=details,
                    target_state=(
                        TargetState.PRESENT if path else TargetState.UNKNOWN
                    ),
                )
            elif event_name == "open":
                details["write"] = values.get("write") == "1"
                # The filename is deliberately never read by the probe.
                yield RawEvent(
                    "open",
                    timestamp,
                    wall_time,
                    result=result,
                    details=details,
                    target_state=TargetState.UNKNOWN,
                )
            elif event_name == "connect":
                # sockaddr is deliberately never read, and a non-blocking socket
                # reports EINPROGRESS, so neither destination nor outcome is known.
                yield RawEvent(
                    "connect",
                    timestamp,
                    wall_time,
                    result=result,
                    details=details,
                    target_state=TargetState.UNKNOWN,
                )

    # -- normalize -------------------------------------------------------

    def _identity(self, raw: RawEvent) -> ProcessIdentity:
        """Build a neutral identity from what the probe actually provides.

        Composite rather than pid alone: within one boot a pid can be reused, so
        the isolation domain and boot id are part of what makes the identifier
        mean anything. Start time is not available and is left unset rather than
        guessed.
        """
        details = raw.details or {}
        pid = details.get("pid", "unknown")
        cgroup = details.get("cgroup")
        domain = (
            IsolationDomain(type=DomainType.CGROUP, id=str(cgroup), platform="linux")
            if cgroup is not None
            else None
        )
        return ProcessIdentity(
            host_id=self._host_id,
            boot_id=self._boot_id,
            process_instance_id=f"{self._boot_id}:{cgroup}:pid:{pid}",
            process_start_time=None,
            isolation_domain=domain,
        )

    def normalize(self, raw: RawEvent) -> MachineEvent:
        """Map a sensor event to the neutral machine-plane schema."""
        if isinstance(raw.type, EventType):
            # Already a gap event built by the parser.
            return MachineEvent(
                type=raw.type,
                producer="unset",
                producer_version="unset",
                platform="unset",
                monotonic_time=raw.monotonic_time,
                wall_time=raw.wall_time,
                privacy_mode=PrivacyMode.METADATA_ONLY,
                coverage_state=CoverageState.GAP,
                identity_confidence=IdentityConfidence.UNKNOWN,
                identity=None,
                result=raw.result,
                platform_details=dict(raw.details or {}),
                coverage_note=raw.coverage_note,
            )

        if raw.type in {"exec_attempt", "exec_started"}:
            event_type = EventType.EXEC
        elif raw.type == "open":
            event_type = (
                EventType.WRITE_ATTEMPT
                if (raw.details or {}).get("write")
                else EventType.READ_ATTEMPT
            )
        elif raw.type == "connect":
            event_type = EventType.CONNECT
        else:
            raise ValueError(f"unsupported Linux eBPF event type: {raw.type!r}")

        details = dict(raw.details or {})
        event = MachineEvent(
            type=event_type,
            producer="unset",
            producer_version="unset",
            platform="unset",
            monotonic_time=raw.monotonic_time,
            wall_time=raw.wall_time,
            privacy_mode=PrivacyMode.METADATA_ONLY,
            coverage_state=CoverageState.COVERED,
            # Not CERTAIN: no start ticks means a reused pid cannot be excluded.
            identity_confidence=IdentityConfidence.AMBIGUOUS,
            identity=self._identity(raw),
            target=raw.target,
            result=raw.result,
            platform_details=details,
            target_state=raw.target_state,
            coverage_note=raw.coverage_note,
        )
        if event_type is EventType.EXEC:
            # The shared classifier decides attempt vs success, so a failed exec
            # is never recorded as an image transition.
            details["outcome"] = exec_outcome(event).value
            event = MachineEvent(
                type=event.type,
                producer=event.producer,
                producer_version=event.producer_version,
                platform=event.platform,
                monotonic_time=event.monotonic_time,
                wall_time=event.wall_time,
                privacy_mode=event.privacy_mode,
                coverage_state=event.coverage_state,
                identity_confidence=event.identity_confidence,
                identity=event.identity,
                target=event.target,
                result=event.result,
                platform_details=details,
                target_state=event.target_state,
                coverage_note=event.coverage_note,
            )
        return event

    # -- reporting -------------------------------------------------------

    @property
    def unparsed_lines(self) -> int:
        """How many lines could not be read. Reported, never swallowed."""
        return self._unparsed


def _interleave(streams: Sequence[IO[str]]) -> Iterator[str]:
    """Round-robin lines from several live streams, tolerating closed ones."""
    import select

    handles = [stream for stream in streams]
    pending: dict[int, list[str]] = {id(stream): [] for stream in handles}
    live = list(handles)
    while live:
        ready, _, _ = select.select(live, [], [], 1.0)
        for stream in ready:
            line = stream.readline()
            if not line:
                live.remove(stream)
                continue
            pending[id(stream)].append(line)
        for stream in list(live):
            buffered = pending[id(stream)]
            while buffered:
                yield buffered.pop(0)
