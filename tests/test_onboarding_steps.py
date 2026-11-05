"""Onboarding steps, and the contract an agent relies on.

The most important assertions here are about what onboarding refuses to do:
adopt a data root without a decision, and let an agent grant consent on the
user's behalf.
"""
from __future__ import annotations

import json

import pytest

from agent_diary import onboarding
from agent_diary.onboarding_state import OnboardingState, SourceState


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Isolate the user config and HOME so init never touches the real machine."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENTDIARY_DATA", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    return tmp_path


# -- init -------------------------------------------------------------------


def test_init_requires_an_explicit_choice(sandbox):
    result = onboarding.run_init()
    assert result["needs_human"] is True
    assert result["status"] == "needs_human"
    assert "data_root" not in result
    assert result["changed"] is False


def test_init_writes_config_when_path_given(sandbox):
    target = sandbox / "diary"
    result = onboarding.run_init(target)
    assert result["status"] == "initialized"
    assert result["changed"] is True

    config = sandbox / "cfg" / "agent-diary" / "config.json"
    assert config.exists()
    payload = json.loads(config.read_text())
    assert payload["data_root"] == str(target.resolve())
    assert oct(config.stat().st_mode)[-3:] == "600"


def test_init_is_idempotent(sandbox):
    target = sandbox / "diary"
    first = onboarding.run_init(target)
    assert first["changed"] is True
    second = onboarding.run_init(target)
    assert second["status"] == "already_initialized"
    assert second["changed"] is False


def test_init_accepting_proposal_uses_documented_default(sandbox):
    result = onboarding.run_init(accept_proposal=True)
    assert result["status"] == "initialized"
    assert result["data_root"].startswith(str(sandbox / "home"))


def test_init_creates_state_in_the_data_root(sandbox):
    target = sandbox / "diary"
    onboarding.run_init(target)
    assert (target / "config" / "onboarding.json").exists()


def test_init_advances_state_to_discover(sandbox):
    """Regression: leaving the state at 'init' made an agent repeat a step."""
    target = sandbox / "diary"
    onboarding.run_init(target)
    state = OnboardingState.load(target)
    assert state.step == "discover"
    assert "init" in state.completed
    assert onboarding.next_action(state)["needs_human"] is False


# -- discover ---------------------------------------------------------------


def test_discover_records_sources_and_asks_nothing(sandbox, monkeypatch):
    monkeypatch.setattr(onboarding, "discover", lambda paths=None: [
        SourceState(id="hermes", kind="hermes_sqlite", detected=True, sessions=7),
    ])
    state = OnboardingState.new(sandbox / "diary")
    result = onboarding.run_discover(state)
    assert result["status"] == "discovered"
    assert result["detected"] == ["hermes"]
    assert result["needs_human"] is False
    assert state.sources[0].sessions == 7


# -- consent ----------------------------------------------------------------


def _approved_state(sandbox, detected=True):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "consent"
    state.sources = [
        SourceState(
            id="hermes",
            kind="hermes_sqlite",
            path=str(sandbox / "state.db"),
            detected=detected,
            sessions=3,
        )
    ]
    return state


def test_consent_is_never_inferred_from_silence(sandbox):
    state = _approved_state(sandbox)
    out = onboarding.run_consent(state, {})
    assert out["status"] == "needs_human"
    assert out["awaiting"] == ["hermes"]
    assert state.orientation_run_id is None
    assert state.sources[0].consent == "pending"


def test_consent_with_nothing_detected_is_not_a_prompt(sandbox):
    state = _approved_state(sandbox, detected=False)
    out = onboarding.run_consent(state, {})
    assert out["status"] == "nothing_to_consent"
    assert out["needs_human"] is False


def test_consent_rejects_an_unknown_scope(sandbox):
    state = _approved_state(sandbox)
    out = onboarding.run_consent(state, {"hermes": "everything"})
    assert out["status"] == "invalid_answer"
    assert state.orientation_run_id is None


def test_consent_creates_an_orientation_run_and_records_it(sandbox):
    from agent_diary.config import default_paths

    state = _approved_state(sandbox)
    paths = default_paths(data_root=sandbox / "diary")
    out = onboarding.run_consent(state, {"hermes": "content"}, paths=paths)

    assert out["status"] == "consented"
    assert out["recorded"] == {"hermes": "content"}
    assert state.orientation_run_id
    assert state.sources[0].consent == "content"
    assert paths.data_root.exists()


def test_denied_source_is_recorded_as_denied(sandbox):
    from agent_diary.config import default_paths

    state = _approved_state(sandbox)
    paths = default_paths(data_root=sandbox / "diary")
    out = onboarding.run_consent(state, {"hermes": "denied"}, paths=paths)
    assert out["status"] == "consented"
    assert state.sources[0].consent == "denied"


# -- the agent contract -----------------------------------------------------


def test_next_action_at_init_needs_a_human(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    action = onboarding.next_action(state)
    assert action["step"] == "init"
    assert action["needs_human"] is True
    assert action["command"].startswith("agent-diary init")


def test_next_action_at_discover_is_agent_runnable(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "discover"
    action = onboarding.next_action(state)
    assert action["needs_human"] is False
    assert action["command"] == "agent-diary --json onboard --step discover"


def test_consent_always_needs_a_human_and_never_offers_a_default(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "consent"
    state.sources = [
        SourceState(id="hermes", kind="hermes_sqlite", detected=True, sessions=3)
    ]
    action = onboarding.next_action(state)
    assert action["needs_human"] is True
    assert action["sources"] == ["hermes"]
    assert "never choose a scope" in action["hint"]
    assert "default" not in json.dumps(action).lower()


def test_consent_is_runnable_once_decided(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "consent"
    state.sources = [
        SourceState(
            id="hermes",
            kind="hermes_sqlite",
            detected=True,
            sessions=3,
            decision="rejected",
        )
    ]
    action = onboarding.next_action(state)
    assert action["needs_human"] is False


def test_review_needs_a_human_only_when_candidates_exist(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "review"
    assert onboarding.next_action(state)["needs_human"] is False
    state.candidates = {"proposed": 4, "approved": 0, "rejected": 0}
    action = onboarding.next_action(state)
    assert action["needs_human"] is True
    assert "4" in action["question"]


def test_status_is_json_serialisable(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "discover"
    payload = onboarding.status(state)
    assert json.loads(json.dumps(payload))["data_root"] == state.data_root
    assert payload["action"]["step"] == "discover"


def test_every_step_has_a_purpose(sandbox):
    from agent_diary.onboarding_state import STEPS

    for step in STEPS:
        assert onboarding.STEP_PURPOSE.get(step), f"missing purpose for {step}"
