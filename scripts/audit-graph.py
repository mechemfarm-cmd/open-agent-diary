#!/usr/bin/env python3
"""Read-only audit of the Agent Diary knowledge graph.

Task 1 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

Purpose: make every claim about graph quality re-measurable. Opens the database
in read-only mode, changes nothing, and prints a diffable report. Run it before
and after each migration task and diff the output.

Usage:
    python3 scripts/audit-graph.py
    python3 scripts/audit-graph.py --db /path/to/memory.db
"""

import argparse
import collections
import json
import os
import sqlite3
import sys

DEFAULT_DB = os.path.expanduser("~/development/agent-diary/data/index/memory.db")


def bucket(confidence: float) -> str:
    if confidence >= 0.5:
        return "0.5-1.0"
    if confidence >= 0.3:
        return "0.3-0.5"
    if confidence >= 0.2:
        return "0.2-0.3"
    if confidence >= 0.1:
        return "0.1-0.2"
    return "0.0-0.1"


def table_exists(cur, name):
    row = cur.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def section(title):
    print(f"\n=== {title} ===")


def count(cur, sql, *args):
    try:
        return cur.execute(sql, args).fetchone()[0]
    except sqlite3.Error:
        return None


def main():
    ap = argparse.ArgumentParser(description="Read-only Agent Diary graph audit")
    ap.add_argument("--db", default=DEFAULT_DB, help="path to memory.db")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}", file=sys.stderr)
        return 1

    size_kb = os.path.getsize(args.db) // 1024
    print(f"Agent Diary graph audit (READ-ONLY)")
    print(f"db: {args.db}")
    print(f"size: {size_kb} KB")

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = con.cursor()

    # ---- headline numbers, kept together so they are easy to diff ----
    section("SUMMARY (diff this block)")
    facts = count(cur, "select count(*) from graph_facts")
    entities = count(cur, "select count(*) from graph_entities")
    evidence = count(cur, "select count(*) from graph_fact_evidence")
    no_ev = count(
        cur,
        "select count(*) from graph_facts f where not exists "
        "(select 1 from graph_fact_evidence e where e.fact_id = f.fact_id)",
    )
    print(f"facts:                    {facts}")
    print(f"entities:                 {entities}")
    print(f"evidence rows:            {evidence}")
    print(f"facts with no evidence:   {no_ev}")
    if facts:
        evidence_n = evidence or 0
        no_ev_n = no_ev or 0
        print(f"evidence per fact:        {evidence_n / facts:.2f}")
        print(f"pct facts with no ev:     {100 * no_ev_n / facts:.1f}%")

    if facts:
        distinct_conf = count(cur, "select count(distinct confidence) from graph_facts")
        print(f"distinct confidence vals: {distinct_conf}   <-- 1 means the column is decoration")
        row = cur.execute("select typeof(confidence) from graph_facts limit 1").fetchone()
        ctype = row[0] if row else "unknown"
        print(f"confidence column type:   {ctype}"
              "   <-- TEXT must never be compared numerically")
        if table_exists(cur, "graph_fact_evidence"):
            w = cur.execute(
                "select count(distinct weight) from graph_fact_evidence"
            ).fetchone()
            print(f"distinct evidence weights:{w[0] if w else '?'}"
                  "   <-- 1 means the weight column is unused")

    dup_groups = count(
        cur,
        "select count(*) from (select subject_entity_id, predicate, "
        "coalesce(object_entity_id, object_value) k, count(*) c from graph_facts "
        "group by 1,2,3 having c > 1)",
    )
    print(f"duplicate triple groups:  {dup_groups}")

    events = count(cur, "select count(*) from graph_assertion_events")
    print(f"assertion events:         {events}   <-- audit trail; 0 = never written")

    # ---- detail ----
    if facts:
        section("fact state")
        for state, n in cur.execute(
            "select state, count(*) from graph_facts group by state order by 2 desc"
        ):
            print(f"  {state!r}: {n}")

        section("confidence distribution (raw values)")
        for val, n in cur.execute(
            "select confidence, count(*) from graph_facts group by 1 order by 2 desc"
        ):
            print(f"  {val!r}: {n}")

        section("top predicates")
        for pred, n in cur.execute(
            "select predicate, count(*) from graph_facts group by 1 order by 2 desc limit 15"
        ):
            print(f"  {pred!r}: {n}")

    if facts:
        section("computed belief layer (metadata.belief)")
        rows = cur.execute("select metadata from graph_facts").fetchall()
        belief_count = 0
        prov = collections.Counter()
        provisional = collections.Counter()
        buckets = collections.Counter()
        for (raw,) in rows:
            meta = {}
            if raw:
                try:
                    meta = json.loads(raw)
                except (ValueError, TypeError):
                    meta = {}
            b = meta.get("belief") if isinstance(meta.get("belief"), dict) else None
            if not b:
                continue
            belief_count += 1
            prov[b.get("provenance", "?")] += 1
            provisional[bool(b.get("provisional", True))] += 1
            try:
                buckets[bucket(float(b.get("confidence", 0.0)))] += 1
            except (TypeError, ValueError):
                pass
        print(f"facts with a computed belief: {belief_count}")
        print(f"facts with NO computed belief: {facts - belief_count}")
        if belief_count:
            print("  provenance:")
            for k, v in prov.most_common():
                print(f"    {k}: {v}")
            print("  provisional:")
            print(f"    provisional: {provisional.get(True, 0)}")
            print(f"    first-hand:  {provisional.get(False, 0)}")
            print("  computed confidence:")
            for b in ["0.5-1.0", "0.3-0.5", "0.2-0.3", "0.1-0.2", "0.0-0.1"]:
                print(f"    {b}: {buckets.get(b, 0)}")

    if entities:
        section("entity types")
        for t, n in cur.execute(
            "select entity_type, count(*) from graph_entities group by 1 order by 2 desc limit 15"
        ):
            print(f"  {t!r}: {n}")

        section("entity lifecycle")
        for s, n in cur.execute(
            "select lifecycle_status, count(*) from graph_entities group by 1 order by 2 desc"
        ):
            print(f"  {s!r}: {n}")

    if table_exists(cur, "graph_extraction_jobs"):
        section("extraction job status")
        for s, n in cur.execute(
            "select status, count(*) from graph_extraction_jobs group by 1 order by 2 desc"
        ):
            print(f"  {s!r}: {n}")

    if table_exists(cur, "entries"):
        section("entry counts")
        print(f"  entries: {count(cur, 'select count(*) from entries')}")
    if table_exists(cur, "work_trace_events"):
        print(f"  work_trace_events: {count(cur, 'select count(*) from work_trace_events')}")
    if table_exists(cur, "artifacts"):
        print(f"  artifacts: {count(cur, 'select count(*) from artifacts')}")

    con.close()
    print("\naudit complete (nothing was modified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())