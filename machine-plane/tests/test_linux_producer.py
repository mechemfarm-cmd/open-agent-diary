"""Tests for the Linux eBPF producer, driven by captured probe output.

Replay mode is what makes these runnable without root and without bpftrace, which
is why the producer has an explicit captured-log mode rather than only a live one.
The capture below is taken from the real Stage 1 run on Emily, including its
awkward cases: a failed exec, a non-blocking connect, and a line carrying a field
that must never reach storage.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from machine_plane.capabilities import Capability
from machine_plane.events import CoverageState, EventType, TargetState
from machine_plane.identity import IdentityConfidence
from machine_plane.producers.linux_ebpf import LinuxEBPFProducer
from machine_plane.spool import Spool

#: Real Stage 1 output, plus two lines that exercise the failure paths: an
#: unrecognised event name and a malformed one.
CAPTURE = """MARK probe=exec phase=start ts=601969823226352
EV exec_attempt pid=407662 tid=407662 rc=-2 cgroup=341251 ts=601972566149302 label=bash
EV exec_started pid=407661 tid=407661 cgroup=341251 ts=601972564087433 path=/bin/echo
EV open pid=407623 tid=407623 write=0 rc=3 cgroup=341251 ts=601969893578342 label=sleep
EV open pid=407624 tid=407624 write=1 rc=4 cgroup=341251 ts=601969893578343 label=sleep command_line=do-not-store
EV connect pid=407668 tid=407668 rc=-115 cgroup=341251 ts=601972620263948 label=python3
EV rename pid=407700 tid=407700 rc=0 cgroup=341251 ts=601972630000000 label=mv
EV open pid=407701 tid=407701 rc=3 label=broken
"""


class LinuxEBPFProducerTest(unittest.TestCase):
    def _producer(self) -> LinuxEBPFProducer:
        handle = tempfile.NamedTemporaryFile(mode="w", delete=False)
        handle.write(CAPTURE)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return LinuxEBPFProducer(
            captured_log_path=handle.name,
            host_id="host-test",
            boot_id="boot-test",
        )

    # -- parsing ---------------------------------------------------------

    def test_parses_the_four_known_event_types(self) -> None:
        raw = list(self._producer().collect())
        self.assertEqual(
            [item.type for item in raw],
            ["exec_attempt", "exec_started", "open", "open", "connect", EventType.UNSUPPORTED_KERNEL_FEATURE, EventType.UNSUPPORTED_KERNEL_FEATURE],
        )
        self.assertEqual(raw[1].target, "/bin/echo")

    def test_an_unreadable_target_is_unknown_not_absent(self) -> None:
        """The probe never reads a filename, so it cannot claim there is none.

        ABSENT means the action genuinely had no target. An open always has one;
        the producer simply cannot see it. Claiming ABSENT manufactures the exact
        ambiguity TargetState exists to remove.
        """
        raw = list(self._producer().collect())
        self.assertEqual(raw[2].target_state, TargetState.UNKNOWN)   # open
        self.assertEqual(raw[4].target_state, TargetState.UNKNOWN)   # connect
        self.assertEqual(raw[0].target_state, TargetState.UNKNOWN)   # exec_attempt
        # Only exec_started carries a pathname, so only it can claim PRESENT.
        self.assertEqual(raw[1].target_state, TargetState.PRESENT)

    def test_wall_time_is_a_real_clock_not_seconds_since_boot(self) -> None:
        """The probes emit nsecs. Used raw as wall_time that is a date in 1970."""
        raw = list(self._producer().collect())
        now = time.time()
        for item in raw:
            self.assertGreater(item.wall_time, now - 3600)
            self.assertLess(item.wall_time, now + 3600)
        # Monotonic stays the kernel's own clock, which is the ordering authority.
        self.assertAlmostEqual(raw[0].monotonic_time, 601972.566, places=2)

    def test_wall_time_preserves_inter_event_deltas(self) -> None:
        raw = list(self._producer().collect())
        monotonic_delta = raw[1].monotonic_time - raw[0].monotonic_time
        wall_delta = raw[1].wall_time - raw[0].wall_time
        self.assertAlmostEqual(monotonic_delta, wall_delta, places=6)

    def test_unreadable_lines_become_gaps_not_silence(self) -> None:
        producer = self._producer()
        raw = list(producer.collect())
        gaps = [item for item in raw if item.type is EventType.UNSUPPORTED_KERNEL_FEATURE]
        self.assertEqual(len(gaps), 2)
        reasons = " ".join(str(gap.result) for gap in gaps)
        self.assertIn("rename", reasons)          # unrecognised event name
        self.assertIn("missing", reasons)         # malformed: no cgroup/ts
        self.assertEqual(producer.unparsed_lines, 2)

    # -- normalization ---------------------------------------------------

    def test_normalizes_to_neutral_event_types(self) -> None:
        producer = self._producer()
        normalized = [producer.normalize(item) for item in producer.collect()]
        self.assertEqual(
            [event.type for event in normalized],
            [
                EventType.EXEC,
                EventType.EXEC,
                EventType.READ_ATTEMPT,
                EventType.WRITE_ATTEMPT,
                EventType.CONNECT,
                EventType.UNSUPPORTED_KERNEL_FEATURE,
                EventType.UNSUPPORTED_KERNEL_FEATURE,
            ],
        )
        self.assertEqual(normalized[0].platform_details["outcome"], "attempted")
        self.assertEqual(normalized[1].platform_details["outcome"], "succeeded")

    def test_gap_events_are_marked_as_coverage_gaps(self) -> None:
        producer = self._producer()
        normalized = [producer.normalize(item) for item in producer.collect()]
        gaps = [event for event in normalized if event.type is EventType.UNSUPPORTED_KERNEL_FEATURE]
        for event in gaps:
            self.assertEqual(event.coverage_state, CoverageState.GAP)
            note = event.coverage_note
            self.assertIsNotNone(note)
            assert note is not None  # narrowing for the type checker
            self.assertTrue(note.scoped)
            self.assertEqual(note.lost, 1)

    def test_identity_is_not_pid_alone_and_confidence_is_honest(self) -> None:
        """No start ticks means pid reuse cannot be excluded."""
        producer = self._producer()
        normalized = [producer.normalize(item) for item in producer.collect()]
        exec_event = normalized[0]
        identity = exec_event.identity
        self.assertIsNotNone(identity)
        assert identity is not None  # narrowing for the type checker
        self.assertIn("pid:407662", identity.process_instance_id)
        self.assertIn("boot-test", identity.process_instance_id)
        self.assertIn("341251", identity.process_instance_id)
        self.assertIsNone(identity.process_start_time)
        self.assertEqual(exec_event.identity_confidence, IdentityConfidence.AMBIGUOUS)

    # -- capabilities ----------------------------------------------------

    def test_every_stage1_gap_is_declared_unavailable_with_a_reason(self) -> None:
        reports = {
            report.capability: report
            for report in self._producer().capabilities().reports
        }
        for capability in (
            Capability.FILE_RENAME,
            Capability.FILE_UNLINK,
            Capability.PROCESS_EXIT,
            Capability.PROCESS_START_TIME,
            Capability.FILE_PATHS,
            Capability.NETWORK_DESTINATIONS,
            Capability.NETWORK_CONNECT_OUTCOMES,
        ):
            self.assertIn(capability, reports, f"{capability} is not declared at all")
            self.assertFalse(reports[capability].available)
            self.assertTrue(reports[capability].reason)
            self.assertIsNotNone(reports[capability].from_time)

    def test_supported_capabilities_are_declared_available(self) -> None:
        reports = {
            report.capability: report
            for report in self._producer().capabilities().reports
        }
        for capability in (
            Capability.BPF_AVAILABLE,
            Capability.CGROUP_TRACKING,
            Capability.PROCESS_EXEC,
            Capability.FILE_ACTIVITY,
            Capability.NETWORK_CONNECT,
        ):
            self.assertTrue(reports[capability].available)

    # -- privacy and storage ---------------------------------------------

    def test_content_like_fields_never_reach_a_stored_event(self) -> None:
        producer = self._producer()
        events = list(producer.events())
        for event in events:
            self.assertNotIn("command_line", event.platform_details)
            self.assertNotIn("do-not-store", event.to_json())
            event.validate_for_storage()

    def test_pipeline_writes_valid_records_to_the_spool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = Spool(Path(directory) / "events.jsonl", recorder="test-recorder")
            count = self._producer().run(spool)
            self.assertEqual(count, 7)
            records = list(spool.read_from())
            self.assertEqual(len(records), 7)
            for _, _, envelope in records:
                self.assertEqual(envelope.recorder, "test-recorder")
                self.assertNotIn("command_line", envelope.event["platform_details"])

    def test_unsupported_raw_type_is_refused_rather_than_guessed(self) -> None:
        from machine_plane.producer import RawEvent

        producer = self._producer()
        with self.assertRaises(ValueError):
            producer.normalize(RawEvent("something_else", 1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
