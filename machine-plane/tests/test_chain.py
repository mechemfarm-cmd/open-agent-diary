"""Tamper evidence, and the limit of it.

The tests that matter here are the ones distinguishing two different claims:
"the surviving links are unaltered" and "nothing was removed". A local chain
gives you the first and cannot give you the second. Getting those confused is
how a design ends up selling an audit product while shipping an observability
pipeline.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from machine_plane.chain import (
    GENESIS,
    ChainLink,
    Checkpoint,
    HashChain,
    IntegrityStatus,
    build,
    compute_hash,
    verify,
)
from machine_plane.events import EventType
from machine_plane.testing import MockProducer, action_event


def _events(count: int = 3):
    raws = [
        action_event(
            EventType.WRITE_ATTEMPT,
            target=f"/home/x/work/file{index}.txt",
            wall_time=1_700_000_000.0 + index,
        )
        for index in range(count)
    ]
    return list(MockProducer(raws).events())


class ChainBuildTest(unittest.TestCase):
    def test_the_first_link_follows_genesis(self) -> None:
        chain = build(_events(1))
        self.assertEqual(chain.links[0].sequence, 0)
        self.assertEqual(chain.links[0].prev_hash, GENESIS)

    def test_each_link_points_at_the_one_before_it(self) -> None:
        chain = build(_events(3))
        for index, link in enumerate(chain.links):
            self.assertEqual(link.sequence, index)
            if index:
                self.assertEqual(link.prev_hash, chain.links[index - 1].hash)

    def test_a_fresh_chain_verifies(self) -> None:
        self.assertTrue(verify(build(_events(3))).ok)

    def test_an_empty_chain_verifies(self) -> None:
        self.assertTrue(verify(HashChain()).ok)


class ModificationTest(unittest.TestCase):
    def test_altering_a_link_is_detected(self) -> None:
        chain = build(_events(3))
        links = list(chain.links)
        broken = replace(links[1], recorded_at=links[1].recorded_at + 1)
        links[1] = broken

        result = verify(links)

        self.assertEqual(result.status, IntegrityStatus.MODIFIED)
        self.assertEqual(result.index, 1)

    def test_reordering_links_is_detected(self) -> None:
        chain = build(_events(3))
        links = list(chain.links)
        links[1], links[2] = links[2], links[1]

        result = verify(links)

        self.assertEqual(result.status, IntegrityStatus.BROKEN_LINK)

    def test_forging_a_link_consistently_still_moves_every_later_hash(self) -> None:
        """A rewrite that recomputes its own hash breaks the *next* link."""
        chain = build(_events(3))
        links = list(chain.links)
        original = links[1]
        forged = replace(
            original,
            event_id="a" * 64,
            hash=compute_hash(
                original.sequence, original.prev_hash, original.recorded_at, "a" * 64
            ),
        )
        links[1] = forged

        result = verify(links)

        self.assertEqual(result.status, IntegrityStatus.BROKEN_LINK)
        self.assertEqual(result.index, 2)

    def test_altering_an_event_body_changes_its_chained_id(self) -> None:
        """Event ids are content-derived, so the chain sees a changed record."""
        events = _events(1)
        chain = build(events)
        altered = replace(events[0], result="denied")
        self.assertNotEqual(chain.links[0].event_id, altered.event_id)


class TruncationTest(unittest.TestCase):
    """A valid prefix of a valid chain is a valid chain.

    That is the whole reason off-box checkpoints are required, and it is pinned
    here so no one mistakes the local check for a deletion check.
    """

    def test_a_truncated_chain_still_verifies_without_a_checkpoint(self) -> None:
        chain = build(_events(4))
        prefix = chain.links[:2]

        self.assertTrue(
            verify(prefix).ok,
            "a prefix of an intact chain is itself intact",
        )

    def test_a_truncated_chain_fails_against_a_checkpoint(self) -> None:
        chain = build(_events(4))
        checkpoint = chain.checkpoint(taken_at=1.0)

        result = verify(chain.links[:2], checkpoint=checkpoint)

        self.assertEqual(result.status, IntegrityStatus.TRUNCATED)
        self.assertIn("2 links", result.detail)

    def test_replacing_the_chain_with_a_different_one_is_rolled_back(self) -> None:
        chain = build(_events(4))
        checkpoint = chain.checkpoint(taken_at=1.0)

        other = build(_events(4))
        # Same length, different content, so the head hash at that index differs.
        tampered = list(other.links)
        original = tampered[3]
        tampered[3] = replace(
            original,
            recorded_at=original.recorded_at + 5,
            hash=compute_hash(
                original.sequence,
                original.prev_hash,
                original.recorded_at + 5,
                original.event_id,
            ),
        )

        result = verify(tampered, checkpoint=checkpoint)

        self.assertIn(
            result.status,
            {IntegrityStatus.ROLLED_BACK, IntegrityStatus.BROKEN_LINK},
        )

    def test_an_intact_chain_passes_the_checkpoint_check(self) -> None:
        chain = build(_events(4))
        checkpoint = chain.checkpoint(taken_at=1.0)
        self.assertTrue(verify(chain, checkpoint=checkpoint).ok)

    def test_growing_the_chain_past_the_checkpoint_is_fine(self) -> None:
        """A checkpoint is a floor, not an exact length."""
        chain = build(_events(2))
        checkpoint = chain.checkpoint(taken_at=1.0)
        for event in _events(3):
            chain.append(event)
        self.assertTrue(verify(chain, checkpoint=checkpoint).ok)


class SealTest(unittest.TestCase):
    def test_sealing_marks_the_end_of_the_chain(self) -> None:
        chain = build(_events(2), seal_at=1_700_000_500.0)
        head = chain.head()
        assert head is not None
        self.assertEqual(head.kind, "seal")
        self.assertTrue(verify(chain).ok)

    def test_a_seal_has_no_event(self) -> None:
        chain = build(_events(1), seal_at=1.0)
        head = chain.head()
        assert head is not None
        self.assertEqual(head.event_id, "")

    def test_an_interrupted_chain_is_distinguishable_from_a_sealed_one(self) -> None:
        sealed = build(_events(2), seal_at=1.0)
        interrupted = build(_events(2))
        sealed_head = sealed.head()
        interrupted_head = interrupted.head()
        assert sealed_head is not None and interrupted_head is not None
        self.assertNotEqual(sealed_head.kind, interrupted_head.kind)
        self.assertEqual(interrupted_head.kind, "event")


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "chain.jsonl"

    def test_saving_and_loading_preserves_the_chain(self) -> None:
        chain = build(_events(3))
        chain.save(self.path)

        restored = HashChain.load(self.path)

        self.assertEqual(len(restored), 3)
        self.assertEqual(
            [link.hash for link in restored.links],
            [link.hash for link in chain.links],
        )
        self.assertTrue(verify(restored).ok)

    def test_loading_a_missing_file_gives_an_empty_chain(self) -> None:
        self.assertEqual(len(HashChain.load(self.path)), 0)

    def test_a_link_from_an_unknown_version_is_rejected(self) -> None:
        payload = build(_events(1)).links[0].to_dict()
        payload["chain_version"] = 99
        with self.assertRaises(ValueError):
            ChainLink.from_dict(payload)


class CheckpointTest(unittest.TestCase):
    def test_a_checkpoint_records_length_and_head(self) -> None:
        chain = build(_events(3))
        checkpoint = chain.checkpoint(taken_at=42.0)
        head = chain.head()
        assert head is not None
        self.assertEqual(checkpoint.length, 3)
        self.assertEqual(checkpoint.head_hash, head.hash)
        self.assertEqual(checkpoint.taken_at, 42.0)

    def test_a_checkpoint_round_trips(self) -> None:
        checkpoint = Checkpoint(length=3, head_hash="a" * 64, taken_at=1.0)
        self.assertEqual(
            Checkpoint.from_dict(checkpoint.to_dict()).to_dict(),
            checkpoint.to_dict(),
        )

    def test_an_empty_chain_checkpoints_at_genesis(self) -> None:
        checkpoint = HashChain().checkpoint(taken_at=1.0)
        self.assertEqual(checkpoint.length, 0)
        self.assertEqual(checkpoint.head_hash, GENESIS)


if __name__ == "__main__":
    unittest.main()
