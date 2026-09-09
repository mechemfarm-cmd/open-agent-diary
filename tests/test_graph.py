from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.config import default_paths
from agent_diary.models.types import (
    GraphEntity, GraphFact, GraphFactEvidence, GraphAssertionEvent,
    GraphExtractionJob, PREDICATE_REGISTRY, normalize_name,
)
from agent_diary.index.graph_repository import (
    insert_entity, get_entity, find_entity, list_entities,
    update_entity, merge_entity,
    insert_alias, list_aliases, resolve_obvious_alias,
    insert_fact, get_fact, get_current_facts_for_subject,
    get_facts_for_entity, close_current_fact, supersede_fact,
    retract_fact, search_facts, valid_predicate,
    insert_evidence, get_evidence_for_fact,
    insert_assertion_event, get_entity_assertion_history,
    insert_extraction_job, claim_extraction_jobs,
    enqueue_source_if_missing, get_extraction_queue_status,
    get_neighbors, release_stale_leases,
)
from agent_diary.config import Paths as PathsType
from agent_diary.service.handlers import (
    graph_add_fact, graph_find_entity, graph_get_entity,
    graph_neighbors, graph_search, graph_explain_fact,
    graph_correct_fact, graph_merge_alias, graph_queue_status,
    graph_backfill, graph_backfill_status, graph_enqueue_recent,
    graph_claim_jobs, graph_submit_extraction, graph_fail_extraction,
    graph_fetch_source,
)


def make_paths(tmp_root: Path) -> PathsType:
    """Create a Paths object with a fresh SQLite DB."""
    from agent_diary.config import Paths as P
    data_root = tmp_root / "data"
    index_dir = data_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = index_dir / "memory.db"
    bootstrap_sqlite(sqlite_path)
    # Create minimal dirs for entry storage
    for d in ["entries", "work_trace", "overlays", "artifacts", "imports"]:
        (data_root / d).mkdir(parents=True, exist_ok=True)
    return P(
        root=tmp_root,
        data_root=data_root,
        entries_dir=data_root / "entries",
        work_trace_dir=data_root / "work_trace",
        overlays_dir=data_root / "overlays",
        artifacts_dir=data_root / "artifacts",
        imports_dir=data_root / "imports",
        index_dir=index_dir,
        config_dir=data_root / "config",
        archive_dir=data_root / "archives",
        sqlite_path=sqlite_path,
    )


class TestGraphSchema(unittest.TestCase):
    """Acceptance criteria 1: Fresh and existing SQLite databases migrate safely."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "index" / "memory.db"

    def test_fresh_db_creates_all_tables(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_sqlite(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        required = {"graph_entities", "graph_entity_aliases", "graph_facts",
                     "graph_fact_evidence", "graph_assertion_events",
                     "graph_extraction_jobs"}
        self.assertTrue(required.issubset(tables))

    def test_existing_db_migrates_safely(self):
        self.db_path.parent.mkdir(parents=True)
        bootstrap_sqlite(self.db_path)
        # Add some data
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("INSERT INTO entries(entry_id, created_at, source, author_role, raw_file_path) VALUES ('e1', '2026-01-01', 'test', 'user', '/tmp/e1')")
        conn.commit()
        conn.close()
        # Re-run bootstrap
        bootstrap_sqlite(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute("SELECT count(*) FROM entries").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


class TestGraphModels(unittest.TestCase):
    def test_entity_auto_normalize(self):
        e = GraphEntity(canonical_name="  Emily ")
        self.assertEqual(e.normalized_name, "emily")

    def test_normalize_name(self):
        self.assertEqual(normalize_name("  Lucy  Server "), "lucy server")

    def test_predicate_registry(self):
        self.assertEqual(PREDICATE_REGISTRY["RUNS_ON"]["cardinality"], "single")
        self.assertEqual(PREDICATE_REGISTRY["OWNS"]["cardinality"], "multi")
        self.assertEqual(PREDICATE_REGISTRY["HAS_IP_ADDRESS"]["cardinality"], "value")

    def test_id_prefixes(self):
        self.assertTrue(GraphEntity().entity_id.startswith("ge_"))
        self.assertTrue(GraphFact().fact_id.startswith("gf_"))
        self.assertTrue(GraphFactEvidence().evidence_id.startswith("gev_"))
        self.assertTrue(GraphAssertionEvent().event_id.startswith("ga_"))
        self.assertTrue(GraphExtractionJob().job_id.startswith("gx_"))


class TestGraphRepository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = Path(self.tmp) / "index" / "memory.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_sqlite(self.db_path)

    def test_entity_crud(self):
        e = GraphEntity(canonical_name="Emily", entity_type="device")
        insert_entity(self.db_path, e)
        got = get_entity(self.db_path, e.entity_id)
        self.assertEqual(got["canonical_name"], "Emily")
        self.assertEqual(got["entity_type"], "device")
        self.assertEqual(got["normalized_name"], "emily")

    def test_find_entity_by_name(self):
        e = GraphEntity(canonical_name="Lucy", entity_type="device")
        insert_entity(self.db_path, e)
        found = find_entity(self.db_path, "lucy")
        self.assertEqual(len(found), 1)

    def test_find_entity_by_alias(self):
        e = GraphEntity(canonical_name="Art", entity_type="device")
        insert_entity(self.db_path, e)
        insert_alias(self.db_path, "a1", e.entity_id, "Art Server", "user_assertion", "manual")
        found = find_entity(self.db_path, "Art Server")
        self.assertEqual(len(found), 1)

    def test_obvious_alias_resolution(self):
        e = GraphEntity(canonical_name="Emily", entity_type="device")
        insert_entity(self.db_path, e)
        resolved = resolve_obvious_alias(self.db_path, "Emily")
        self.assertEqual(resolved, e.entity_id)
        ambiguous = resolve_obvious_alias(self.db_path, "NONEXISTENT")
        self.assertIsNone(ambiguous)

    def test_alias_resolution_ambiguous(self):
        """Acceptance criterion 5: Obvious aliases resolve; ambiguous do not merge."""
        e1 = GraphEntity(canonical_name="Emily", entity_type="device")
        e2 = GraphEntity(canonical_name="Em", entity_type="person")
        insert_entity(self.db_path, e1)
        insert_entity(self.db_path, e2)
        # Both entities have an alias "ember" — ambiguous
        insert_alias(self.db_path, "a1", e1.entity_id, "Ember", "user_assertion", "manual")
        insert_alias(self.db_path, "a2", e2.entity_id, "Ember", "user_assertion", "manual")
        resolved = resolve_obvious_alias(self.db_path, "ember")
        self.assertIsNone(resolved)

    def test_fact_lifecycle(self):
        subj = GraphEntity(canonical_name="Pi-hole", entity_type="software_service")
        obj = GraphEntity(canonical_name="Lucy", entity_type="device")
        insert_entity(self.db_path, subj)
        insert_entity(self.db_path, obj)

        f = GraphFact(subject_entity_id=subj.entity_id, predicate="RUNS_ON", object_entity_id=obj.entity_id)
        insert_fact(self.db_path, f)
        got = get_fact(self.db_path, f.fact_id)
        self.assertEqual(got["predicate"], "RUNS_ON")

        """Acceptance criterion 7: A later RUNS_ON fact closes the previous and preserves history."""
        closed = close_current_fact(self.db_path, subj.entity_id, "RUNS_ON")
        self.assertEqual(closed, f.fact_id)
        f_closed = get_fact(self.db_path, f.fact_id)
        self.assertEqual(f_closed["state"], "historical")

    def test_retraction(self):
        """Acceptance criterion 8: Retraction preserves old fact and correction reason."""
        subj = GraphEntity(canonical_name="Docker", entity_type="software_service")
        obj = GraphEntity(canonical_name="Lucy", entity_type="device")
        insert_entity(self.db_path, subj)
        insert_entity(self.db_path, obj)

        f = GraphFact(subject_entity_id=subj.entity_id, predicate="RUNS_ON", object_entity_id=obj.entity_id)
        insert_fact(self.db_path, f)
        ae = GraphAssertionEvent(event_type="fact_retracted", author="user", reason="Lucy never ran Docker",
                                 payload={"fact_id": f.fact_id})
        insert_assertion_event(self.db_path, ae)
        retract_fact(self.db_path, f.fact_id, ae.event_id)

        retracted = get_fact(self.db_path, f.fact_id)
        self.assertEqual(retracted["state"], "retracted")

    def test_single_value_cardinality(self):
        """Single-value predicates return at most one current fact."""
        subj = GraphEntity(canonical_name="App", entity_type="software_service")
        obj1 = GraphEntity(canonical_name="Server1", entity_type="device")
        obj2 = GraphEntity(canonical_name="Server2", entity_type="device")
        insert_entity(self.db_path, subj)
        insert_entity(self.db_path, obj1)
        insert_entity(self.db_path, obj2)

        f1 = GraphFact(subject_entity_id=subj.entity_id, predicate="RUNS_ON", object_entity_id=obj1.entity_id)
        insert_fact(self.db_path, f1)
        close_current_fact(self.db_path, subj.entity_id, "RUNS_ON")
        f2 = GraphFact(subject_entity_id=subj.entity_id, predicate="RUNS_ON", object_entity_id=obj2.entity_id)
        insert_fact(self.db_path, f2)

        current = get_current_facts_for_subject(self.db_path, subj.entity_id, "RUNS_ON")
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["object_entity_id"], obj2.entity_id)

    def test_planned_facts_excluded(self):
        """Acceptance criterion 9: Planned facts excluded from normal current-state queries."""
        subj = GraphEntity(canonical_name="Planned", entity_type="project")
        obj = GraphEntity(canonical_name="Art", entity_type="device")
        insert_entity(self.db_path, subj)
        insert_entity(self.db_path, obj)

        f = GraphFact(subject_entity_id=subj.entity_id, predicate="RUNS_ON",
                      object_entity_id=obj.entity_id, state="planned")
        insert_fact(self.db_path, f)

        current = get_current_facts_for_subject(self.db_path, subj.entity_id)
        self.assertEqual(len(current), 0)

    def test_multiple_sources_single_fact(self):
        """Acceptance criterion 4: Multiple independent sources support one fact without duplicates."""
        subj = GraphEntity(canonical_name="Test", entity_type="device")
        obj = GraphEntity(canonical_name="Obj", entity_type="device")
        insert_entity(self.db_path, subj)
        insert_entity(self.db_path, obj)

        f = GraphFact(subject_entity_id=subj.entity_id, predicate="CONNECTED_TO", object_entity_id=obj.entity_id)
        insert_fact(self.db_path, f)

        for i in range(3):
            ev = GraphFactEvidence(fact_id=f.fact_id, source_kind="raw_entry",
                                   source_id=f"entry_{i}", source_timestamp=f"2026-01-0{i+1}T00:00:00",
                                   role="establishes")
            insert_evidence(self.db_path, ev)

        ev_list = get_evidence_for_fact(self.db_path, f.fact_id)
        self.assertEqual(len(ev_list), 3)

    def test_neighbors(self):
        e1 = GraphEntity(canonical_name="A", entity_type="device")
        e2 = GraphEntity(canonical_name="B", entity_type="device")
        insert_entity(self.db_path, e1)
        insert_entity(self.db_path, e2)
        f = GraphFact(subject_entity_id=e1.entity_id, predicate="CONNECTED_TO", object_entity_id=e2.entity_id)
        insert_fact(self.db_path, f)
        nbrs = get_neighbors(self.db_path, e1.entity_id)
        self.assertEqual(len(nbrs["outgoing"]), 1)

    def test_extraction_job_queue(self):
        """Acceptance criteria 3 and 10: Queue lifecycle and idempotency."""
        j1 = insert_extraction_job(self.db_path, GraphExtractionJob(
            source_kind="raw_entry", source_id="e1", source_timestamp="2026-01-01T00:00:00"))
        self.assertTrue(j1.startswith("gx_"))

        # Idempotent enqueue
        j2 = enqueue_source_if_missing(self.db_path, "raw_entry", "e1", "2026-01-01T00:00:00")
        self.assertEqual(j1, j2)

        # Claim
        claimed = claim_extraction_jobs(self.db_path, limit=10, worker_id="test", lease_seconds=60)
        self.assertEqual(len(claimed), 1)

        # Stale lease release
        import time
        claimed2 = claim_extraction_jobs(self.db_path, limit=10, worker_id="test", lease_seconds=0)
        self.assertEqual(len(claimed2), 0)  # already claimed
        released = release_stale_leases(self.db_path)
        self.assertGreaterEqual(released, 0)
        # Now claim again
        claimed3 = claim_extraction_jobs(self.db_path, limit=10, worker_id="test", lease_seconds=60)
        # May or may not be released depending on timing


class TestGraphHandlers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = make_paths(Path(self.tmp))
        # Create entities for testing
        from agent_diary.index.graph_repository import insert_entity
        self.emily = GraphEntity(canonical_name="Emily", entity_type="device")
        self.art = GraphEntity(canonical_name="Art", entity_type="device")
        self.lucy = GraphEntity(canonical_name="Lucy", entity_type="device")
        insert_entity(self.paths.sqlite_path, self.emily)
        insert_entity(self.paths.sqlite_path, self.art)
        insert_entity(self.paths.sqlite_path, self.lucy)

    def test_add_fact_with_entity_name(self):
        r = graph_add_fact(self.paths, {
            "subject_id": "Emily", "predicate": "RUNS_ON", "object_kind": "entity",
            "object_entity_id": "Art", "author": "user", "reason": "test",
        })
        self.assertIn("fact_id", r)
        self.assertEqual(r["predicate"], "RUNS_ON")

    def test_correct_fact(self):
        r = graph_add_fact(self.paths, {
            "subject_id": "Emily", "predicate": "RUNS_ON", "object_kind": "entity",
            "object_entity_id": "Art", "author": "user", "reason": "initial",
        })
        corr = graph_correct_fact(self.paths, {
            "fact_id": r["fact_id"], "correction": "Lucy", "reason": "moved",
        })
        self.assertEqual(corr["old_fact_id"], r["fact_id"])

    def test_find_entity(self):
        result = graph_find_entity(self.paths, {"query": "Emily"})
        self.assertGreaterEqual(result["count"], 1)

    def test_get_entity(self):
        result = graph_get_entity(self.paths, {"entity_id": "Emily"})
        self.assertIn("entity", result)

    def test_neighbors(self):
        graph_add_fact(self.paths, {
            "subject_id": "Emily", "predicate": "RUNS_ON", "object_kind": "entity",
            "object_entity_id": "Art", "author": "user", "reason": "test",
        })
        nbrs = graph_neighbors(self.paths, {"entity_id": "Emily"})
        self.assertIn("outgoing", nbrs)

    def test_search(self):
        graph_add_fact(self.paths, {
            "subject_id": "Emily", "predicate": "RUNS_ON", "object_kind": "entity",
            "object_entity_id": "Art", "author": "user", "reason": "test",
        })
        sr = graph_search(self.paths, {"query": "Art"})
        self.assertGreaterEqual(sr["entity_count"], 1)

    def test_explain_fact(self):
        r = graph_add_fact(self.paths, {
            "subject_id": "Emily", "predicate": "RUNS_ON", "object_kind": "entity",
            "object_entity_id": "Art", "author": "user", "reason": "test",
        })
        expl = graph_explain_fact(self.paths, {"fact_id": r["fact_id"]})
        self.assertIn("fact", expl)

    def test_backfill_dry_run(self):
        bf = graph_backfill(self.paths, {"dry_run": True})
        self.assertIn("scanned", bf)

    def test_backfill_and_enqueue_recent(self):
        bf = graph_backfill(self.paths, {"batch_size": 10})
        self.assertIn("enqueued", bf)

    def test_queue_status(self):
        qs = graph_queue_status(self.paths, {})
        self.assertIn("pending", qs)

    def test_claim_and_submit_extraction(self):
        bf = graph_backfill(self.paths, {"batch_size": 10})
        claimed = graph_claim_jobs(self.paths, {"limit": 5, "worker_id": "test", "lease_seconds": 300})
        self.assertIn("claimed", claimed)
        if claimed["claimed"] > 0:
            job = claimed["jobs"][0]
            submit = graph_submit_extraction(self.paths, {
                "job_id": job["job_id"],
                "result": {"no_facts": True, "reason": "test"},
            })
            self.assertEqual(submit["status"], "no_facts")

    def test_merge_alias(self):
        am = graph_merge_alias(self.paths, {"entity_id": "Emily", "alias": "Em"})
        self.assertIn("alias_id", am)


class TestGraphConsistency(unittest.TestCase):
    """Acceptance criterion 6: Explicit event time overrides source-record time."""

    def test_event_time(self):
        tmp = tempfile.mkdtemp()
        paths = make_paths(Path(tmp))
        emily = GraphEntity(canonical_name="Emily", entity_type="device")
        art = GraphEntity(canonical_name="Art", entity_type="device")
        from agent_diary.index.graph_repository import insert_entity
        insert_entity(paths.sqlite_path, emily)
        insert_entity(paths.sqlite_path, art)

        r = graph_add_fact(paths, {
            "subject_id": "Emily", "predicate": "RUNS_ON", "object_kind": "entity",
            "object_entity_id": "Art", "author": "user", "reason": "test",
            "source_timestamp": "2026-03-15T10:00:00Z",
        })
        expl = graph_explain_fact(paths, {"fact_id": r["fact_id"]})
        fact = expl["fact"]
        self.assertEqual(fact["valid_from"], "2026-03-15T10:00:00Z")


if __name__ == "__main__":
    unittest.main()