from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths
from agent_diary.orientation import (
    CONSENT_APPROVED,
    CONSENT_REJECTED,
    CONSENT_REVOKED,
    CONSENT_PENDING,
    create_run,
    load_run,
    list_runs,
    set_consent,
)


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


class TestOrientationManifest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.paths = make_paths(self.root)

    def test_create_run_defaults_sources_to_pending_metadata_only(self):
        run = create_run(
            self.paths,
            [
                {"source_id": "history", "kind": "hermes_history", "locator": "sessions"},
                {"source_id": "projects", "kind": "filesystem_root", "locator": "/home/example/Projects"},
            ],
        )
        self.assertEqual(run["status"], "awaiting_consent")
        self.assertEqual(len(run["sources"]), 2)
        self.assertTrue(all(s["consent"] == CONSENT_PENDING for s in run["sources"]))
        self.assertTrue(all(s["scope"] == "metadata" for s in run["sources"]))
        self.assertEqual(load_run(self.paths, run["run_id"]), run)

    def test_approve_content_scope_is_explicit(self):
        run = create_run(
            self.paths,
            [{"source_id": "project", "kind": "project", "locator": "/work/project", "scope": "content"}],
        )
        updated = set_consent(self.paths, run["run_id"], "project", CONSENT_APPROVED, reason="user selected project")
        source = updated["sources"][0]
        self.assertEqual(source["consent"], CONSENT_APPROVED)
        self.assertEqual(source["scope"], "content")
        self.assertTrue(source["content_access_granted"])
        self.assertEqual(updated["status"], "approved")

    def test_rejection_keeps_content_access_false(self):
        run = create_run(self.paths, [{"source_id": "home", "kind": "filesystem_root", "locator": "/home/example"}])
        updated = set_consent(self.paths, run["run_id"], "home", CONSENT_REJECTED, reason="too broad")
        source = updated["sources"][0]
        self.assertEqual(source["consent"], CONSENT_REJECTED)
        self.assertFalse(source["content_access_granted"])
        self.assertEqual(updated["status"], "rejected")

    def test_revoke_is_terminal_for_source_and_run(self):
        run = create_run(self.paths, [{"source_id": "history", "kind": "hermes_history", "locator": "sessions"}])
        set_consent(self.paths, run["run_id"], "history", CONSENT_APPROVED)
        updated = set_consent(self.paths, run["run_id"], "history", CONSENT_REVOKED, reason="user withdrew consent")
        self.assertEqual(updated["sources"][0]["consent"], CONSENT_REVOKED)
        self.assertFalse(updated["sources"][0]["content_access_granted"])
        self.assertEqual(updated["status"], "revoked")

    def test_invalid_transition_and_duplicate_source_are_rejected(self):
        with self.assertRaises(ValueError):
            create_run(self.paths, [{"source_id": "same", "kind": "project", "locator": "/a"}, {"source_id": "same", "kind": "project", "locator": "/b"}])
        run = create_run(self.paths, [{"source_id": "history", "kind": "hermes_history", "locator": "sessions"}])
        set_consent(self.paths, run["run_id"], "history", CONSENT_REJECTED)
        with self.assertRaises(ValueError):
            set_consent(self.paths, run["run_id"], "history", CONSENT_APPROVED)

    def test_list_runs_is_newest_first(self):
        first = create_run(self.paths, [{"source_id": "a", "kind": "project", "locator": "/a"}], run_id="run-a")
        second = create_run(self.paths, [{"source_id": "b", "kind": "project", "locator": "/b"}], run_id="run-b")
        runs = list_runs(self.paths)
        self.assertEqual({r["run_id"] for r in runs}, {first["run_id"], second["run_id"]})


if __name__ == "__main__":
    unittest.main()
