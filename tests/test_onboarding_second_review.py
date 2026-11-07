"""Second-review regressions.

Each test corresponds to a finding in the second architecture review, so a
regression names itself rather than surfacing as a vague failure elsewhere.
"""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3

import pytest

from agent_diary import onboarding
from agent_diary.cli.transcript_adapter import adapt_session_export
from agent_diary.config import default_paths
from agent_diary.onboarding_connect import connect_hermes
from agent_diary.onboarding_export import export_hermes_history
from agent_diary.onboarding_state import OnboardingError, OnboardingState, SourceState
from agent_diary.orientation import create_run, set_consent

SOURCE_ID = "hermes"


# --------------------------------------------------------------------------
# 1. connect: parseable but mismatched existing entry
# --------------------------------------------------------------------------


def test_connect_refuses_parseable_mismatch_without_duplicating(tmp_path):
    config = tmp_path / "config.yaml"
    original = (
        "model: x\n"
        "mcp_servers:\n"
        "  agent-diary:\n"
        "    command: node\n"
        "    args:\n"
        "      - old-wrapper.js\n"
    )
    config.write_text(original)

    out = connect_hermes(config_path=config)

    assert out["status"] == "existing_entry_mismatch"
    assert out["needs_human"] is True
    assert out["changed"] is False
    assert config.read_text() == original  # nothing written
    assert config.read_text().count("agent-diary:") == 1  # no duplicate key


def test_connect_still_inserts_when_the_key_is_absent(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("mcp_servers:\n  other:\n    command: node\n    args:\n      - x.js\n")

    out = connect_hermes(config_path=config)
    assert out["status"] == "connected"
    assert config.read_text().count("agent-diary:") == 1


# --------------------------------------------------------------------------
# 2. export -> import -> new session -> export -> import
# --------------------------------------------------------------------------


def _make_db(path: Path, rows: list[tuple]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)")
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0)"
        )
        for row in rows:
            connection.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
                row,
            )
        for session_id in {r[1] for r in rows}:
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?)", (session_id, session_id, 1_760_000_000.0)
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _pipeline(tmp_path):
    db = _make_db(
        tmp_path / "state.db",
        [
            (1, "s1", "user", "I prefer the north bed.", 1_760_000_000.0),
            (2, "s1", "user", "We decided to order seeds.", 1_760_007_200.0),
        ],
    )
    state = OnboardingState.new(tmp_path / "diary")
    paths = default_paths(data_root=Path(state.data_root))
    created = create_run(
        paths, [{"source_id": SOURCE_ID, "kind": "hermes_history", "locator": str(db), "scope": "content"}]
    )
    state.orientation_run_id = created["run_id"]
    set_consent(paths, created["run_id"], SOURCE_ID, "approved")
    state.sources = [
        SourceState(
            id=SOURCE_ID,
            kind="hermes_sqlite",
            path=str(db),
            detected=True,
            sessions=1,
            decision="approved",
            requested_scope="content",
        )
    ]
    return state, paths, db


def test_later_export_generation_is_imported(tmp_path):
    """The fixed import id must not swallow later sessions."""
    state, paths, db = _pipeline(tmp_path)

    first_export = export_hermes_history(paths, state.orientation_run_id, SOURCE_ID)
    assert first_export["status"] == "exported"
    state.sources[0].exported_path = first_export["exported_path"]
    state.sources[0].format_name = first_export["format_name"]

    state.step = "preview"
    state.completed = ["init", "discover", "consent", "export"]
    state.save()
    state.advance("import")
    first_import = onboarding.run_import(state, paths=paths)
    assert first_import["imported_count"] > 0

    # A new session appears.
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
        (99, "s2", "user", "We decided to buy a new hose.", 1_761_000_000.0),
    )
    connection.execute("INSERT INTO sessions VALUES (?,?,?)", ("s2", "s2", 1_761_000_000.0))
    connection.commit()
    connection.close()

    second_export = export_hermes_history(paths, state.orientation_run_id, SOURCE_ID)
    assert second_export["status"] == "exported"
    assert second_export["sessions_exported"] == 1
    state.sources[0].exported_path = second_export["exported_path"]

    second_import = onboarding.run_import(state, paths=paths)
    assert second_import["imported_count"] > 0, "later export generation was not imported"


def test_import_rerun_keeps_a_truthful_cumulative_count(tmp_path):
    state, paths, _ = _pipeline(tmp_path)
    state.advance("discover")
    state.advance("consent")
    state.advance("export")
    out = export_hermes_history(paths, state.orientation_run_id, SOURCE_ID)
    state.sources[0].exported_path = out["exported_path"]
    state.sources[0].format_name = out["format_name"]
    state.advance("preview")
    state.advance("import")

    first = onboarding.run_import(state, paths=paths)
    assert state.imported_entries == first["imported_count"] > 0

    second = onboarding.run_import(state, paths=paths)
    assert second["imported_count"] == 0
    assert state.imported_entries == first["imported_count"], "status lost its count on rerun"


# --------------------------------------------------------------------------
# 3. state validation: skipped intermediate steps
# --------------------------------------------------------------------------


def test_state_at_preview_with_missing_intermediate_steps_is_rejected(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    path = state.save()
    payload = json.loads(path.read_text())
    payload["step"] = "preview"
    payload["completed"] = ["init"]  # discover, consent, export missing
    path.write_text(json.dumps(payload))

    with pytest.raises(OnboardingError) as exc:
        OnboardingState.load(tmp_path / "diary")
    assert "does not match the steps preceding" in str(exc.value)


# --------------------------------------------------------------------------
# 4. no-source review must not create a None run
# --------------------------------------------------------------------------


def test_review_without_a_run_creates_no_artifact(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    state.advance("discover")
    state.advance("consent")
    state.advance("export")
    state.advance("preview")
    state.advance("import")
    state.advance("review")
    state.save()

    out = onboarding.run_review(state)
    assert out["status"] == "nothing_to_review"
    assert out["needs_human"] is False

    stray = list(Path(state.data_root).rglob("*None*"))
    assert stray == [], stray


# --------------------------------------------------------------------------
# 5. registry identity
# --------------------------------------------------------------------------


def test_registry_id_kind_mismatch_is_a_controlled_error(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    state.sources = [SourceState(id="openclaw", kind="hermes_sqlite", detected=False)]
    with pytest.raises(OnboardingError) as exc:
        state.validate()
    assert "registry declares" in str(exc.value)


def test_unknown_source_id_is_a_controlled_error(tmp_path):
    state = OnboardingState.new(tmp_path / "diary")
    state.sources = [SourceState(id="mystery", kind="hermes_sqlite", detected=False)]
    with pytest.raises(OnboardingError) as exc:
        state.validate()
    assert "unknown onboarding source id" in str(exc.value)


# --------------------------------------------------------------------------
# 6. connect must not complete onboarding when a human is needed
# --------------------------------------------------------------------------


def test_connect_without_a_config_reports_needs_human(tmp_path):
    out = connect_hermes(config_path=tmp_path / "absent.yaml")
    assert out["status"] == "config_missing"
    assert out["needs_human"] is True
    assert not (tmp_path / "absent.yaml").exists()


def test_run_connect_without_an_agent_does_not_mark_connected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
    state = OnboardingState.new(tmp_path / "diary")
    out = onboarding.run_connect(state)
    assert out["needs_human"] is True
    assert state.agent_connected is False
