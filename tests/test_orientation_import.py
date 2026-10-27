from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths
from agent_diary.orientation import create_run, load_run, set_consent
from agent_diary.orientation_import import import_approved_history


def make_paths(root: Path) -> Paths:
    data = root / "data"
    return Paths(
        root=root,
        data_root=data,
        entries_dir=data / "entries",
        work_trace_dir=data / "work_trace",
        overlays_dir=data / "overlays",
        artifacts_dir=data / "artifacts",
        imports_dir=data / "imports",
        index_dir=data / "index",
        config_dir=data / "config",
        archive_dir=data / "archives",
        sqlite_path=data / "index" / "memory.db",
    )


class TestOrientationImport(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.paths = make_paths(self.root)
        self.history = self.root / "history.jsonl"
        self.history.write_text(
            "\n".join(
                [
                    json.dumps({"message_id": "1", "created_at": "2026-01-01T00:00:00Z", "author_role": "human", "speaker": "User", "content": "Question"}),
                    json.dumps({"message_id": "2", "created_at": "2026-01-01T00:01:00Z", "author_role": "agent", "speaker": "Agent", "content": "Answer"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        create_run(
            self.paths,
            [{"source_id": "history", "kind": "hermes_history", "locator": str(self.history), "scope": "content"}],
            run_id="import-run",
        )

    def test_pending_history_cannot_import(self):
        with self.assertRaises(PermissionError):
            import_approved_history(self.paths, run_id="import-run", source_id="history", format_name="generic-message-jsonl")

    def test_dry_run_does_not_create_entries_or_artifact(self):
        set_consent(self.paths, "import-run", "history", "approved")
        result = import_approved_history(
            self.paths,
            run_id="import-run",
            source_id="history",
            format_name="generic-message-jsonl",
            dry_run=True,
        )
        self.assertFalse(result["imported"])
        self.assertEqual(result["imported_count"], 1)
        self.assertIsNone(result["artifact_id"])
        self.assertEqual(list(self.paths.entries_dir.glob("*")), [])

    def test_approved_import_is_idempotent_and_links_artifact(self):
        set_consent(self.paths, "import-run", "history", "approved")
        first = import_approved_history(
            self.paths,
            run_id="import-run",
            source_id="history",
            format_name="generic-message-jsonl",
        )
        second = import_approved_history(
            self.paths,
            run_id="import-run",
            source_id="history",
            format_name="generic-message-jsonl",
        )
        self.assertTrue(first["imported"])
        self.assertEqual(first["imported_count"], 1)
        self.assertEqual(second["imported_count"], 0)
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        manifest = load_run(self.paths, "import-run")
        source = manifest["sources"][0]
        self.assertEqual(source["metadata"]["artifact_id"], first["artifact_id"])
        self.assertEqual(len(list(self.paths.entries_dir.rglob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
