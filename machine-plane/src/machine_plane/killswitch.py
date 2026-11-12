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

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import itertools


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


class InvalidTransition(Exception):
    """Raised when an action is not legal from the switch's current state."""


@dataclass(frozen=True)
class ControllerLease:
    """How long a controller's silence counts as presence, and for whom.

    Without a stated timeout, "the controller is gone" is an opinion. This makes
    it a rule: a controller is considered lost when it has not been seen for
    longer than ``timeout`` seconds.
    """

    controller: str
    timeout: float

    def is_lost(self, *, last_seen: float, now: float) -> bool:
        return (now - last_seen) > self.timeout


@dataclass(frozen=True)
class Observation:
    """A report about the switch's state, and where it came from.

    It must quote the challenge issued with the request it answers and name the
    scope it observed. Both bind the report to one specific action: without them
    a genuine observation of an earlier state could be replayed as proof of a
    later one.
    """

    observer: str
    observed_state: "SwitchState"
    at: float
    source: "ObservationSource" = None  # type: ignore[assignment]
    #: The challenge issued with the request this observation answers.
    challenge: str | None = None
    #: The scope the observer actually looked at.
    scope: str | None = None


def _counter_challenges() -> Callable[[], str]:
    """Deterministic challenges, so tests stay readable.

    A real component must draw these from an unpredictable source. This model
    cannot tell the difference — which is exactly why the factory is injectable
    and why this docstring says so rather than implying the default is secure.
    """
    counter = itertools.count(1)
    return lambda: f"challenge-{next(counter)}"


class ObservationSource(str, Enum):
    """Who is making the claim, which decides whether it counts.

    This package cannot verify that an observer is genuinely external — only the
    caller knows that. What it can do is refuse the observers it knows are not,
    and refuse to let an untrusted seam be mistaken for proof.
    """

    #: A registered observer outside the controlled scope.
    EXTERNAL_VERIFIER = "external_verifier"
    #: The controlled agent reporting on itself. Recorded, never counted.
    SELF_REPORT = "self_report"
    #: A test calling the method directly. Recorded, never counted.
    TEST_SEAM = "test_seam"


@dataclass(frozen=True)
class SwitchRecord:
    action: SwitchAction
    requested_by: str
    at: float
    plan: SwitchPlan
    #: Issued with the request; an observation must quote it to count.
    challenge: str | None = None


@dataclass(frozen=True)
class ControllerLoss:
    """A recorded fail-direction transition. Unverified by construction."""

    at: float
    resulting_state: "SwitchState"
    verified: bool = False


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
        observers: tuple[str, ...] = (),
        lease: ControllerLease | None = None,
        challenge_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(fail_direction, FailDirection):
            raise TypeError(
                "fail_direction is required: decide whether an unreachable "
                "controller leaves the agent running or stopped"
            )
        self.scope = scope
        self.fail_direction = fail_direction
        self.authorised_controllers = tuple(authorised_controllers)
        #: Who may report an external observation. This list *is* the trust
        #: boundary: the package cannot verify that an observer is genuinely
        #: outside the scope, so it can only refuse the ones it knows are not.
        self.observers = tuple(observers)
        self.lease = lease
        self._challenge_factory = challenge_factory or _counter_challenges()
        self._challenge: str | None = None
        self._state = SwitchState.RUNNING
        self._records: list[SwitchRecord] = []
        self._observations: list[Observation] = []
        self._losses: list[ControllerLoss] = []
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
    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._observations)

    @property
    def losses(self) -> tuple["ControllerLoss", ...]:
        return tuple(self._losses)

    @property
    def proven(self) -> bool:
        """A switch nobody has ever verified is a decoration."""
        return self._verified_state is not None

    def controller_lost(self, *, last_seen: float, now: float) -> bool:
        """Whether the lease says the controller has gone quiet.

        Without a lease this raises rather than guessing: "the controller is
        gone" is a rule with a timeout, or it is an opinion.
        """
        if self.lease is None:
            raise ValueError(
                "no controller lease is configured, so controller loss has no "
                "defined meaning; set one before relying on this"
            )
        return self.lease.is_lost(last_seen=last_seen, now=now)

    def predicted_state_on_controller_loss(self) -> SwitchState:
        """What the agent's state *should* become if the controller goes away.

        A prediction, deliberately named as one. Use ``apply_controller_loss``
        to record it as the model's state.
        """
        if self.fail_direction is FailDirection.FAIL_CLOSED:
            return SwitchState.STOPPED
        return SwitchState.RUNNING

    def apply_controller_loss(self, *, at: float) -> SwitchState:
        """Record the fail-direction transition, as an unverified state.

        The resulting state is the model's belief, not an observation: nobody
        has confirmed it from outside, so it does not make the switch proven.
        """
        target = self.predicted_state_on_controller_loss()
        self._state = target
        self._losses.append(ControllerLoss(at=at, resulting_state=target))
        return target

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
        # The transition table is a constraint, not a label. Without this check
        # the model would happily issue STOP from RUNNING and produce a state
        # sequence no real switch could follow.
        if plan.from_state is not self._state:
            raise InvalidTransition(
                f"{action.value} is only legal from {plan.from_state.value}, "
                f"but the switch is {self._state.value}"
            )

        self._records.append(
            SwitchRecord(
                action=action,
                requested_by=requested_by,
                at=at,
                plan=plan,
                challenge=self._challenge_factory(),
            )
        )
        self._challenge = self._records[-1].challenge
        self._state = plan.to_state
        return plan

    def record_observation(self, observation: Observation) -> bool:
        """Record what someone reported, and whether it counts.

        An observation counts only when all of these hold:

        - it comes from a **registered observer** and declares itself an
          external verification;
        - it quotes the challenge issued with the request it answers, so a real
          observation of an earlier state cannot be replayed as proof of a later
          one;
        - it names the scope that was actually observed;
        - it does not predate the request it answers.

        Everything else — a self-report, an unbound report, or a test calling
        this directly — is recorded but does not mark the switch proven.

        This is still not a verification mechanism, and does not pretend to be
        one. The package cannot establish that an observer is genuinely outside
        the scope, nor that a challenge was unpredictable; only the caller can,
        by choosing who is registered and by supplying a real challenge source.
        What this does is make that boundary explicit and refuse the cases that
        are known to be untrustworthy, rather than accepting a boolean that any
        caller could set.
        """
        self._observations.append(observation)

        if observation.source is not ObservationSource.EXTERNAL_VERIFIER:
            return False
        if observation.observer not in self.observers:
            return False
        if self._challenge is None or observation.challenge != self._challenge:
            return False
        if observation.scope != self.scope:
            return False
        if self._records and observation.at < self._records[-1].at:
            return False

        self._verified_state = observation.observed_state
        return True
