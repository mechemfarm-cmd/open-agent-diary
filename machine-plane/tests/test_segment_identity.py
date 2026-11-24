"""Segment identity must be globally unique, reserved atomically, and never overwritten.

The failure this prevents: the sequence is a count of sealed files, retention changes
that count, and capture IDs are operator-supplied and reusable. When those collided the
segment path repeated, publication overwrote the artifact, and the store kept every
record imported from the larger original — 118,813 records left pointing at files that
no longer match them.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from machine_plane.events import EventType, MachineEvent
from machine_plane.recorder import SegmentCollision, SegmentWriter
from machine_plane.testing import make_identity

RECORDER = "machine-plane-recorder"


def event(index: int = 0) -> MachineEvent:
    return MachineEvent(
        type=EventType.OPEN,
        producer="test",
        producer_version="1",
        platform="linux",
        monotonic_time=float(index),
        wall_time=1.0 + index,
        result="ok",
        identity=make_identity(),
    )


class SegmentIdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.spool = Path(tempfile.mkdtemp(prefix="mp-identity-"))
        self.addCleanup(lambda: shutil.rmtree(self.spool, ignore_errors=True))

    def writer(self, **overrides) -> SegmentWriter:
        kwargs = dict(
            capture_id="cap-0", boot_id="boot1234", sequence=0, recorder_id=RECORDER
        )
        kwargs.update(overrides)
        return SegmentWriter(self.spool, **kwargs)


class TestUniqueness(SegmentIdentityTestCase):
    def test_a_reused_capture_id_does_not_reuse_a_path(self) -> None:
        """Two runs, same capture id and same sequence, must not collide."""
        first = self.writer()
        first.append(event(0))
        first.seal(event(1))

        second = self.writer()          # identical capture id and sequence
        second.append(event(0))
        sealed = second.seal(event(1))

        self.assertNotEqual(first.sealed, sealed)
        self.assertTrue(first.sealed.exists())
        self.assertTrue(sealed.exists())

    def test_retention_changing_the_sealed_count_does_not_collide(self) -> None:
        """The sequence is a count, so retention can hand the same number out again."""
        first = self.writer(sequence=0)
        first.append(event(0))
        first.seal(event(1))

        # Retention removes the sealed segment; the count goes back to zero and the
        # next run is handed sequence 0 again.
        first.sealed.unlink()

        second = self.writer(sequence=0)
        second.append(event(0))
        sealed = second.seal(event(1))

        self.assertTrue(sealed.exists())

    def test_the_identity_is_recorded_on_the_writer(self) -> None:
        writer = self.writer()
        self.assertTrue(writer.segment_id)
        self.assertIn(writer.segment_id[:8], writer.sealed.name)


class TestReservation(SegmentIdentityTestCase):
    def test_the_partial_is_reserved_before_anything_is_written(self) -> None:
        writer = self.writer()
        self.assertTrue(writer.partial.exists())
        self.assertEqual(writer.partial.stat().st_size, 0)

    def test_an_existing_sealed_destination_is_refused(self) -> None:
        writer = self.writer(segment_token="fixed0001")
        writer.sealed.write_text("previous artifact", encoding="utf-8")

        with self.assertRaises(SegmentCollision):
            self.writer(segment_token="fixed0001")

    def test_an_existing_voided_path_is_refused(self) -> None:
        writer = self.writer(segment_token="fixed0002")
        writer.voided.write_text("voided", encoding="utf-8")

        with self.assertRaises(SegmentCollision):
            self.writer(segment_token="fixed0002")

    def test_an_interrupted_reservation_is_refused_not_adopted(self) -> None:
        """A leftover partial means an unfinished run, not a file to take over."""
        first = self.writer(segment_token="fixed0003")
        first.append(event(0))          # left behind, never sealed

        with self.assertRaises(SegmentCollision):
            self.writer(segment_token="fixed0003")

        # And the abandoned run's data is still there, not overwritten.
        self.assertGreater(first.partial.stat().st_size, 0)


class TestPublicationCannotOverwrite(SegmentIdentityTestCase):
    def test_sealing_over_an_existing_segment_is_refused(self) -> None:
        writer = self.writer(segment_token="fixed0004")
        writer.append(event(0))

        # Something claims the destination between reservation and publication.
        writer.sealed.write_text("someone else's artifact", encoding="utf-8")

        with self.assertRaises(SegmentCollision):
            writer.seal(event(1))

        self.assertEqual(
            writer.sealed.read_text(encoding="utf-8"), "someone else's artifact"
        )
        self.assertTrue(writer.partial.exists(), "the partial must survive the refusal")


class TestRaces(SegmentIdentityTestCase):
    def test_only_one_of_two_racing_runs_claims_the_identity(self) -> None:
        results: list[object] = []
        barrier = threading.Barrier(2)

        def claim() -> None:
            barrier.wait()
            try:
                results.append(self.writer(segment_token="racing001"))
            except SegmentCollision as error:
                results.append(error)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [r for r in results if isinstance(r, SegmentWriter)]
        refusals = [r for r in results if isinstance(r, SegmentCollision)]
        self.assertEqual(len(winners), 1, f"expected exactly one winner: {results}")
        self.assertEqual(len(refusals), 1, f"expected exactly one refusal: {results}")


if __name__ == "__main__":
    unittest.main()
