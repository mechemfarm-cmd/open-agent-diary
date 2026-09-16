"""Usage tracking and candidate loading for belief recall.

Tasks 5 + 6 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

Closes the loop from stored facts to ranked recall:

    graph_facts + metadata.belief  ->  Candidate  ->  ranking.rank()  ->  block

Usage counters now live in the normalised ``belief_usage`` table (see
``belief_usage``), not in ``metadata`` JSON. Recall happens on every turn, so a
read-modify-write race there was likely rather than theoretical.

Recall is still not written to ``graph_assertion_events``: being read is not a
revision of belief, and one event per recall would drown the trail that exists to
explain belief *changes*.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_diary.analytics.ranking import Candidate
from agent_diary.index import belief_usage

BELIEF_KEY = "belief"


def _load_meta(raw) -> dict:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def record_surface(db_path: Path, fact_ids, when: str | None = None) -> int:
    """Record that these facts were surfaced. Spends attention. See belief_usage."""
    return belief_usage.record_surface(db_path, fact_ids, when)


def record_acted_on(db_path: Path, fact_ids, when: str | None = None) -> int:
    """Record that these facts were acted on. Earns credit back."""
    return belief_usage.record_acted_on(db_path, fact_ids, when)


def credit_from_new_evidence(db_path: Path, evidence) -> list[str]:
    """Signal A: credit facts that were surfaced and then corroborated.

    ``evidence`` is an iterable of ``{"fact_id", "observed_at", "provenance"}``.
    """
    return belief_usage.credit_from_new_evidence(db_path, evidence)


def get_usage(db_path: Path, fact_id: str) -> dict:
    """Usage row for one fact. Unknown facts report zeros rather than erroring."""
    return belief_usage.get_usage(db_path, fact_id)


def load_candidates(
    db_path: Path,
    *,
    states: tuple[str, ...] = ("current",),
    limit: int | None = None,
) -> list[Candidate]:
    """Load facts as ranking candidates, joined to their belief and usage.

    Facts with no stored belief are still returned, with confidence 0.0 — they
    score zero and cannot be recalled, which is the right default for something
    nothing has evaluated yet.
    """
    sql = """
        select f.fact_id, f.predicate, f.metadata, f.object_value,
               se.canonical_name as subj_name,
               oe.canonical_name as obj_name
        from graph_facts f
        left join graph_entities se on se.entity_id = f.subject_entity_id
        left join graph_entities oe on oe.entity_id = f.object_entity_id
    """
    params: list = []
    if states:
        sql += " where f.state in (%s)" % ",".join("?" * len(states))
        params.extend(states)
    sql += " order by f.fact_id"
    if limit:
        sql += " limit ?"
        params.append(int(limit))

    rows: list[dict] = []
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    usage_map = belief_usage.get_usage_map(db_path, [r["fact_id"] for r in rows])

    out: list[Candidate] = []
    for row in rows:
        meta = _load_meta(row["metadata"])
        belief = meta.get(BELIEF_KEY)
        if not isinstance(belief, dict):
            belief = {}

        usage = usage_map.get(row["fact_id"], belief_usage.EMPTY)
        tracked = row["fact_id"] in usage_map

        obj = row["obj_name"] or row["object_value"] or "?"
        statement = f"{row['subj_name'] or '?'} {row['predicate'].lower().replace('_', ' ')} {obj}"

        out.append(Candidate(
            fact_id=row["fact_id"],
            statement=statement,
            provenance=str(belief.get("provenance") or "unknown"),
            strength=float(belief.get("strength") or 0.0),
            confidence=float(belief.get("confidence") or 0.0),
            # No usage row -> unknown, not zero. A fact that has never been
            # surfaced must not be penalised for a signal that does not exist.
            surfaced_since_credit=(
                int(usage["surfaced_since_credit"]) if tracked else None
            ),
            acted_on_count=int(usage["acted_on_count"]) if tracked else None,
            # Absent belief -> provisional. Nothing has evaluated it yet.
            provisional=bool(belief.get("provisional", True)),
        ))
    return out