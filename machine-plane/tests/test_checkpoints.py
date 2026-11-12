"""Checkpoints, and the trust domain that decides what they are worth."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from machine_plane.chain import HashChain, IntegrityStatus, build
from machine_plane.checkpoints import (
    JsonlCheckpointSink,
    MemoryCheckpointSink,
    TrustDomain,
    assess,
    publish,
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


class PublishTest(unittest.TestCase):
    def test_publishing_records_the_chain_head(self) -> None:
        chain = build(_events(3))
        sink = MemoryCheckpointSink()
        published = publish(chain, sink, taken_at=1.0)

        self.assertEqual(published.checkpoint.length, 3)
        self.assertEqual(published.checkpoint, sink.latest())

    def test_publishing_appends_rather_than_overwrites(self) -> None:
        """Keeping only the newest checkpoint loses the older anchor."""
        chain = build(_events(1))
        sink = MemoryCheckpointSink()
        publish(chain, sink, taken_at=1.0)
        chain.append(_events(1)[0])
        publish(chain, sink, taken_at=2.0)

        self.assertEqual(len(sink.all()), 2)
        self.assertEqual([c.length for c in sink.all()], [1, 2])


class TrustDomainTest(unittest.TestCase):
    def test_a_same_host_checkpoint_is_not_evidence(self) -> None:
        chain = build(_events(3))
        sink = MemoryCheckpointSink(
            name="local-disk", trust_domain=TrustDomain.SAME_HOST
        )
        publish(chain, sink, taken_at=1.0)

        result = assess(chain, sink)

        self.assertEqual(result.status, IntegrityStatus.OK)
        self.assertFalse(result.off_box)
        self.assertFalse(result.evidence)
        self.assertIn("does not rule out", result.detail)

    def test_a_separate_host_checkpoint_is_evidence(self) -> None:
        chain = build(_events(3))
        sink = MemoryCheckpointSink(
            name="off-box", trust_domain=TrustDomain.SEPARATE_HOST
        )
        publish(chain, sink, taken_at=1.0)

        result = assess(chain, sink)

        self.assertTrue(result.evidence)

    def test_an_unknown_domain_does_not_claim_the_good_case(self) -> None:
        chain = build(_events(3))
        sink = MemoryCheckpointSink(
            name="somewhere", trust_domain=TrustDomain.UNKNOWN
        )
        publish(chain, sink, taken_at=1.0)

        result = assess(chain, sink)

        self.assertFalse(result.evidence)
        self.assertIn("unknown", result.detail)

    def test_a_file_sink_defaults_to_unknown_rather_than_off_box(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlCheckpointSink(Path(directory) / "checkpoints.jsonl")
        self.assertEqual(sink.trust_domain, TrustDomain.UNKNOWN)

    def test_no_checkpoint_at_all_cannot_detect_deletion(self) -> None:
        chain = build(_events(3))
        sink = MemoryCheckpointSink()
        result = assess(chain, sink)

        self.assertFalse(result.evidence)
        self.assertIn("no checkpoint has been published", result.detail)


class DeletionTest(unittest.TestCase):
    def test_a_truncated_chain_is_caught_by_an_off_box_checkpoint(self) -> None:
        chain = build(_events(4))
        sink = MemoryCheckpointSink(trust_domain=TrustDomain.SEPARATE_HOST)
        publish(chain, sink, taken_at=1.0)

        truncated = HashChain(chain.links[:2])
        result = assess(truncated, sink)

        self.assertEqual(result.status, IntegrityStatus.TRUNCATED)
        self.assertFalse(result.evidence)

    def test_the_same_truncated_chain_passes_without_a_checkpoint(self) -> None:
        """The distinction the whole module exists to keep.

        Without a checkpoint the prefix verifies, which is exactly why a local
        chain cannot be described as evidence of completeness.
        """
        chain = build(_events(4))
        sink = MemoryCheckpointSink()  # nothing published

        truncated = HashChain(chain.links[:2])
        result = assess(truncated, sink)

        self.assertEqual(result.status, IntegrityStatus.OK)
        self.assertFalse(result.evidence, "intact, but not evidence of completeness")


class JsonlSinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "checkpoints.jsonl"

    def test_checkpoints_survive_a_reopen(self) -> None:
        chain = build(_events(2))
        sink = JsonlCheckpointSink(self.path)
        publish(chain, sink, taken_at=1.0)

        reopened = JsonlCheckpointSink(self.path)
        latest = reopened.latest()
        assert latest is not None
        self.assertEqual(latest.length, 2)

    def test_an_absent_file_reads_as_no_checkpoint(self) -> None:
        self.assertIsNone(JsonlCheckpointSink(self.path).latest())

    def test_an_appended_checkpoint_does_not_erase_an_earlier_one(self) -> None:
        chain = build(_events(1))
        sink = JsonlCheckpointSink(self.path)
        publish(chain, sink, taken_at=1.0)
        chain.append(_events(1)[0])
        publish(chain, sink, taken_at=2.0)

        self.assertEqual([c.length for c in sink.all()], [1, 2])


if __name__ == "__main__":
    unittest.main()
