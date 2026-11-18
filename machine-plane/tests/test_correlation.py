"""Correlation: comparing the agent's account with the machine's.

The tests that matter most here are the ones about *not* matching. A confident
wrong match is worse than an honest gap, and a comparison that cannot be made
must never be reported as agreement.
"""

from __future__ import annotations

import unittest

from machine_plane.correlation import (
    CorrelationStatus,
    CoverageMap,
    RunRegistry,
    correlate,
)
from machine_plane.events import EventType
from machine_plane.privacy import PathDisplay, PrivacyPolicy
from machine_plane.testing import (
    MockProducer,
    action_event,
    gap_event,
    make_claim,
    make_registration,
)

WALL = 1_700_000_000.0


def _events(raws, policy=None):
    return list(MockProducer(raws, policy=policy).events())


def _running(raws, policy=None):
    """Events with the recorder declared running.

    A confirmation now needs both halves to be sound, and the coverage model
    already treated everything before the first ``recorder_started`` as blind. So
    a fixture that expects a confirmed match has to say the recorder was running
    — otherwise it is asserting a match nobody observed.
    """
    return _events(
        [gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100), *raws], policy
    )


def _policy() -> PrivacyPolicy:
    return PrivacyPolicy(
        allowed_roots=("/home/x/work",), path_display=PathDisplay.BASENAME
    )


def _registry(*registrations) -> RunRegistry:
    return RunRegistry(registrations or (make_registration(),))


class ConfirmedMatchTest(unittest.TestCase):
    def test_identity_action_and_target_agreeing_is_a_confirmed_match(self) -> None:
        policy = _policy()
        events = _running(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL,
                )
            ],
            policy=policy,
        )
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(
            report.counts().get(CorrelationStatus.CONFIRMED_MATCH.value), 1
        )
        self.assertEqual(report.exceptions(), ())

    def test_the_claim_target_is_redacted_before_comparison(self) -> None:
        """Both sides get identical treatment, not one redacted against one raw."""
        policy = _policy()
        events = _running(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL,
                )
            ],
            policy=policy,
        )
        # The claim carries the real path; the event carries the basename.
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)
        self.assertEqual(
            report.records[0].status, CorrelationStatus.CONFIRMED_MATCH
        )


class HonestNonMatchTest(unittest.TestCase):
    def test_a_dropped_target_is_ambiguous_not_agreement(self) -> None:
        """Redaction removed the target, so nothing can be compared."""
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/private/notes.txt",
                    wall_time=WALL,
                ),
            ],
            policy,
        )
        claims = [
            make_claim("write", target="/home/x/private/notes.txt", wall_time=WALL)
        ]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(
            report.records[0].status, CorrelationStatus.AMBIGUOUS_MATCH
        )
        self.assertEqual(
            report.records[0].reason, "target_not_comparable_after_redaction"
        )
        # The claim is reported too, as having no observed effect. Consuming it
        # here would let one un-attributable event swallow unrelated claims.
        self.assertEqual(
            report.records[1].status, CorrelationStatus.AGENT_ONLY
        )

    def test_a_surviving_target_rules_out_a_dropped_one(self) -> None:
        """One target survived redaction and one did not, so they differ.

        A surviving event target proves the path was inside the allowed roots;
        a dropped claim target proves it was not. Treating that as undecidable
        made every unrelated event ambiguous.
        """
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/secret.txt",
                    wall_time=WALL,
                ),
            ],
            policy,
        )
        claims = [
            make_claim("write", target="/home/x/private/notes.txt", wall_time=WALL)
        ]

        report = correlate(events, claims, _registry(), policy=policy)

        statuses = {record.status for record in report.records}
        self.assertIn(CorrelationStatus.MACHINE_ONLY, statuses)
        self.assertIn(CorrelationStatus.AGENT_ONLY, statuses)
        self.assertNotIn(CorrelationStatus.AMBIGUOUS_MATCH, statuses)

    def test_time_alone_never_promotes_a_match(self) -> None:
        """Same instant, different target: two records, no match."""
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/actual.txt",
                    wall_time=WALL,
                ),
            ],
            policy,
        )
        claims = [make_claim("write", target="/home/x/work/other.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        statuses = {record.status for record in report.records}
        self.assertIn(CorrelationStatus.MACHINE_ONLY, statuses)
        self.assertIn(CorrelationStatus.AGENT_ONLY, statuses)

    def test_an_unmatched_claim_without_coverage_is_not_called_agent_only(self) -> None:
        """With no recorder_started, we cannot claim the recorder was watching.

        Reporting "agent only" here would assert that nothing happened, when
        what we actually know is that we were not looking.
        """
        policy = _policy()
        events = _events(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/actual.txt",
                    wall_time=WALL,
                )
            ],
            policy,
        )
        claims = [make_claim("write", target="/home/x/work/other.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        statuses = {record.status for record in report.records}
        self.assertIn(CorrelationStatus.RECORDER_UNAVAILABLE, statuses)
        self.assertNotIn(CorrelationStatus.AGENT_ONLY, statuses)

    def test_two_agreeing_claims_are_ambiguous(self) -> None:
        policy = _policy()
        events = _events(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL,
                )
            ],
            policy,
        )
        claims = [
            make_claim("write", target="/home/x/work/report.txt", wall_time=WALL),
            make_claim("write_file", target="/home/x/work/report.txt", wall_time=WALL),
        ]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(
            report.records[0].status, CorrelationStatus.AMBIGUOUS_MATCH
        )
        self.assertIn("claims_agree", report.records[0].reason)

    def test_an_unknown_action_matches_nothing(self) -> None:
        policy = _policy()
        events = _events(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL,
                )
            ],
            policy,
        )
        claims = [
            make_claim("frobnicate", target="/home/x/work/report.txt", wall_time=WALL)
        ]

        report = correlate(events, claims, _registry(), policy=policy)
        self.assertEqual(report.records[0].status, CorrelationStatus.MACHINE_ONLY)


class ScopeAndDelayTest(unittest.TestCase):
    def test_an_effect_from_no_registered_run_is_out_of_scope(self) -> None:
        policy = _policy()
        events = _events(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL,
                )
            ],
            policy,
        )
        report = correlate(events, [], RunRegistry(), policy=policy)
        self.assertEqual(
            report.records[0].status, CorrelationStatus.UNMATCHED_DUE_TO_SCOPE
        )

    def test_a_registration_for_another_boot_does_not_own_the_event(self) -> None:
        policy = _policy()
        events = _events(
            [action_event(EventType.EXEC, target="/usr/bin/python3", wall_time=WALL)],
            policy,
        )
        registry = RunRegistry([make_registration(boot_id="a-different-boot")])
        report = correlate(events, [], registry, policy=policy)
        self.assertEqual(
            report.records[0].status, CorrelationStatus.UNMATCHED_DUE_TO_SCOPE
        )

    def test_an_effect_after_the_run_closed_is_delayed(self) -> None:
        policy = _policy()
        events = _running(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL + 500,
                )
            ],
            policy=policy,
        )
        registry = RunRegistry(
            [make_registration(started_at=0.0, ended_at=WALL + 10)]
        )
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, registry, policy=policy)
        self.assertEqual(report.records[0].status, CorrelationStatus.DELAYED_MATCH)


class CoverageTest(unittest.TestCase):
    def _gap_events(self):
        return _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                gap_event(EventType.BUFFER_OVERFLOW, wall_time=WALL - 50),
            ]
        )

    def test_before_the_first_recorder_start_nothing_is_known(self) -> None:
        coverage = CoverageMap.from_events(self._gap_events())
        self.assertTrue(coverage.is_blind(WALL - 200))
        self.assertFalse(coverage.recorder_running_at(WALL - 200))

    def test_a_buffer_overflow_opens_a_blind_interval(self) -> None:
        coverage = CoverageMap.from_events(self._gap_events())
        self.assertTrue(coverage.is_blind(WALL))
        self.assertTrue(coverage.recorder_running_at(WALL))

    def test_a_claim_inside_a_blind_interval_is_a_gap_not_a_silent_absence(self) -> None:
        events = self._gap_events()
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry())

        statuses = [record.status for record in report.records]
        self.assertIn(CorrelationStatus.UNMATCHED_DUE_TO_GAP, statuses)

    def test_a_claim_before_the_recorder_ever_started_is_recorder_unavailable(self) -> None:
        events = _events(
            [gap_event(EventType.RECORDER_STARTED, wall_time=WALL + 100)]
        )
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry())

        statuses = [record.status for record in report.records]
        self.assertIn(CorrelationStatus.RECORDER_UNAVAILABLE, statuses)

    def test_a_clean_run_yields_agent_only_for_an_unobserved_claim(self) -> None:
        events = _events(
            [gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100)]
        )
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry())
        statuses = [record.status for record in report.records]
        self.assertIn(CorrelationStatus.AGENT_ONLY, statuses)


class ReportTest(unittest.TestCase):
    def test_exceptions_exclude_only_confirmed_matches(self) -> None:
        policy = _policy()
        events = _running(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL,
                ),
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/other.txt",
                    wall_time=WALL,
                ),
            ],
            policy=policy,
        )
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(len(report.records), 2)
        self.assertEqual(len(report.exceptions()), 1)
        self.assertEqual(
            report.exceptions()[0].status, CorrelationStatus.MACHINE_ONLY
        )

    def test_the_report_serialises_with_categories_not_a_divergence_count(self) -> None:
        payload = correlate([], [], RunRegistry()).to_dict()
        self.assertIn("counts", payload)
        self.assertIn("records", payload)
        self.assertNotIn("divergences", payload)


if __name__ == "__main__":
    unittest.main()
