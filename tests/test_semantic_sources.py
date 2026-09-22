from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
import json

from agent_diary.config import Paths as PathsType
from agent_diary.index.sqlite_index import bootstrap_sqlite
from agent_diary.index.graph_repository import insert_entity, insert_evidence, insert_fact
from agent_diary.index.repository import insert_work_trace_event
from agent_diary.index.repository import insert_entry
from agent_diary.models.types import GraphEntity, GraphFact, GraphFactEvidence, RawEntry, WorkTraceEvent
from agent_diary.analytics.semantic_sources import collect_semantic_candidates


def make_paths(tmp_root: Path) -> PathsType:
    from agent_diary.config import Paths as P

    data_root = tmp_root / "data"
    index_dir = data_root / "index"
    index_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = index_dir / "memory.db"
    bootstrap_sqlite(sqlite_path)
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


class TestSemanticSources(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = make_paths(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_fact(self, *, subject: str = "Project Atlas", state: str = "current", obj: str = "read-only prototype") -> str:
        slug = subject.lower().replace(" ", "-")
        subj = GraphEntity(canonical_name=subject, entity_type="project")
        insert_entity(self.paths.sqlite_path, subj)
        fact = GraphFact(
            fact_id=f"gf_{slug}_{state}",
            subject_entity_id=subj.entity_id,
            predicate="USES",
            object_kind="value",
            object_value=obj,
            state=state,
            valid_from="2026-09-20T10:00:00+00:00",
            confidence="high",
            metadata={"belief": {"confidence": 0.72, "provisional": True, "provenance": "sanitized-test"}},
        )
        insert_fact(self.paths.sqlite_path, fact)
        insert_evidence(
            self.paths.sqlite_path,
            GraphFactEvidence(
                evidence_id=f"gev_{slug}_{state}",
                fact_id=fact.fact_id,
                source_kind="raw_entry",
                source_id=f"entry_{slug}_{state}",
                source_timestamp="2026-09-20T09:59:00+00:00",
                role="establishes",
            ),
        )
        return fact.fact_id

    def test_graph_candidates_preserve_provenance_timestamp_and_status(self):
        fact_id = self._seed_fact()

        candidates = collect_semantic_candidates(self.paths, topic="Project Atlas", limit=10)

        candidate = next(c for c in candidates if c.source_id == fact_id)
        self.assertEqual(candidate.source_kind, "graph_fact")
        self.assertEqual(candidate.subject, "Project Atlas")
        self.assertEqual(candidate.timestamp, "2026-09-20T10:00:00+00:00")
        self.assertEqual(candidate.status, "current")
        self.assertEqual(candidate.confidence, "high")
        self.assertTrue(candidate.provisional)
        self.assertEqual(candidate.evidence_refs[0].source_kind, "raw_entry")
        self.assertEqual(candidate.evidence_refs[0].source_id, "entry_project-atlas_current")

    def test_candidate_collection_is_bounded_and_deterministic(self):
        self._seed_fact(subject="Project Atlas", obj="first")
        self._seed_fact(subject="Agent Diary", obj="second")

        candidates = collect_semantic_candidates(self.paths, topic="", limit=1)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].timestamp, "2026-09-20T10:00:00+00:00")

    def test_arbitrary_project_names_do_not_require_seeded_facts(self):
        self._seed_fact(subject="Blue Finch migration", obj="dry-run mode")

        candidates = collect_semantic_candidates(self.paths, topic="Blue Finch", limit=10)

        self.assertEqual(candidates[0].subject, "Blue Finch migration")
        self.assertIn("dry-run mode", candidates[0].text)

    def test_empty_store_returns_no_candidates(self):
        self.assertEqual(collect_semantic_candidates(self.paths, topic="Project Atlas", limit=10), [])

    def test_work_trace_candidates_preserve_project_and_actor_without_writes(self):
        event = WorkTraceEvent(
            event_id="work_preview_001",
            event_type="command",
            summary="Previewed Project Atlas semantic-memory situation without changing live recall.",
            created_at="2026-09-21T12:00:00+00:00",
            project="Project Atlas semantic memory",
            source_surface="cli",
            actor="agent",
            related_entry_ids=["entry_project-atlas_current"],
            tags=["semantic-memory"],
        )
        insert_work_trace_event(self.paths.sqlite_path, event, str(self.paths.work_trace_dir / "work_preview_001.json"))
        before = self._count_belief_usage_rows()

        candidates = collect_semantic_candidates(self.paths, topic="Project Atlas", limit=10)

        after = self._count_belief_usage_rows()
        candidate = next(c for c in candidates if c.source_kind == "work_trace")
        self.assertEqual(candidate.source_id, "work_preview_001")
        self.assertEqual(candidate.subject, "Project Atlas semantic memory")
        self.assertEqual(candidate.author_role, "agent")
        self.assertEqual(candidate.evidence_refs[0].source_kind, "work_trace")
        self.assertEqual(before, after)

    def test_raw_entry_candidates_are_source_linked_and_do_not_require_graph_facts(self):
        entry = RawEntry(
            entry_id="entry_blue_finch_status",
            entry_type="note",
            source="synthetic",
            author_role="human",
            content="Blue Finch migration is in dry-run. Review checksum comparison before rollout.",
            created_at="2026-09-22T08:00:00+00:00",
            title="Blue Finch status",
            metadata={"semantic": {"subject": "Blue Finch migration", "role": "state"}},
        )
        raw_path = self.paths.entries_dir / "entry_blue_finch_status.json"
        raw_path.write_text(json.dumps(entry.to_dict()), encoding="utf-8")
        insert_entry(self.paths.sqlite_path, entry, str(raw_path))

        candidates = collect_semantic_candidates(self.paths, topic="Blue Finch", limit=10)

        candidate = next(c for c in candidates if c.source_kind == "raw_entry")
        self.assertEqual(candidate.source_id, "entry_blue_finch_status")
        self.assertEqual(candidate.subject, "Blue Finch migration")
        self.assertEqual(candidate.author_role, "human")
        self.assertEqual(candidate.evidence_refs[0].source_kind, "raw_entry")
        self.assertEqual(candidate.evidence_refs[0].source_id, "entry_blue_finch_status")
        self.assertEqual(candidate.metadata["semantic_role"], "state")

    def _count_belief_usage_rows(self) -> int:
        with sqlite3.connect(self.paths.sqlite_path) as conn:
            exists = conn.execute(
                "select count(*) from sqlite_master where type='table' and name='belief_usage'"
            ).fetchone()[0]
            if not exists:
                return 0
            return conn.execute("select count(*) from belief_usage").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
