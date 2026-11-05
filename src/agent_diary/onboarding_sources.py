"""Where an agent's history might live, discovered without reading it.

Onboarding must be able to say "you have 412 sessions, four months old" without
looking at a single message. Every detector here runs metadata queries only:
for Hermes that means one aggregate query against the sessions table on a
read-only connection, and nothing else.

A missing, locked, or unrecognised store reports "not detected" rather than
failing — a new user with no history is a normal, valid state.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent_diary.onboarding_state import SourceState


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    default_path: str
    description: str


# Deliberately short. Hermes ships proven; the rest are declared so the
# interface is stable, and are detected conservatively (presence only).
SOURCES: tuple[Source, ...] = (
    Source("hermes", "hermes_sqlite", "~/.hermes/state.db", "Hermes agent sessions"),
    Source("openclaw", "openclaw_store", "~/.openclaw", "OpenClaw sessions"),
    Source("telegram", "telegram_export", "", "Telegram export (you supply the path)"),
    Source("jsonl", "canonical_jsonl", "", "A canonical JSONL file (you supply the path)"),
)


def _iso(value) -> str | None:
    """Timestamps arrive as unix seconds or ISO strings depending on the store."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value)
    return text or None


def detect_hermes(path: Path) -> tuple[bool, int, str | None, str | None]:
    """Session count and date range. Reads the sessions table and nothing else."""
    if not path.is_file():
        return (False, 0, None, None)
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return (False, 0, None, None)
    try:
        row = connection.execute(
            "SELECT COUNT(*), MIN(started_at), MAX(started_at) FROM sessions"
        ).fetchone()
    except sqlite3.Error:
        # Unknown schema, or a database that is not a Hermes store.
        return (False, 0, None, None)
    finally:
        connection.close()
    if not row:
        return (False, 0, None, None)
    count = int(row[0] or 0)
    return (count > 0, count, _iso(row[1]), _iso(row[2]))


def detect(source: Source, *, path: str | Path | None = None) -> SourceState:
    """Detect one source. Returns metadata only."""
    location = str(path or source.default_path or "").strip()
    resolved = Path(location).expanduser() if location else None

    if source.kind == "hermes_sqlite":
        detected, sessions, oldest, newest = (
            detect_hermes(resolved) if resolved else (False, 0, None, None)
        )
        return SourceState(
            id=source.id,
            kind=source.kind,
            path=str(resolved or ""),
            detected=detected,
            sessions=sessions,
            oldest=oldest,
            newest=newest,
        )

    # Presence-only detection for everything else: we never open the files.
    detected = bool(resolved and resolved.exists())
    return SourceState(
        id=source.id,
        kind=source.kind,
        path=str(resolved or ""),
        detected=detected,
    )


def discover(*, paths: dict[str, str] | None = None) -> list[SourceState]:
    """Run every detector. User-supplied paths override the defaults."""
    overrides = paths or {}
    return [detect(source, path=overrides.get(source.id)) for source in SOURCES]


def describe(state: SourceState) -> str:
    """One human-readable line per source."""
    if not state.detected:
        return f"{state.id:<10} not found"
    if state.sessions:
        window = ""
        if state.oldest and state.newest:
            window = f"   {state.oldest[:10]} → {state.newest[:10]}"
        return f"{state.id:<10} {state.sessions} sessions{window}   ({state.path})"
    return f"{state.id:<10} found   ({state.path})"
