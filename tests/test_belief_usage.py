"""Tests for the spend/replenish usage store.

Task 6 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md
Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_belief_usage -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_diary.analytics import ranking as rk
from agent_diary.analytics.signal_policy import AGENT_DERIVED, INFERRED
from agent_diary.index import belief_usage as bu
from agent_diary.index.graph_repository import insert_entity, insert_fact
from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.models.types import GraphEntity, GraphFact

T1 = "2026-09-15T10:00:00+00:00"
T2 = "2026-09-15T11:00:00+00:00"
T3 = "2026-09-15T12:00:00+00:00"


class TestBeliefUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Path(self.tmp) / "index" / "memory.db"
        bootstrap_sqlite(self.db)
        # Usage rows are validated against real facts, so the tests need real ones.
        self.f1 = self._fact("One")
        self.f2 = self._fact("Two")
        self.f3 = self._fact("Three")

    def _fact(self, label):
        e = GraphEntity(canonical_name=f"Host{label}")
        insert_entity(self.db, e)
        return insert_fact(self.db, GraphFact(
            subject_entity_id=e.entity_id, predicate="RUNS_ON",
            object_kind="value", object_value=f"thing-{label}",
        ))

    def _evidence(self, fact_id, observed_at, provenance: str | None = "harvested"):
        return {"fact_id": fact_id, "observed_at": observed_at, "provenance": provenance}

    # ── basics ───────────────────────────────────────────────────────────────
    def test_unknown_fact_reports_zeros(self):
        u = bu.get_usage(self.db, "nope")
        self.assertEqual(u["surfaced_count"], 0)
        self.assertEqual(u["surfaced_since_credit"], 0)
        self.assertIsNone(u["last_surfaced_at"])

    def test_reads_work_before_any_write(self):
        self.assertEqual(bu.get_usage_map(self.db, [self.f1]), {})

    def test_surface_increments_and_accumulates_pressure(self):
        bu.record_surface(self.db, [self.f1])
        bu.record_surface(self.db, [self.f1])
        u = bu.get_usage(self.db, self.f1)
        self.assertEqual(u["surfaced_count"], 2)
        self.assertEqual(u["surfaced_since_credit"], 2)

    def test_empty_input_is_a_no_op(self):
        self.assertEqual(bu.record_surface(self.db, []), 0)
        self.assertEqual(bu.record_acted_on(self.db, []), 0)

    def test_unknown_fact_ids_are_skipped_not_recorded(self):
        self.assertEqual(bu.record_surface(self.db, ["ghost"]), 0)
        self.assertEqual(bu.get_usage_map(self.db, ["ghost"]), {})

    def test_usage_map_handles_many_and_missing(self):
        bu.record_surface(self.db, [self.f1, self.f2])
        m = bu.get_usage_map(self.db, [self.f1, self.f2, "missing"])
        self.assertEqual(set(m), {self.f1, self.f2})

    # ── acted-on PAYS DOWN, does not forgive ─────────────────────────────────
    def test_acted_on_decrements_pressure_rather_than_clearing_it(self):
        for _ in range(100):
            bu.record_surface(self.db, [self.f1])
        bu.record_acted_on(self.db, [self.f1])
        self.assertEqual(bu.get_usage(self.db, self.f1)["surfaced_since_credit"], 99)

    def test_BLOCKER_one_act_on_must_not_erase_many_surfacings(self):
        # The regression Codex found: clearing pressure to zero meant a single
        # act-on restored a fact to full salience after a hundred surfacings.
        for _ in range(100):
            bu.record_surface(self.db, [self.f1])
        bu.record_acted_on(self.db, [self.f1])
        pressure = bu.get_usage(self.db, self.f1)["surfaced_since_credit"]
        self.assertLess(rk.salience(surfaced_since_credit=pressure), 0.5)

    def test_pressure_never_goes_negative(self):
        bu.record_acted_on(self.db, [self.f1])
        bu.record_acted_on(self.db, [self.f1])
        self.assertEqual(bu.get_usage(self.db, self.f1)["surfaced_since_credit"], 0)

    def test_acted_on_count_is_kept_as_a_statistic(self):
        bu.record_surface(self.db, [self.f1])
        bu.record_acted_on(self.db, [self.f1])
        u = bu.get_usage(self.db, self.f1)
        self.assertEqual(u["acted_on_count"], 1)
        self.assertEqual(u["surfaced_count"], 1)

    def test_timestamps_are_recorded(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        bu.record_acted_on(self.db, [self.f1], when=T2)
        u = bu.get_usage(self.db, self.f1)
        self.assertEqual(u["last_surfaced_at"], T1)
        self.assertEqual(u["last_acted_on_at"], T2)

    # ── Signal A ─────────────────────────────────────────────────────────────
    def test_credit_requires_outstanding_pressure(self):
        credited = bu.credit_from_new_evidence(self.db, [self._evidence(self.f1, T2)])
        self.assertEqual(credited, [])
        self.assertEqual(bu.get_usage(self.db, self.f1)["acted_on_count"], 0)

    def test_credit_pays_pressure_down_by_one(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        bu.record_surface(self.db, [self.f1], when=T1)
        bu.credit_from_new_evidence(self.db, [self._evidence(self.f1, T2)])
        self.assertEqual(bu.get_usage(self.db, self.f1)["surfaced_since_credit"], 1)

    def test_credit_requires_evidence_newer_than_the_surface(self):
        bu.record_surface(self.db, [self.f1], when=T2)
        # evidence predates the surface -> not a return on that exposure
        credited = bu.credit_from_new_evidence(self.db, [self._evidence(self.f1, T1)])
        self.assertEqual(credited, [])

    def test_credit_refuses_agent_generated_evidence(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        credited = bu.credit_from_new_evidence(
            self.db, [self._evidence(self.f1, T2, provenance=AGENT_DERIVED)]
        )
        self.assertEqual(credited, [])

    def test_credit_refuses_inferred_evidence(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        self.assertEqual(
            bu.credit_from_new_evidence(
                self.db, [self._evidence(self.f1, T2, provenance=INFERRED)]), []
        )

    def test_credit_refuses_evidence_with_no_provenance(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        self.assertEqual(
            bu.credit_from_new_evidence(self.db, [self._evidence(self.f1, T2, provenance=None)]),
            [],
        )

    def test_replaying_the_same_evidence_does_not_double_credit(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        bu.record_surface(self.db, [self.f1], when=T1)
        ev = [self._evidence(self.f1, T2)]
        self.assertEqual(bu.credit_from_new_evidence(self.db, ev), [self.f1])
        self.assertEqual(bu.credit_from_new_evidence(self.db, ev), [])
        self.assertEqual(bu.get_usage(self.db, self.f1)["acted_on_count"], 1)

    def test_credit_returns_only_what_it_credited(self):
        bu.record_surface(self.db, [self.f1, self.f2, self.f3], when=T1)
        credited = bu.credit_from_new_evidence(self.db, [self._evidence(self.f2, T2)])
        self.assertEqual(credited, [self.f2])

    def test_credit_skips_malformed_evidence(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        self.assertEqual(bu.credit_from_new_evidence(self.db, [{"fact_id": self.f1}]), [])
        self.assertEqual(bu.credit_from_new_evidence(self.db, [{"observed_at": T3}]), [])

    def test_credit_empty_input(self):
        self.assertEqual(bu.credit_from_new_evidence(self.db, []), [])

    # ── timestamps are instants, not strings ─────────────────────────────────
    def test_offset_evidence_that_is_older_does_not_credit(self):
        # 10:00+02:00 is 08:00Z — EARLIER than the 09:00Z surface, even though it
        # sorts AFTER it as a raw string. String comparison would have credited it.
        bu.record_surface(self.db, [self.f1], when="2026-09-15T09:00:00+00:00")
        credited = bu.credit_from_new_evidence(self.db, [
            {"fact_id": self.f1, "observed_at": "2026-09-15T10:00:00+02:00",
             "provenance": "harvested"},
        ])
        self.assertEqual(credited, [])

    def test_offset_evidence_that_is_newer_does_credit(self):
        bu.record_surface(self.db, [self.f1], when="2026-09-15T09:00:00+00:00")
        credited = bu.credit_from_new_evidence(self.db, [
            {"fact_id": self.f1, "observed_at": "2026-09-15T12:00:00+02:00",
             "provenance": "harvested"},
        ])
        self.assertEqual(credited, [self.f1])

    def test_write_time_is_stored_normalized_to_utc(self):
        bu.record_surface(self.db, [self.f1], when="2026-09-15T12:00:00+02:00")
        self.assertEqual(
            bu.get_usage(self.db, self.f1)["last_surfaced_at"],
            "2026-09-15T10:00:00+00:00",
        )

    def test_unparseable_write_time_is_rejected(self):
        with self.assertRaises(ValueError):
            bu.record_surface(self.db, [self.f1], when="not a timestamp")

    def test_unparseable_evidence_time_is_skipped(self):
        bu.record_surface(self.db, [self.f1], when=T1)
        self.assertEqual(bu.credit_from_new_evidence(self.db, [
            {"fact_id": self.f1, "observed_at": "whenever", "provenance": "harvested"},
        ]), [])


if __name__ == "__main__":
    unittest.main()