"""Rebuild transaction, recovery, and byte preservation.

The protocol's value is entirely in what happens when a swap is interrupted. Untested
recovery code is a claim, not a guarantee, so these drive the real functions against a
real filesystem rather than describing intent.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from machine_plane import rebuild


def record(key: str) -> str:
    return json.dumps({"record_key": key, "event": {"kind": "action"}}) + "\n"


class RebuildTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mp-rebuild-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.store = self.root / "store"
        self.store.mkdir()
        self.spool = self.root / "spool"
        self.spool.mkdir()
        self.snapshot = self.root / "snapshot.ndjson"
        self.manifest = self.root / "manifest.json"

    def make_corpus(self, specs: list[tuple[str, str, int]]) -> None:
        """specs: (segment, classification, records)."""
        segments = {}
        lines: list[str] = []
        for segment, classification, count in specs:
            offsets = []
            offset = 0
            for index in range(count):
                key = f"recorder:{segment}:{offset}"
                line = record(key)
                lines.append(line)
                offsets.append(offset)
                offset += len(line)
            artifact = self.spool / segment
            artifact_bytes = b""
            if classification == "supported-by-artifact":
                artifact_bytes = ("".join(lines[-count:])).encode("utf-8")
                artifact.write_bytes(artifact_bytes)
            info = {
                "store_records": count,
                "classification": classification,
                "artifact_present": classification in {"supported-by-artifact"},
                "artifact_sha256": (
                    hashlib.sha256(artifact_bytes).hexdigest()
                    if classification == "supported-by-artifact"
                    else None
                ),
                "offsets_on_line_start": count,
            }
            segments[segment] = info
        self.snapshot.write_text("".join(lines), encoding="utf-8")
        payload = {
            "store": str(self.store),
            "store_records": len(lines),
            "segments": segments,
        }
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

    def plan(self):
        return rebuild.load_plan(
            store=self.store,
            snapshot=self.snapshot,
            manifest_path=self.manifest,
            spool=self.spool,
            verify_artifacts=False,
        )

    def stage(self):
        plan = self.plan()
        staging = self.root / "staging"
        staged, meta = rebuild.stage(self.snapshot, plan, staging)
        return plan, staged, meta, staging


class TestBytePreservation(RebuildTestCase):
    def test_records_move_as_raw_bytes_not_re_encoded(self) -> None:
        """Odd spacing and key order must survive; re-serialising would hide changes."""
        from machine_plane.events import EventType  # noqa: F401  (import cheapness check)

        self.make_corpus([("seg-a", "supported-by-artifact", 2)])
        odd = '{"record_key":  "recorder:seg-b:0",   "event": {"kind":"action"} }\n'
        self.snapshot.write_text(self.snapshot.read_text() + odd, encoding="utf-8")

        segments = json.loads(self.manifest.read_text())["segments"]
        self.spool.joinpath("seg-b").write_text(odd, encoding="utf-8")
        segments["seg-b"] = {
            "store_records": 1,
            "classification": "supported-by-artifact",
            "artifact_present": True,
            "artifact_sha256": hashlib.sha256(odd.encode("utf-8")).hexdigest(),
            "offsets_on_line_start": 1,
        }
        payload = json.loads(self.manifest.read_text())
        payload["segments"] = segments
        payload["store_records"] = 3
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

        plan, staged, meta, _ = self.stage()
        evidence = next(s for s in staged if s.destination == rebuild.EVIDENCE)
        self.assertIn(odd.strip(), evidence.path.read_text(encoding="utf-8"))

        problems = rebuild.verify(
            self.snapshot, staged, plan, meta, self.store, self.spool
        )
        self.assertEqual(problems, [])


class TestSwapNoReplace(RebuildTestCase):
    def test_an_existing_destination_is_never_overwritten(self) -> None:
        self.make_corpus([("seg-a", "supported-by-artifact", 1)])
        plan, staged, meta, _ = self.stage()

        txn = rebuild.write_transaction(
            self.store,
            plan,
            staged,
            rebuild.Quiescence(lock_path=self.root / "lock", evidence={}),
            {"required_bytes": 0, "free_bytes": 1},
        )
        # A previous generation already occupies the superseded slot the swap must move
        # the live store into. Moving into it would destroy a generation.
        blocked = self.store / f"superseded-{txn.name}" / rebuild.EVIDENCE
        blocked.parent.mkdir(parents=True)
        blocked.write_text("someone else's generation", encoding="utf-8")
        self.store.joinpath(rebuild.EVIDENCE, "events.ndjson").parent.mkdir(
            parents=True, exist_ok=True
        )
        self.store.joinpath(rebuild.EVIDENCE, "events.ndjson").write_text(
            "current generation", encoding="utf-8"
        )

        with self.assertRaises(rebuild.RebuildRefused):
            rebuild.swap(self.store, txn, staged, txn.name)

        self.assertEqual(
            blocked.read_text(encoding="utf-8"), "someone else's generation"
        )


class TestRecoveryIsProgressAware(RebuildTestCase):
    """Each interrupted state the log can explain must be undone, not refused."""

    def setup_txn(self, progress_lines: list[str]) -> tuple[Path, Path]:
        self.store.joinpath(rebuild.EVIDENCE).mkdir(parents=True)
        self.store.joinpath(rebuild.CONTROL).mkdir(parents=True)
        self.store.joinpath(rebuild.LEGACY).mkdir(parents=True)
        txn = self.store / ".rebuild-txn" / "t1"
        txn.mkdir(parents=True)
        (txn / "txn.json").write_text("{}", encoding="utf-8")
        (txn / "progress.log").write_text(
            "".join(line + "\n" for line in progress_lines), encoding="utf-8"
        )
        superseded = self.store / "superseded-t1"
        superseded.mkdir(parents=True)
        return txn, superseded

    def test_moved_aside_only_is_restored(self) -> None:
        txn, superseded = self.setup_txn(["moved-aside   evidence"])
        superseded.joinpath(rebuild.EVIDENCE).write_text("old", encoding="utf-8")

        result = rebuild.recover(self.store)

        self.assertIn("restored", result)
        self.assertEqual(
            (self.store / rebuild.EVIDENCE / "events.ndjson").read_text(
                encoding="utf-8"
            ),
            "old",
        )

    def test_an_explained_install_is_rolled_back(self) -> None:
        """move-aside then install: the new generation must be removed, the old restored."""
        txn, superseded = self.setup_txn(
            ["moved-aside   evidence", "installed     evidence"]
        )
        superseded.joinpath(rebuild.EVIDENCE).write_text("old", encoding="utf-8")
        self.store.joinpath(rebuild.EVIDENCE, "events.ndjson").write_text(
            "new", encoding="utf-8"
        )

        result = rebuild.recover(self.store)

        self.assertIn(rebuild.EVIDENCE, result)
        self.assertEqual(
            (self.store / rebuild.EVIDENCE / "events.ndjson").read_text(
                encoding="utf-8"
            ),
            "old",
        )

    def test_state_the_log_does_not_explain_is_refused(self) -> None:
        txn, superseded = self.setup_txn([])
        superseded.joinpath(rebuild.EVIDENCE).write_text("old", encoding="utf-8")

        with self.assertRaises(rebuild.RebuildRefused):
            rebuild.recover(self.store)

    def test_out_of_order_progress_is_refused(self) -> None:
        txn, superseded = self.setup_txn(
            ["installed     evidence", "moved-aside   evidence"]
        )
        with self.assertRaises(rebuild.RebuildRefused):
            rebuild.recover(self.store)

    def test_a_completed_transaction_is_left_alone(self) -> None:
        txn, _ = self.setup_txn([])
        (txn / "COMPLETE").write_text("t1", encoding="utf-8")

        self.assertEqual(rebuild.recover(self.store), "nothing to recover")


if __name__ == "__main__":
    unittest.main()

class TestRecoveryValidatesBeforeMutating(RebuildTestCase):
    """A refusal must change nothing. Recovery deletes files, so order matters."""

    def setup_txn(self, progress_lines: list[str]) -> tuple[Path, Path]:
        for destination in rebuild.DESTINATIONS:
            self.store.joinpath(destination).mkdir(parents=True, exist_ok=True)
        txn = self.store / ".rebuild-txn" / "t1"
        txn.mkdir(parents=True)
        (txn / "txn.json").write_text("{}", encoding="utf-8")
        (txn / "progress.log").write_text(
            "".join(line + "\n" for line in progress_lines), encoding="utf-8"
        )
        superseded = self.store / "superseded-t1"
        superseded.mkdir(parents=True)
        return txn, superseded

    def test_missing_previous_generation_does_not_delete_the_live_one(self) -> None:
        """moved-aside + installed, but the moved copy is gone: the live one is all we have."""
        txn, _superseded = self.setup_txn(
            ["moved-aside   evidence", "installed     evidence"]
        )
        live = self.store / rebuild.EVIDENCE / "events.ndjson"
        live.write_text("the only remaining generation", encoding="utf-8")

        with self.assertRaises(rebuild.RebuildRefused):
            rebuild.recover(self.store)

        self.assertTrue(live.exists(), "recovery deleted the only surviving generation")
        self.assertEqual(
            live.read_text(encoding="utf-8"), "the only remaining generation"
        )
        self.assertFalse((txn / "ROLLED_BACK").exists())

    def test_installed_with_a_missing_live_store_is_refused(self) -> None:
        """The log claims an install that is not on disk; that contradicts, not succeeds."""
        txn, _superseded = self.setup_txn(["installed     evidence"])

        with self.assertRaises(rebuild.RebuildRefused):
            rebuild.recover(self.store)

        self.assertFalse(
            (txn / "ROLLED_BACK").exists(),
            "a refused recovery must not be marked rolled back",
        )

    def test_an_explained_rollback_still_works(self) -> None:
        txn, superseded = self.setup_txn(
            ["moved-aside   evidence", "installed     evidence"]
        )
        superseded.joinpath(rebuild.EVIDENCE).write_text("old", encoding="utf-8")
        self.store.joinpath(rebuild.EVIDENCE, "events.ndjson").write_text(
            "new", encoding="utf-8"
        )

        rebuild.recover(self.store)

        self.assertEqual(
            (self.store / rebuild.EVIDENCE / "events.ndjson").read_text(
                encoding="utf-8"
            ),
            "old",
        )
        self.assertTrue((txn / "ROLLED_BACK").exists())

    def test_a_failed_precondition_mutates_nothing_anywhere(self) -> None:
        """One bad destination must stop the whole recovery before any file moves."""
        txn, superseded = self.setup_txn(
            [
                "moved-aside   evidence",
                "moved-aside   legacy",
                "installed     legacy",
            ]
        )
        superseded.joinpath(rebuild.EVIDENCE).write_text("old evidence", encoding="utf-8")
        # legacy says installed but its previous generation is missing.
        self.store.joinpath(rebuild.LEGACY, "events.ndjson").write_text(
            "new legacy", encoding="utf-8"
        )

        with self.assertRaises(rebuild.RebuildRefused):
            rebuild.recover(self.store)

        # evidence was a valid restore candidate, but must not have been touched.
        self.assertTrue(superseded.joinpath(rebuild.EVIDENCE).exists())
        self.assertFalse((self.store / rebuild.EVIDENCE / "events.ndjson").exists())
        self.assertEqual(
            (self.store / rebuild.LEGACY / "events.ndjson").read_text(encoding="utf-8"),
            "new legacy",
        )
        self.assertFalse((txn / "ROLLED_BACK").exists())
