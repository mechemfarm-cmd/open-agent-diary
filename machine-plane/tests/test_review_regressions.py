"""Regressions from the hostile implementation review.

Each test here pins one finding, so a change that reintroduces the problem fails
loudly instead of quietly restoring an overclaim. The findings were:

1. ``correlation`` could produce a false confirmed match when both targets were
   absent, because ``None`` was read as agreement.
2. ``MemoryCheckpointSink()`` defaulted to ``SEPARATE_HOST``, so a dry run could
   report ``evidence=True``.
3. ``killswitch`` did not enforce its own transition graph, and proof rested on
   a caller-supplied boolean. (Pinned in ``test_killswitch.py``.)
4. Coverage gaps were unscoped, buffer overflows never recovered, and clock
   jumps were declared but ignored even though correlation runs on wall time.
"""

from __future__ import annotations

import unittest

from machine_plane.chain import build
from machine_plane.checkpoints import (
    MemoryCheckpointSink,
    TrustDomain,
    assess,
    publish,
)
from machine_plane.correlation import (
    CorrelationStatus,
    CoverageMap,
    RunRegistry,
    correlate,
)
from machine_plane.events import EventType, TargetState
from machine_plane.privacy import PathDisplay, PrivacyPolicy
from machine_plane.testing import (
    MockProducer,
    action_event,
    gap_event,
    make_claim,
    make_registration,
)

WALL = 1_700_000_000.0


def _policy() -> PrivacyPolicy:
    return PrivacyPolicy(
        allowed_roots=("/home/x/work",), path_display=PathDisplay.BASENAME
    )


def _events(raws, policy=None):
    return list(MockProducer(raws, policy=policy).events())


def _registry(*registrations) -> RunRegistry:
    return RunRegistry(registrations or (make_registration(),))


class FindingOneUnknownTargetTest(unittest.TestCase):
    """A target nobody recorded is not a target that agreed."""

    def test_a_claim_that_never_mentions_a_target_is_not_a_confirmed_match(self) -> None:
        """The original bug: both sides ``None``, reported as agreement.

        The event's target was absent and the claim never mentioned one, so the
        comparator had nothing to compare. Returning a confirmed match let a
        claim with no target evidence confirm against an observation with no
        target evidence.
        """
        policy = _policy()
        events = _events(
            [action_event(EventType.WRITE_ATTEMPT, target=None, wall_time=WALL)],
            policy,
        )
        claims = [
            make_claim(
                "write",
                target=None,
                wall_time=WALL,
                target_state=TargetState.UNKNOWN,
            )
        ]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(
            report.records[0].status, CorrelationStatus.AMBIGUOUS_MATCH
        )
        self.assertNotIn(
            CorrelationStatus.CONFIRMED_MATCH.value, report.counts()
        )

    def test_two_explicitly_absent_targets_are_a_stated_agreement(self) -> None:
        """Absence can still match — when both sides say so rather than imply it."""
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                action_event(EventType.WRITE_ATTEMPT, target=None, wall_time=WALL),
            ],
            policy,
        )
        claims = [make_claim("write", target=None, wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(report.records[0].status, CorrelationStatus.CONFIRMED_MATCH)

    def test_a_redacted_event_target_does_not_agree_with_an_unknown_claim(self) -> None:
        """A dropped path and an unrecorded one are not the same absence."""
        policy = _policy()
        events = _events(
            [
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/etc/shadow",
                    wall_time=WALL,
                )
            ],
            policy,
        )
        claims = [
            make_claim(
                "write",
                target=None,
                wall_time=WALL,
                target_state=TargetState.UNKNOWN,
            )
        ]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertNotEqual(
            report.records[0].status, CorrelationStatus.CONFIRMED_MATCH
        )

    def test_the_record_refuses_a_present_state_with_no_target(self) -> None:
        """The state and the value cannot contradict each other."""
        from machine_plane.events import MachineEvent

        with self.assertRaises(ValueError):
            MachineEvent(
                type=EventType.WRITE_ATTEMPT,
                producer="mock",
                producer_version="0.1.0",
                platform="linux",
                monotonic_time=1.0,
                wall_time=WALL,
                target=None,
                target_state=TargetState.PRESENT,
            )


class FindingTwoCheckpointDefaultTest(unittest.TestCase):
    """A dry run must not be able to call itself evidence."""

    def test_the_default_memory_sink_is_not_a_separate_host(self) -> None:
        sink = MemoryCheckpointSink()
        self.assertIs(sink.trust_domain, TrustDomain.UNKNOWN)

    def test_a_default_dry_run_reports_no_evidence(self) -> None:
        events = _events([action_event(EventType.EXEC, target="/usr/bin/env")])
        chain = build(events, seal_at=WALL)
        sink = MemoryCheckpointSink()
        publish(chain, sink, taken_at=WALL)

        self.assertFalse(assess(chain, sink).evidence)

    def test_an_off_box_sink_still_reports_evidence_when_asked(self) -> None:
        """The honest case has to keep working, or the fix is just pessimism."""
        events = _events([action_event(EventType.EXEC, target="/usr/bin/env")])
        chain = build(events, seal_at=WALL)
        sink = MemoryCheckpointSink(trust_domain=TrustDomain.SEPARATE_HOST)
        publish(chain, sink, taken_at=WALL)

        self.assertTrue(assess(chain, sink).evidence)


class FindingFourScopedCoverageTest(unittest.TestCase):
    """A gap has to say what it affects, and whether it has ended."""

    def test_a_scoped_gap_does_not_blind_an_unrelated_subject(self) -> None:
        """Losing write tracing does not make an exec observation unobservable."""
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                gap_event(
                    EventType.BUFFER_OVERFLOW,
                    wall_time=WALL - 5,
                    affected=("write_attempt",),
                ),
            ]
        )
        coverage = CoverageMap.from_events(events)

        self.assertTrue(coverage.is_blind(WALL, affected="write_attempt"))
        self.assertFalse(coverage.is_blind(WALL, affected="exec"))

    def test_an_unscoped_gap_still_covers_everything(self) -> None:
        """A producer that does not scope its gap is entitled to claim the lot."""
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                gap_event(EventType.BUFFER_OVERFLOW, wall_time=WALL - 5),
            ]
        )
        coverage = CoverageMap.from_events(events)

        self.assertTrue(coverage.is_blind(WALL, affected="exec"))

    def test_a_buffer_overflow_recovers_when_it_says_when(self) -> None:
        """A dropped buffer is a bounded loss, not permanent blindness."""
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                gap_event(
                    EventType.BUFFER_OVERFLOW,
                    wall_time=WALL - 5,
                    recovered_at=WALL,
                    lost=40,
                ),
            ]
        )
        coverage = CoverageMap.from_events(events)

        self.assertTrue(coverage.is_blind(WALL - 1))
        self.assertFalse(coverage.is_blind(WALL + 1))

    def test_a_clock_jump_marks_the_interval_unreliable(self) -> None:
        """Correlation runs on wall time, so a step cannot be ignored."""
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                gap_event(EventType.CLOCK_JUMP, wall_time=WALL - 5),
            ]
        )
        coverage = CoverageMap.from_events(events)

        self.assertTrue(coverage.clock_unreliable_at(WALL))
        self.assertFalse(coverage.clock_unreliable_at(WALL - 50))

    def test_a_claim_inside_a_clock_unreliable_window_is_a_gap(self) -> None:
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                gap_event(EventType.CLOCK_JUMP, wall_time=WALL - 5),
            ],
            policy,
        )
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(
            report.records[0].status, CorrelationStatus.UNMATCHED_DUE_TO_GAP
        )
        self.assertEqual(
            report.records[0].reason, "wall_clock_unreliable_at_claim_time"
        )

    def test_a_scoped_gap_leaves_an_unrelated_claim_as_agent_only(self) -> None:
        """The payoff: a narrow gap does not turn real evidence into a gap."""
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                gap_event(
                    EventType.BUFFER_OVERFLOW,
                    wall_time=WALL - 5,
                    affected=("write_attempt",),
                ),
            ],
            policy,
        )
        claims = [make_claim("exec", target="/usr/bin/env", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(report.records[0].status, CorrelationStatus.AGENT_ONLY)


class FollowUpInvariantTest(unittest.TestCase):
    """Follow-up finding 1: the state and the value cannot contradict."""

    def test_an_event_cannot_carry_a_target_and_claim_absence(self) -> None:
        from machine_plane.events import MachineEvent

        with self.assertRaises(ValueError):
            MachineEvent(
                type=EventType.WRITE_ATTEMPT,
                producer="mock",
                producer_version="0.1.0",
                platform="linux",
                monotonic_time=1.0,
                wall_time=WALL,
                target="/etc/shadow",
                target_state=TargetState.ABSENT,
            )

    def test_a_claim_cannot_carry_a_target_and_claim_absence(self) -> None:
        from machine_plane.correlation import AgentClaim

        with self.assertRaises(ValueError):
            AgentClaim(
                capture_id="run-1",
                action="write",
                wall_time=WALL,
                target="/home/x/work/report.txt",
                target_state=TargetState.ABSENT,
            )

    def test_a_claim_cannot_claim_presence_without_a_target(self) -> None:
        from machine_plane.correlation import AgentClaim

        with self.assertRaises(ValueError):
            AgentClaim(
                capture_id="run-1",
                action="write",
                wall_time=WALL,
                target=None,
                target_state=TargetState.PRESENT,
            )

    def test_redacted_carries_no_value(self) -> None:
        """The decision, enforced: REDACTED means the value was removed.

        A state that carried a *transformed* value would be a different state,
        not a REDACTED that sometimes has one.
        """
        from machine_plane.correlation import AgentClaim

        with self.assertRaises(ValueError):
            AgentClaim(
                capture_id="run-1",
                action="write",
                wall_time=WALL,
                target="/home/x/work/report.txt",
                target_state=TargetState.REDACTED,
            )


class FollowUpExportRoundTripTest(unittest.TestCase):
    """Follow-up finding 2: the state must survive the neutral boundary."""

    def _round_trip(self, claim):
        from machine_plane.claims import adapt_record
        from machine_plane.export import to_record

        restored, problem, _ = adapt_record(to_record(claim), line_number=1)
        self.assertIsNone(problem)
        return restored

    def test_present_survives_the_round_trip(self) -> None:
        claim = make_claim("write", target="/home/x/work/report.txt")
        self.assertIs(
            self._round_trip(claim).target_state, TargetState.PRESENT
        )

    def test_absent_survives_the_round_trip(self) -> None:
        """The case that used to come back as UNKNOWN."""
        claim = make_claim("write", target=None, target_state=TargetState.ABSENT)
        self.assertIs(self._round_trip(claim).target_state, TargetState.ABSENT)

    def test_redacted_survives_the_round_trip(self) -> None:
        claim = make_claim("write", target=None, target_state=TargetState.REDACTED)
        self.assertIs(self._round_trip(claim).target_state, TargetState.REDACTED)

    def test_unknown_survives_the_round_trip(self) -> None:
        claim = make_claim("write", target=None, target_state=TargetState.UNKNOWN)
        self.assertIs(self._round_trip(claim).target_state, TargetState.UNKNOWN)

    def test_a_contradictory_record_is_reported_not_adapted(self) -> None:
        from machine_plane.claims import adapt_record

        payload = {
            "capture_id": "run-1",
            "action": "write",
            "wall_time": WALL,
            "target": "/home/x/work/report.txt",
            "target_state": "absent",
        }
        claim, problem, _ = adapt_record(payload, line_number=1)

        self.assertIsNone(claim)
        self.assertIsNotNone(problem)
        self.assertEqual(problem.reason, "target_state_contradicts_target")

    def test_an_absent_claim_still_correlates_after_the_round_trip(self) -> None:
        """The point of the fix: the sound agreement reaches the core."""
        from machine_plane.claims import adapt_record
        from machine_plane.export import to_record

        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                action_event(EventType.WRITE_ATTEMPT, target=None, wall_time=WALL),
            ],
            policy,
        )
        claim, _, _ = adapt_record(
            to_record(
                make_claim("write", target=None, target_state=TargetState.ABSENT)
            ),
            line_number=1,
        )

        report = correlate(events, [claim], _registry(), policy=policy)

        self.assertEqual(report.records[0].status, CorrelationStatus.CONFIRMED_MATCH)


class FollowUpMatchCoverageTest(unittest.TestCase):
    """Follow-up finding 3: coverage limits what a *match* can prove."""

    def _matched(self, extra_raws, *, claim_action="write", claim_target="/home/x/work/report.txt"):
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                *extra_raws,
                action_event(
                    EventType.WRITE_ATTEMPT, target=claim_target, wall_time=WALL
                ),
            ],
            policy,
        )
        claims = [make_claim(claim_action, target=claim_target, wall_time=WALL)]
        return correlate(events, claims, _registry(), policy=policy)

    def test_a_match_inside_a_clock_unreliable_window_is_not_confirmed(self) -> None:
        report = self._matched(
            [gap_event(EventType.CLOCK_JUMP, wall_time=WALL - 5)]
        )
        self.assertEqual(
            report.records[0].status, CorrelationStatus.MATCH_NOT_PROVABLE
        )
        self.assertEqual(report.records[0].reason, "wall_clock_unreliable_at_match_time")

    def test_a_match_inside_a_scoped_gap_is_not_confirmed(self) -> None:
        report = self._matched(
            [
                gap_event(
                    EventType.BUFFER_OVERFLOW,
                    wall_time=WALL - 5,
                    affected=("write_attempt",),
                )
            ]
        )
        self.assertEqual(
            report.records[0].status, CorrelationStatus.MATCH_NOT_PROVABLE
        )
        self.assertEqual(report.records[0].reason, "recorder_blind_at_claim_time")

    def test_a_match_inside_an_unscoped_gap_is_not_confirmed(self) -> None:
        report = self._matched([gap_event(EventType.BUFFER_OVERFLOW, wall_time=WALL - 5)])
        self.assertEqual(
            report.records[0].status, CorrelationStatus.MATCH_NOT_PROVABLE
        )

    def test_a_match_before_the_recorder_started_is_not_confirmed(self) -> None:
        policy = _policy()
        events = _events(
            [
                gap_event(EventType.RECORDER_STARTED, wall_time=WALL + 100),
                action_event(
                    EventType.WRITE_ATTEMPT,
                    target="/home/x/work/report.txt",
                    wall_time=WALL,
                ),
            ],
            policy,
        )
        claims = [make_claim("write", target="/home/x/work/report.txt", wall_time=WALL)]

        report = correlate(events, claims, _registry(), policy=policy)

        self.assertEqual(
            report.records[0].status, CorrelationStatus.MATCH_NOT_PROVABLE
        )
        self.assertEqual(report.records[0].reason, "recorder_not_running")

    def test_a_scoped_gap_does_not_block_an_unrelated_match(self) -> None:
        """The payoff: a narrow gap must not un-prove an unrelated match."""
        report = self._matched(
            [
                gap_event(
                    EventType.BUFFER_OVERFLOW,
                    wall_time=WALL - 5,
                    affected=("exec",),
                )
            ]
        )
        self.assertEqual(report.records[0].status, CorrelationStatus.CONFIRMED_MATCH)

    def test_a_match_outside_the_gap_window_is_still_confirmed(self) -> None:
        report = self._matched(
            [
                gap_event(
                    EventType.BUFFER_OVERFLOW,
                    wall_time=WALL - 50,
                    recovered_at=WALL - 20,
                )
            ]
        )
        self.assertEqual(report.records[0].status, CorrelationStatus.CONFIRMED_MATCH)


class PublicSurfaceTest(unittest.TestCase):
    """The package's declared surface must actually resolve."""

    def test_every_exported_name_resolves(self) -> None:
        import machine_plane

        missing = [
            name for name in machine_plane.__all__ if not hasattr(machine_plane, name)
        ]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
