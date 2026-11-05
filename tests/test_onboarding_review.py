"""Review step: derived facts become knowledge only by explicit approval."""
from __future__ import annotations

from pathlib import Path
import sqlite3

from agent_diary import onboarding
from agent_diary.config import default_paths
from agent_diary.onboarding_export import export_hermes_history
from agent_diary.onboarding_state import OnboardingState, SourceState
from agent_diary.orientation import create_run, set_consent

SOURCE_ID = "hermes"

# Human-only transcript. The generic import format carries no session id, so the
# whole transcript becomes one entry; if it mixed both sides that entry would be
# author_role "mixed", and the candidate generator deliberately reads only
# human-authored entries.
CONVERSATION = [
    (1, "s1", "user", "I prefer the SHT31 sensor for the greenhouse.", 1_760_000_000.0),
    (2, "s1", "user", "We decided to install it next weekend in the north bed.", 1_760_007_200.0),
]


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
        for row in CONVERSATION:
            connection.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?,?,?,?,?)",
                row,
            )
        for session_id in {row[1] for row in CONVERSATION}:
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?)", (session_id, session_id, 1_760_000_000.0)
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _state_at_review(tmp_path):
    db = _make_db(tmp_path / "state.db")
    state = OnboardingState.new(tmp_path / "diary")
    paths = default_paths(data_root=Path(state.data_root))
    created = create_run(
        paths, [{"source_id": SOURCE_ID, "kind": "hermes_history", "locator": str(db), "scope": "content"}]
    )
    state.orientation_run_id = created["run_id"]
    set_consent(paths, created["run_id"], SOURCE_ID, "approved")
    out = export_hermes_history(paths, created["run_id"], SOURCE_ID)

    state.sources = [
        SourceState(
            id=SOURCE_ID,
            kind="hermes_sqlite",
            path=str(db),
            detected=True,
            sessions=1,
            decision="approved",
            requested_scope="content",
            exported_path=out["exported_path"],
            format_name=out["format_name"],
        )
    ]
    state.step = "import"
    onboarding.run_import(state, paths=paths)
    state.advance("review")
    state.save()
    return state, paths


def test_review_generates_candidates_and_never_auto_approves(tmp_path):
    state, _ = _state_at_review(tmp_path)
    out = onboarding.run_review(state)

    assert out["counts"]["proposed"] > 0
    assert out["counts"]["approved"] == 0
    assert out["needs_human"] is True
    assert out["pending"]
    assert out["status"] == "needs_review"


def test_rejecting_everything_is_a_valid_outcome(tmp_path):
    state, _ = _state_at_review(tmp_path)
    out = onboarding.run_review(state, auto_reject_unreviewed=True)

    assert out["status"] == "reviewed"
    assert out["counts"]["approved"] == 0
    assert out["counts"]["pending"] == 0
    assert out["needs_human"] is False


def test_approving_one_candidate_creates_knowledge(tmp_path):
    state, paths = _state_at_review(tmp_path)
    preview = onboarding.run_review(state)
    candidate_id = preview["pending"][0]["candidate_id"]

    out = onboarding.run_review(state, approve=[candidate_id])
    assert out["counts"]["approved"] == 1
    assert candidate_id in out["decided"]

    from agent_diary.orientation_knowledge import list_knowledge

    knowledge = list_knowledge(paths)
    assert any(item.get("candidate_id") == candidate_id for item in knowledge)


def test_review_does_not_advance_itself(tmp_path):
    state, _ = _state_at_review(tmp_path)
    onboarding.run_review(state)
    assert state.step == "review"
