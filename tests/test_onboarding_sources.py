"""Discovery must learn facts about history without reading the history.

The load-bearing test here is `test_hermes_detection_never_touches_messages`:
it records every statement the detector executes and fails if anything beyond
the sessions table is queried. That is what keeps "metadata only" true as the
code changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_diary.onboarding_sources import SOURCES, describe, detect, detect_hermes
from agent_diary.onboarding_state import SourceState


def _make_hermes_db(path: Path, sessions: int = 3) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at INTEGER)"
        )
        connection.execute("CREATE TABLE messages (id TEXT, content TEXT)")
        for i in range(sessions):
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (f"s{i}", f"session {i}", 1_760_000_000 + i),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?)", (f"m{i}", "private content")
            )
        connection.commit()
    finally:
        connection.close()
    return path


def test_missing_database_is_not_detected(tmp_path):
    detected, count, oldest, newest = detect_hermes(tmp_path / "nope.db")
    assert detected is False
    assert count == 0
    assert oldest is None and newest is None


def test_real_database_reports_count_and_window(tmp_path):
    db = _make_hermes_db(tmp_path / "state.db", sessions=4)
    detected, count, oldest, newest = detect_hermes(db)
    assert detected is True
    assert count == 4
    assert oldest is not None and newest is not None
    assert oldest < newest


def test_empty_database_is_detected_with_zero_sessions(tmp_path):
    """An empty diary is not a missing diary."""
    db = _make_hermes_db(tmp_path / "state.db", sessions=0)
    detected, count, _, _ = detect_hermes(db)
    assert detected is True
    assert count == 0


def test_non_hermes_database_degrades_quietly(tmp_path):
    path = tmp_path / "other.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE something (x INT)")
    connection.commit()
    connection.close()
    detected, count, _, _ = detect_hermes(path)
    assert detected is False
    assert count == 0


def test_hermes_detection_never_touches_messages(tmp_path, monkeypatch):
    """Record every statement executed and assert only sessions is read."""
    db = _make_hermes_db(tmp_path / "state.db", sessions=2)
    executed: list[str] = []
    real_connect = sqlite3.connect

    class RecordingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            executed.append(sql)
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            return self._inner.close()

        def __getattr__(self, item):
            return getattr(self._inner, item)

    def fake_connect(*args, **kwargs):
        return RecordingConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    detect_hermes(db)

    assert executed, "detector ran no statements — test would be vacuous"
    for statement in executed:
        lowered = statement.lower()
        assert "messages" not in lowered, f"detector read message content: {statement}"
        assert "sessions" in lowered


def test_connection_is_opened_read_only(tmp_path, monkeypatch):
    db = _make_hermes_db(tmp_path / "state.db", sessions=1)
    seen: list[str] = []
    real_connect = sqlite3.connect

    def fake_connect(dsn, *args, **kwargs):
        seen.append(str(dsn))
        return real_connect(dsn, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    detect_hermes(db)
    assert seen and "mode=ro" in seen[0]


def test_detect_hermes_through_registry(tmp_path):
    db = _make_hermes_db(tmp_path / "state.db", sessions=2)
    source = next(s for s in SOURCES if s.id == "hermes")
    state = detect(source, path=db)
    assert state.detected is True
    assert state.sessions == 2
    assert state.consent == "pending"


def test_undetected_source_cannot_be_described_as_present():
    state = SourceState(id="hermes", kind="hermes_sqlite", detected=False)
    assert "not found" in describe(state)


def test_describe_includes_window_for_detected_sessions():
    state = SourceState(
        id="hermes",
        kind="hermes_sqlite",
        detected=True,
        sessions=42,
        oldest="2026-03-02T00:00:00+00:00",
        newest="2026-11-05T00:00:00+00:00",
        path="/home/u/.hermes/state.db",
    )
    text = describe(state)
    assert "42 sessions" in text
    assert "2026-03-02" in text
