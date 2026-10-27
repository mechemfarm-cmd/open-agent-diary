"""Append-only measurements for memory retrieval calls.

Telemetry is deliberately best-effort and stores no raw query text. It exists to
measure context and provider-cost pressure before changing recall behavior.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS retrieval_events (
  event_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  route TEXT NOT NULL,
  query_hash TEXT NOT NULL,
  query_chars INTEGER NOT NULL DEFAULT 0,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  result_count INTEGER NOT NULL DEFAULT 0,
  returned_chars INTEGER NOT NULL DEFAULT 0,
  estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
  provider_input_tokens INTEGER,
  provider_cost REAL,
  latency_ms REAL NOT NULL DEFAULT 0,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_retrieval_events_created
  ON retrieval_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retrieval_events_route
  ON retrieval_events(route, created_at DESC);
"""


def ensure_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def estimate_tokens(text_or_chars: str | int) -> int:
    """Return a conservative deterministic estimate: four characters per token."""
    chars = len(text_or_chars) if isinstance(text_or_chars, str) else int(text_or_chars)
    if chars < 0:
        raise ValueError("character count must not be negative")
    return (chars + 3) // 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def record_event(
    db_path: Path,
    *,
    route: str,
    query: str = "",
    candidate_count: int = 0,
    result_count: int = 0,
    returned_chars: int = 0,
    latency_ms: float = 0.0,
    provider_input_tokens: int | None = None,
    provider_cost: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Record one retrieval measurement and return its unique event id."""
    values = {
        "candidate_count": candidate_count,
        "result_count": result_count,
        "returned_chars": returned_chars,
    }
    if any(int(value) < 0 for value in values.values()):
        raise ValueError("retrieval counts must not be negative")
    if latency_ms < 0:
        raise ValueError("latency_ms must not be negative")
    if provider_input_tokens is not None and provider_input_tokens < 0:
        raise ValueError("provider_input_tokens must not be negative")
    if provider_cost is not None and provider_cost < 0:
        raise ValueError("provider_cost must not be negative")

    ensure_schema(db_path)
    event_id = f"retrieval_{uuid.uuid4().hex}"
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.execute(
            """
            INSERT INTO retrieval_events (
              event_id, created_at, route, query_hash, query_chars,
              candidate_count, result_count, returned_chars,
              estimated_input_tokens, provider_input_tokens, provider_cost,
              latency_ms, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                _now(),
                str(route),
                _query_hash(str(query)),
                len(str(query)),
                int(candidate_count),
                int(result_count),
                int(returned_chars),
                estimate_tokens(returned_chars),
                provider_input_tokens,
                provider_cost,
                float(latency_ms),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        conn.commit()
    return event_id


def list_events(db_path: Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Read recent measurements for reports and tests; never mutates usage."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    ensure_schema(db_path)
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM retrieval_events ORDER BY created_at DESC, event_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def elapsed_ms(started: float) -> float:
    """Return monotonic elapsed milliseconds, rounded for stable telemetry."""
    return round(max(0.0, time.monotonic() - started) * 1000.0, 3)
