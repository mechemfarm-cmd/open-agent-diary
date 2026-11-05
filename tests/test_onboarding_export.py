"""Export step: Hermes database to readable transcript.

The five cases from the plan, plus two the real database forced (compacted rows
and text-free messages), each of which would otherwise corrupt or bloat an
import.
"""
from __future__ import annotations

from pathlib import Path
import json
import sqlite3

import pytest

from agent_diary.cli.transcript_adapter import adapt_session_export
from agent_diary.config import default_paths
from agent_diary.onboarding_export import EXPORT_FORMAT, ExportRefused, export_hermes_history
from agent_diary.orientation import create_run, load_run, set_consent

SOURCE_ID = "hermes"


def _make_db(path: Path, *, messages: list[tuple] | None = None) -> Path:
    """Hermes-shaped database. Each message row is (id, session, role, content, ts)."""
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
        rows = messages or [
            (1, "s1", "user", "hello from the user", 1_760_000_000.0),
            (2, "s1", "assistant", "hello from the agent", 1_760_000_001.0),
            (3, "s2", "user", "another session", 1_760_000_100.0),
        ]
        for row in rows:
            connection.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
                row,
            )
        for session_id in {r[1] for r in rows}:
            connection.execute(
                "INSERT INTO sessions (id, title, started_at) VALUES (?,?,?)",
                (session_id, session_id, 1_760_000_000.0),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _run_with_consent(tmp_path, db: Path, *, scope: str = "content", decision: str | None = "approved"):
    paths = default_paths(data_root=tmp_path / "diary")
    created = create_run(
        paths,
        [{"source_id": SOURCE_ID, "kind": "hermes_history", "locator": str(db), "scope": scope}],
    )
    run_id = created["run_id"]
    if decision:
        set_consent(paths, run_id, SOURCE_ID, decision)
    return paths, run_id


# -- consent gate ------------------------------------------------------------


def test_export_refuses_without_consent(tmp_path):
    db = _make_db(tmp_path / "state.db")
    paths, run_id = _run_with_consent(tmp_path, db, decision=None)
    with pytest.raises(ExportRefused) as exc:
        export_hermes_history(paths, run_id, SOURCE_ID)
    assert "approved consent" in str(exc.value)


def test_export_refuses_with_metadata_scope(tmp_path):
    """Metadata consent must not leak content."""
    db = _make_db(tmp_path / "state.db")
    paths, run_id = _run_with_consent(tmp_path, db, scope="metadata")
    with pytest.raises(ExportRefused):
        export_hermes_history(paths, run_id, SOURCE_ID)
    assert not list(paths.imports_dir.glob("hermes-export-*.jsonl"))


def test_export_refuses_when_consent_was_rejected(tmp_path):
    db = _make_db(tmp_path / "state.db")
    paths, run_id = _run_with_consent(tmp_path, db, decision="rejected")
    with pytest.raises(ExportRefused):
        export_hermes_history(paths, run_id, SOURCE_ID)


# -- the happy path ----------------------------------------------------------


def test_export_produces_a_file_the_adapter_accepts(tmp_path):
    db = _make_db(tmp_path / "state.db")
    paths, run_id = _run_with_consent(tmp_path, db)

    out = export_hermes_history(paths, run_id, SOURCE_ID)
    assert out["status"] == "exported"
    assert out["messages_exported"] == 3
    assert out["sessions_exported"] == 2

    export_path = Path(out["exported_path"])
    assert export_path.is_file()

    # The exact call preview and import will make.
    adapted = adapt_session_export(
        input_path=export_path,
        output_path=tmp_path / "canonical.jsonl",
        format_name=EXPORT_FORMAT,
    )
    assert adapted["message_count"] == 3

    # Locator now points at the transcript, and the format is recorded for
    # preview/import to use.
    run = load_run(paths, run_id)
    source = next(s for s in run["sources"] if s["source_id"] == SOURCE_ID)
    assert source["locator"] == str(export_path)
    assert source["metadata"]["format_name"] == EXPORT_FORMAT
    # Consent itself is untouched.
    assert source["consent"] == "approved"


def test_exported_rows_carry_the_required_fields(tmp_path):
    db = _make_db(tmp_path / "state.db")
    paths, run_id = _run_with_consent(tmp_path, db)
    out = export_hermes_history(paths, run_id, SOURCE_ID)

    rows = [json.loads(line) for line in Path(out["exported_path"]).read_text().splitlines()]
    assert len(rows) == 3
    for row in rows:
        assert row["message_id"]
        assert row["created_at"]
        assert row["role"] in {"user", "assistant", "tool"}
        assert row["content"]
    # Timestamps are ISO, not raw epoch — the adapter parses them.
    assert "T" in rows[0]["created_at"]


# -- idempotence -------------------------------------------------------------


def test_second_run_exports_only_new_sessions(tmp_path):
    db = _make_db(tmp_path / "state.db")
    paths, run_id = _run_with_consent(tmp_path, db)

    first = export_hermes_history(paths, run_id, SOURCE_ID)
    assert first["sessions_exported"] == 2

    second = export_hermes_history(paths, run_id, SOURCE_ID)
    assert second["status"] == "nothing_new"
    assert second["sessions_exported"] == 0
    assert second["messages_exported"] == 0
    # The locator is not repointed at a file that does not exist.
    assert Path(second["exported_path"]).is_file()

    # A new session appears; only that one is exported.
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
        (99, "s3", "user", "brand new", 1_760_000_500.0),
    )
    connection.execute("INSERT INTO sessions VALUES (?,?,?)", ("s3", "s3", 1_760_000_500.0))
    connection.commit()
    connection.close()

    third = export_hermes_history(paths, run_id, SOURCE_ID)
    assert third["status"] == "exported"
    assert third["sessions_exported"] == 1
    assert third["messages_exported"] == 1


def test_deleted_session_does_not_break_a_rerun(tmp_path):
    db = _make_db(tmp_path / "state.db")
    paths, run_id = _run_with_consent(tmp_path, db)
    export_hermes_history(paths, run_id, SOURCE_ID)

    connection = sqlite3.connect(db)
    connection.execute("DELETE FROM messages WHERE session_id = ?", ("s2",))
    connection.execute("DELETE FROM sessions WHERE id = ?", ("s2",))
    connection.commit()
    connection.close()

    out = export_hermes_history(paths, run_id, SOURCE_ID)
    assert out["status"] == "nothing_new"


# -- filters the real database required --------------------------------------


def test_compacted_and_inactive_rows_are_excluded(tmp_path):
    """Otherwise a summary and the original it replaced both get imported."""
    db = _make_db(
        tmp_path / "state.db",
        messages=[
            (1, "s1", "user", "live message", 1_760_000_000.0),
            (2, "s1", "user", "compacted away", 1_760_000_001.0),
            (3, "s1", "user", "deactivated", 1_760_000_002.0),
        ],
    )
    connection = sqlite3.connect(db)
    connection.execute("UPDATE messages SET compacted = 1 WHERE id = 2")
    connection.execute("UPDATE messages SET active = 0 WHERE id = 3")
    connection.commit()
    connection.close()

    paths, run_id = _run_with_consent(tmp_path, db)
    out = export_hermes_history(paths, run_id, SOURCE_ID)

    assert out["messages_exported"] == 1
    rows = [json.loads(x) for x in Path(out["exported_path"]).read_text().splitlines()]
    assert rows[0]["content"] == "live message"


def test_text_free_messages_are_skipped_and_counted(tmp_path):
    """Tool-call-only turns have no text; the transcript format has nowhere to put them."""
    db = _make_db(
        tmp_path / "state.db",
        messages=[
            (1, "s1", "user", "a question", 1_760_000_000.0),
            (2, "s1", "assistant", "", 1_760_000_001.0),
            (3, "s1", "assistant", None, 1_760_000_002.0),
            (4, "s1", "tool", "tool output", 1_760_000_003.0),
        ],
    )
    paths, run_id = _run_with_consent(tmp_path, db)
    out = export_hermes_history(paths, run_id, SOURCE_ID)

    assert out["messages_exported"] == 2
    assert out["messages_skipped_empty"] == 2


def test_session_meta_rows_are_not_exported(tmp_path):
    db = _make_db(
        tmp_path / "state.db",
        messages=[
            (1, "s1", "user", "real", 1_760_000_000.0),
            (2, "s1", "session_meta", "internal bookkeeping", 1_760_000_001.0),
        ],
    )
    paths, run_id = _run_with_consent(tmp_path, db)
    out = export_hermes_history(paths, run_id, SOURCE_ID)
    assert out["messages_exported"] == 1


def test_source_database_is_not_modified(tmp_path):
    db = _make_db(tmp_path / "state.db")
    before = db.read_bytes()
    paths, run_id = _run_with_consent(tmp_path, db)
    export_hermes_history(paths, run_id, SOURCE_ID)
    assert db.read_bytes() == before
