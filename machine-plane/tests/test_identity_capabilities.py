"""Neutral identity and capability reporting.

Two rules are load-bearing here: isolation domains nest, and an unmentioned
capability is never read as support.
"""

from __future__ import annotations

import unittest

from machine_plane.capabilities import (
    Capability,
    CapabilityReport,
    CapabilitySet,
)
from machine_plane.identity import DomainType, IsolationDomain, ProcessIdentity


class IsolationDomainTest(unittest.TestCase):
    def test_domains_nest_and_report_a_chain_outermost_first(self) -> None:
        container = IsolationDomain(type=DomainType.CONTAINER, id="ctr-1")
        cgroup = IsolationDomain(
            type=DomainType.CGROUP, id="scope-1", parent=container
        )
        self.assertEqual(
            [node.type for node in cgroup.chain()],
            [DomainType.CONTAINER, DomainType.CGROUP],
        )

    def test_a_lone_domain_is_its_own_chain(self) -> None:
        domain = IsolationDomain(type=DomainType.NONE)
        self.assertEqual(domain.chain(), (domain,))

    def test_nesting_survives_a_round_trip(self) -> None:
        domain = IsolationDomain(
            type=DomainType.CGROUP,
            id="scope-1",
            scope="agent-run",
            platform="linux",
            parent=IsolationDomain(type=DomainType.VM, id="vm-1"),
        )
        restored = IsolationDomain.from_dict(domain.to_dict())
        assert restored is not None
        self.assertEqual(restored.to_dict(), domain.to_dict())
        self.assertEqual(len(restored.chain()), 2)


class ProcessIdentityTest(unittest.TestCase):
    def test_identity_round_trips(self) -> None:
        identity = ProcessIdentity(
            host_id="host-1",
            boot_id="boot-1",
            process_instance_id="proc-1",
            process_start_time=1234.5,
            parent_process_instance_id="proc-0",
            isolation_domain=IsolationDomain(type=DomainType.JOB_OBJECT, id="job-1"),
        )
        restored = ProcessIdentity.from_dict(identity.to_dict())
        self.assertEqual(restored, identity)

    def test_identity_carries_no_linux_specific_field_names(self) -> None:
        identity = ProcessIdentity(
            host_id="host-1", boot_id="boot-1", process_instance_id="proc-1"
        )
        self.assertNotIn("cgroup_id", identity.to_dict())
        self.assertIn("isolation_domain", identity.to_dict())


class CapabilityIntervalTest(unittest.TestCase):
    def test_a_capability_holds_only_inside_its_interval(self) -> None:
        report = CapabilityReport(
            capability=Capability.BTF_AVAILABLE,
            available=False,
            from_time=100.0,
            to_time=200.0,
            reason="missing_kernel_btf",
        )
        self.assertFalse(report.covers(99.0))
        self.assertTrue(report.covers(150.0))
        self.assertFalse(report.covers(200.0))

    def test_open_interval_covers_everything_after_it_starts(self) -> None:
        report = CapabilityReport(
            capability=Capability.BPF_AVAILABLE, available=True, from_time=100.0
        )
        self.assertTrue(report.covers(10_000.0))

    def test_unmentioned_capability_is_never_available(self) -> None:
        capabilities = CapabilitySet(
            [
                CapabilityReport(
                    capability=Capability.PROCESS_EXEC,
                    available=True,
                    from_time=0.0,
                )
            ]
        )
        self.assertTrue(capabilities.available_at(Capability.PROCESS_EXEC, 5.0))
        self.assertFalse(capabilities.available_at(Capability.BTF_AVAILABLE, 5.0))

    def test_a_capability_that_ended_is_reported_missing_afterwards(self) -> None:
        capabilities = CapabilitySet(
            [
                CapabilityReport(
                    capability=Capability.PROCESS_EXEC,
                    available=True,
                    from_time=0.0,
                    to_time=50.0,
                )
            ]
        )
        self.assertFalse(capabilities.available_at(Capability.PROCESS_EXEC, 60.0))
        self.assertIn(Capability.PROCESS_EXEC, capabilities.missing_at(60.0))

    def test_capability_set_round_trips(self) -> None:
        original = CapabilitySet(
            [
                CapabilityReport(
                    capability=Capability.BTF_AVAILABLE,
                    available=False,
                    from_time=1.0,
                    to_time=2.0,
                    reason="missing_kernel_btf",
                )
            ]
        )
        restored = CapabilitySet.from_list(original.to_list())
        self.assertEqual(restored.to_list(), original.to_list())


if __name__ == "__main__":
    unittest.main()
