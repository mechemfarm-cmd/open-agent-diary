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
from .recorder import Recorder, RecorderConfig, RunResult, recover_torn_tails
from .retention import RetentionJournal
from .supervisor import SystemdSupervisor, current_boot_id, require_root

DEFAULT_SPOOL = Path("/var/lib/machine-plane/spool")
DEFAULT_CONSENT = Path("/etc/machine-plane/containment.json")
DEFAULT_PROBES = Path("/usr/lib/machine-plane/probes")


def _detect_or_error():
    try:
        return detect(), None
    except DetectionError as error:
        return None, str(error)


def _build_config(args) -> RecorderConfig:
    probes = tuple(sorted(DEFAULT_PROBES.glob("*.bt"))) if not args.probe else tuple(
        Path(item) for item in args.probe
    )
    return RecorderConfig(
        capture_id=args.capture_id,
        spool_dir=Path(args.spool),
        probes=probes,
        workload=tuple(args.workload),
        duration_s=args.duration,
        max_bytes=args.max_bytes,
        journal_max_bytes=args.journal_max_bytes,
    )


# -- install ---------------------------------------------------------------


def _install(args) -> int:
    detection, error = _detect_or_error()
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
    detection, error = _detect_or_error()
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

    sealed = sorted(spool.glob("segment-*.ndjson"))
    partial = sorted(spool.glob("segment-*.ndjson.partial"))
    total = sum(path.stat().st_size for path in sealed)
    print(f"spool     : {spool}")
    print(f"  segments: {len(sealed)} sealed, {total} bytes")
    print(f"  partial : {len(partial)} unsealed" + (" (torn tail)" if partial else ""))

    journal_path = spool / "retention.journal"
    journal = RetentionJournal(journal_path, args.journal_max_bytes)
    totals = journal.totals()
    print(f"  journal : {journal.size()} bytes, {totals.events} drops, "
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
    config = _build_config(args)
    if not config.probes:
        print(f"no probes found under {DEFAULT_PROBES}", file=sys.stderr)
        return 1

    detection, error = _detect_or_error()
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
