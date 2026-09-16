"""Tests for belief candidate loading and usage wiring.

Tasks 5 + 6 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md
Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_belief_repository -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_diary.index import belief_usage
from agent_diary.index.belief_repository import (
    credit_from_new_evidence,
    get_usage,
    load_candidates,
    record_acted_on,
    record_surface,
)
from agent_diary.index.graph_repository import insert_entity, insert_evidence, insert_fact
from agent_diary.index.sqlite_index import bootstrap_sqlite


class TestBeliefRepository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "index" / "memory.db"
        bootstrap_sqlite(self.db_path)

    def _seed(self, belief=None, subject="Emily", predicate="RUNS_ON", obj="Hermes"):
        from agent_diary.models.types import GraphEntity, GraphFact, GraphFactEvidence

        s = GraphEntity(canonical_name=subject)
        o = GraphEntity(canonical_name=obj)
        insert_entity(self.db_path, s)
        insert_entity(self.db_path, o)
        fact = GraphFact(
            subject_entity_id=s.entity_id, predicate=predicate, object_kind="entity",
            object_entity_id=o.entity_id,
        )
        if belief:
            fact.metadata = {"belief": belief}
        fid = insert_fact(self.db_path, fact)
        insert_evidence(self.db_path, GraphFactEvidence(
            fact_id=fid, source_kind="raw_entry", source_id="entry_1",
        ))
        return fid

    def _meta(self, fid):
        con = sqlite3.connect(self.db_path)
        raw = con.execute(
            "select metadata from graph_facts where fact_id=?", (fid,)
        ).fetchone()[0]
        con.close()
        return json.loads(raw)

    # ── usage ────────────────────────────────────────────────────────────────
    def test_unknown_fact_reports_zeros(self):
        self.assertEqual(get_usage(self.db_path, "gf_nope")["surfaced_count"], 0)

    def test_record_surface_increments(self):
        fid = self._seed()
        self.assertEqual(record_surface(self.db_path, [fid]), 1)
        self.assertEqual(get_usage(self.db_path, fid)["surfaced_count"], 1)
        record_surface(self.db_path, [fid])
        self.assertEqual(get_usage(self.db_path, fid)["surfaced_count"], 2)

    def test_surface_sets_timestamp(self):
        fid = self._seed()
        record_surface(self.db_path, [fid], when="2026-09-15T10:00:00+00:00")
        self.assertEqual(
            get_usage(self.db_path, fid)["last_surfaced_at"], "2026-09-15T10:00:00+00:00"
        )

    def test_acted_on_pays_pressure_down_by_one(self):
        fid = self._seed()
        record_surface(self.db_path, [fid])
        record_surface(self.db_path, [fid])
        record_acted_on(self.db_path, [fid])
        u = get_usage(self.db_path, fid)
        self.assertEqual(u["acted_on_count"], 1)
        # One act-on cancels one surfacing — it does not clear the whole debt.
        self.assertEqual(u["surfaced_since_credit"], 1)

    def test_unknown_ids_are_skipped_not_raised(self):
        fid = self._seed()
        self.assertEqual(record_surface(self.db_path, [fid, "gf_missing"]), 1)

    def test_empty_list_is_a_no_op(self):
        self.assertEqual(record_surface(self.db_path, []), 0)

    def test_surfacing_does_not_write_assertion_events(self):
        # Being read is not a revision of belief; the audit trail stays meaning.
        fid = self._seed()
        record_surface(self.db_path, [fid])
        con = sqlite3.connect(self.db_path)
        n = con.execute("select count(*) from graph_assertion_events").fetchone()[0]
        con.close()
        self.assertEqual(n, 0)

    def test_usage_is_not_written_into_fact_metadata(self):
        # Counters live in the normalised table, not JSON, so writes are atomic.
        fid = self._seed(belief={"strength": 0.5, "confidence": 0.4})
        record_surface(self.db_path, [fid])
        meta = self._meta(fid)
        self.assertNotIn("usage", meta)
        self.assertEqual(meta["belief"]["confidence"], 0.4)

    def test_usage_row_survives_and_belief_is_untouched(self):
        fid = self._seed(belief={"strength": 0.5, "confidence": 0.4,
                                 "provenance": "harvested"})
        record_surface(self.db_path, [fid])
        self.assertEqual(belief_usage.get_usage(self.db_path, fid)["surfaced_count"], 1)
        self.assertEqual(self._meta(fid)["belief"]["provenance"], "harvested")

    # ── candidates ───────────────────────────────────────────────────────────
    def test_load_candidates_returns_a_usable_statement(self):
        fid = self._seed(subject="Emily", predicate="RUNS_ON", obj="Hermes")
        cands = {c.fact_id: c for c in load_candidates(self.db_path)}
        self.assertIn("Emily runs on Hermes", cands[fid].statement)

    def test_fact_without_a_stored_belief_scores_zero_confidence(self):
        fid = self._seed()
        cands = {c.fact_id: c for c in load_candidates(self.db_path)}
        self.assertEqual(cands[fid].confidence, 0.0)
        self.assertEqual(cands[fid].provenance, "unknown")

    def test_usage_is_carried_into_the_candidate(self):
        fid = self._seed(belief={"strength": 1.0, "confidence": 0.5,
                                 "provenance": "harvested"})
        record_surface(self.db_path, [fid])
        cands = {c.fact_id: c for c in load_candidates(self.db_path)}
        self.assertEqual(cands[fid].surfaced_since_credit, 1)

    def test_unsurfaced_fact_has_unknown_usage_not_zero(self):
        # A fact with no usage row must not be penalised for a signal that does
        # not exist.
        fid = self._seed(belief={"strength": 1.0, "confidence": 0.5,
                                 "provenance": "harvested"})
        cands = {c.fact_id: c for c in load_candidates(self.db_path)}
        self.assertIsNone(cands[fid].surfaced_since_credit)

    def test_non_current_states_are_excluded_by_default(self):
        fid = self._seed()
        con = sqlite3.connect(self.db_path)
        con.execute("update graph_facts set state='historical' where fact_id=?", (fid,))
        con.commit()
        con.close()
        self.assertEqual(load_candidates(self.db_path), [])

    def test_candidates_flow_into_ranking(self):
        from agent_diary.analytics.ranking import rank
        fid = self._seed(belief={"strength": 1.0, "confidence": 0.6,
                                 "provenance": "harvested"})
        out = rank(load_candidates(self.db_path), limit=5, min_score=0.15)
        self.assertEqual([c.fact_id for c, _ in out], [fid])

    def test_a_heavily_surfaced_unused_fact_loses_to_an_untouched_one(self):
        from agent_diary.analytics.ranking import rank
        old = self._seed(belief={"strength": 1.0, "confidence": 0.60,
                                 "provenance": "harvested"},
                         subject="HostOld", obj="Old")
        fresh = self._seed(belief={"strength": 1.0, "confidence": 0.55,
                                   "provenance": "harvested"},
                           subject="HostNew", obj="Fresh")
        for _ in range(30):
            record_surface(self.db_path, [old])
        out = rank(load_candidates(self.db_path), limit=5, min_score=0.15)
        self.assertEqual(out[0][0].fact_id, fresh)

    def test_repository_credit_wrapper_matches_the_helper(self):
        # The wrapper had a stale signature and called the helper with the wrong
        # arguments, so every caller going through the repository abstraction
        # raised TypeError. The route only worked because it imports the helper
        # directly — the abstraction was broken and nothing noticed.
        fid = self._seed()
        record_surface(self.db_path, [fid], when="2026-09-15T09:00:00+00:00")
        credited = credit_from_new_evidence(self.db_path, [
            {"fact_id": fid, "observed_at": "2026-09-15T10:00:00+00:00",
             "provenance": "harvested"},
        ])
        self.assertEqual(credited, [fid])
        self.assertEqual(get_usage(self.db_path, fid)["surfaced_since_credit"], 0)


if __name__ == "__main__":
    unittest.main()