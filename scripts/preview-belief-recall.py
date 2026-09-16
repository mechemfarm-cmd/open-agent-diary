#!/usr/bin/env python3
"""Preview what belief recall would surface. Read-only.

Task 5 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

Computes beliefs for the live corpus, ranks them, and renders the block that
would be injected into the agent's context — including the hard character budget.

Usage is UNKNOWN across the whole corpus, because recall tracking does not exist
yet. Scenario B therefore simulates recall so the lever can be seen moving; it is
labelled as a simulation and changes nothing on disk.

Usage:
    python3 scripts/preview-belief-recall.py
    python3 scripts/preview-belief-recall.py --limit 5 --budget 600
"""

import argparse
import collections
import os
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from agent_diary.analytics.confidence import compute_belief  # noqa: E402
from agent_diary.analytics.evidence import count_evidence  # noqa: E402
from agent_diary.analytics.ranking import Candidate, rank, render_block  # noqa: E402
from agent_diary.analytics.signal_policy import classify_fact  # noqa: E402
from agent_diary.analytics.volatility import RETROSPECTIVE, measure_churn, parse_ts  # noqa: E402

DEFAULT_DB = os.path.expanduser("~/development/agent-diary/data/index/memory.db")


def build(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    now = datetime.now(timezone.utc)

    churn_rows = cur.execute(
        """
        select f.subject_entity_id, f.predicate, f.object_entity_id, f.object_value,
               e.source_timestamp
        from graph_facts f
        join graph_fact_evidence e on e.fact_id = f.fact_id
        """
    ).fetchall()
    groups = collections.defaultdict(list)
    for r in churn_rows:
        groups[(r["subject_entity_id"], r["predicate"])].append(
            (parse_ts(r["source_timestamp"]), r["object_entity_id"] or r["object_value"] or "")
        )
    churn_map = {}
    for key, obs in groups.items():
        try:
            churn_map[key] = measure_churn(obs, source=RETROSPECTIVE)
        except ValueError:
            pass

    facts = cur.execute(
        """
        select f.*, se.canonical_name as subj_name, oe.canonical_name as obj_name
        from graph_facts f
        left join graph_entities se on se.entity_id = f.subject_entity_id
        left join graph_entities oe on oe.entity_id = f.object_entity_id
        where f.state = 'current'
        """
    ).fetchall()

    out = []
    for f in facts:
        ev = cur.execute(
            "select e.role, e.source_kind, e.source_id, e.source_timestamp, "
            "       en.author_role "
            "from graph_fact_evidence e "
            "left join entries en on en.entry_id = e.source_id "
            "where e.fact_id = ?",
            (f["fact_id"],),
        ).fetchall()
        counts = count_evidence(ev)
        # An all-contradicting fact must never read as supporting. Only fall back
        # to distinct_sources when there is genuinely no role information at all.
        supporting, contradicting = counts.supporting, counts.contradicting
        if supporting == 0 and contradicting == 0 and ev:
            supporting = counts.distinct_sources
        newest = None
        for e in ev:
            ts = parse_ts(e["source_timestamp"])
            if ts and (newest is None or ts > newest):
                newest = ts
        if newest is None:
            newest = parse_ts(f["created_at"])
        age = max(0.0, (now - newest).total_seconds() / 86400.0) if newest else 0.0

        churn = churn_map.get((f["subject_entity_id"], f["predicate"]))
        volatility = churn.volatility if churn else "medium"
        provenance = classify_fact(
            [(e["source_kind"], e["author_role"]) for e in ev]
        ) if ev else "unknown"

        belief = compute_belief(
            n_supporting=supporting, n_contradicting=contradicting,
            age_days=age, volatility=volatility,
            provenance=provenance, days_since_last_use=None,
        )
        obj = f["obj_name"] or f["object_value"] or "?"
        statement = f"{f['subj_name'] or '?'} {f['predicate'].lower().replace('_',' ')} {obj}"
        out.append((Candidate(
            fact_id=f["fact_id"], statement=statement, provenance=provenance,
            strength=belief.strength, confidence=belief.confidence,
        ), supporting, volatility))
    con.close()
    return out


def show(title, selected, budget, note=""):
    print(f"\n=== {title} ===")
    if note:
        print(f"    {note}")
    for cand, s in selected:
        print(f"  {s:.3f}  {cand.statement[:78]}")
    block = render_block(selected, char_budget=budget)
    print(f"\n  --- rendered recall block ({len(block)} chars of {budget}) ---")
    for line in block.splitlines():
        print(f"  {line}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--budget", type=int, default=900)
    args = ap.parse_args()

    rows = build(args.db)
    cands = [c for c, _, _ in rows]
    print(f"current facts considered: {len(cands)}")

    # An empty graph is the CORRECT state for a new deployment, so this is not an
    # error path — it just has nothing to rank.
    if not cands:
        print("no facts to rank yet. An empty graph is correct at cold start;")
        print("the base of truth is built by usage, never shipped.")
        print("\n(no writes — this preview is read-only)")
        return

    # Scenario A — reality today: usage unknown, so salience is neutral.
    show("SCENARIO A — today, usage unknown", rank(cands, limit=args.limit), args.budget,
         "salience neutral (1.0) for every fact")

    # Scenario B — simulation only. Shows the spend/replenish trade: every fact
    # has been surfaced repeatedly without being used, except one that has been
    # acted on. The spent ones fall; the earned one holds its place.
    top_ids = {c.fact_id for c, _ in rank(cands, limit=args.limit)}
    pool = [c for c in cands if c.fact_id not in top_ids and c.confidence >= 0.4]
    target = max(pool, key=lambda c: c.confidence) if pool else max(cands, key=lambda c: c.confidence)
    tracked = []
    for c in cands:
        if c.fact_id == target.fact_id:
            # Surfaced 30x and acted on 30x: the debt is paid down by EARNING,
            # not forgiven. Pressure returns to zero through use.
            tracked.append(replace(c, surfaced_since_credit=0, acted_on_count=30))
        else:
            tracked.append(replace(c, surfaced_since_credit=30, acted_on_count=0))
    show("SCENARIO B — simulation: one fact has been earned", rank(tracked, limit=args.limit),
         args.budget,
         f"'{target.statement[:50]}' surfaced 30x and acted on 30x; others surfaced 30x, never earned")
    print("\n(no writes — this preview is read-only)")


if __name__ == "__main__":
    main()
