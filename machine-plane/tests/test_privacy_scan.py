"""Gate 2: what actually landed, checked before anything is reported.

The first live segment failed this gate with absolute paths in it — the recorder
was going straight from ``normalize`` to the spool and skipping the pipeline's
redaction stage. These tests pin both halves: the scan catches an unredacted
target, and the recorder cannot produce one.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from machine_plane.events import EventType, MachineEvent, TargetState
from machine_plane.privacy_scan import gate2, looks_like_path
from machine_plane.recorder import Recorder, RecorderConfig
from machine_plane.spool import Spool
from machine_plane.testing import make_identity


def open_event(target: str | None = None, **details) -> MachineEvent:
    return MachineEvent(
        type=EventType.OPEN,
        producer="test",
        producer_version="1",
        platform="linux",
        monotonic_time=1.0,
        wall_time=2.0,
        result="ok",
        identity=make_identity(),
        target=target,
        target_state=TargetState.PRESENT if target else TargetState.UNKNOWN,
        platform_details=dict(details),
    )


class PathShape(unittest.TestCase):
    def test_absolute_and_relative_paths_are_path_shaped(self) -> None:
        for value in ("/etc/passwd", "~/x", "./x", "../x", "a/b"):
            self.assertTrue(looks_like_path(value), value)

    def test_a_redacted_basename_is_not(self) -> None:
        self.assertFalse(looks_like_path("passwd"))


class TestScanCatchesUnredactedTarget(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-scan-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def segment(self, events) -> Path:
        path = self.directory / "segment-test.ndjson"
        spool = Spool(path, "recorder", mode=0o600)
        for event in events:
            spool.append(event)
        return path

    def test_an_absolute_target_is_refused(self) -> None:
        path = self.segment([open_event("/usr/lib/sysstat/sa1")])
        result = gate2(path, recorder_id="recorder", probe_fields={"label"})
        self.assertFalse(result.passed)
        self.assertTrue(any(f.rule == "unredacted-target" for f in result.findings))

    def test_a_redacted_target_passes(self) -> None:
        path = self.segment([open_event("sa1")])
        result = gate2(path, recorder_id="recorder", probe_fields={"label"})
        self.assertTrue(result.passed, result.report())
        self.assertTrue(result.target_redacted)

    def test_an_undefined_field_is_refused(self) -> None:
        path = self.segment([open_event("sa1", mystery_field="x")])
        result = gate2(path, recorder_id="recorder", probe_fields={"label"})
        self.assertFalse(result.passed)
        self.assertTrue(any(f.rule == "undefined-field" for f in result.findings))

    def test_a_slash_in_comm_is_not_treated_as_a_path(self) -> None:
        """fail2ban renames itself 'f2b/f.sshd'; that is a process name."""
        path = self.segment([open_event("sa1", label="f2b/f.sshd")])
        result = gate2(path, recorder_id="recorder", probe_fields={"label", "label"})
        self.assertFalse(
            any(f.rule == "path-shaped-detail" for f in result.findings), result.report()
        )


class TestRecorderRedactsBeforeStoring(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-redact-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def test_a_path_never_reaches_the_spool_unredacted(self) -> None:
        """The pipeline has a redaction stage; the recorder must run it."""
        from machine_plane.containment import detect

        class ScopedProducer:
            def collect(self):
                yield "one"

            def normalize(self, _raw) -> MachineEvent:
                return open_event("/etc/passwd")

            def redact(self, event: MachineEvent) -> MachineEvent:
                # Stand-in for Producer.redact: the point is that it is called.
                from machine_plane.events import TargetState as State

                return MachineEvent(
                    type=event.type,
                    producer=event.producer,
                    producer_version=event.producer_version,
                    platform=event.platform,
                    monotonic_time=event.monotonic_time,
                    wall_time=event.wall_time,
                    result=event.result,
                    identity=event.identity,
                    target="passwd",
                    target_state=State.PRESENT,
                )

        class Scope:
            name = "mp-test.scope"
            cgroup_inode = "1"

            def wait(self, timeout: float) -> int:
                return 0

            def stop(self) -> None:
                pass

        class Supervisor:
            def start(self, workload):
                return Scope()

        detection = detect(
            account="u", uid=1001, host_id="h",
            sudo_lister=lambda _: "not allowed to run sudo",
            sudoers_lister=lambda _: (), group_lister=lambda a: (a,),
        )
        config = RecorderConfig(
            capture_id="cap",
            spool_dir=self.directory / "spool",
            probes=(Path("/p/x.bt"),),
            workload=("true",),
            duration_s=0.01,
        )
        result = Recorder(
            config,
            supervisor=Supervisor(),
            producer=ScopedProducer(),
            boot_id="b",
            detection=detection,
        ).run()

        self.assertTrue(result.ok, result.error)
        assert result.segment is not None
        scan = gate2(result.segment, recorder_id="machine-plane-recorder",
                     probe_fields={"label"})
        self.assertTrue(scan.passed, scan.report())


if __name__ == "__main__":
    unittest.main()
