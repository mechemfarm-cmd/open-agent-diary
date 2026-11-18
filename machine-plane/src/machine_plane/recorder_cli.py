"""``machine-plane-recorder`` — install, inspect, and run one bounded capture.

Deliberately three verbs and no daemon. The unit is a template oneshot with no
``[Install]`` section, so nothing here can leave a recorder running after a reboot;
``record`` is the only thing that attaches a probe, and it does so once.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .containment import (
    DetectionError,
    Level,
    Consent,
    ConsentKind,
    consent_message,
    decide_startup,
    detect,
    read_consent,
    write_consent,
)
from .privacy_gate import run_gate
from .privacy_scan import gate2
from .recorder import Recorder, RecorderConfig, RunResult, recover_torn_tails
from .retention import RetentionJournal
from .supervisor import SystemdSupervisor, current_boot_id, require_root

DEFAULT_SPOOL = Path("/var/lib/machine-plane/spool")
DEFAULT_CONSENT = Path("/etc/machine-plane/containment.json")
DEFAULT_PROBES = Path("/usr/lib/machine-plane/probes")
#: The paired unfiltered set. A separate directory, not a flag on the same files:
#: a control that can be produced by turning the filter off at runtime is one
#: typo away from being evidence.
DEFAULT_CONTROL_PROBES = Path("/usr/lib/machine-plane/probes/control")


def _detect_or_error(agent_account: str | None = None):
    """Detect the *agent's* protection level, not the caller's.

    The recorder runs as root, so detecting the current account would always
    answer "root can do anything" — true, and useless. The question is what the
    agent can do, which means the account has to be named.
    """
    if agent_account is None:
        if os.geteuid() == 0:
            return None, (
                "running as root: pass --agent-account so the protection level "
                "describes the agent rather than the recorder"
            )
        try:
            return detect(), None
        except DetectionError as error:
            return None, str(error)

    try:
        import pwd

        entry = pwd.getpwnam(agent_account)
    except (ImportError, KeyError):
        return None, f"no such account: {agent_account}"

    try:
        return detect(account=entry.pw_name, uid=entry.pw_uid), None
    except DetectionError as error:
        return None, str(error)



def _producer_module() -> Path:
    import machine_plane

    return Path(machine_plane.__file__).parent / "producers" / "linux_ebpf.py"


def _gate(config: RecorderConfig):
    """Gate 1, before anything is installed or attached.

    Run before consent is written and before a probe exists: a dereference that
    nobody approved is not repaired by a later scan of what it produced.
    """
    result = run_gate(
        list(config.probes),
        producer_module=_producer_module(),
        mode=config.capture_mode,
    )
    print(result.report())
    return (0 if result.passed else 1), result.fields


def _gate_code(config: RecorderConfig) -> int:
    return _gate(config)[0]



def _build_config(args) -> RecorderConfig:
    probe_root = DEFAULT_CONTROL_PROBES if args.control else DEFAULT_PROBES
    probes = tuple(sorted(probe_root.glob("*.bt"))) if not args.probe else tuple(
        Path(item) for item in args.probe
    )
    if not probes:
        raise ValueError(f"no probes found under {probe_root}")
    return RecorderConfig(
        capture_id=args.capture_id,
        spool_dir=Path(args.spool),
        probes=probes,
        workload=tuple(args.workload),
        duration_s=args.duration,
        max_bytes=args.max_bytes,
        journal_max_bytes=args.journal_max_bytes,
        capture_mode="control" if args.control else "filtered",
    )


# -- install ---------------------------------------------------------------


def _install(args) -> int:
    if args.gate:
        if _gate_code(_build_config(args)) != 0:
            print("\nrefusing to install: Gate 1 failed", file=sys.stderr)
            return 1
    detection, error = _detect_or_error(args.agent_account)
    config = _build_config(args)
    consent = None
    try:
        consent = read_consent(args.consent_file)
    except ValueError as read_error:
        print(f"warning: {read_error}", file=sys.stderr)

    decision = decide_startup(
        detection=detection,
        detection_error=error,
        consent=consent,
        current_config=config.config_fingerprint(),
    )

    if decision.allowed and detection is None:
        print("internal: an allowed decision requires a detection", file=sys.stderr)
        return 1

    if decision.allowed and decision.outcome.name.startswith("OK_ENFORCED"):
        write_consent(
            args.consent_file,
            Consent(
                level=detection.level,
                fingerprint=detection.fingerprint(),
                config_fingerprint=config.config_fingerprint(),
                detected_at=time.time(),
                consent_kind=ConsentKind.INTERACTIVE,
                consented_at=time.time(),
            ),
        )
        print(f"protection level: {detection.level.value}")
        print("no advisory consent is required on this machine")
        return 0

    if decision.allowed:
        # Refresh the record so the fingerprint tracks the current state.
        write_consent(
            args.consent_file,
            Consent(
                level=detection.level,
                fingerprint=detection.fingerprint(),
                config_fingerprint=config.config_fingerprint(),
                detected_at=time.time(),
                consent_kind=ConsentKind.INTERACTIVE,
                consented_at=time.time(),
            ),
        )
        print(f"protection level: {detection.level.value} (consent current)")
        return 0

    print(consent_message(decision, detection))

    if args.accept_advisory:
        kind = ConsentKind.FLAG
    elif sys.stdin.isatty() and not args.quiet:
        answer = input("\n> ").strip().lower()
        if answer not in {"y", "yes"}:
            print("not accepted; nothing was written", file=sys.stderr)
            return decision.exit_code
        kind = ConsentKind.INTERACTIVE
    else:
        print(
            "\nrefusing: advisory mode requires an explicit answer."
            " Re-run with --accept-advisory for a non-interactive install.",
            file=sys.stderr,
        )
        return decision.exit_code

    assert detection is not None
    write_consent(
        args.consent_file,
        Consent(
            level=detection.level,
            fingerprint=detection.fingerprint(),
            config_fingerprint=config.config_fingerprint(),
            detected_at=time.time(),
            consent_kind=kind,
            consented_at=time.time(),
        ),
    )
    print(f"accepted: {detection.level.value} ({kind.value}) -> {args.consent_file}")
    return 0


# -- status ----------------------------------------------------------------


def _status(args) -> int:
    detection, error = _detect_or_error(args.agent_account)
    if detection is None:
        print(f"protection: UNKNOWN ({error})")
    else:
        print(f"protection: {detection.level.value.upper()}")
        for reason in detection.reasons:
            print(f"  - {reason}")

    try:
        consent = read_consent(args.consent_file)
    except ValueError as read_error:
        print(f"consent   : unreadable ({read_error})")
        consent = None

    if consent is None:
        print("consent   : none on file")
    elif detection is not None and consent.fingerprint == detection.fingerprint():
        print(f"consent   : current ({consent.consent_kind.value}, {consent.level.value})")
    else:
        print("consent   : STALE — the protection situation has changed")

    spool = Path(args.spool)
    if not spool.is_dir():
        print(f"spool     : {spool} (absent)")
        return 0

    try:
        sealed = sorted(spool.glob("segment-*.ndjson"))
        partial = sorted(spool.glob("segment-*.ndjson.partial"))
        total = sum(path.stat().st_size for path in sealed)
    except PermissionError:
        # Expected for an unprivileged caller: the spool is root:mp-read 0750.
        # Say so rather than reporting an empty spool, which would read as "no
        # data" — the opposite of what the permission denial means.
        print(f"spool     : {spool} (not readable by this account)")
        return 0

    print(f"spool     : {spool}")
    print(f"  segments: {len(sealed)} sealed, {total} bytes")
    print(f"  partial : {len(partial)} unsealed" + (" (torn tail)" if partial else ""))

    journal_path = spool / "retention.journal"
    journal = RetentionJournal(journal_path, args.journal_max_bytes)
    try:
        totals = journal.totals()
        size = journal.size()
    except PermissionError:
        print("  journal : not readable by this account")
        return 0
    print(f"  journal : {size} bytes, {totals.events} drops, "
          f"{totals.compactions} compactions")
    if totals.detail_from is not None:
        print(
            "            retention detail before "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(totals.detail_from))}"
            " has been compacted"
        )
    return 0


# -- record ----------------------------------------------------------------


def _record(args) -> int:
    require_root("recorder")
    gate_code, probe_fields = _gate(_build_config(args))
    if gate_code != 0:
        print("\nrefusing to attach: Gate 1 failed", file=sys.stderr)
        return 1
    config = _build_config(args)
    if not config.probes:
        print(f"no probes found under {DEFAULT_PROBES}", file=sys.stderr)
        return 1

    detection, error = _detect_or_error(args.agent_account)
    consent = None
    try:
        consent = read_consent(args.consent_file)
    except ValueError:
        consent = None

    boot_id = current_boot_id()
    sealed_before = len(list(Path(config.spool_dir).glob("segment-*.ndjson")))

    def factory(scope):
        from .producers.linux_ebpf import LinuxEBPFProducer

        return LinuxEBPFProducer(
            probe_paths=config.probes,
            host_id=detection.host_id if detection else "unknown-host",
            boot_id=boot_id,
            scope_by_cgroup={scope.cgroup_inode: scope.name},
        )

    recorder = Recorder(
        config,
        supervisor=SystemdSupervisor(capture_id=config.capture_id),
        producer_factory=factory,
        boot_id=boot_id,
        sequence=sealed_before,
        detection=detection,
        detection_error=error,
        consent=consent,
    )

    result: RunResult = recorder.run()
    if result.refusal is not None:
        print(consent_message(result.refusal, detection))
        print(f"\nrefused (exit {result.exit_code}); nothing was recorded", file=sys.stderr)
        return result.exit_code

    if result.segment is not None:
        print(f"segment   : {result.segment}")
        print(f"records   : {result.records}")
        print(f"end reason: {result.end_reason}")
    if result.dropped:
        print(f"dropped   : {', '.join(result.dropped)}")
    if result.error:
        print(f"error     : {result.error}", file=sys.stderr)

    # Gate 2 after sealing, while the recorder still has write access. A segment
    # that fails is renamed out of the importable namespace immediately, so it
    # cannot be ingested by anyone who runs the importer first.
    if result.ok and result.segment is not None:
        scan = gate2(
            result.segment,
            recorder_id=config.recorder_id,
            probe_fields=probe_fields,
        )
        print(scan.report())
        if not scan.passed:
            voided = result.segment.with_name(result.segment.name + ".voided")
            result.segment.rename(voided)
            print(f"voided    : {voided} — this segment will not be imported",
                  file=sys.stderr)
            return 8
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="machine-plane-recorder")
    parser.add_argument("command", choices=["install", "status", "record", "recover"])
    parser.add_argument("--capture-id", default=None)
    parser.add_argument("--spool", default=str(DEFAULT_SPOOL))
    parser.add_argument("--consent-file", default=str(DEFAULT_CONSENT))
    parser.add_argument("--probe", action="append", default=[])
    parser.add_argument("--workload", nargs=argparse.REMAINDER, default=[])
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--max-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--journal-max-bytes", type=int, default=1024 * 1024)
    parser.add_argument(
        "--agent-account",
        default=None,
        help="the account the agent runs as; required when running as root",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        default=True,
        help="run Gate 1 (static privacy inspection); on by default",
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="record the paired unfiltered control run; never workload evidence",
    )
    parser.add_argument("--accept-advisory", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.capture_id is None:
        args.capture_id = time.strftime("mp-stage3-%Y%m%dT%H%M%SZ", time.gmtime())

    if args.command == "install":
        return _install(args)
    if args.command == "status":
        return _status(args)
    if args.command == "recover":
        sealed = recover_torn_tails(Path(args.spool))
        print(f"sealed {len(sealed)} torn tail(s)")
        return 0
    if not args.workload:
        print("record needs a workload, e.g. -- sleep 30", file=sys.stderr)
        return 2
    return _record(args)


if __name__ == "__main__":
    sys.exit(main())
