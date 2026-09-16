#!/usr/bin/env python3
"""Backfill computed strength/confidence for knowledge-graph facts.

Tasks 3 + 4 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

Volatility is MEASURED per (subject, predicate) from churn in the evidence
timeline, never declared from a predicate list. Pairs with too little history
fall back to a default and are marked unmeasured.

The timeline used is ``graph_fact_evidence.source_timestamp``. Do NOT use
``graph_facts.created_at``: in a bulk import every fact shares a creation date
(the live corpus spans 2.5 days) which collapses the timeline and produces
absurd churn rates.

Review fixes applied:
  * confidence counts INDEPENDENT SOURCES, not evidence rows (see
    analytics/evidence.py)
  * ``--min-span-days`` is actually threaded into the churn measurement
  * assertion events are APPEND-ONLY with a run stamp and carry ``fact_id``;
    rewriting a deterministic event id destroyed the audit trail
  * unchanged beliefs are skipped entirely — no write, no event

Read-only by default. Pass --apply to write. Written values go into
``graph_facts.metadata``; the existing TEXT ``confidence`` column is
deliberately not overwritten.

Usage:
    python3 scripts/backfill-fact-confidence.py                    # dry run
    python3 scripts/backfill-fact-confidence.py --apply
"""

import argparse
import collections
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from agent_diary.analytics.confidence import CLASS_CEILING, compute_belief  # noqa: E402
from agent_diary.analytics.evidence import count_evidence  # noqa: E402
from agent_diary.analytics.signal_policy import classify_fact  # noqa: E402
from agent_diary.analytics.volatility import (  # noqa: E402
    RETROSPECTIVE,
    measure_churn,
    parse_ts,
)

DEFAULT_DB = os.path.expanduser("~/development/agent-diary/data/index/memory.db")
METHOD = "evidence-backfill-v3"
UNMEASURED = "unmeasured"

#: Tolerance for "did this change?". Confidence depends on AGE, which moves with
#: the clock, so exact equality reports every fact as changed on every run.
CONFIDENCE_TOLERANCE = 1e-3
_CATEGORICAL = ("provenance", "volatility")


def _unchanged(existing, payload, tol=CONFIDENCE_TOLERANCE):
    """True when re-running would store effectively the same belief."""
    if not existing:
        return False
    if any(existing.get(k) != payload.get(k) for k in _CATEGORICAL):
        return False
    for key in ("strength", "confidence"):
        was, now = existing.get(key), payload.get(key)
        if was is None or now is None:
            return False
        try:
            if abs(float(was) - float(now)) > tol:
                return False
        except (TypeError, ValueError):
            return False
    return True


def bucket(c):
    if c >= 0.5:
        return "0.5-1.0"
    if c >= 0.3:
        return "0.3-0.5"
    if c >= 0.2:
        return "0.2-0.3"
    if c >= 0.1:
        return "0.1-0.2"
    return "0.0-0.1"


def build_churn_map(cur, min_span_days):
    """Measure churn per (subject_entity_id, predicate) from evidence times."""
    rows = cur.execute(
        """
        select f.subject_entity_id, f.predicate, f.object_entity_id, f.object_value,
               e.source_timestamp
        from graph_facts f
        join graph_fact_evidence e on e.fact_id = f.fact_id
        order by f.subject_entity_id, f.predicate, e.source_timestamp
        """
    ).fetchall()

    groups = collections.defaultdict(list)
    for subj, pred, obj_id, obj_val, ts in rows:
        groups[(subj, pred)].append((parse_ts(ts), obj_id or obj_val or ""))

    out = {}
    for key, obs in groups.items():
        try:
            out[key] = measure_churn(obs, source=RETROSPECTIVE, min_span_days=min_span_days)
        except ValueError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser(description="Backfill computed fact confidence")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--volatility", default="medium",
                    help="fallback for pairs with too little history to measure")
    ap.add_argument("--min-span-days", type=float, default=30.0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    # Microseconds in the run id: second precision collides when two applies
    # start in the same second, and the primary key then aborts the whole run.
    run_id = now.strftime("%Y%m%dT%H%M%S.%fZ")
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    churn_map = build_churn_map(cur, args.min_span_days)
    facts = cur.execute("select * from graph_facts").fetchall()
    print(f"facts: {len(facts)}")
    print(f"(subject, predicate) pairs measured: {len(churn_map)}  (min span {args.min_span_days}d)")
    print("MODE:", "APPLY" if args.apply else "DRY RUN (no writes)")
    print()

    results = []
    for f in facts:
        # author_role is joined from the source entry: raw_entry is a container,
        # and WHO spoke is what decides whether this is knowledge or narration.
        ev = cur.execute(
            "select e.role, e.source_kind, e.source_id, e.source_timestamp, "
            "       en.author_role "
            "from graph_fact_evidence e "
            "left join entries en on en.entry_id = e.source_id "
            "where e.fact_id = ?",
            (f["fact_id"],),
        ).fetchall()

        counts = count_evidence(ev)
        supporting = counts.supporting
        contradicting = counts.contradicting
        if supporting == 0 and contradicting == 0 and ev:
            supporting = counts.distinct_sources

        newest = None
        for e in ev:
            ts = parse_ts(e["source_timestamp"])
            if ts and (newest is None or ts > newest):
                newest = ts
        if newest is None:
            newest = parse_ts(f["created_at"])
        age_days = max(0.0, (now - newest).total_seconds() / 86400.0) if newest else 0.0

        provenance = classify_fact(
            [(e["source_kind"], e["author_role"]) for e in ev]
        ) if ev else "unknown"

        churn = churn_map.get((f["subject_entity_id"], f["predicate"]))
        if churn:
            volatility, vol_src = churn.volatility, "measured-provisional"
        else:
            volatility, vol_src = args.volatility, UNMEASURED

        belief = compute_belief(
            n_supporting=supporting,
            n_contradicting=contradicting,
            age_days=age_days,
            volatility=volatility,
            provenance=provenance,
            days_since_last_use=None,  # recall tracking is new; see belief_repository
        )
        results.append({
            "fact_id": f["fact_id"],
            "predicate": f["predicate"],
            "provenance": provenance,
            "volatility": volatility,
            "vol_src": vol_src,
            "strength": belief.strength,
            "confidence": belief.confidence,
            "age_days": round(age_days, 1),
            "n_supporting": supporting,
            "n_contradicting": contradicting,
            "dup_rows": counts.duplicate_rows,
            "old_label": f["confidence"],
        })

    # ── report ────────────────────────────────────────────────────────────────
    confs = sorted(r["confidence"] for r in results)
    print("=== computed confidence distribution ===")
    counts_by_bucket = collections.Counter(bucket(c) for c in confs)
    for b in ["0.5-1.0", "0.3-0.5", "0.2-0.3", "0.1-0.2", "0.0-0.1"]:
        n = counts_by_bucket.get(b, 0)
        print(f"  {b}: {n:5d}  {'#' * int(40 * n / max(len(confs), 1))}")
    if confs:
        print(f"\n  min {confs[0]:.3f}  median {confs[len(confs)//2]:.3f}  max {confs[-1]:.3f}")

    dup_facts = sum(1 for r in results if r["dup_rows"] > 0)
    contra = sum(1 for r in results if r["n_contradicting"] > 0)
    print(f"\n  facts with duplicate evidence rows: {dup_facts}  (now deduplicated by source)")
    print(f"  facts with contradicting evidence:  {contra}")

    print("\n=== measured volatility ===")
    for k, v in collections.Counter(r["volatility"] for r in results).most_common():
        print(f"  {k}: {v}")

    print(f"\n=== {args.show} LOWEST confidence ===")
    for r in sorted(results, key=lambda x: x["confidence"])[: args.show]:
        print(f"  {r['confidence']:.3f}  {r['predicate']:<16} vol={r['volatility']:<8} "
              f"src={r['n_supporting']:3d} age={r['age_days']:7.1f}d")

    print(f"\n=== {args.show} HIGHEST confidence ===")
    for r in sorted(results, key=lambda x: -x["confidence"])[: args.show]:
        print(f"  {r['confidence']:.3f}  {r['predicate']:<16} vol={r['volatility']:<8} "
              f"src={r['n_supporting']:3d} age={r['age_days']:7.1f}d")

    if not args.apply:
        print("\nDRY RUN — nothing written.")
        con.close()
        return 0

    # ── apply: append-only events, skip unchanged ─────────────────────────────
    changed = unchanged = 0
    for r in results:
        row = cur.execute(
            "select metadata from graph_facts where fact_id = ?", (r["fact_id"],)
        ).fetchone()
        meta = {}
        if row and row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except (ValueError, TypeError):
                meta = {}
        existing = meta.get("belief") if isinstance(meta.get("belief"), dict) else {}

        payload = {
            "fact_id": r["fact_id"],
            "strength": r["strength"],
            "confidence": r["confidence"],
            "provenance": r["provenance"],
            "volatility": r["volatility"],
            "volatility_source": r["vol_src"],
            "provisional": r["vol_src"] != "measured",
            "method": METHOD,
        }

        if _unchanged(existing, payload):
            unchanged += 1
            continue

        meta["belief"] = payload
        cur.execute(
            "update graph_facts set metadata = ?, updated_at = ? where fact_id = ?",
            (json.dumps(meta), now.isoformat(), r["fact_id"]),
        )
        # Append-only: a run stamp keeps history instead of overwriting it.
        cur.execute(
            "insert into graph_assertion_events (event_id, event_type, author, reason, "
            "payload, created_at) values (?,?,?,?,?,?)",
            (
                f"ga_conf_{r['fact_id']}_{run_id}",
                "fact_confidence_computed",
                METHOD,
                f"was confidence={existing.get('confidence', r['old_label'])}",
                json.dumps(payload),
                now.isoformat(),
            ),
        )
        changed += 1

    con.commit()
    con.close()
    print(f"\nAPPLIED run {run_id}: {changed} changed, {unchanged} unchanged (skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
