"""The control path, end to end, against a real sealed segment.

Unit tests proved the pieces: the mode stamp is written, the guard rejects a
mismatch, Gate 1 knows two modes. None of that proves the importer wires them
together — and it did not: the importer called Gate 1 in its default filtered mode,
so ``--control`` with the control probe set was refused as ``missing-scope-predicate``
before mode validation ever ran. The control path was unreachable.

These tests start where that bug lived: a sealed segment on disk and the real
command-line entry point.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from machine_plane import importer_cli
from machine_plane.events import EventType, MachineEvent
from machine_plane.spool import Spool
from machine_plane.testing import make_identity

PROBES = Path(__file__).resolve().parents[1] / "probes"
CONTROL_PROBES = PROBES / "control"

INODE = "385715"


def _event(event_type: EventType, index: int, details: dict | None = None) -> MachineEvent:
    return MachineEvent(
        type=event_type,
        producer="test",
        producer_version="1",
        platform="linux",
        monotonic_time=float(index),
        wall_time=1000.0 + index,
        result="ok",
        identity=None if details is not None else make_identity(),
        platform_details=dict(details or {}),
    )


def seal(path: Path, mode: str) -> Path:
    """A sealed segment that looks like the recorder wrote it."""
    spool = Spool(path, "machine-plane-recorder")
    spool.append(
        _event(
            EventType.RECORDER_STARTED,
            0,
            {
                "capture_id": path.stem,
                "cgroup_inode": INODE,
                "scope": "mp-test.scope",
                "protection_level": "advisory",
                "supervisor_mode": "supervisor",
                "capture_mode": mode,
            },
        )
    )
    for index in range(1, 4):
        spool.append(_event(EventType.OPEN, index))
    spool.append(
        _event(
            EventType.RECORDER_STOPPED,
            9,
            {"records": 4, "protection_level": "advisory", "capture_mode": mode},
        )
    )
    return path


class TestControlImportPath(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mp-ctrl-import-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.spool = self.root / "spool"
        self.spool.mkdir()
        self.store = self.root / "control" / "events.ndjson"

    def run_import(self, *extra: str) -> tuple[int, str, str]:
        argv = [
            "--spool", str(self.spool),
            "--store", str(self.store),
            "--checkpoint-dir", str(self.root / "cp"),
            "--refused-dir", str(self.root / "refused"),
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = importer_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_control_segment_imports_with_control_mode(self) -> None:
        """Gate 1 must pass the control probes, and nothing may reach the store
        through the workload path."""
        seal(self.spool / "segment-b-000001-ctrl.ndjson", "control")

        code, out, err = self.run_import("--control", "--probes", str(CONTROL_PROBES))

        self.assertEqual(code, 0, f"control import failed:\n{out}\n{err}")
        self.assertIn("PASSED", out)
        self.assertNotIn("missing-scope-predicate", out + err)
        self.assertTrue(self.store.exists(), "control store was not written")

    def test_a_control_segment_is_refused_by_the_normal_path(self) -> None:
        seal(self.spool / "segment-b-000001-ctrl.ndjson", "control")

        code, out, err = self.run_import("--probes", str(PROBES))

        self.assertNotEqual(code, 0, "a control segment was accepted as evidence")
        self.assertIn("control artifact", err)
        # The refusal must be mode validation, not Gate 1 happening to reject the
        # probes: a Gate 1 failure here would mean the guard was never reached.
        self.assertNotIn("Gate 1 failed", err)
        self.assertFalse(self.store.exists(), "a refused segment still wrote a store")

    def test_a_filtered_segment_is_refused_by_the_control_path(self) -> None:
        seal(self.spool / "segment-b-000002-filt.ndjson", "filtered")

        code, out, err = self.run_import("--control", "--probes", str(CONTROL_PROBES))

        self.assertNotEqual(code, 0, "the control store accepted a filtered segment")
        self.assertIn("not a control run", err)
        self.assertNotIn("Gate 1 failed", err)
        self.assertFalse(self.store.exists())

    def test_the_control_probe_set_actually_reaches_mode_validation(self) -> None:
        """The original bug: Gate 1 refused these probes before the mode check."""
        seal(self.spool / "segment-b-000003-filt.ndjson", "filtered")

        code, out, err = self.run_import("--control", "--probes", str(CONTROL_PROBES))

        self.assertNotEqual(code, 0)
        self.assertNotIn("missing-scope-predicate", out + err)
        self.assertIn("Gate 1: PASSED", out)


if __name__ == "__main__":
    unittest.main()


class TestOneBadSegmentDoesNotStarveTheRest(unittest.TestCase):
    """A skipped segment must not abort the import.

    Found in the live validation window: a control segment sitting in the spool made
    the normal import return before it reached the filtered capture that sorted after
    it, so the capture was never imported and the run looked like it had failed.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="mp-skip-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.spool = self.root / "spool"
        self.spool.mkdir()
        self.store = self.root / "events.ndjson"

    def run_import(self, *extra: str) -> tuple[int, str, str]:
        argv = [
            "--spool", str(self.spool),
            "--store", str(self.store),
            "--checkpoint-dir", str(self.root / "cp"),
            "--refused-dir", str(self.root / "refused"),
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = importer_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_control_segment_does_not_block_a_later_filtered_one(self) -> None:
        # Sorted order matters: 000001 sorts before 000002.
        seal(self.spool / "segment-b-000001-ctrl.ndjson", "control")
        seal(self.spool / "segment-b-000002-filt.ndjson", "filtered")

        code, out, err = self.run_import("--probes", str(PROBES))

        self.assertIn("control artifact", err)
        self.assertIn("skipped for mode mismatch", err)
        # The filtered segment after it must still have reached the store.
        self.assertTrue(self.store.exists(), "the later segment was starved")
        stored = [
            json.loads(line)
            for line in self.store.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(stored), 5, f"expected the filtered segment only: {out}")
        # Non-zero because a segment was skipped, but the work still happened.
        self.assertNotEqual(code, 0)
