"""The onboarding state machine must be safe to resume and impossible to skip.

These tests pin the three properties the whole onboarding design depends on:
atomic writes, legal-only transitions, and a consent that cannot be granted for
a source that was never detected.
"""
from __future__ import annotations

import json

import pytest

from agent_diary.onboarding_state import (
    OnboardingError,
    OnboardingState,
    SourceState,
    STEPS,
)


def test_missing_state_starts_at_init(tmp_path):
    state = OnboardingState.load(tmp_path / "diary")
    assert state.step == "init"
    assert state.completed == []


def test_save_and_reload_roundtrips(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    state.sources.append(SourceState(id="hermes", kind="hermes_sqlite", detected=True))
    state.save()

    again = OnboardingState.load(tmp_path / "diary")
    assert again.data_root == state.data_root
    assert [s.id for s in again.sources] == ["hermes"]


def test_state_file_is_private(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_save_leaves_no_temp_file(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    siblings = [p.name for p in path.parent.iterdir()]
    assert siblings == [path.name]


def test_corrupt_state_raises_rather_than_resetting(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(OnboardingError):
        OnboardingState.load(tmp_path / "diary")


def test_unsupported_version_raises(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    payload = json.loads(path.read_text())
    payload["version"] = 99
    path.write_text(json.dumps(payload))
    with pytest.raises(OnboardingError):
        OnboardingState.load(tmp_path / "diary")


def test_legal_transition_advances_and_records(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    state.advance("discover")
    assert state.step == "discover"
    assert state.completed == ["init"]


def test_illegal_transition_raises(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    with pytest.raises(OnboardingError) as exc:
        state.advance("import")
    assert "legal next steps" in str(exc.value)


def test_full_sequence_is_walkable(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    for step in STEPS[1:]:
        state.advance(step)
    assert state.step == "done"
    assert state.completed == list(STEPS[:-1])


def test_terminal_step_has_no_transitions(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    for step in STEPS[1:]:
        state.advance(step)
    with pytest.raises(OnboardingError):
        state.advance("consent")


def test_consent_requires_detection():
    with pytest.raises(OnboardingError):
        SourceState(id="hermes", kind="hermes_sqlite", detected=False, consent="content")


def test_consent_rejects_unknown_value():
    with pytest.raises(OnboardingError):
        SourceState(id="hermes", kind="hermes_sqlite", detected=True, consent="everything")


def test_pending_human_questions_lists_undecided_sources():
    state = OnboardingState.new("/tmp/x")
    state.sources = [
        SourceState(id="hermes", kind="hermes_sqlite", detected=True, consent="pending"),
        SourceState(id="jsonl", kind="canonical_jsonl", detected=True, consent="denied"),
        SourceState(id="telegram", kind="telegram_export", detected=False),
    ]
    assert state.pending_human_questions() == ["hermes"]
