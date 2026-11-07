"""End-to-end: a fresh user goes from nothing to a connected diary.

Runs the real CLI as a subprocess against a scratch HOME, because the thing
being tested is the story a new user experiences, not the functions behind it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _run(args: list[str], home: Path, *, expect_ok: bool = True) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(home / ".hermes")
    env["XDG_CONFIG_HOME"] = str(home / "cfg")
    env.pop("AGENTDIARY_DATA", None)
    env["PYTHONPATH"] = str(REPO / "src")

    completed = subprocess.run(
        [sys.executable, "-m", "agent_diary.cli.main", "--json", *args],
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if expect_ok and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {args}\n"
            f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
        )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def _make_hermes_store(home: Path) -> Path:
    db = home / ".hermes" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)"
        )
        connection.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
            "content TEXT, timestamp REAL, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0)"
        )
        connection.executemany(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
            [
                (1, "s1", "user", "I prefer the north bed for the tomatoes.", 1_760_000_000.0),
                (2, "s1", "user", "We decided to order the seeds on Friday.", 1_760_007_200.0),
            ],
        )
        connection.execute("INSERT INTO sessions VALUES (?,?,?)", ("s1", "s1", 1_760_000_000.0))
        connection.commit()
    finally:
        connection.close()
    return db


def _make_hermes_config(home: Path) -> Path:
    config = home / ".hermes" / "config.yaml"
    config.write_text("# scratch config\nmodel: test-model\n")
    return config


def test_new_user_reaches_a_connected_diary(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _make_hermes_store(home)
    config = _make_hermes_config(home)
    diary = home / "diary"

    # 1. init — a deliberate choice, not a default.
    init = _run(["init", "--data-root", str(diary)], home)
    assert init["status"] == "initialized"
    assert init["doctor"]["error_count"] == 0

    # 2. discover — metadata only.
    found = _run(["onboard", "--step", "discover"], home)
    hermes = next(s for s in found["sources"] if s["id"] == "hermes")
    assert hermes["detected"] is True
    assert hermes["sessions"] == 1

    # 3. consent — the user grants content.
    consent = _run(["onboard", "--step", "consent", "--answer", '{"hermes":"content"}'], home)
    assert consent["status"] == "consented"

    # 4. export.
    exported = _run(["onboard", "--step", "export"], home)
    assert exported["status"] == "exported"
    assert exported["messages_exported"] == 2

    # 5. preview — must not advance without confirmation.
    preview = _run(["onboard", "--step", "preview"], home)
    assert preview["needs_human"] is True
    assert _run(["onboard", "--status"], home)["step"] == "preview"

    confirmed = _run(["onboard", "--step", "preview", "--confirm"], home)
    assert confirmed["status"] == "confirmed"

    # 6. import.
    imported = _run(["onboard", "--step", "import"], home)
    assert imported["status"] == "imported", imported
    assert imported["imported_count"] > 0

    # 7. review — decline all derived knowledge, a legitimate ending.
    reviewed = _run(["onboard", "--step", "review", "--auto-reject-unreviewed"], home)
    assert reviewed["status"] == "reviewed"
    assert reviewed["counts"]["approved"] == 0

    # 8. connect — writes the agent config, with a backup.
    connected = _run(["onboard", "--step", "connect"], home)
    assert connected["status"] == "connected", connected
    assert Path(connected["backup"]).exists()
    assert "agent-diary" in config.read_text()
    assert "# scratch config" in config.read_text()  # comments survived

    final = _run(["onboard", "--status"], home)
    assert final["step"] == "done"
    assert final["agent_connected"] is True
    assert final["imported_entries"] > 0
    assert final["action"]["command"] is None

    # The diary actually contains what was imported.
    entry_files = list((diary / "entries").rglob("*.json"))
    assert entry_files, "no entries written"


def test_resume_after_interruption(tmp_path):
    """Stopping mid-flow and asking for status resumes at the right step."""
    home = tmp_path / "home"
    home.mkdir()
    _make_hermes_store(home)
    _make_hermes_config(home)

    _run(["init", "--data-root", str(home / "diary")], home)
    _run(["onboard", "--step", "discover"], home)

    status = _run(["onboard", "--status"], home)
    assert status["step"] == "consent"
    assert status["action"]["needs_human"] is True
    assert "consent" in status["action"]["command"]
    assert "--json" in status["action"]["command"]


def test_out_of_order_step_is_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _make_hermes_store(home)
    _make_hermes_config(home)
    _run(["init", "--data-root", str(home / "diary")], home)

    out = _run(["onboard", "--step", "import"], home, expect_ok=False)
    assert out["error"] == "out_of_order"
    assert out["action"]["step"] == "discover"
