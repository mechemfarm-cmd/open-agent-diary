"""Accounting: disjoint classes, exactly-once classification, and the scope rule.

The defects these guard against were both seam errors, not bugs in a component:
a gap event counted in two populations inflates the total, and a scope test that
means "equal inode" in the kernel but "equal name" in Python disagrees precisely
where it matters.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from machine_plane.accounting import (
    GAP_TYPES,
    LIFECYCLE_TYPES,
    Counts,
    EventClass,
    classify,
    is_in_scope,
)
from machine_plane.events import EventType, MachineEvent
from machine_plane.identity import DomainType, IsolationDomain, ProcessIdentity
from machine_plane.privacy_scan import account_segment
from machine_plane.spool import Spool
from machine_plane.testing import make_identity

SCOPE_INODE = "385715"


class TestClassesAreDisjoint(unittest.TestCase):
    def test_no_event_type_belongs_to_two_classes(self) -> None:
        self.assertEqual(
            LIFECYCLE_TYPES & GAP_TYPES,
            frozenset(),
            "a type in both sets would be double-counted",
        )

    def test_every_type_classifies_to_exactly_one_class(self) -> None:
        for event_type in EventType:
            with self.subTest(event=event_type.value):
                self.assertIsInstance(classify(event_type), EventClass)

    def test_a_real_gap_event_is_a_gap_and_not_lifecycle(self) -> None:
        """The specific confusion: event_kind() calls these GAP; so does accounting,
        but only these are gaps — recorder start/stop are lifecycle."""
        self.assertIs(classify(EventType.BUFFER_OVERFLOW), EventClass.GAP)
        self.assertIs(classify(EventType.CLOCK_JUMP), EventClass.GAP)
        self.assertIs(classify(EventType.SINK_UNAVAILABLE), EventClass.GAP)
        self.assertIs(classify(EventType.RECORDER_STARTED), EventClass.LIFECYCLE)
        self.assertIs(classify(EventType.RECORDER_STOPPED), EventClass.LIFECYCLE)
        self.assertIs(classify(EventType.OPEN), EventClass.ACTION)


class TestScopeRule(unittest.TestCase):
    def identity(self, domain_id: str | None, scope: str | None = None):
        return ProcessIdentity(
            host_id="h",
            boot_id="b",
            process_instance_id="p",
            isolation_domain=IsolationDomain(
                type=DomainType.CGROUP, id=domain_id, scope=scope
            ),
        )

    def test_numeric_equality(self) -> None:
        self.assertTrue(is_in_scope(self.identity(SCOPE_INODE), SCOPE_INODE))
        self.assertFalse(is_in_scope(self.identity("385716"), SCOPE_INODE))

    def test_leading_zeros_are_the_same_number(self) -> None:
        """The kernel compares integers, so Python must not compare strings."""
        self.assertTrue(is_in_scope(self.identity("0385715"), SCOPE_INODE))

    def test_absent_things_are_out_of_scope(self) -> None:
        self.assertFalse(is_in_scope(None, SCOPE_INODE))
        self.assertFalse(is_in_scope(self.identity(SCOPE_INODE), None))
        self.assertFalse(is_in_scope(self.identity(None), SCOPE_INODE))

    def test_a_non_numeric_cgroup_is_out_of_scope(self) -> None:
        self.assertFalse(is_in_scope(self.identity("not-a-number"), SCOPE_INODE))

    def test_the_scope_name_is_not_the_join_key(self) -> None:
        """A matching name with a different inode must not count as in scope."""
        identity = self.identity("999", scope="mp-whatever.scope")
        self.assertFalse(is_in_scope(identity, SCOPE_INODE))


class TestCounts(unittest.TestCase):
    def test_equations(self) -> None:
        counts = Counts(lifecycle=2, action=10, gaps=1)
        counts.in_scope_actions = 8
        counts.out_of_scope_actions = 1
        counts.unmatched_actions = 1
        self.assertTrue(counts.totals_balance)
        self.assertTrue(counts.actions_balance)
        self.assertEqual(counts.records, 13)
        self.assertFalse(counts.filtered_clean)

    def test_unattributed_is_not_zero_matches(self) -> None:
        counts = Counts(action=5)
        counts.unmatched_actions = 5
        self.assertIn("unattributed", counts.attribution_label())
        counts.scope_in_segment = "385715"
        self.assertIn("in_scope 0", counts.attribution_label())


class TestSegmentAccounting(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-acct-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def event(self, event_type, *, cgroup: str | None = SCOPE_INODE, **details):
        identity = None
        if event_type not in LIFECYCLE_TYPES and event_type not in GAP_TYPES:
            identity = ProcessIdentity(
                host_id="h",
                boot_id="b",
                process_instance_id="p",
                isolation_domain=(
                    IsolationDomain(type=DomainType.CGROUP, id=cgroup)
                    if cgroup is not None
                    else None
                ),
            )
        return MachineEvent(
            type=event_type,
            producer="machine-plane-recorder",
            producer_version="1",
            platform="linux",
            monotonic_time=1.0,
            wall_time=2.0,
            result="ok",
            identity=identity,
            platform_details=dict(details),
        )

    def segment(self, events) -> Path:
        path = self.directory / "segment-test.ndjson"
        spool = Spool(path, "machine-plane-recorder", mode=0o600)
        for event in events:
            spool.append(event)
        return path

    def test_lifecycle_gap_and_action_are_counted_once_each(self) -> None:
        """A real gap event, plus lifecycle, plus actions — no double counting."""
        path = self.segment(
            [
                self.event(
                    EventType.RECORDER_STARTED,
                    cgroup=None,
                    cgroup_inode=SCOPE_INODE,
                    scope="mp-x.scope",
                ),
                self.event(EventType.OPEN),
                self.event(EventType.OPEN),
                self.event(EventType.BUFFER_OVERFLOW, cgroup=None),
                self.event(EventType.CLOCK_JUMP, cgroup=None),
                self.event(EventType.RECORDER_STOPPED, cgroup=None),
            ]
        )
        counts, events, already = account_segment(path, recorder_id="machine-plane-recorder")

        self.assertEqual(counts.records, 6)
        self.assertEqual(counts.lifecycle, 2)
        self.assertEqual(counts.action, 2)
        self.assertEqual(counts.gaps, 2)
        self.assertEqual(counts.lifecycle + counts.gaps, 4, "gaps must not be lifecycle")
        self.assertTrue(counts.totals_balance)
        self.assertTrue(counts.actions_balance)
        self.assertEqual(already, 0)

    def test_out_of_scope_actions_are_distinguished(self) -> None:
        path = self.segment(
            [
                self.event(EventType.RECORDER_STARTED, cgroup=None, cgroup_inode=SCOPE_INODE),
                self.event(EventType.OPEN),
                self.event(EventType.OPEN, cgroup="330814"),
                self.event(EventType.RECORDER_STOPPED, cgroup=None),
            ]
        )
        counts, _, _ = account_segment(path, recorder_id="machine-plane-recorder")
        self.assertEqual(counts.in_scope_actions, 1)
        self.assertEqual(counts.out_of_scope_actions, 1)
        self.assertEqual(counts.unmatched_actions, 0)
        self.assertFalse(counts.filtered_clean)

    def test_actions_without_identity_are_unmatched(self) -> None:
        path = self.segment(
            [
                self.event(EventType.RECORDER_STARTED, cgroup=None, cgroup_inode=SCOPE_INODE),
                self.event(EventType.OPEN, cgroup=None),
            ]
        )
        counts, _, _ = account_segment(path, recorder_id="machine-plane-recorder")
        self.assertEqual(counts.unmatched_actions, 1)
        self.assertEqual(counts.action, 1)

    def test_no_header_means_attribution_is_unknown(self) -> None:
        path = self.segment([self.event(EventType.OPEN)])
        counts, _, _ = account_segment(path, recorder_id="machine-plane-recorder")
        self.assertIsNone(counts.scope_in_segment)
        self.assertIn("unattributed", counts.attribution_label())

    def test_a_mismatched_scope_name_is_refused(self) -> None:
        path = self.segment(
            [self.event(EventType.RECORDER_STARTED, cgroup=None, scope="mp-actual.scope")]
        )
        with self.assertRaises(ValueError):
            account_segment(
                path, recorder_id="machine-plane-recorder", expected_scope="mp-other.scope"
            )


if __name__ == "__main__":
    unittest.main()

class TestNoScopeReportingIsConsistent(unittest.TestCase):
    def test_a_segment_scope_without_a_requested_scope_is_not_called_unattributed(
        self,
    ) -> None:
        from machine_plane.accounting import Counts

        counts = Counts(scope_in_segment="385715", scope_requested=False)
        counts.action = 2
        counts.in_scope_actions = 2
        label = counts.attribution_label()
        self.assertNotIn("unattributed", label)
        self.assertIn("in_scope 2", label)
        self.assertIn("--scope not", label)

    def test_no_scope_in_the_segment_is_unattributed(self) -> None:
        from machine_plane.accounting import Counts

        counts = Counts()
        counts.action = 3
        self.assertIn("unattributed", counts.attribution_label())

    def test_a_requested_scope_is_reported_as_matched(self) -> None:
        from machine_plane.accounting import Counts

        counts = Counts(scope_in_segment="385715", scope_requested=True)
        self.assertIn("matches --scope", counts.attribution_label())


class TestClassificationIsClosed(unittest.TestCase):
    def test_every_event_type_in_the_schema_is_classified(self) -> None:
        """A new EventType must break this test, not slip into a population."""
        from machine_plane.accounting import ACTION_TYPES, GAP_TYPES, LIFECYCLE_TYPES
        from machine_plane.events import EventType

        covered = LIFECYCLE_TYPES | ACTION_TYPES | GAP_TYPES
        missing = [member.name for member in EventType if member not in covered]
        self.assertEqual(
            missing,
            [],
            f"event types with no accounting class: {missing} — add them to "
            "accounting.py before the equations can be trusted",
        )

    def test_an_unclassified_type_fails_closed(self) -> None:
        from machine_plane.accounting import classify

        with self.assertRaises(ValueError):
            classify("some_future_event")

    def test_a_new_gap_type_cannot_enter_the_action_population(self) -> None:
        """The specific failure: default-to-ACTION would balance while lying."""
        from machine_plane.accounting import ACTION_TYPES, GAP_TYPES

        for member in GAP_TYPES:
            self.assertNotIn(member, ACTION_TYPES)
