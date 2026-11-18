"""The recorder: one run, one segment, sealed by rename.

These exercise the ordering without root: the supervisor and the producer are
fakes, so what is under test is the sequence — decide, rotate, start, record,
seal — and what happens when a step fails.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from machine_plane.containment import Consent, ConsentKind, Level, detect
from machine_plane.events import EventType, MachineEvent
from machine_plane.recorder import (
    Recorder,
    RecorderConfig,
    SegmentWriter,
    recover_torn_tails,
)
from machine_plane.testing import make_identity


def enforced() -> object:
    return detect(
        account="test-unprivileged",
        uid=1001,
        host_id="host",
        sudo_lister=lambda _: "not allowed to run sudo",
        group_lister=lambda _: ("test-unprivileged",),
        unit_controller=lambda _: False,
    )


class FakeScope:
    def __init__(self) -> None:
        self.name = "mp-stage3-test.scope"
        self.cgroup_inode = "123456"
        self.waited = None
        self.stopped = False

    def wait(self, timeout: float) -> int:
        self.waited = timeout
        return 0

    def stop(self) -> None:
        self.stopped = True


class FakeSupervisor:
    def __init__(self) -> None:
        self.scope = FakeScope()
        self.started_with = None

    def start(self, workload):
        self.started_with = tuple(workload)
        return self.scope


class FakeProducer:
    def __init__(self, count: int = 3, explode: bool = False) -> None:
        self.count = count
        self.explode = explode

    def collect(self):
        for index in range(self.count):
            if self.explode and index == 1:
                raise RuntimeError("bpftrace died")
            yield index

    def normalize(self, raw) -> MachineEvent:
        return MachineEvent(
            type=EventType.OPEN,
            producer="test",
            producer_version="1",
            platform="linux",
            monotonic_time=float(raw),
            wall_time=1000.0 + float(raw),
            result="ok",
            identity=make_identity(),
        )


class RecorderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-recorder-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )
        self.spool = self.directory / "spool"

    def config(
        self,
        *,
        capture_id: str = "cap-1",
        max_bytes: int = 256 * 1024 * 1024,
        journal_max_bytes: int = 1024 * 1024,
    ) -> RecorderConfig:
        return RecorderConfig(
            capture_id=capture_id,
            spool_dir=self.spool,
            probes=(Path("/probes/exec.bt"),),
            workload=("sleep", "1"),
            duration_s=1.0,
            max_bytes=max_bytes,
            journal_max_bytes=journal_max_bytes,
        )

    def recorder(self, *, producer=None, config=None, detection=None) -> Recorder:
        return Recorder(
            config or self.config(),
            supervisor=FakeSupervisor(),
            producer=producer or FakeProducer(),
            boot_id="boot1234",
            sequence=0,
            detection=detection if detection is not None else enforced(),
        )


class TestSealing(RecorderTestCase):
    def test_a_run_produces_one_sealed_segment_and_no_partial(self) -> None:
        result = self.recorder().run()

        self.assertTrue(result.ok, result.error)
        assert result.segment is not None
        self.assertTrue(result.segment.exists())
        self.assertEqual(list(self.spool.glob("*.partial")), [])
        self.assertEqual(len(list(self.spool.glob("*.ndjson"))), 1)

    def test_the_segment_opens_and_closes_with_lifecycle_records(self) -> None:
        result = self.recorder().run()
        assert result.segment is not None
        types = [
            MachineEvent.from_dict(json.loads(line)["event"]).type
            for line in result.segment.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertIs(types[0], EventType.RECORDER_STARTED)
        self.assertIs(types[-1], EventType.RECORDER_STOPPED)

    def test_the_header_carries_the_protection_level_and_the_join_key(self) -> None:
        result = self.recorder().run()
        assert result.segment is not None
        header = json.loads(result.segment.read_text(encoding="utf-8").splitlines()[0])
        details = header["event"]["platform_details"]
        self.assertEqual(details["cgroup_inode"], "123456")
        self.assertEqual(details["protection_level"], "enforced")
        self.assertEqual(details["scope"], "mp-stage3-test.scope")


class TestRefusal(RecorderTestCase):
    def test_advisory_without_consent_writes_nothing(self) -> None:
        advisory = detect(
            account="agent-account",
            uid=1000,
            host_id="host",
            sudo_lister=lambda _: "(ALL : ALL) NOPASSWD: ALL",
            group_lister=lambda _: ("agent-account",),
        )
        result = self.recorder(detection=advisory).run()

        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertIsNone(result.segment)
        self.assertEqual(list(self.spool.glob("*.ndjson")), [])


class TestFailure(RecorderTestCase):
    def test_a_probe_error_seals_with_an_explicit_reason(self) -> None:
        result = self.recorder(producer=FakeProducer(count=3, explode=True)).run()

        self.assertFalse(result.ok)
        self.assertEqual(result.end_reason, "probe_error")
        assert result.segment is not None
        self.assertTrue(result.segment.exists(), "a failed run is still sealed")
        self.assertEqual(list(self.spool.glob("*.partial")), [])

        last = json.loads(result.segment.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last["event"]["result"], "probe_error")


class TestTornTails(RecorderTestCase):
    def test_a_leftover_partial_is_sealed_as_a_torn_tail(self) -> None:
        self.spool.mkdir(parents=True, exist_ok=True)
        partial = self.spool / "segment-boot1234-000000-cap-0.ndjson.partial"
        writer = SegmentWriter(
            self.spool,
            capture_id="cap-0",
            boot_id="boot1234",
            sequence=0,
            recorder_id="machine-plane-recorder",
        )
        writer.append(
            MachineEvent(
                type=EventType.OPEN,
                producer="test",
                producer_version="1",
                platform="linux",
                monotonic_time=0.0,
                wall_time=1.0,
                result="ok",
                identity=make_identity(),
            )
        )
        self.assertTrue(partial.exists())

        sealed = recover_torn_tails(self.spool)

        self.assertEqual(len(sealed), 1)
        self.assertEqual(list(self.spool.glob("*.partial")), [])
        published = self.spool / "segment-boot1234-000000-cap-0.ndjson"
        self.assertTrue(published.exists())
        last = json.loads(published.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last["event"]["result"], "torn_tail")


if __name__ == "__main__":
    unittest.main()
