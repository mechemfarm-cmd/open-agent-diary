"""The ingestion boundary.

Three properties matter more than throughput:

- a partial record is never consumed as if complete, so ingestion resumes;
- an unreadable record stops the import rather than being skipped;
- the importer stamps ``plane=machine`` itself, so nothing in the payload can
  claim to be machine-observed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from machine_plane.events import EventType, MachineEvent, SchemaMismatch
from machine_plane.importer import Importer, MemoryStore
from machine_plane.spool import SPOOL_VERSION, Spool, SpoolEnvelope, SpoolError
from machine_plane.testing import MockProducer, action_event


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.spool = Spool(self.root / "machine.spool", recorder="recorder-1")
        self.checkpoint = self.root / "offset.json"

    def make_event(self, index: int = 0) -> MachineEvent:
        raws = [
            action_event(EventType.WRITE_ATTEMPT, target=f"/home/x/file{index}.txt")
        ]
        return next(iter(MockProducer(raws).events()))

    def write_raw_line(self, payload: dict) -> None:
        with open(self.spool.path, "ab") as handle:
            handle.write((json.dumps(payload) + "\n").encode("utf-8"))

    def envelope(self, event: dict, recorder: str = "recorder-1") -> dict:
        return {
            "spool_version": SPOOL_VERSION,
            "recorder": recorder,
            "recorded_at": 1.0,
            "event": event,
        }


class SpoolRoundTripTest(_Fixture):
    def test_appended_events_are_read_back(self) -> None:
        self.spool.append(self.make_event())
        self.spool.append(self.make_event(1))
        records = list(self.spool.read_from(0))
        self.assertEqual(len(records), 2)

    def test_reading_from_an_offset_skips_earlier_records(self) -> None:
        self.spool.append(self.make_event())
        first = list(self.spool.read_from(0))[0][1]
        self.spool.append(self.make_event(1))
        remaining = list(self.spool.read_from(first))
        self.assertEqual(len(remaining), 1)

    def test_records_report_their_own_offsets(self) -> None:
        self.spool.append(self.make_event())
        self.spool.append(self.make_event(1))
        records = list(self.spool.read_from(0))
        self.assertEqual(records[0][0], 0)
        self.assertEqual(records[0][1], records[1][0])


class PartialRecordTest(_Fixture):
    def test_a_partial_trailing_record_is_not_consumed(self) -> None:
        self.spool.append(self.make_event())
        after_first = list(self.spool.read_from(0))[0][1]

        # A record that has been written but not yet terminated by a newline.
        partial = SpoolEnvelope(
            spool_version=SPOOL_VERSION,
            recorder="recorder-1",
            recorded_at=1.0,
            event=self.make_event(1).to_dict(),
        ).to_json()
        with open(self.spool.path, "ab") as handle:
            handle.write(partial.encode("utf-8"))

        records = list(self.spool.read_from(after_first))
        self.assertEqual(records, [], "a half-written record must not be read")

        with open(self.spool.path, "ab") as handle:
            handle.write(b"\n")

        records = list(self.spool.read_from(after_first))
        self.assertEqual(len(records), 1, "the completed record is now readable")

    def test_import_resumes_after_a_partial_record(self) -> None:
        importer = Importer(
            self.spool, "recorder-1", MemoryStore(), self.checkpoint
        )
        self.spool.append(self.make_event())
        partial = SpoolEnvelope(
            spool_version=SPOOL_VERSION,
            recorder="recorder-1",
            recorded_at=1.0,
            event=self.make_event(1).to_dict(),
        ).to_json()
        with open(self.spool.path, "ab") as handle:
            handle.write(partial.encode("utf-8"))

        first = importer.run()
        self.assertEqual(first.imported, 1)

        with open(self.spool.path, "ab") as handle:
            handle.write(b"\n")

        second = importer.run()
        self.assertEqual(second.imported, 1)
        self.assertEqual(second.duplicates, 0)


class ImporterTest(_Fixture):
    def test_plane_is_stamped_by_the_importer(self) -> None:
        store = MemoryStore()
        self.spool.append(self.make_event())
        Importer(self.spool, "recorder-1", store, self.checkpoint).run()
        self.assertEqual(store.records[0]["plane"], "machine")

    def test_rerunning_does_not_duplicate(self) -> None:
        store = MemoryStore()
        self.spool.append(self.make_event())
        importer = Importer(self.spool, "recorder-1", store, self.checkpoint)
        first = importer.run()
        second = importer.run()
        self.assertEqual(first.imported, 1)
        self.assertEqual(second.imported, 0)
        self.assertEqual(len(store.records), 1)

    def test_two_identical_events_are_both_kept(self) -> None:
        """Redaction can make distinct events look identical.

        Two different paths, both dropped by policy, produce records with the
        same content and the same content-derived event id. Merging them would
        silently lose one real action — a wrong merge is the same class of
        error as a wrong match, so identity is the record's position, not its
        content.
        """
        store = MemoryStore()
        event = self.make_event()
        self.spool.append(event)
        self.spool.append(event)
        result = Importer(self.spool, "recorder-1", store, self.checkpoint).run()

        self.assertEqual(result.imported, 2)
        self.assertEqual(result.duplicates, 0)
        self.assertEqual(len(store.records), 2)
        self.assertEqual(
            store.records[0]["event_id"], store.records[1]["event_id"]
        )
        self.assertNotEqual(
            store.records[0]["record_key"], store.records[1]["record_key"]
        )

    def test_only_new_records_are_imported_on_a_later_run(self) -> None:
        store = MemoryStore()
        importer = Importer(self.spool, "recorder-1", store, self.checkpoint)
        self.spool.append(self.make_event())
        importer.run()
        self.spool.append(self.make_event(1))
        second = importer.run()
        self.assertEqual(second.imported, 1)
        self.assertEqual(len(store.records), 2)


class FailClosedTest(_Fixture):
    def test_unreadable_schema_raises_and_leaves_the_checkpoint_alone(self) -> None:
        store = MemoryStore()
        payload = self.make_event(1).to_dict()
        payload["schema_version"] = 999
        self.write_raw_line(self.envelope(payload))

        importer = Importer(self.spool, "recorder-1", store, self.checkpoint)
        with self.assertRaises(SchemaMismatch):
            importer.run()

        self.assertEqual(importer.load_offset(), 0)
        self.assertEqual(store.records, [])

    def test_a_good_record_before_a_bad_one_is_kept_and_the_rest_waits(self) -> None:
        """Fail-closed means 'stop here', not 'discard what already landed'."""
        store = MemoryStore()
        importer = Importer(self.spool, "recorder-1", store, self.checkpoint)
        self.spool.append(self.make_event())
        good_offset = self.spool.size()

        payload = self.make_event(1).to_dict()
        payload["schema_version"] = 999
        self.write_raw_line(self.envelope(payload))

        with self.assertRaises(SchemaMismatch):
            importer.run()

        self.assertEqual(len(store.records), 1)
        self.assertEqual(importer.load_offset(), good_offset)

    def test_the_spool_refuses_to_write_a_content_bearing_event(self) -> None:
        from machine_plane.events import MachineEvent
        from machine_plane.testing import make_identity

        event = MachineEvent(
            type=EventType.WRITE_ATTEMPT,
            producer="mock",
            producer_version="0.1.0",
            platform="linux",
            monotonic_time=1.0,
            wall_time=1.0,
            identity=make_identity(),
            platform_details={"command_line": "echo secret"},
        )
        with self.assertRaises(ValueError):
            self.spool.append(event)
        self.assertEqual(self.spool.size(), 0)

    def test_a_foreign_recorder_is_rejected(self) -> None:
        store = MemoryStore()
        self.write_raw_line(
            self.envelope(self.make_event().to_dict(), recorder="someone-else")
        )
        importer = Importer(self.spool, "recorder-1", store, self.checkpoint)
        with self.assertRaises(SpoolError):
            importer.run()
        self.assertEqual(store.records, [])

    def test_an_unreadable_spool_version_is_rejected(self) -> None:
        payload = self.envelope(self.make_event().to_dict())
        payload["spool_version"] = 99
        self.write_raw_line(payload)
        with self.assertRaises(SpoolError):
            list(self.spool.read_from(0))


class ForgeryTest(_Fixture):
    def test_an_event_payload_has_nowhere_to_claim_a_plane(self) -> None:
        """The event schema has no plane field, so a payload cannot assert one."""
        payload = self.make_event().to_dict()
        self.assertNotIn("plane", payload)

    def test_a_payload_cannot_smuggle_a_plane_through_details(self) -> None:
        store = MemoryStore()
        event = self.make_event()
        payload = event.to_dict()
        payload["platform_details"] = {"plane": "agent"}
        self.write_raw_line(self.envelope(payload))
        Importer(self.spool, "recorder-1", store, self.checkpoint).run()
        self.assertEqual(store.records[0]["plane"], "machine")


if __name__ == "__main__":
    unittest.main()
