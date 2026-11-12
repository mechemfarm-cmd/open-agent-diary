"""The kill switch: authority, fail direction, and not trusting self-reports."""

from __future__ import annotations

import inspect
import unittest

from machine_plane import killswitch as killswitch_module
from machine_plane.killswitch import (
    ControllerLease,
    FailDirection,
    InvalidTransition,
    KillSwitch,
    Observation,
    ObservationSource,
    SwitchAction,
    SwitchState,
    Unauthorised,
    counter_challenges,
)

CONTROLLERS = ("human:bill", "supervisor:emily")
OBSERVERS = ("host:emily", "human:bill")


def _switch(**overrides: object) -> KillSwitch:
    options: dict[str, object] = dict(
        scope="agent-run-1",
        fail_direction=FailDirection.FAIL_CLOSED,
        authorised_controllers=CONTROLLERS,
        observers=OBSERVERS,
    )
    options.update(overrides)
    return KillSwitch(**options)  # type: ignore[arg-type]


class FailDirectionTest(unittest.TestCase):
    def test_the_fail_direction_must_be_stated(self) -> None:
        """No default: both answers are defensible and the wrong one is found late."""
        with self.assertRaises(TypeError):
            KillSwitch(scope="x", authorised_controllers=CONTROLLERS)  # type: ignore[call-arg]

    def test_fail_closed_stops_the_agent_when_the_controller_is_lost(self) -> None:
        switch = _switch(fail_direction=FailDirection.FAIL_CLOSED)
        self.assertEqual(
            switch.predicted_state_on_controller_loss(), SwitchState.STOPPED
        )

    def test_fail_open_leaves_the_agent_running(self) -> None:
        switch = _switch(fail_direction=FailDirection.FAIL_OPEN)
        self.assertEqual(
            switch.predicted_state_on_controller_loss(), SwitchState.RUNNING
        )

    def test_predicting_the_loss_state_does_not_change_the_model(self) -> None:
        """Asking what *would* happen is not the same as it happening."""
        switch = _switch(fail_direction=FailDirection.FAIL_CLOSED)
        switch.predicted_state_on_controller_loss()
        self.assertEqual(switch.state, SwitchState.RUNNING)
        self.assertEqual(switch.losses, ())

    def test_applying_the_loss_records_it_without_proving_it(self) -> None:
        switch = _switch(fail_direction=FailDirection.FAIL_CLOSED)
        resulting = switch.apply_controller_loss(at=5.0)
        self.assertEqual(resulting, SwitchState.STOPPED)
        self.assertEqual(switch.state, SwitchState.STOPPED)
        self.assertEqual(len(switch.losses), 1)
        self.assertFalse(switch.losses[0].verified)
        self.assertFalse(switch.proven)

    def test_controller_loss_needs_a_lease_to_have_meaning(self) -> None:
        """Without a timeout, 'the controller is gone' is an opinion."""
        switch = _switch()
        with self.assertRaises(ValueError):
            switch.controller_lost(last_seen=0.0, now=100.0)

    def test_the_lease_decides_when_silence_is_loss(self) -> None:
        switch = _switch(lease=ControllerLease(controller="human:bill", timeout=30.0))
        self.assertFalse(switch.controller_lost(last_seen=100.0, now=120.0))
        self.assertTrue(switch.controller_lost(last_seen=100.0, now=131.0))


class AuthorityTest(unittest.TestCase):
    def test_a_registered_controller_may_request_an_action(self) -> None:
        switch = _switch()
        plan = switch.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
        self.assertEqual(plan.to_state, SwitchState.PAUSED)

    def test_an_agent_cannot_ask_for_its_own_pause(self) -> None:
        """Being consulted is not being controlled."""
        switch = _switch()
        with self.assertRaises(Unauthorised):
            switch.request(SwitchAction.PAUSE, requested_by="agent:tom", at=1.0)
        self.assertEqual(switch.state, SwitchState.RUNNING)

    def test_a_refused_request_is_not_recorded_as_an_action(self) -> None:
        switch = _switch()
        with self.assertRaises(Unauthorised):
            switch.request(SwitchAction.STOP, requested_by="agent:tom", at=1.0)
        self.assertEqual(switch.records, ())


class PlanningTest(unittest.TestCase):
    def test_planning_changes_nothing(self) -> None:
        switch = _switch()
        switch.plan(SwitchAction.STOP)
        self.assertEqual(switch.state, SwitchState.RUNNING)

    def test_the_chain_of_actions_runs_in_order(self) -> None:
        switch = _switch()
        for action in (
            SwitchAction.PAUSE,
            SwitchAction.FREEZE,
            SwitchAction.ISOLATE,
            SwitchAction.STOP,
        ):
            switch.request(action, requested_by="human:bill", at=1.0)
        self.assertEqual(switch.state, SwitchState.STOPPED)

    def test_freezing_uses_the_cgroup_freezer_not_a_signal(self) -> None:
        plan = _switch().plan(SwitchAction.FREEZE)
        mechanisms = {step.mechanism for step in plan.steps}
        self.assertIn("cgroup_freezer", mechanisms)
        self.assertNotIn("signal", mechanisms)

    def test_stopping_is_marked_not_reversible(self) -> None:
        plan = _switch().plan(SwitchAction.STOP)
        self.assertTrue(any(not step.reversible for step in plan.steps))

    def test_a_plan_explains_itself_in_words(self) -> None:
        text = _switch().plan(SwitchAction.FREEZE).describe()
        self.assertIn("paused -> frozen", text)
        self.assertIn("cgroup freezer", text)


_CURRENT = object()


def _requested(**overrides: object) -> KillSwitch:
    """A switch with one authorised request made, so a challenge exists."""
    switch = _switch(**overrides)
    switch.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
    return switch


def _observation(
    switch: KillSwitch,
    state: object = _CURRENT,
    *,
    observer: str = "host:emily",
    source: ObservationSource = ObservationSource.EXTERNAL_VERIFIER,
    at: float = 2.0,
    challenge: object = _CURRENT,
    scope: object = _CURRENT,
) -> Observation:
    """A report bound to the switch's latest request, unless told otherwise.

    The observed state defaults to the state that request was meant to produce,
    because an observation of some other state is not proof of this transition.
    """
    expected = (
        switch.records[-1].plan.to_state if switch.records else SwitchState.RUNNING
    )
    return Observation(
        observer=observer,
        observed_state=expected if state is _CURRENT else state,  # type: ignore[arg-type]
        at=at,
        source=source,
        challenge=(
            switch.records[-1].challenge if challenge is _CURRENT else challenge  # type: ignore[arg-type]
        ),
        scope=switch.scope if scope is _CURRENT else scope,  # type: ignore[arg-type]
    )


class VerificationTest(unittest.TestCase):
    def test_a_switch_is_not_proven_until_verified_from_outside(self) -> None:
        switch = _requested()
        self.assertFalse(switch.proven)
        self.assertIsNone(switch.verified_state)

    def test_an_external_observation_counts(self) -> None:
        switch = _requested()
        accepted = switch.record_observation(_observation(switch))
        self.assertTrue(accepted)
        self.assertTrue(switch.proven)
        self.assertEqual(switch.verified_state, SwitchState.PAUSED)
        self.assertTrue(switch.current_state_verified)

    def test_an_observation_of_the_wrong_state_does_not_count(self) -> None:
        """The challenge says which transition is being checked.

        A RESUME request answered with STOPPED is not proof the resume happened,
        even though the challenge, the observer and the scope are all correct.
        """
        switch = _requested()
        accepted = switch.record_observation(
            _observation(switch, state=SwitchState.STOPPED)
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_a_stronger_state_is_not_accepted_as_proof(self) -> None:
        """'Proven paused' and 'proven stopped' are different claims."""
        switch = _requested()
        accepted = switch.record_observation(
            _observation(switch, state=SwitchState.ISOLATED)
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_a_self_report_does_not_count(self) -> None:
        """'Stopped', said by the thing meant to stop, is not evidence."""
        switch = _requested()
        accepted = switch.record_observation(
            _observation(switch, source=ObservationSource.SELF_REPORT)
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)
        self.assertEqual(len(switch.observations), 1)

    def test_an_unregistered_observer_does_not_count(self) -> None:
        """The registration list is the trust boundary, so it has to bite."""
        switch = _requested()
        accepted = switch.record_observation(
            _observation(switch, observer="agent:hermes")
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_a_test_seam_is_not_proof(self) -> None:
        """Calling the method from a test is not an external verification."""
        switch = _requested()
        accepted = switch.record_observation(
            _observation(switch, source=ObservationSource.TEST_SEAM)
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_an_unbound_observation_does_not_count(self) -> None:
        """Without a challenge it is not tied to any action that was requested."""
        switch = _requested()
        accepted = switch.record_observation(
            _observation(switch, challenge=None)
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_an_observation_quoting_a_stale_challenge_does_not_count(self) -> None:
        """A real observation of an earlier state is not proof of a later one."""
        switch = _requested()
        stale = switch.records[-1].challenge
        switch.request(SwitchAction.RESUME, requested_by="human:bill", at=1.5)
        accepted = switch.record_observation(
            _observation(switch, challenge=stale, at=2.0)
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_an_observation_of_another_scope_does_not_count(self) -> None:
        switch = _requested()
        accepted = switch.record_observation(
            _observation(switch, scope="some-other-run")
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_an_observation_predating_its_request_does_not_count(self) -> None:
        """Freshness: it cannot have observed a state that did not exist yet."""
        switch = _requested()
        accepted = switch.record_observation(_observation(switch, at=0.5))
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)

    def test_each_request_issues_a_fresh_challenge(self) -> None:
        switch = _switch()
        first = switch.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
        second = switch.request(SwitchAction.RESUME, requested_by="human:bill", at=2.0)
        self.assertNotEqual(switch.records[0].challenge, switch.records[1].challenge)
        self.assertEqual(first.action, SwitchAction.PAUSE)
        self.assertEqual(second.action, SwitchAction.RESUME)

    def test_a_boolean_can_no_longer_prove_the_switch(self) -> None:
        """The old API took ``verified_externally=True`` from any caller.

        An assertion is not an observation, so the parameter is gone: proof now
        requires naming a registered observer, declaring where the report came
        from, and quoting the challenge issued with the request it answers.
        """
        switch = _switch()
        self.assertFalse(hasattr(switch, "record_verification"))
        self.assertFalse(hasattr(switch, "on_controller_loss"))


class TransitionTest(unittest.TestCase):
    def test_stopping_from_running_is_refused(self) -> None:
        """The table is a constraint, not a label."""
        switch = _switch()
        with self.assertRaises(InvalidTransition):
            switch.request(SwitchAction.STOP, requested_by="human:bill", at=1.0)
        self.assertEqual(switch.state, SwitchState.RUNNING)

    def test_the_legal_sequence_is_accepted(self) -> None:
        """The escalation ladder, in the only order it permits."""
        switch = _switch()
        switch.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
        switch.request(SwitchAction.FREEZE, requested_by="human:bill", at=2.0)
        switch.request(SwitchAction.ISOLATE, requested_by="human:bill", at=3.0)
        switch.request(SwitchAction.STOP, requested_by="human:bill", at=4.0)
        self.assertEqual(switch.state, SwitchState.STOPPED)

    def test_skipping_a_rung_of_the_ladder_is_refused(self) -> None:
        """Pause then stop is not a sequence a real switch can follow."""
        switch = _switch()
        switch.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
        with self.assertRaises(InvalidTransition):
            switch.request(SwitchAction.STOP, requested_by="human:bill", at=2.0)
        self.assertEqual(switch.state, SwitchState.PAUSED)

    def test_a_refused_transition_leaves_no_record(self) -> None:
        switch = _switch()
        before = len(switch.records)
        with self.assertRaises(InvalidTransition):
            switch.request(SwitchAction.STOP, requested_by="human:bill", at=1.0)
        self.assertEqual(len(switch.records), before)


class ProofLifetimeTest(unittest.TestCase):
    """Proof is bound to a transition, not to the switch's lifetime."""

    def test_a_new_request_invalidates_an_earlier_proof(self) -> None:
        """Proven paused is not proven frozen."""
        switch = _requested()
        self.assertTrue(switch.record_observation(_observation(switch)))
        self.assertTrue(switch.current_state_verified)

        switch.request(SwitchAction.FREEZE, requested_by="human:bill", at=3.0)

        self.assertFalse(switch.proven)
        self.assertFalse(switch.current_state_verified)
        self.assertIsNone(switch.verified_state)

    def test_controller_loss_invalidates_an_earlier_proof(self) -> None:
        """The loss transition moves the switch to a state nobody observed."""
        switch = _requested()
        self.assertTrue(switch.record_observation(_observation(switch)))

        switch.apply_controller_loss(at=3.0)

        self.assertFalse(switch.proven)
        self.assertFalse(switch.current_state_verified)

    def test_the_new_state_can_be_proved_in_its_turn(self) -> None:
        switch = _requested()
        switch.record_observation(_observation(switch))
        switch.request(SwitchAction.FREEZE, requested_by="human:bill", at=3.0)

        self.assertTrue(switch.record_observation(_observation(switch, at=4.0)))
        self.assertEqual(switch.verified_state, SwitchState.FROZEN)
        self.assertTrue(switch.current_state_verified)


class ChallengeSourceTest(unittest.TestCase):
    """The default challenge must not be predictable."""

    def test_the_default_challenge_is_not_a_counter(self) -> None:
        """Two switches built the same way must not issue the same challenge."""
        first = _switch()
        second = _switch()
        first.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
        second.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)

        self.assertNotEqual(first.records[0].challenge, second.records[0].challenge)
        self.assertIsNotNone(first.records[0].challenge)
        self.assertGreaterEqual(len(first.records[0].challenge or ""), 32)

    def test_a_test_can_still_name_its_challenge(self) -> None:
        switch = _switch(challenge_factory=counter_challenges())
        switch.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
        self.assertEqual(switch.records[0].challenge, "challenge-1")


class BoundaryTest(unittest.TestCase):
    def test_the_module_executes_nothing(self) -> None:
        """A regression tripwire, not a security control.

        It reads source text, so it cannot prove the module will never execute
        anything — an indirect call or a helper module would defeat it. What it
        catches is the drift that matters: a future edit growing a real
        ``subprocess`` or ``signal`` dependency in a module that is supposed to
        only model the switch.
        """
        source = inspect.getsource(killswitch_module)
        for forbidden in ("subprocess", "os.system", "popen", "signal."):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
