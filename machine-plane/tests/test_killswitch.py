"""The kill switch: authority, fail direction, and not trusting self-reports."""

from __future__ import annotations

import inspect
import unittest

from machine_plane import killswitch as killswitch_module
from machine_plane.killswitch import (
    FailDirection,
    KillSwitch,
    SwitchAction,
    SwitchState,
    Unauthorised,
)

CONTROLLERS = ("human:bill", "supervisor:emily")


def _switch(**overrides: object) -> KillSwitch:
    options: dict[str, object] = dict(
        scope="agent-run-1",
        fail_direction=FailDirection.FAIL_CLOSED,
        authorised_controllers=CONTROLLERS,
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
        self.assertEqual(switch.on_controller_loss(), SwitchState.STOPPED)

    def test_fail_open_leaves_the_agent_running(self) -> None:
        switch = _switch(fail_direction=FailDirection.FAIL_OPEN)
        self.assertEqual(switch.on_controller_loss(), SwitchState.RUNNING)


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


class VerificationTest(unittest.TestCase):
    def test_a_switch_is_not_proven_until_verified_from_outside(self) -> None:
        switch = _switch()
        switch.request(SwitchAction.PAUSE, requested_by="human:bill", at=1.0)
        self.assertFalse(switch.proven)
        self.assertIsNone(switch.verified_state)

    def test_an_external_observation_counts(self) -> None:
        switch = _switch()
        accepted = switch.record_verification(
            observed_state=SwitchState.STOPPED, verified_externally=True, at=2.0
        )
        self.assertTrue(accepted)
        self.assertTrue(switch.proven)
        self.assertEqual(switch.verified_state, SwitchState.STOPPED)

    def test_a_self_report_does_not_count(self) -> None:
        """'Stopped', said by the thing meant to stop, is not evidence."""
        switch = _switch()
        accepted = switch.record_verification(
            observed_state=SwitchState.STOPPED, verified_externally=False, at=2.0
        )
        self.assertFalse(accepted)
        self.assertFalse(switch.proven)


class BoundaryTest(unittest.TestCase):
    def test_the_module_executes_nothing(self) -> None:
        """It models the switch; the privileged component is separate work."""
        source = inspect.getsource(killswitch_module)
        for forbidden in ("subprocess", "os.system", "popen", "signal."):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
