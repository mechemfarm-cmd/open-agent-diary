"""The kill switch, as an architecture rather than a button.

A switch the agent can reach is not a switch. A switch whose behaviour when the
controller disappears was never decided is not a switch either — it is a hope.
So this module is deliberately a *model and a planner*: it decides who may ask
for what, what each action means, and what happens when the controller is
unreachable. It executes nothing.

Three things it refuses to leave implicit:

**Fail direction.** There is no default. Whether an unreachable controller means
the agent keeps running or stops is a decision someone has to make out loud,
because both are defensible and the wrong one is discovered at the worst moment.

**Authority.** Only registered controllers may request an action. The agent is
not one of them, and asking is refused rather than logged and honoured.

**Verification.** A switch is only proven by observing it from outside. An
outcome the agent reports is recorded as unverified, because "stopped" said by
the thing that was supposed to stop is not evidence that it did.

Concrete commands are not invented here. Each step names its mechanism and what
it would do; the privileged component that actually issues them is a separate
piece of work, and inventing plausible command lines would be worse than saying
so.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SwitchState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    FROZEN = "frozen"
    ISOLATED = "isolated"
    STOPPED = "stopped"


class SwitchAction(str, Enum):
    PAUSE = "pause"
    FREEZE = "freeze"
    ISOLATE = "isolate"
    STOP = "stop"
    RESUME = "resume"


class FailDirection(str, Enum):
    """What an unreachable controller means for the agent."""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class Unauthorised(Exception):
    """Raised when a requester is not a registered controller."""


@dataclass(frozen=True)
class SwitchStep:
    description: str
    mechanism: str
    reversible: bool


@dataclass(frozen=True)
class SwitchPlan:
    action: SwitchAction
    from_state: SwitchState
    to_state: SwitchState
    steps: tuple[SwitchStep, ...]

    def describe(self) -> str:
        lines = [
            f"{self.action.value}: {self.from_state.value} -> {self.to_state.value}"
        ]
        for step in self.steps:
            reversible = "reversible" if step.reversible else "not reversible"
            lines.append(f"  - {step.description} [{step.mechanism}, {reversible}]")
        return "\n".join(lines)


@dataclass(frozen=True)
class SwitchRecord:
    action: SwitchAction
    requested_by: str
    at: float
    plan: SwitchPlan


_TRANSITIONS: dict[SwitchAction, tuple[SwitchState, SwitchState]] = {
    SwitchAction.PAUSE: (SwitchState.RUNNING, SwitchState.PAUSED),
    SwitchAction.FREEZE: (SwitchState.PAUSED, SwitchState.FROZEN),
    SwitchAction.ISOLATE: (SwitchState.FROZEN, SwitchState.ISOLATED),
    SwitchAction.STOP: (SwitchState.ISOLATED, SwitchState.STOPPED),
    SwitchAction.RESUME: (SwitchState.PAUSED, SwitchState.RUNNING),
}

_STEPS: dict[SwitchAction, tuple[SwitchStep, ...]] = {
    SwitchAction.PAUSE: (
        SwitchStep(
            "ask the supervisor to stop taking new work, preserving state",
            "control_channel",
            True,
        ),
    ),
    SwitchAction.FREEZE: (
        SwitchStep(
            "stop the run's process tree with the cgroup freezer",
            "cgroup_freezer",
            True,
        ),
    ),
    SwitchAction.ISOLATE: (
        SwitchStep(
            "revoke the run's network access",
            "firewall",
            True,
        ),
    ),
    SwitchAction.STOP: (
        SwitchStep(
            "terminate the run's scope",
            "service_manager",
            False,
        ),
    ),
    SwitchAction.RESUME: (
        SwitchStep(
            "allow the supervisor to take work again",
            "control_channel",
            True,
        ),
    ),
}


class KillSwitch:
    """The switch's authority and state model. Executes nothing."""

    def __init__(
        self,
        *,
        scope: str,
        fail_direction: FailDirection,
        authorised_controllers: tuple[str, ...],
    ) -> None:
        if not isinstance(fail_direction, FailDirection):
            raise TypeError(
                "fail_direction is required: decide whether an unreachable "
                "controller leaves the agent running or stopped"
            )
        self.scope = scope
        self.fail_direction = fail_direction
        self.authorised_controllers = tuple(authorised_controllers)
        self._state = SwitchState.RUNNING
        self._records: list[SwitchRecord] = []
        self._verified_state: SwitchState | None = None

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> SwitchState:
        """The state the switch believes it is in. Not evidence."""
        return self._state

    @property
    def verified_state(self) -> SwitchState | None:
        """The last state confirmed from outside. ``None`` until that happens."""
        return self._verified_state

    @property
    def records(self) -> tuple[SwitchRecord, ...]:
        return tuple(self._records)

    @property
    def proven(self) -> bool:
        """A switch nobody has ever verified is a decoration."""
        return self._verified_state is not None

    def on_controller_loss(self) -> SwitchState:
        """What the agent's state should become if the controller goes away."""
        if self.fail_direction is FailDirection.FAIL_CLOSED:
            return SwitchState.STOPPED
        return SwitchState.RUNNING

    # -- actions ---------------------------------------------------------

    def plan(self, action: SwitchAction) -> SwitchPlan:
        """Describe what an action would do. Changes nothing."""
        if action not in _TRANSITIONS:
            raise ValueError(f"unknown action: {action}")
        from_state, to_state = _TRANSITIONS[action]
        return SwitchPlan(
            action=action,
            from_state=from_state,
            to_state=to_state,
            steps=_STEPS[action],
        )

    def request(self, action: SwitchAction, *, requested_by: str, at: float) -> SwitchPlan:
        """Authorise and record a request. Does not carry it out.

        Refusing an unauthorised requester is the point: an agent that can ask
        for its own pause is not being controlled, it is being consulted.
        """
        if requested_by not in self.authorised_controllers:
            raise Unauthorised(
                f"{requested_by!r} is not a registered controller for {self.scope!r}"
            )

        plan = self.plan(action)
        self._records.append(
            SwitchRecord(action=action, requested_by=requested_by, at=at, plan=plan)
        )
        self._state = plan.to_state
        return plan

    def record_verification(
        self,
        *,
        observed_state: SwitchState,
        verified_externally: bool,
        at: float,
    ) -> bool:
        """Record what an outside observer saw.

        Returns whether the observation counts. A self-reported outcome is
        recorded as a request record but does not mark the switch proven —
        "stopped" said by the thing that was meant to stop is not evidence.
        """
        if not verified_externally:
            self._records.append(
                SwitchRecord(
                    action=SwitchAction.STOP
                    if observed_state is SwitchState.STOPPED
                    else SwitchAction.PAUSE,
                    requested_by="self_report",
                    at=at,
                    plan=self.plan(
                        SwitchAction.STOP
                        if observed_state is SwitchState.STOPPED
                        else SwitchAction.PAUSE
                    ),
                )
            )
            return False

        self._verified_state = observed_state
        return True
