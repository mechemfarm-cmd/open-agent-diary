"""Importing across segments.

The importer was written when a spool was one append-only file. Stage 3 produces
one segment per run, and every segment restarts its byte offsets at zero — so a
record key built from recorder plus offset makes the first record of each segment
collide with the first record of every other.

That is not a theoretical concern: the first real import reported exactly three
duplicates, and every one of them was a distinct event being silently merged with
a stranger.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from machine_plane.events import EventType, MachineEvent
from machine_plane.importer import Importer, MemoryStore
from machine_plane.spool import Spool
from machine_plane.testing import make_identity


def event(index: int) -> MachineEvent:
    return MachineEvent(
        type=EventType.OPEN,
        producer="test",
        producer_version="1",
        platform="linux",
        monotonic_time=float(index),
        wall_time=1000.0 + index,
        result="ok",
        identity=make_identity(),
    )


class TestSegmentsDoNotCollide(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-segments-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def write_segment(self, name: str, count: int) -> Path:
        path = self.directory / name
        spool = Spool(path, "recorder")
        for index in range(count):
            spool.append(event(index))
        return path

    def test_the_first_record_of_each_segment_survives(self) -> None:
        """Two segments, identical offsets, both first records must be kept."""
        first = self.write_segment("segment-boot-000000-a.ndjson", 2)
        second = self.write_segment("segment-boot-000001-b.ndjson", 2)

        store = MemoryStore()
        for path in (first, second):
            Importer(
                Spool(path, "recorder"),
                recorder_id="recorder",
                store=store,
            ).run()

        self.assertEqual(
            len(store.records), 4, "every record from both segments must be stored"
        )
        self.assertEqual(len({r["record_key"] for r in store.records}), 4)

    def test_reimporting_one_segment_is_still_a_no_op(self) -> None:
        """The fix must not break idempotence within a single segment."""
        path = self.write_segment("segment-boot-000000-a.ndjson", 3)
        store = MemoryStore()

        first = Importer(Spool(path, "recorder"), recorder_id="recorder", store=store).run()
        second = Importer(Spool(path, "recorder"), recorder_id="recorder", store=store).run()

        self.assertEqual(first.imported, 3)
        self.assertEqual(second.imported, 0)
        self.assertEqual(second.duplicates, 3)
        self.assertEqual(len(store.records), 3)


if __name__ == "__main__":
    unittest.main()
