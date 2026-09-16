"""Tests for the live belief-recall wiring.

Task 6 wiring of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md

These exercise the route the running service actually calls, including the
side effect that matters most: surfacing SPENDS. A test suite that only checked
the returned block would miss the entire mechanism.

Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_belief_recall_route -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths as PathsType
from agent_diary.index.belief_usage import get_usage, record_surface
from agent_diary.index.graph_repository import insert_entity, insert_evidence, insert_fact
from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.service.handlers import credit_beliefs, recall_beliefs

T1 = "2026-09-15T10:00:00+00:00"
T2 = "2026-09-15T11:00:00+00:00"


def make_paths(tmp_root: Path) -> PathsType:
    data_root = tmp_root / "data"
    index_dir = data_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = index_dir / "memory.db"
    bootstrap_sqlite(sqlite_path)
    for d in ("entries", "work_trace", "overlays", "artifacts", "imports"):
        (data_root / d).mkdir(parents=True, exist_ok=True)
    return PathsType(
        root=tmp_root, data_root=data_root,
        entries_dir=data_root / "entries", work_trace_dir=data_root / "work_trace",
        overlays_dir=data_root / "overlays", artifacts_dir=data_root / "artifacts",
        imports_dir=data_root / "imports", index_dir=index_dir,
        config_dir=data_root / "config", archive_dir=data_root / "archives",
        sqlite_path=sqlite_path,
    )


class TestBeliefRecallRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = make_paths(self.tmp)
        self.db = self.paths.sqlite_path

    def _seed(self, label, confidence, *, provisional=True, with_belief=True):
        from agent_diary.models.types import GraphEntity, GraphFact, GraphFactEvidence

        s = GraphEntity(canonical_name=f"Host{label}")
        o = GraphEntity(canonical_name=f"Thing{label}")
        insert_entity(self.db, s)
        insert_entity(self.db, o)
        fact = GraphFact(
            subject_entity_id=s.entity_id, predicate="RUNS_ON",
            object_kind="entity", object_entity_id=o.entity_id,
        )
        if with_belief:
            fact.metadata = {"belief": {
                "strength": 1.0, "confidence": confidence,
                "provenance": "harvested", "provisional": provisional,
            }}
        fid = insert_fact(self.db, fact)
        insert_evidence(self.db, GraphFactEvidence(
            fact_id=fid, source_kind="raw_entry", source_id=f"e_{label}",
        ))
        return fid

    # ── empty graph is correct, not broken ───────────────────────────────────
    def test_empty_graph_returns_an_empty_block_without_error(self):
        out = recall_beliefs(self.paths, {})
        self.assertEqual(out["block"], "")
        self.assertEqual(out["considered"], 0)
        self.assertEqual(out["surfaced"], 0)
        self.assertEqual(out["facts"], [])

    def test_empty_graph_charges_nothing(self):
        recall_beliefs(self.paths, {})
        self.assertEqual(get_usage(self.db, "nonexistent")["surfaced_count"], 0)

    # ── selection ────────────────────────────────────────────────────────────
    def test_fact_without_a_belief_never_appears(self):
        # Nothing has evaluated it, so it scores zero. Intended default.
        self._seed("Unjudged", 0.0, with_belief=False)
        out = recall_beliefs(self.paths, {})
        self.assertEqual(out["facts"], [])

    def test_higher_confidence_is_surfaced(self):
        low = self._seed("Low", 0.20)
        high = self._seed("High", 0.80)
        out = recall_beliefs(self.paths, {"limit": 1})
        self.assertEqual(out["facts"][0]["fact_id"], high)
        self.assertNotIn(low, [f["fact_id"] for f in out["facts"]])

    def test_block_contains_the_statement(self):
        self._seed("Only", 0.9)
        out = recall_beliefs(self.paths, {})
        self.assertIn("HostOnly", out["block"])

    def test_response_reports_the_belief_it_used(self):
        self._seed("Detail", 0.7)
        fact = recall_beliefs(self.paths, {})["facts"][0]
        self.assertEqual(fact["provenance"], "harvested")
        self.assertEqual(fact["confidence"], 0.7)
        self.assertTrue(fact["provisional"])

    def test_limit_is_respected(self):
        for i in range(6):
            self._seed(f"L{i}", 0.5)
        self.assertLessEqual(len(recall_beliefs(self.paths, {"limit": 2})["facts"]), 2)

    def test_invalid_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            recall_beliefs(self.paths, {"limit": 0})

    def test_invalid_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            recall_beliefs(self.paths, {"char_budget": 0})

    def test_limit_above_the_cap_is_rejected(self):
        with self.assertRaises(ValueError):
            recall_beliefs(self.paths, {"limit": 5000})

    def test_char_budget_above_the_cap_is_rejected(self):
        with self.assertRaises(ValueError):
            recall_beliefs(self.paths, {"char_budget": 10 ** 6})

    def test_an_oversized_fact_does_not_block_the_block(self):
        self._seed("x" * 300, 0.9)
        small = self._seed("Small", 0.5)
        out = recall_beliefs(self.paths, {"limit": 2, "char_budget": 80})
        self.assertNotEqual(out["block"], "")
        self.assertIn("Small", out["block"])
        self.assertEqual([f["fact_id"] for f in out["facts"]], [small])

    # ── the side effect: surfacing spends ────────────────────────────────────
    def test_surfaced_facts_are_charged_one_unit_each(self):
        self._seed("A", 0.9)
        fid = self._seed("B", 0.8)
        out = recall_beliefs(self.paths, {})
        self.assertEqual(out["surfaced"], len(out["facts"]))
        for f in out["facts"]:
            with self.subTest(fid=f["fact_id"]):
                self.assertEqual(get_usage(self.db, f["fact_id"])["surfaced_count"], 1)
        self.assertEqual(get_usage(self.db, fid)["surfaced_since_credit"], 1)

    def test_repeated_recall_accumulates_pressure(self):
        self._seed("Solo", 0.9)
        total = 0
        for _ in range(4):
            total += recall_beliefs(self.paths, {"limit": 1})["surfaced"]
        self.assertEqual(total, 4)

    def test_facts_cut_by_the_budget_are_not_charged(self):
        # Charging a fact that was never displayed would penalise exactly the
        # facts that never got their turn.
        for i in range(4):
            self._seed(f"Budget{i}", 0.5)
        out = recall_beliefs(self.paths, {"limit": 4, "char_budget": 40})
        self.assertLess(len(out["facts"]), 4, "budget should have cut some facts")
        self.assertEqual(out["surfaced"], len(out["facts"]))
        for f in out["facts"]:
            self.assertEqual(get_usage(self.db, f["fact_id"])["surfaced_count"], 1)

    def test_rotation_a_never_shown_fact_gets_its_turn(self):
        for i in range(3):
            self._seed(f"Rot{i}", 0.5)
        shown = [recall_beliefs(self.paths, {"limit": 1})["facts"][0]["fact_id"]
                 for _ in range(8)]
        self.assertEqual(len(set(shown)), 3, "spending must rotate the block")

    def test_spend_can_demote_below_a_fresh_fact(self):
        hammered = self._seed("Hammered", 0.9)
        fresh = self._seed("Fresh", 0.6)
        for _ in range(6):
            record_surface(self.db, [hammered])
        out = recall_beliefs(self.paths, {"limit": 1})
        self.assertEqual(out["facts"][0]["fact_id"], fresh)

    def test_heavily_used_fact_is_still_recallable(self):
        # Spend demotes; it must never make a fact unreachable.
        fid = self._seed("Persistent", 0.9)
        for _ in range(50):
            record_surface(self.db, [fid])
        self.assertIn(fid, [f["fact_id"] for f in recall_beliefs(self.paths, {})["facts"]])

    # ── credit route ─────────────────────────────────────────────────────────
    def test_credit_route_credits_surfaced_fact(self):
        fid = self._seed("Credited", 0.9)
        record_surface(self.db, [fid], when=T1)
        out = credit_beliefs(self.paths, {"evidence": [
            {"fact_id": fid, "observed_at": T2, "provenance": "harvested"}
        ]})
        self.assertEqual(out["credited"], [fid])
        self.assertEqual(out["count"], 1)
        self.assertEqual(get_usage(self.db, fid)["surfaced_since_credit"], 0)

    def test_credit_route_refuses_unsurfaced_fact(self):
        fid = self._seed("Unshown", 0.9)
        out = credit_beliefs(self.paths, {"evidence": [
            {"fact_id": fid, "observed_at": T2, "provenance": "harvested"}
        ]})
        self.assertEqual(out["credited"], [])

    def test_credit_route_rejects_non_list(self):
        with self.assertRaises(ValueError):
            credit_beliefs(self.paths, {"evidence": "not a list"})

    def test_credit_route_handles_missing_evidence(self):
        self.assertEqual(credit_beliefs(self.paths, {})["count"], 0)


if __name__ == "__main__":
    unittest.main()