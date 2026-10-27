from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths
from agent_diary.orientation import create_run, set_consent
from agent_diary.orientation_preview import preview_approved_source


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


class TestOrientationPreview(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.paths = make_paths(self.root)
        self.transcript = self.root / "history.jsonl"
        self.transcript.write_text(
            "\n".join(
                [
                    json.dumps({"message_id": "1", "created_at": "2026-01-01T00:00:00Z", "author_role": "human", "speaker": "User", "content": "A private question"}),
                    json.dumps({"message_id": "2", "created_at": "2026-01-01T00:01:00Z", "author_role": "agent", "speaker": "Agent", "content": "A private answer"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _run(self, scope="content"):
        run = create_run(
            self.paths,
            [{"source_id": "history", "kind": "hermes_history", "locator": str(self.transcript), "scope": scope}],
            run_id="preview-run",
        )
        return run

    def test_pending_source_cannot_be_previewed(self):
        self._run()
        with self.assertRaises(PermissionError):
            preview_approved_source(self.paths, "preview-run", "history", format_name="generic-message-jsonl")

    def test_metadata_scope_cannot_be_previewed_even_when_approved(self):
        self._run(scope="metadata")
        set_consent(self.paths, "preview-run", "history", "approved")
        with self.assertRaises(PermissionError):
            preview_approved_source(self.paths, "preview-run", "history", format_name="generic-message-jsonl")

    def test_approved_content_preview_is_bounded_and_does_not_import(self):
        self._run()
        set_consent(self.paths, "preview-run", "history", "approved")
        result = preview_approved_source(
            self.paths,
            "preview-run",
            "history",
            format_name="generic-message-jsonl",
            max_messages=1,
            sample_chars=8,
        )
        self.assertEqual(result["message_count"], 2)
        self.assertEqual(result["sample_count"], 1)
        self.assertTrue(result["content_access_granted"])
        self.assertLessEqual(len(result["samples"][0]["content"]), 8)
        self.assertEqual(list(self.paths.entries_dir.glob("*")), [])


if __name__ == "__main__":
    unittest.main()
