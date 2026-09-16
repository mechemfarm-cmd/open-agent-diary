"""Spend and replenish: usage accounting for belief recall.

Task 6 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

The model:

* **Surfacing a fact SPENDS attention.** Being shown is an expenditure, not an
  achievement. Rewarding it made recall self-reinforcing: whatever surfaced
  ranked higher, so it surfaced again.
* **Being acted on EARNS it back — one act-on cancels one surfacing.**

The single quantity that drives ranking is ``surfaced_since_credit``:
**outstanding unearned exposure**. It goes up when a fact is surfaced and down
when it is acted on. ``acted_on_count`` is kept as a statistic but is NOT used in
scoring.

Two bugs found in external review and fixed here, both worth stating because they
are easy to reintroduce:

1. Acted-on used to CLEAR the outstanding pressure to zero, so a single act-on
   erased a hundred surfacings. It now decrements, so the debt is paid down
   rather than forgiven.
2. Lifetime ``acted_on_count`` used to offset pressure in the score. That let a
   fact that was useful once immunise itself against all future unearned
   exposure. Scoring now reads ONLY the outstanding pressure, which carries no
   memory of past credit.

Because a fact that has never been surfaced has zero outstanding pressure, it
sits at full value and rises past facts shown repeatedly without earning. That is
where rotation comes from — but see the note on strength in ``ranking.salience``.

Stored in its own table rather than JSON in ``metadata``: these counters are
written on every recall, which makes a read-modify-write race likely rather than
theoretical. Every write runs in one immediate transaction, and the credit path
uses a single conditional statement so two workers cannot double-credit.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from agent_diary.analytics.signal_policy import (
    AGENT_DERIVED,
    INFERRED,
    UNKNOWN,
)

SCHEMA = """
create table if not exists belief_usage (
    fact_id               text primary key,
    surfaced_count        integer not null default 0,
    surfaced_since_credit integer not null default 0,
    last_surfaced_at      text,
    acted_on_count        integer not null default 0,
    last_acted_on_at      text
)
"""

EMPTY = {
    "surfaced_count": 0,
    "surfaced_since_credit": 0,
    "last_surfaced_at": None,
    "acted_on_count": 0,
    "last_acted_on_at": None,
}

#: One act-on cancels one surfacing. Deliberately 1:1 — a fact shown a hundred
#: times that paid off once has ninety-nine outstanding.
CREDIT_PER_ACT = 1

#: Provenance classes that must never earn credit: they are the system talking to
#: itself. Crediting them would let the agent reward itself for its own output.
NOT_CREDITABLE_PROVENANCE = frozenset({AGENT_DERIVED, INFERRED, UNKNOWN})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_instant(value) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Returns None when it cannot be parsed. Every timestamp this module stores is
    normalized to UTC first, which is what keeps plain string comparison in SQL
    correct. Comparing raw timestamps as strings is wrong across mixed ISO forms
    or offsets: "2026-09-15T10:00:00+02:00" sorts after
    "2026-09-15T09:30:00+00:00" even though it is the EARLIER instant. That would
    credit evidence that is older than the surface it is meant to corroborate.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _instant(value) -> str | None:
    """Normalized UTC string, or None when unparseable."""
    dt = _parse_instant(value)
    return dt.isoformat() if dt is not None else None


def _write_time(value) -> str:
    """Normalize a caller-supplied write time.

    Unparseable input raises rather than silently becoming 'now': a wrong stored
    timestamp corrupts every later freshness comparison for that fact.
    """
    if value is None:
        return _now()
    normalized = _instant(value)
    if normalized is None:
        raise ValueError(f"unparseable timestamp: {value!r}")
    return normalized


def ensure_schema(db_path: Path) -> None:
    """Idempotent. Kept local so existing databases pick the table up on first
    use — the live diary predates this table and nothing bootstraps it for us."""
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _existing_fact_ids(conn, ids: list[str]) -> list[str]:
    """Filter to facts that actually exist.

    Usage rows are keyed by fact_id with no foreign key, so without this a typo
    would silently accumulate orphan rows. One query per call, not per fact.
    """
    found: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        found.update(
            row[0] for row in conn.execute(
                f"select fact_id from graph_facts where fact_id in ({marks})", chunk
            ).fetchall()
        )
    return [fid for fid in ids if fid in found]


def get_usage(db_path: Path, fact_id: str) -> dict:
    """Usage for one fact. Unknown facts report zeros rather than erroring."""
    ensure_schema(db_path)
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select * from belief_usage where fact_id = ?", (fact_id,)
        ).fetchone()
    return dict(row) if row else dict(EMPTY)


def get_usage_map(db_path: Path, fact_ids) -> dict[str, dict]:
    """Usage for many facts, in one query per chunk."""
    ids = list(fact_ids)
    if not ids:
        return {}
    ensure_schema(db_path)
    out: dict[str, dict] = {}
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"select * from belief_usage where fact_id in ({marks})", chunk
            ).fetchall():
                out[row["fact_id"]] = dict(row)
    return out


def record_surface(db_path: Path, fact_ids, when: str | None = None) -> int:
    """Record that these facts were surfaced to the agent. Spends attention."""
    ensure_schema(db_path)
    when = _write_time(when)
    ids = [f for f in fact_ids]
    if not ids:
        return 0
    touched = 0
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.execute("begin immediate")
        for fact_id in _existing_fact_ids(conn, ids):
            conn.execute(
                """
                insert into belief_usage
                    (fact_id, surfaced_count, surfaced_since_credit, last_surfaced_at,
                     acted_on_count, last_acted_on_at)
                values (?, 1, 1, ?, 0, null)
                on conflict(fact_id) do update set
                    surfaced_count = surfaced_count + 1,
                    surfaced_since_credit = surfaced_since_credit + 1,
                    last_surfaced_at = excluded.last_surfaced_at
                """,
                (fact_id, when),
            )
            touched += 1
        conn.commit()
    return touched


def record_acted_on(db_path: Path, fact_ids, when: str | None = None) -> int:
    """Record a direct act-on (Signal C — explicit confirmation).

    Decrements outstanding pressure by ``CREDIT_PER_ACT`` rather than clearing
    it: one act-on cancels one surfacing, not the whole history.
    """
    ensure_schema(db_path)
    when = _write_time(when)
    ids = [f for f in fact_ids]
    if not ids:
        return 0
    touched = 0
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.execute("begin immediate")
        for fact_id in _existing_fact_ids(conn, ids):
            conn.execute(
                """
                insert into belief_usage
                    (fact_id, surfaced_count, surfaced_since_credit, last_surfaced_at,
                     acted_on_count, last_acted_on_at)
                values (?, 0, 0, null, 1, ?)
                on conflict(fact_id) do update set
                    acted_on_count = acted_on_count + 1,
                    surfaced_since_credit =
                        max(0, surfaced_since_credit - ?),
                    last_acted_on_at = excluded.last_acted_on_at
                """,
                (fact_id, when, CREDIT_PER_ACT),
            )
            touched += 1
        conn.commit()
    return touched


def credit_from_new_evidence(db_path: Path, evidence) -> list[str]:
    """Signal A: credit facts that were surfaced and then corroborated.

    ``evidence`` is an iterable of dicts, one per corroborating observation:

        {"fact_id": str, "observed_at": iso8601, "provenance": class}

    All three of Signal A's hard requirements are enforced here rather than left
    to the caller, because a helper that only checks "has outstanding pressure"
    will happily credit a re-run importer or the agent's own narration:

    * **new** — ``observed_at`` must be strictly newer than the surface
    * **after surfacing** — the fact must have outstanding pressure to clear.
      A fact that was never surfaced gets nothing: new evidence for it is normal
      accumulation, not a return on attention already spent.
    * **independent** — the evidence's provenance must not be agent-produced,
      inferred, or unknown. Reality must have confirmed the fact, not the system.

    The write is a single conditional statement inside one transaction, so two
    workers cannot both read "has pressure" and double-credit the same event.
    Returns the fact_ids actually credited.
    """
    ensure_schema(db_path)
    credited: list[str] = []
    if not evidence:
        return credited

    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("begin immediate")
        for item in evidence:
            fact_id = item.get("fact_id")
            provenance = item.get("provenance")
            if not fact_id:
                continue
            if provenance in NOT_CREDITABLE_PROVENANCE or provenance is None:
                continue
            # Normalized here so both the Python check below and the SQL guard
            # are comparing UTC instants, never raw strings from mixed sources.
            observed_at = _instant(item.get("observed_at"))
            if observed_at is None:
                continue

            row = conn.execute(
                "select last_surfaced_at, last_acted_on_at, surfaced_since_credit "
                "from belief_usage where fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None or row["surfaced_since_credit"] <= 0:
                continue
            # Not newer than anything we already counted -> not news. This also
            # makes replaying the same evidence event idempotent: a re-run
            # importer cannot credit the same observation twice.
            newest_known = max(
                (v for v in (row["last_surfaced_at"], row["last_acted_on_at"]) if v),
                default=None,
            )
            if newest_known and str(observed_at) <= str(newest_known):
                continue

            cur = conn.execute(
                """
                update belief_usage
                   set acted_on_count = acted_on_count + 1,
                       surfaced_since_credit = max(0, surfaced_since_credit - ?),
                       last_acted_on_at = ?
                 where fact_id = ?
                   and surfaced_since_credit > 0
                   and (last_acted_on_at is null or ? > last_acted_on_at)
                returning fact_id
                """,
                (CREDIT_PER_ACT, str(observed_at), fact_id, str(observed_at)),
            )
            if cur.fetchone() is not None:
                credited.append(fact_id)
        conn.commit()
    return credited