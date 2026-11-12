"""The producer pipeline: collect, normalize, redact, annotate, emit."""

from __future__ import annotations

import unittest

from machine_plane.events import EventType, PrivacyMode
from machine_plane.privacy import PathDisplay, PrivacyPolicy
from machine_plane.testing import MockProducer, action_event


class PipelineRedactionTest(unittest.TestCase):
    def _producer(self) -> MockProducer:
        policy = PrivacyPolicy(
            allowed_roots=("/home/x/work",), path_display=PathDisplay.BASENAME
        )
        raws = [
            action_event(EventType.EXEC, target="/usr/bin/python3"),
            action_event(EventType.WRITE_ATTEMPT, target="/home/x/work/report.txt"),
            action_event(EventType.WRITE_ATTEMPT, target="/home/x/private/notes.txt"),
            action_event(EventType.WRITE_ATTEMPT, target="/home/x/.ssh/id_ed25519"),
            action_event(
                EventType.CONNECT,
                target="https://api.example.com/v1/complete?token=secret",
            ),
            action_event(
                EventType.WRITE_ATTEMPT,
                target="/home/x/work/report.txt",
                details={"pid": 4821, "command_line": "echo hi > report.txt"},
            ),
        ]
        return MockProducer(raws, policy=policy)

    def test_pipeline_redacts_before_emitting(self) -> None:
        events = list(self._producer().events())
        targets = [event.target for event in events]

        self.assertEqual(targets[1], "report.txt")
        self.assertIsNone(targets[2], "path outside allowed roots must not survive")
        self.assertIsNone(targets[3], "credential path must not survive")

    def test_network_destinations_are_coarsened(self) -> None:
        events = list(self._producer().events())
        connect = events[4]
        self.assertEqual(connect.target, "https://api.example.com")

    def test_command_lines_never_reach_the_event(self) -> None:
        events = list(self._producer().events())
        last = events[5]
        self.assertEqual(last.platform_details, {"pid": 4821})

    def test_redaction_happens_before_the_sink_is_reached(self) -> None:
        captured: list = []

        class Recorder:
            def append(self, event) -> None:
                captured.append(event)

        self._producer().run(Recorder())
        for event in captured:
            self.assertNotIn("command_line", event.platform_details)
            self.assertIsNot(event.privacy_mode, PrivacyMode.CONTENT_BEARING)


class PipelineProvenanceTest(unittest.TestCase):
    def test_provenance_is_stamped_by_the_pipeline_not_the_producer(self) -> None:
        producer = MockProducer([action_event()])
        event = next(iter(producer.events()))
        self.assertEqual(event.producer, "mock")
        self.assertEqual(event.producer_version, "0.1.0")
        self.assertEqual(event.platform, "linux")

    def test_capabilities_are_captured_on_the_event(self) -> None:
        producer = MockProducer([action_event()])
        event = next(iter(producer.events()))
        self.assertGreater(len(event.capabilities_at_capture), 0)


class ContentModeTest(unittest.TestCase):
    def test_content_bearing_mode_is_refused_rather_than_silently_allowed(self) -> None:
        from dataclasses import replace

        from machine_plane.events import MachineEvent, PrivacyMode
        from machine_plane.testing import make_identity

        event = MachineEvent(
            type=EventType.WRITE_ATTEMPT,
            producer="mock",
            producer_version="0.1.0",
            platform="linux",
            monotonic_time=1.0,
            wall_time=1.0,
            identity=make_identity(),
            privacy_mode=PrivacyMode.CONTENT_BEARING,
        )
        producer = MockProducer([])
        with self.assertRaises(NotImplementedError):
            producer.redact(event)


if __name__ == "__main__":
    unittest.main()
