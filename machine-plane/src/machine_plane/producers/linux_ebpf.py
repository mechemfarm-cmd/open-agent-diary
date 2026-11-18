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
import threading
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

#: Which correlation *subjects* each sensor event can produce.
#:
#: Correlation compares a gap's ``affected`` labels against a claim's subject,
#: which is an ``EventType`` value (``exec``, ``write_attempt``, ``connect``, …) —
#: see ``correlation._claim_subject``. A gap labelled with anything else is
#: recorded but never matches, so it silently fails to constrain anything. A
#: malformed line whose event *name* is readable is therefore scoped to the real
#: subjects that class could have produced.
EVENT_SUBJECTS: dict[str, tuple[str, ...]] = {
    "exec_attempt": ("exec",),
    "exec_started": ("exec",),
    "open": ("read_attempt", "write_attempt"),
    "connect": ("connect",),
}

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
        scope_by_cgroup: dict[str | int, str] | None = None,
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
        #: Supervisor-provided map of cgroup inode -> scope name.
        #:
        #: The probes emit the cgroup *inode*; correlation matches runs on
        #: ``isolation_domain.scope``. Without this map every event is
        #: structurally unmatchable, so the supervisor that launches the scope has
        #: to hand its name over — it already knows it, and it is the only party
        #: that can.
        self._scope_by_cgroup = {
            str(cgroup): scope for cgroup, scope in (scope_by_cgroup or {}).items()
        }
        #: Wall clock paired with the first monotonic timestamp seen, used to give
        #: events a real wall time without pretending the kernel emitted one.
        self._wall_clock_origin = (
            wall_clock_origin if wall_clock_origin is not None else time.time()
        )
        self._monotonic_origin: float | None = None
        #: Last kernel timestamp seen, so a gap lands beside the events it spoiled
        #: instead of at a wall-clock instant in a different clock.
        self._last_monotonic: float = 0.0
        self._unparsed = 0
        #: Live bpftrace children, so the recorder can stop collection when
        #: the window ends rather than waiting for them to exit on their own.
        self._live_processes: list[subprocess.Popen[str]] = []
        #: Set when collection is stopped deliberately. A probe that we
        #: terminated exits non-zero by definition, and reporting that as a
        #: probe failure would make every successful run look broken.
        self._stopping = False
        #: Set once every probe script has printed its start marker, which is
        #: the only honest signal that attachment finished. The recorder holds
        #: the workload behind a gate until this fires, because a workload that
        #: starts and finishes in two seconds otherwise completes before any
        #: probe is listening — producing a capture with no attributable events.
        self.attached = threading.Event()
        self._seen_probes: set[str] = set()
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

    def stop(self) -> None:
        """Terminate any live probes. Collection then ends normally."""
        self._stopping = True
        for process in self._live_processes:
            if process.poll() is None:
                process.terminate()

    def _expected_probes(self) -> set[str]:
        """Probe names as the scripts announce them in their start marker."""
        return {Path(path).stem for path in self._probe_paths}

    def scope_argument(self) -> str:
        """The measured cgroup inode, as the probes' positional argument.

        The probes' predicate is ``/cgroup == $1/``. Passing this is what makes
        the capture narrow; omitting it does **not** fail — bpftrace evaluates an
        absent ``$1`` as 0, every EV block matches nothing, and the run produces a
        successful-looking empty capture. That is why the argument is derived
        here, required here, and asserted on each child's argv below.
        """
        if len(self._scope_by_cgroup) != 1:
            raise ValueError(
                "live capture needs exactly one cgroup->scope entry to derive the "
                f"probe argument; got {len(self._scope_by_cgroup)}"
            )
        inode = next(iter(self._scope_by_cgroup))
        try:
            int(inode)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"cgroup inode {inode!r} is not an integer, so it cannot match the "
                "kernel predicate's numeric comparison"
            ) from error
        return str(inode)

    def require_live_prerequisites(self) -> None:
        """Check that this process can actually attach the probes.

        Attaching needs root: on the hosts this was evaluated on
        ``unprivileged_bpf_disabled=2`` and ``perf_event_paranoid=4``, so an
        ordinary agent process cannot attach at all. That is a launch contract, not
        a detail — the recorder runs privileged, and calling this from an
        unprivileged process should say so plainly instead of surfacing as an
        opaque bpftrace failure later.

        The scope map is required for the same reason: without it, every event is
        structurally unmatchable by correlation, and the run would produce
        evidence that can never join to an agent claim.
        """
        import os

        if os.geteuid() != 0:
            raise PermissionError(
                "live capture requires a privileged recorder: bpftrace cannot "
                "attach as an unprivileged process on this host "
                "(unprivileged_bpf_disabled / perf_event_paranoid). Run the "
                "recorder under the supervisor that owns the scope."
            )
        if not self._scope_by_cgroup:
            raise ValueError(
                "live capture requires scope_by_cgroup: the probes emit the cgroup "
                "inode, but correlation matches runs on isolation_domain.scope. "
                "Without the map every event is unmatchable."
            )
        # Fail before attaching rather than after producing an empty capture.
        self.scope_argument()

    def collect(self) -> Iterable[RawEvent]:
        """Yield parsed events from a captured log, or from live probes."""
        if self._captured_log_path is not None:
            with open(self._captured_log_path, encoding="utf-8") as stream:
                yield from self._parse_lines(stream)
            return

        if not self._probe_paths:
            raise ValueError("probe_paths are required unless captured_log_path is set")

        self.require_live_prerequisites()
        scope_argument = self.scope_argument()

        # One process per probe script. A single invocation given several scripts
        # runs only the first and silently ignores the rest.
        processes: list[subprocess.Popen[str]] = []
        drains: list[tuple[subprocess.Popen[str], threading.Thread, list[str]]] = []
        try:
            for path in self._probe_paths:
                process = subprocess.Popen(
                    [self._bpftrace, path, scope_argument],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # The runtime half of the guarantee: the source carries the
                # predicate (Gate 1) and the invocation carries the value (here).
                # Neither implies the other.
                if process.args[-1] != scope_argument:
                    raise RuntimeError(
                        f"{path}: probe was not given the scope argument"
                    )
                processes.append(process)
                self._live_processes.append(process)
                # Drain stderr continuously. Leaving it unread until the process
                # exits deadlocks a chatty probe: the pipe buffer fills (~64 KiB)
                # and bpftrace blocks writing to it while we block reading stdout.
                thread, collected = _drain(process)
                drains.append((process, thread, collected))
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
                if process.returncode and not self._stopping:
                    failures.append(f"{path}: {_collected_stderr(process, drains)}")
            for _, thread, _ in drains:
                thread.join(timeout=2)
            _close_pipes(processes)
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

    def _gap(
        self, reason: str, affected: tuple[str, ...] = (), lost: int = 1
    ) -> RawEvent:
        """Build a gap event. An unreadable line is not silence.

        ``affected`` must be correlation *subjects* (EventType values) to have any
        effect. When the line's class is unknowable — it could not be tokenised, or
        carried an event name this producer does not recognise — the gap is left
        **unscoped**, which correlation treats as applying to everything. That
        over-reports, but the alternative is recording a gap that constrains
        nothing at all.
        """
        self._unparsed += lost
        # Monotonic and wall time are different clocks. A gap lands beside the
        # events it spoiled; using the wall clock here would place it in 1970
        # relative to every other event.
        moment = self._last_monotonic
        return RawEvent(
            EventType.UNSUPPORTED_KERNEL_FEATURE,
            moment,
            self._wall_time_for(moment),
            result=reason,
            details={"reason": reason},
            coverage_note=CoverageNote(affected=affected, lost=lost),
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
                yield self._gap("unparseable line")
                continue
            if not fields or fields[0] == "MARK":
                # A start marker is the only honest evidence that a probe is
                # attached and listening. The recorder holds the workload until
                # every probe has one, because a workload that runs for two
                # seconds otherwise finishes before attachment completes.
                if len(fields) >= 3 and "phase=start" in fields:
                    for field_value in fields[1:]:
                        if field_value.startswith("probe="):
                            self._seen_probes.add(field_value.split("=", 1)[1])
                    if self._seen_probes.issuperset(self._expected_probes()):
                        self.attached.set()
                continue
            if not fields or fields[0] != "EV":
                # The "Attaching N probes..." banner is expected.
                continue

            if len(fields) < 2:
                yield self._gap("event line has no event name")
                continue
            event_name = fields[1]

            values: dict[str, str] = {}
            for field in fields[2:]:
                if "=" not in field:
                    continue
                key, value = field.split("=", 1)
                values[key] = value

            if event_name not in KNOWN_EVENT_NAMES:
                yield self._gap(f"unrecognised sensor event {event_name!r}")
                continue

            missing = [name for name in REQUIRED_FIELDS if name not in values]
            if missing:
                yield self._gap(
                    f"event {event_name!r} missing {','.join(missing)}",
                    EVENT_SUBJECTS.get(event_name, ()),
                )
                continue

            try:
                timestamp = float(values["ts"]) / 1_000_000_000
                pid = int(values["pid"])
                tid = int(values["tid"])
            except ValueError:
                yield self._gap(
                    f"event {event_name!r} has non-numeric identity fields",
                    EVENT_SUBJECTS.get(event_name, ()),
                )
                continue

            self._last_monotonic = timestamp
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

        ``scope`` is the field correlation actually matches a run on
        (``RunRegistry.for_event`` reads ``isolation_domain.scope``, not ``.id``).
        The probe only emits the cgroup *inode*, so the scope name comes from the
        supervisor's map. When it is absent, ``scope`` stays ``None`` and the
        event cannot match anything by scope — which is the truth, and is why
        live mode requires the map.
        """
        details = raw.details or {}
        pid = details.get("pid", "unknown")
        cgroup = details.get("cgroup")
        scope = (
            self._scope_by_cgroup.get(str(cgroup)) if cgroup is not None else None
        )
        domain = (
            IsolationDomain(
                type=DomainType.CGROUP,
                id=str(cgroup),
                scope=scope,
                platform="linux",
            )
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


def _drain(process: subprocess.Popen[str]) -> tuple[threading.Thread, list[str]]:
    """Read a process's stderr on a thread, keeping only a bounded tail.

    A probe's stderr is normally a few lines, but leaving the pipe unread until
    the process exits deadlocks as soon as it writes more than the pipe buffer:
    the child blocks on write while the parent blocks on stdout. Bounded so a
    runaway probe cannot exhaust memory either.
    """
    collected: list[str] = []

    def _read() -> None:
        stream = process.stderr
        if stream is None:
            return
        for line in stream:
            collected.append(line)
            if len(collected) > 200:
                del collected[:100]

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    return thread, collected


def _collected_stderr(
    process: subprocess.Popen[str],
    drains: list[tuple[subprocess.Popen[str], threading.Thread, list[str]]],
) -> str:
    """The drained stderr belonging to *this* process, and no other.

    Keyed by process rather than taking the first non-empty buffer: with three
    probes attached, a shared buffer would attribute one probe's error to another,
    and the error text is the only thing that says which probe misbehaved.
    """
    for owner, thread, collected in drains:
        if owner is not process:
            continue
        if thread.is_alive():
            thread.join(timeout=1.0)
        if collected:
            return " ".join("".join(collected).split())[:200]
        return "(no stderr captured)"
    return "(probe was never started)"


def _close_pipes(processes: Sequence[subprocess.Popen[str]]) -> None:
    """Close the pipes we opened, so they are not left to the garbage collector."""
    for process in processes:
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


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
