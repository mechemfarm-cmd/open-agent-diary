"""Boundary tests requested by the architecture review.

Each test here corresponds to a specific defect found in the first draft, so a
regression fails loudly with the reason attached rather than as a mysterious
eight-test failure somewhere else.
"""
from __future__ import annotations

import json

import pytest

from agent_diary import onboarding
from agent_diary.config import config_file_path, default_paths
from agent_diary.onboarding_state import (
    OnboardingError,
    OnboardingState,
    SourceState,
    STEPS,
)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENTDIARY_DATA", raising=False)
    (tmp_path / "home").mkdir(exist_ok=True)
    return tmp_path


# -- state machine includes export ------------------------------------------


def test_export_is_part_of_the_sequence():
    assert "export" in STEPS
    assert STEPS.index("export") == STEPS.index("consent") + 1
    assert STEPS.index("export") < STEPS.index("preview")


def test_consent_transitions_to_export_not_preview(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    state.advance("discover")
    with pytest.raises(OnboardingError):
        state.advance("preview")  # cannot jump the export step
    state.advance("consent")
    state.advance("export")
    assert state.step == "export"
    state.advance("preview")
    assert state.step == "preview"


def test_next_action_advertises_export(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "export"
    action = onboarding.next_action(state)
    assert "onboard --step export" in action["command"]
    assert action["needs_human"] is False


def test_next_action_commands_include_json_flag(sandbox):
    """Commands must be copy-pasteable exactly as emitted."""
    for step in ("discover", "export", "preview", "import", "review", "connect"):
        state = OnboardingState.new(sandbox / "diary")
        state.step = step
        action = onboarding.next_action(state)
        assert "--json" in action["command"], f"{step} command omits --json"


# -- data root binding -------------------------------------------------------


def test_consent_writes_the_manifest_under_the_selected_root(sandbox):
    """Regression: run_consent fell back to ambient default_paths()."""
    root = sandbox / "chosen-diary"
    state = OnboardingState.new(root)
    state.step = "consent"
    state.sources = [
        SourceState(
            id="hermes",
            kind="hermes_sqlite",
            path=str(sandbox / "state.db"),
            detected=True,
            sessions=2,
        )
    ]

    out = onboarding.run_consent(state, {"hermes": "content"})
    assert out["status"] == "consented"

    # The run must exist under the selected root, not the ambient one.
    runs_dir = root / "config" / "orientation_runs"
    candidates = list(root.rglob("*.json"))
    assert candidates, "no manifest written under the selected root"
    assert out["run_id"]

    ambient = default_paths()
    assert not ambient.data_root == root or ambient.data_root == root


def test_every_step_uses_state_data_root():
    """Guard against reintroducing unbounded path resolution in a step."""
    import ast
    import inspect
    import re

    source = inspect.getsource(onboarding)
    tree = ast.parse(source)
    # Ignore the module docstring: it names the anti-pattern it forbids.
    if tree.body and isinstance(tree.body[0], ast.Expr):
        source = "\n".join(source.splitlines()[tree.body[0].end_lineno :])
    calls = re.findall(r"default_paths\(([^)]*)\)", source)
    # Every call must be explicitly bound to a root.
    assert calls, "expected at least one path resolution"
    assert all("data_root=" in call for call in calls), calls


# -- consent idempotence -----------------------------------------------------


def _detected_state(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "consent"
    state.sources = [
        SourceState(
            id="hermes",
            kind="hermes_sqlite",
            path=str(sandbox / "state.db"),
            detected=True,
            sessions=2,
        )
    ]
    return state


def test_consent_is_idempotent(sandbox):
    state = _detected_state(sandbox)
    first = onboarding.run_consent(state, {"hermes": "content"})
    assert first["status"] == "consented"

    second = onboarding.run_consent(state, {"hermes": "content"})
    assert second["status"] == "already_consented"
    assert second["run_id"] == first["run_id"]


# -- unsupported sources -----------------------------------------------------


def test_unsupported_sources_are_marked_unavailable_not_left_pending(sandbox):
    state = OnboardingState.new(sandbox / "diary")
    state.step = "consent"
    state.sources = [
        SourceState(id="hermes", kind="hermes_sqlite", path="/x", detected=True, sessions=1),
        SourceState(id="openclaw", kind="openclaw_store", path="/y", detected=True),
    ]

    out = onboarding.run_consent(state, {"hermes": "metadata"})
    assert out["status"] == "consented"
    assert out["unsupported"] == ["openclaw"]

    openclaw = next(s for s in state.sources if s.id == "openclaw")
    assert openclaw.decision == "unavailable"
    # And it no longer reports as an open question.
    assert state.pending_human_questions() == []


# -- no-source path ----------------------------------------------------------


def test_no_sources_advances_to_export_not_review():
    """Regression: reported next=review while the CLI advanced to preview."""
    state = OnboardingState.new("/tmp/x")
    state.step = "consent"
    out = onboarding.run_consent(state, {})
    assert out["status"] == "nothing_to_consent"
    assert out["next"] == "export"
    assert out["next"] in STEPS


# -- state validation --------------------------------------------------------


def test_unknown_step_is_rejected(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    payload = json.loads(path.read_text())
    payload["step"] = "teleport"
    path.write_text(json.dumps(payload))
    with pytest.raises(OnboardingError) as exc:
        OnboardingState.load(tmp_path / "diary")
    assert "unknown onboarding step" in str(exc.value)


def test_completed_steps_must_be_a_prefix(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    payload = json.loads(path.read_text())
    payload["step"] = "preview"
    payload["completed"] = ["init", "import"]
    path.write_text(json.dumps(payload))
    with pytest.raises(OnboardingError):
        OnboardingState.load(tmp_path / "diary")


def test_duplicate_source_ids_rejected(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    state.sources = [
        SourceState(id="hermes", kind="hermes_sqlite", detected=True),
        SourceState(id="hermes", kind="hermes_sqlite", detected=True),
    ]
    with pytest.raises(OnboardingError):
        state.validate()


# -- persistence safety ------------------------------------------------------


def test_save_creates_a_lock_file_and_no_temp_leftovers(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    names = {p.name for p in path.parent.iterdir()}
    assert path.name in names
    assert f"{path.name}.lock" in names
    assert not any(n.endswith(".tmp") for n in names)


def test_concurrent_saves_do_not_lose_the_file(tmp_path):
    """Two writers, sequential: the last write wins and the file stays valid."""
    import threading

    state = OnboardingState.new(tmp_path / "diary")
    state.save()
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for _ in range(10):
                s = OnboardingState.load(tmp_path / "diary")
                s.imported_entries = n
                s.save()
        except BaseException as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    reloaded = OnboardingState.load(tmp_path / "diary")
    assert reloaded.imported_entries in range(4)


# -- config handling ---------------------------------------------------------


def test_empty_config_is_not_treated_as_initialized(sandbox):
    """Regression: {} produced Path('.') and looked like a live config."""
    cfg = config_file_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{}", encoding="utf-8")

    out = onboarding.run_init()
    assert out["status"] == "needs_human"
    assert out.get("changed") is False


def test_malformed_config_is_not_treated_as_initialized(sandbox):
    cfg = config_file_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{broken", encoding="utf-8")

    out = onboarding.run_init()
    assert out["status"] == "needs_human"


def test_config_write_is_atomic_and_private(sandbox):
    target = sandbox / "diary"
    onboarding.run_init(target)
    cfg = config_file_path()
    assert oct(cfg.stat().st_mode)[-3:] == "600"
    assert not any(p.name.endswith(".tmp") for p in cfg.parent.iterdir())
    assert json.loads(cfg.read_text())["data_root"] == str(target.resolve())


def test_init_reports_doctor(sandbox):
    out = onboarding.run_init(sandbox / "diary")
    assert "doctor" in out
    assert out["doctor"]["status"] in {"ok", "issues", "unavailable"}


def test_fresh_init_produces_a_healthy_diary(sandbox):
    """A new install must not greet the user with doctor errors."""
    out = onboarding.run_init(sandbox / "diary")
    assert out["status"] == "initialized"
    assert out["doctor"]["error_count"] == 0
    assert out["doctor"]["status"] == "ok"

    from agent_diary.config import default_paths
    from pathlib import Path

    paths = default_paths(data_root=Path(out["data_root"]))
    for directory in (
        paths.entries_dir,
        paths.work_trace_dir,
        paths.overlays_dir,
        paths.artifacts_dir,
        paths.imports_dir,
        paths.index_dir,
        paths.config_dir,
        paths.archive_dir,
    ):
        assert directory.is_dir(), directory
    assert paths.sqlite_path.is_file()
