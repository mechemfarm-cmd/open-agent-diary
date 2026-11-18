"""Retention: the cap, the accounting, and the cases that broke earlier revisions.

The property under test is not "rotation deletes a file". It is that **the record of
what was dropped outlives the thing that was dropped**, because a marker stored in a
segment is destroyed by the mechanism it describes.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from machine_plane.retention import (
    RetentionError,
    RetentionJournal,
    Segment,
    choose_victim,
    rotate,
    sealed_segments,
)


class RetentionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-retention-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.spool = self.directory / "spool"
        self.spool.mkdir()
        self.journal_path = self.spool / "retention.journal"

    def make_segment(self, name: str, size: int) -> Path:
        path = self.spool / name
        path.write_bytes(b"x" * size)
        return path

    def journal(self, max_bytes: int = 1024 * 1024) -> RetentionJournal:
        return RetentionJournal(self.journal_path, max_bytes)


class TestOneSealedSegment(RetentionTestCase):
    """The case that made revision 2 wrong: victim and newest are the same file."""

    def test_dropping_the_only_segment_keeps_its_accounting(self) -> None:
        self.make_segment("segment-boot-000000-a.ndjson", 200)
        journal = self.journal()

        dropped = rotate(
            segments=sealed_segments(sorted(self.spool.glob("*.ndjson"))),
            journal=journal,
            spool_dir=self.spool,
            now=100.0,
            total_bytes=200,
            max_bytes=50,
        )

        self.assertEqual(dropped, ["segment-boot-000000-a.ndjson"])
        self.assertFalse((self.spool / "segment-boot-000000-a.ndjson").exists())
        totals = journal.totals()
        self.assertEqual(totals.events, 1, "the drop must still be counted")
        self.assertEqual(totals.bytes_freed, 200)

    def test_the_record_survives_recovery(self) -> None:
        self.make_segment("segment-boot-000000-a.ndjson", 200)
        rotate(
            segments=sealed_segments(sorted(self.spool.glob("*.ndjson"))),
            journal=self.journal(),
            spool_dir=self.spool,
            now=100.0,
            total_bytes=200,
            max_bytes=50,
        )
        reopened = self.journal()
        self.assertEqual(reopened.totals().events, 1)


class TestHistoryOutlivesSegments(RetentionTestCase):
    """Revision 2 could not have passed this: markers died with their segment."""

    def test_repeated_rotation_keeps_every_drop(self) -> None:
        journal = self.journal()
        created = 0
        for index in range(6):
            name = f"segment-boot-{index:06d}-cap.ndjson"
            self.make_segment(name, 100)
            created += 1
            segments = sealed_segments(sorted(self.spool.glob("*.ndjson")))
            total = sum(segment.size for segment in segments)
            rotate(
                segments=segments,
                journal=journal,
                spool_dir=self.spool,
                now=100.0 + index,
                total_bytes=total,
                max_bytes=150,
            )

        remaining = len(list(self.spool.glob("*.ndjson")))
        self.assertLess(remaining, created, "rotation must actually have happened")
        totals = self.journal().totals()
        self.assertEqual(
            totals.events,
            created - remaining,
            "every dropped segment is still accounted for after the file is gone",
        )


class TestCompaction(RetentionTestCase):
    def test_journal_is_bounded_and_says_so(self) -> None:
        journal = self.journal(max_bytes=400)
        for index in range(40):
            journal.record_drop(
                victim=f"segment-boot-{index:06d}-cap.ndjson",
                victim_oldest=float(index),
                size=1000,
                at=100.0 + index,
            )

        self.assertLessEqual(journal.size(), 400 + 200)
        totals = journal.totals()
        self.assertEqual(totals.events, 40, "compaction must not lose the count")
        self.assertEqual(totals.bytes_freed, 40_000)
        self.assertIsNotNone(totals.detail_from, "the horizon must be stated")
        self.assertGreaterEqual(totals.compactions, 1)

    def test_interrupted_compaction_is_discarded(self) -> None:
        journal = self.journal(max_bytes=400)
        journal.record_drop(victim="a.ndjson", victim_oldest=1.0, size=10, at=1.0)
        before = journal.totals()

        temporary = self.journal_path.with_suffix(".journal.new")
        temporary.write_text("{\"kind\": \"summary\", \"events\": 999}\n")

        self.assertTrue(journal.recover_interrupted_compaction())
        self.assertFalse(temporary.exists())
        self.assertEqual(journal.totals().events, before.events)


class TestUnwritableJournal(RetentionTestCase):
    def test_a_journal_that_cannot_be_written_stops_before_deleting(self) -> None:
        victim = self.make_segment("segment-boot-000000-a.ndjson", 200)
        # A directory where the journal should be: every write to it fails.
        self.journal_path.mkdir()

        with self.assertRaises(RetentionError):
            rotate(
                segments=sealed_segments([victim]),
                journal=self.journal(),
                spool_dir=self.spool,
                now=1.0,
                total_bytes=200,
                max_bytes=50,
            )
        self.assertTrue(victim.exists(), "nothing may be deleted before it is recorded")


class TestVictimChoice(RetentionTestCase):
    def test_partials_are_never_candidates(self) -> None:
        self.make_segment("segment-boot-000000-a.ndjson.partial", 10)
        self.assertEqual(sealed_segments(sorted(self.spool.glob("*.ndjson*"))), [])

    def test_oldest_record_time_wins_over_name(self) -> None:
        chosen = choose_victim(
            [
                Segment("segment-zzz.ndjson", 10, oldest_record_at=5.0),
                Segment("segment-aaa.ndjson", 10, oldest_record_at=1.0),
            ]
        )
        assert chosen is not None
        self.assertEqual(chosen.name, "segment-aaa.ndjson")


if __name__ == "__main__":
    unittest.main()
