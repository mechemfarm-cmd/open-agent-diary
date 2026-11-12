"""The record must refuse to be ambiguous about what it is."""

from __future__ import annotations

import unittest
from typing import Any

from machine_plane.events import (
    CoverageState,
    EventKind,
    EventType,
    MachineEvent,
    PrivacyMode,
    SchemaMismatch,
    event_kind,
)
from machine_plane.testing import make_identity


def _event(**overrides: Any) -> MachineEvent:
    payload: dict[str, Any] = dict(
        type=EventType.WRITE_ATTEMPT,
        producer="mock",
        producer_version="0.1.0",
        platform="linux",
        monotonic_time=10.0,
        wall_time=1_700_000_000.0,
        identity=make_identity(),
        target="/home/x/notes.txt",
        result="ok",
    )
    payload.update(overrides)
    return MachineEvent(**payload)


class EventKindTest(unittest.TestCase):
    def test_gap_types_are_gaps_and_effects_are_actions(self) -> None:
        self.assertIs(event_kind(EventType.BUFFER_OVERFLOW), EventKind.GAP)
        self.assertIs(event_kind(EventType.CLOCK_JUMP), EventKind.GAP)
        self.assertIs(event_kind(EventType.WRITE_ATTEMPT), EventKind.ACTION)

    def test_action_event_requires_an_identity(self) -> None:
        with self.assertRaises(ValueError):
            _event(identity=None)

    def test_gap_event_needs_no_identity(self) -> None:
        event = _event(type=EventType.BUFFER_OVERFLOW, identity=None, target=None)
        self.assertIs(event.kind, EventKind.GAP)


class SchemaVersionTest(unittest.TestCase):
    def test_unreadable_schema_raises_rather_than_defaulting(self) -> None:
        payload = _event().to_dict()
        payload["schema_version"] = 999
        with self.assertRaises(SchemaMismatch):
            MachineEvent.from_dict(payload)

    def test_round_trip_preserves_the_record(self) -> None:
        original = _event(
            platform_details={"pid": 4821},
            coverage_state=CoverageState.COVERED,
        )
        restored = MachineEvent.from_dict(original.to_dict())
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored.event_id, original.event_id)


class ContentGuardTest(unittest.TestCase):
    def test_storage_guard_rejects_content_bearing_detail_keys(self) -> None:
        event = _event(platform_details={"command_line": "rm -rf /"})
        self.assertEqual(event.content_violations(), ("command_line",))
        with self.assertRaises(ValueError):
            event.validate_for_storage()

    def test_construction_stays_permissive_so_redaction_can_run(self) -> None:
        """The pipeline builds the event *before* redacting it.

        Refusing at construction would make the redaction stage unreachable —
        the guarantee belongs at the point of writing, not of building.
        """
        event = _event(platform_details={"command_line": "echo hi"})
        self.assertEqual(event.platform_details["command_line"], "echo hi")

    def test_metadata_only_allows_shape_only_details(self) -> None:
        event = _event(platform_details={"pid": 4821, "size": 12})
        event.validate_for_storage()
        self.assertEqual(event.platform_details["pid"], 4821)

    def test_content_bearing_mode_is_not_the_default(self) -> None:
        self.assertIs(_event().privacy_mode, PrivacyMode.METADATA_ONLY)


class EventIdTest(unittest.TestCase):
    def test_identical_content_yields_an_identical_id(self) -> None:
        self.assertEqual(_event().event_id, _event().event_id)

    def test_different_content_yields_a_different_id(self) -> None:
        other = _event(target="/home/x/other.txt")
        self.assertNotEqual(_event().event_id, other.event_id)


if __name__ == "__main__":
    unittest.main()
