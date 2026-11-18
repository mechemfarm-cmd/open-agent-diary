"""Gate 2 must sit on the import path, not beside it.

A detector an operator can run after the fact is not a boundary. These tests are
the enforcement: a segment that fails the scan is refused, recorded as refused, and
never reaches the store.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from machine_plane import importer_cli
from machine_plane.events import EventType, MachineEvent, TargetState
from machine_plane.privacy_scan import account_segment, attribution
from machine_plane.spool import Spool
from machine_plane.testing import make_identity

SHIPPED = Path("/usr/lib/machine-plane/probes")


def open_event(target: str | None, label: str = "cat") -> MachineEvent:
    return MachineEvent(
        type=EventType.OPEN,
        producer="test",
        producer_version="1",
        platform="linux",
        monotonic_time=1.0,
        wall_time=2.0,
        result="ok",
        identity=make_identity(),
        target=target,
        target_state=TargetState.PRESENT if target else TargetState.UNKNOWN,
        platform_details={"label": label},
    )


class TestImportRefusesUnsafeSegments(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-import-gate-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )
        self.spool = self.directory / "spool"
        self.spool.mkdir()
        self.store = self.directory / "store" / "events.ndjson"

    def segment(self, name: str, events) -> Path:
        path = self.spool / name
        spool = Spool(path, "machine-plane-recorder", mode=0o600)
        # Every real segment opens with a header, and since mode provenance landed
        # a segment without one is legacy and refused. These fixtures model ordinary
        # filtered captures, so they carry the header a recorder would write.
        spool.append(
            MachineEvent(
                type=EventType.RECORDER_STARTED,
                producer="machine-plane-recorder",
                producer_version="1",
                platform="linux",
                monotonic_time=0.0,
                wall_time=1.0,
                result="enforced",
                platform_details={"capture_mode": "filtered"},
            )
        )
        for event in events:
            spool.append(event)
        return path

    def run_import(self) -> int:
        return importer_cli.main(
            [
                "--spool", str(self.spool),
                "--store", str(self.store),
                "--checkpoint-dir", str(self.directory / "store" / "checkpoints"),
                "--refused-dir", str(self.directory / "store" / "refused"),
            ]
        )

    @unittest.skipUnless(SHIPPED.is_dir(), "shipped probes are not installed here")
    def test_an_unredacted_segment_is_refused_and_not_imported(self) -> None:
        self.segment("segment-bad.ndjson", [open_event("/etc/passwd")])

        code = self.run_import()

        self.assertEqual(code, 1)
        self.assertFalse(
            self.store.exists() and self.store.stat().st_size > 0,
            "nothing from a refused segment may reach the store",
        )
        refused = list((self.directory / "store" / "refused").glob("*.refused.json"))
        self.assertEqual(len(refused), 1)
        payload = json.loads(refused[0].read_text())
        self.assertEqual(payload["segment"], "segment-bad.ndjson")
        self.assertTrue(any("unredacted-target" in f for f in payload["findings"]))

    @unittest.skipUnless(SHIPPED.is_dir(), "shipped probes are not installed here")
    def test_a_clean_segment_is_imported(self) -> None:
        self.segment("segment-good.ndjson", [open_event("passwd"), open_event("cat")])

        code = self.run_import()

        self.assertEqual(code, 0)
        # the two open events plus the RECORDER_STARTED header
        self.assertEqual(len(self.store.read_text().splitlines()), 3)


class TestAccounting(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-accounting-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def test_the_equation_balances_with_lifecycle_records(self) -> None:
        path = self.directory / "segment-a.ndjson"
        spool = Spool(path, "machine-plane-recorder", mode=0o600)
        spool.append(
            MachineEvent(
                type=EventType.RECORDER_STARTED,
                producer="machine-plane-recorder",
                producer_version="1",
                platform="linux",
                monotonic_time=0.0,
                wall_time=1.0,
                result="enforced",
                # Explicit: a header with no mode is a legacy capture, and the
                # importer now refuses those as evidence. This fixture is meant to
                # be an ordinary filtered segment.
                platform_details={"capture_mode": "filtered"},
            )
        )
        spool.append(open_event("passwd"))
        spool.append(open_event("cat"))
        spool.append(
            MachineEvent(
                type=EventType.RECORDER_STOPPED,
                producer="machine-plane-recorder",
                producer_version="1",
                platform="linux",
                monotonic_time=2.0,
                wall_time=3.0,
                result="workload_exit",
            )
        )

        counts, events, _already = account_segment(
            path, recorder_id="machine-plane-recorder"
        )

        self.assertEqual(counts.records, 4)
        self.assertEqual(counts.lifecycle, 2)
        self.assertEqual(counts.action, 2)
        self.assertEqual(counts.gaps, 0)
        self.assertTrue(counts.totals_balance)
        self.assertEqual(len(events), 4)


class TestAttribution(unittest.TestCase):
    def test_the_join_key_is_the_inode_not_the_scope_name(self) -> None:
        from machine_plane.accounting import is_in_scope
        from machine_plane.identity import DomainType, IsolationDomain, ProcessIdentity

        def identity(domain_id: str, scope: str | None) -> ProcessIdentity:
            return ProcessIdentity(
                host_id="h",
                boot_id="b",
                process_instance_id="p",
                isolation_domain=IsolationDomain(
                    type=DomainType.CGROUP, id=domain_id, scope=scope
                ),
            )

        self.assertTrue(is_in_scope(identity("385715", "mp-x.scope"), "385715"))
        self.assertFalse(is_in_scope(identity("330814", "mp-x.scope"), "385715"))


if __name__ == "__main__":
    unittest.main()
