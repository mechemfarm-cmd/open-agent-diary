"""Signal A end-to-end: does corroboration actually pay a fact's debt down?

Drives the REAL extraction path (`graph_submit_extraction`), not the credit
helper in isolation. The helper is already covered in test_belief_usage; what
these tests prove is that the wiring feeds it correctly — the right fact, the
right timestamp, and a provenance class that was resolved from the source
rather than assumed.

Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_signal_a_credit -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths as PathsType
from agent_diary.index.belief_usage import get_usage, record_surface
from agent_diary.index.graph_repository import (
    enqueue_source_if_missing,
    insert_entity,
    insert_fact,
)
from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.service.handlers import append_entry, graph_submit_extraction

SURFACED_AT = "2026-09-15T10:00:00+00:00"
LATER = "2026-09-15T12:00:00+00:00"
EARLIER = "2026-09-15T08:00:00+00:00"


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


class TestSignalACredit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.paths = make_paths(self.tmp)
        self.db = self.paths.sqlite_path

    # ── fixtures ─────────────────────────────────────────────────────────────
    def _existing_fact(self):
        """An entity pair plus one current fact between them."""
        from agent_diary.models.types import GraphEntity, GraphFact

        subj = GraphEntity(canonical_name="HostAlpha")
        obj = GraphEntity(canonical_name="ThingAlpha")
        insert_entity(self.db, subj)
        insert_entity(self.db, obj)
        fact = GraphFact(
            subject_entity_id=subj.entity_id, predicate="RUNS_ON",
            object_kind="entity", object_entity_id=obj.entity_id,
        )
        fact.metadata = {"belief": {
            "strength": 1.0, "confidence": 0.8,
            "provenance": "harvested", "provisional": True,
        }}
        return insert_fact(self.db, fact)

    def _job_for(self, author_role: str, timestamp: str = LATER) -> str:
        """A real raw_entry, plus an extraction job pointing at it."""
        entry = append_entry(self.paths, {
            "entry_type": "chat_log",
            "source": "signal-a-test",
            "author_role": author_role,
            "content": "HostAlpha runs ThingAlpha, confirmed again.",
            "created_at": timestamp,
        })
        return enqueue_source_if_missing(
            self.db, "raw_entry", entry["entry_id"], timestamp
        )

    def _submit_matching(self, job_id: str, timestamp: str = LATER) -> dict:
        """Submit extraction whose fact matches the existing one, so the
        pipeline takes the dedup path — new evidence for a known fact."""
        return graph_submit_extraction(self.paths, {
            "job_id": job_id,
            "result": {
                "entities": [],
                "facts": [{
                    "subject": "HostAlpha",
                    "predicate": "RUNS_ON",
                    "object": "ThingAlpha",
                    "object_kind": "entity",
                    "timestamp": timestamp,
                }],
            },
        })

    def _pressure(self, fact_id: str) -> int:
        return get_usage(self.db, fact_id)["surfaced_since_credit"]

    # ── the earn signal ──────────────────────────────────────────────────────
    def test_human_corroboration_pays_a_surfaced_facts_debt_down(self):
        fid = self._existing_fact()
        record_surface(self.db, [fid], when=SURFACED_AT)
        record_surface(self.db, [fid], when=SURFACED_AT)
        self.assertEqual(self._pressure(fid), 2)

        out = self._submit_matching(self._job_for("human"))

        self.assertEqual(out["credited"], [fid])
        self.assertEqual(self._pressure(fid), 1)

    def test_the_credit_is_reported_in_the_job_summary(self):
        fid = self._existing_fact()
        record_surface(self.db, [fid], when=SURFACED_AT)
        out = self._submit_matching(self._job_for("human"))
        self.assertIn("1 credited", out["summary"])

    def test_agent_authored_evidence_earns_nothing(self):
        # The loop we must not reopen: the agent restating a fact it was just
        # shown must not restore that fact's salience.
        fid = self._existing_fact()
        record_surface(self.db, [fid], when=SURFACED_AT)

        out = self._submit_matching(self._job_for("agent"))

        self.assertEqual(out["credited"], [])
        self.assertEqual(self._pressure(fid), 1)

    def test_a_fact_that_was_never_surfaced_earns_nothing(self):
        # New evidence for an unsurfaced fact is ordinary accumulation, not a
        # return on attention already spent.
        fid = self._existing_fact()
        out = self._submit_matching(self._job_for("human"))
        self.assertEqual(out["credited"], [])
        self.assertEqual(self._pressure(fid), 0)

    def test_evidence_older_than_the_surface_does_not_credit(self):
        fid = self._existing_fact()
        record_surface(self.db, [fid], when=SURFACED_AT)

        out = self._submit_matching(
            self._job_for("human", timestamp=EARLIER), timestamp=EARLIER
        )

        self.assertEqual(out["credited"], [])
        self.assertEqual(self._pressure(fid), 1)

    def test_replaying_the_same_job_does_not_credit_twice(self):
        fid = self._existing_fact()
        record_surface(self.db, [fid], when=SURFACED_AT)
        record_surface(self.db, [fid], when=SURFACED_AT)
        job = self._job_for("human")

        first = self._submit_matching(job)
        second = self._submit_matching(job)

        self.assertEqual(first["credited"], [fid])
        self.assertEqual(second["credited"], [])
        self.assertEqual(self._pressure(fid), 1)

    def test_a_genuinely_new_fact_is_not_credited(self):
        # Nothing to credit: a brand-new fact has no surfacing history.
        job = self._job_for("human")
        out = graph_submit_extraction(self.paths, {
            "job_id": job,
            "result": {
                "entities": [],
                "facts": [{
                    "subject": "HostBeta", "predicate": "RUNS_ON",
                    "object": "ThingBeta", "object_kind": "entity",
                    "timestamp": LATER,
                }],
            },
        })
        self.assertEqual(out["credited"], [])

    def test_no_facts_at_all_is_still_safe(self):
        job = self._job_for("human")
        out = graph_submit_extraction(self.paths, {
            "job_id": job,
            "result": {"no_facts": True, "reason": "nothing here"},
        })
        self.assertEqual(out["status"], "no_facts")


if __name__ == "__main__":
    unittest.main()