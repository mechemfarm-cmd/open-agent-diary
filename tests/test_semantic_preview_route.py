from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_diary.index.repository import insert_entry
from agent_diary.models.types import RawEntry
from agent_diary.service.handlers import semantic_preview
from tests.test_semantic_sources import make_paths


class TestSemanticPreviewRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = make_paths(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_preview_requires_topic_and_purpose_and_enforces_bounds(self):
        with self.assertRaises(ValueError):
            semantic_preview(self.paths, {"purpose": "current_status"})
        with self.assertRaises(ValueError):
            semantic_preview(self.paths, {"topic": "Blue Finch", "purpose": "not-a-purpose"})
        with self.assertRaises(ValueError):
            semantic_preview(self.paths, {"topic": "Blue Finch", "purpose": "current_status", "limit": 500})

    def test_preview_is_read_only_and_returns_source_linked_output(self):
        entry = RawEntry(
            entry_id="entry_blue_finch_preview",
            entry_type="note",
            source="synthetic",
            author_role="human",
            content="Blue Finch migration is in dry-run before checksum comparison.",
            created_at="2026-09-22T08:30:00+00:00",
            title="Blue Finch preview",
            metadata={"semantic": {"subject": "Blue Finch migration", "role": "state"}},
        )
        raw_path = self.paths.entries_dir / "entry_blue_finch_preview.json"
        raw_path.write_text(json.dumps(entry.to_dict()), encoding="utf-8")
        insert_entry(self.paths.sqlite_path, entry, str(raw_path))
        before_hash = hashlib.sha256(self.paths.sqlite_path.read_bytes()).hexdigest()
        before_usage = self._belief_usage_rows()

        result = semantic_preview(
            self.paths,
            {"topic": "Blue Finch", "purpose": "current_status", "limit": 5, "char_budget": 1200},
        )

        after_hash = hashlib.sha256(self.paths.sqlite_path.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before_usage, self._belief_usage_rows())
        self.assertTrue(result["read_only"])
        self.assertEqual(result["purpose"], "current_status")
        self.assertIn("raw_entry:entry_blue_finch_preview", result["preview"])
        self.assertEqual(result["situation"]["current_state"][0]["source_refs"][0]["source_id"], "entry_blue_finch_preview")

    def _belief_usage_rows(self) -> int:
        with sqlite3.connect(self.paths.sqlite_path) as conn:
            exists = conn.execute("select count(*) from sqlite_master where type='table' and name='belief_usage'").fetchone()[0]
            if not exists:
                return 0
            return conn.execute("select count(*) from belief_usage").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
