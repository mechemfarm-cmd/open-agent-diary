from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_diary.config import Paths
from agent_diary.orientation import create_run, set_consent
from agent_diary.orientation_artifacts import (
    add_representation,
    create_artifact,
    load_artifact,
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


class TestOrientationArtifacts(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.paths = make_paths(self.root)
        self.orientation_run = create_run(
            self.paths,
            [{"source_id": "project", "kind": "project", "locator": "/work/project", "scope": "content"}],
            run_id="artifact-run",
        )

    def test_create_artifact_preserves_source_and_consent_boundary(self):
        artifact = create_artifact(
            self.paths,
            run_id="artifact-run",
            source_id="project",
            artifact_type="document",
            locator="/work/project/README.md",
            mime_type="text/markdown",
            size=128,
            modified_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(artifact["artifact_type"], "document")
        self.assertEqual(artifact["source_id"], "project")
        self.assertEqual(artifact["consent"], "pending")
        self.assertFalse(artifact["content_access_granted"])
        self.assertEqual(load_artifact(self.paths, artifact["artifact_id"]), artifact)

    def test_representation_requires_approved_content_consent(self):
        artifact = create_artifact(
            self.paths,
            run_id="artifact-run",
            source_id="project",
            artifact_type="image",
            locator="/work/project/photo.jpg",
            mime_type="image/jpeg",
        )
        with self.assertRaises(PermissionError):
            add_representation(self.paths, artifact["artifact_id"], "caption", "A vineyard")

        set_consent(self.paths, "artifact-run", "project", "approved")
        updated = add_representation(
            self.paths,
            artifact["artifact_id"],
            "caption",
            "A vineyard",
            method="vision-caption-v1",
        )
        self.assertEqual(updated["representations"][0]["kind"], "caption")
        self.assertEqual(updated["representations"][0]["content"], "A vineyard")
        self.assertEqual(updated["representations"][0]["method"], "vision-caption-v1")

    def test_invalid_artifact_type_and_negative_size_rejected(self):
        with self.assertRaises(ValueError):
            create_artifact(self.paths, run_id="artifact-run", source_id="project", artifact_type="unknown", locator="/x")
        with self.assertRaises(ValueError):
            create_artifact(self.paths, run_id="artifact-run", source_id="project", artifact_type="document", locator="/x", size=-1)

    def test_revoked_source_cannot_create_new_artifact(self):
        set_consent(self.paths, "artifact-run", "project", "revoked")
        with self.assertRaises(PermissionError):
            create_artifact(self.paths, run_id="artifact-run", source_id="project", artifact_type="document", locator="/x")


if __name__ == "__main__":
    unittest.main()
