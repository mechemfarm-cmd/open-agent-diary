"""Preview and import: the user sees real content before anything is written,
and a re-run never double-imports.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from agent_diary import onboarding
from agent_diary.config import default_paths
from agent_diary.onboarding_export import export_hermes_history
from agent_diary.onboarding_state import OnboardingState, SourceState
from agent_diary.orientation import create_run, set_consent

SOURCE_ID = "hermes"


def _make_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0)"
        )
        rows = [
            (1, "s1", "user", "what did we decide about the greenhouse sensor", 1_760_000_000.0),
            (2, "s1", "assistant", "we chose the SHT31 and a 15 minute interval", 1_760_000_030.0),
            (3, "s1", "user", "and where does it log", 1_760_000_060.0),
            (4, "s1", "assistant", "to the diary, under greenhouse", 1_760_000_090.0),
        ]
        for row in rows:
            connection.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
                row,
            )
        connection.execute("INSERT INTO sessions VALUES (?,?,?)", ("s1", "s1", 1_760_000_000.0))
        connection.commit()
    finally:
        connection.close()
    return path


def _state_at(tmp_path, step: str, *, scope: str = "content"):
    """A real state + orientation run at the export-complete stage."""
    db = _make_db(tmp_path / "state.db")
    state = OnboardingState.new(tmp_path / "diary")
    state.step = "discover"
    state.advance("consent")
    created = create_run(
        default_paths(data_root=Path(state.data_root)),
        [{"source_id": SOURCE_ID, "kind": "hermes_history", "locator": str(db), "scope": scope}],
    )
    state.orientation_run_id = created["run_id"]
    set_consent(default_paths(data_root=Path(state.data_root)), created["run_id"], SOURCE_ID, "approved")

    source = SourceState(
        id=SOURCE_ID,
        kind="hermes_sqlite",
        path=str(db),
        detected=True,
        sessions=1,
        decision="approved",
        requested_scope=scope,
    )
    state.sources = [source]
    state.advance("export")

    if scope == "content":
        out = export_hermes_history(
            default_paths(data_root=Path(state.data_root)), created["run_id"], SOURCE_ID
        )
        source.exported_path = out["exported_path"]
        source.format_name = out["format_name"]

    state.advance("preview")
    state.save()
    return state, db


# -- preview ----------------------------------------------------------------


def test_preview_shows_samples_without_writing(tmp_path):
    state, _ = _state_at(tmp_path, "preview")
    out = onboarding.run_preview(state)

    assert out["status"] == "preview_ready"
    assert out["needs_human"] is True
    assert out["counts"][0]["message_count"] == 4
    assert out["samples"][0]
    assert out["samples"][0][0]["content"]
    # Nothing imported on preview.
    assert state.imported_entries == 0


def test_preview_does_not_advance_without_confirm(tmp_path):
    state, _ = _state_at(tmp_path, "preview")
    onboarding.run_preview(state, confirm=False)
    assert state.step == "preview"


def test_preview_confirm_reports_confirmed(tmp_path):
    state, _ = _state_at(tmp_path, "preview")
    out = onboarding.run_preview(state, confirm=True)
    assert out["status"] == "confirmed"
    assert out["needs_human"] is False


def test_preview_states_metadata_only_plainly(tmp_path):
    state, _ = _state_at(tmp_path, "preview", scope="metadata")
    out = onboarding.run_preview(state)
    assert out["metadata_only"]
    assert "Metadata only" in out["metadata_only"][0]["detail"]


# -- import -----------------------------------------------------------------


def test_import_writes_entries_and_reports_counts(tmp_path):
    state, _ = _state_at(tmp_path, "preview")
    state.advance("import")

    out = onboarding.run_import(state)
    assert out["status"] == "imported", out
    assert out["imported_count"] > 0
    assert not out["failures"]
    assert state.imported_entries == out["imported_count"]


def test_import_is_idempotent(tmp_path):
    state, _ = _state_at(tmp_path, "preview")
    state.advance("import")

    first = onboarding.run_import(state)
    assert first["imported_count"] > 0

    second = onboarding.run_import(state)
    assert second["imported_count"] == 0
    assert second["skipped_count"] >= first["imported_count"]


def test_import_never_reports_success_for_a_failure(tmp_path):
    state, _ = _state_at(tmp_path, "preview")
    state.advance("import")
    # Point the locator at something that is not a readable transcript.
    from agent_diary.orientation import set_source_locator

    set_source_locator(
        default_paths(data_root=Path(state.data_root)),
        state.orientation_run_id,
        SOURCE_ID,
        str(tmp_path / "missing.jsonl"),
    )

    out = onboarding.run_import(state)
    assert out["status"] == "partial_failure"
    assert out["failures"]
    assert out["imported_count"] == 0
