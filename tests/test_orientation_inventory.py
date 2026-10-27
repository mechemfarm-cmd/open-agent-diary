from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_diary.orientation_inventory import inventory_metadata


class TestOrientationInventory(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "docs").mkdir()
        (self.root / "docs" / "README.md").write_text("private content", encoding="utf-8")
        (self.root / "docs" / "plan.md").write_text("plan", encoding="utf-8")
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_ed25519").write_text("secret", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=secret", encoding="utf-8")

    def test_inventory_is_metadata_only_and_excludes_sensitive_paths(self):
        result = inventory_metadata(self.root)
        paths = {item["relative_path"] for item in result["items"]}
        self.assertIn("docs", paths)
        self.assertIn("docs/README.md", paths)
        self.assertNotIn(".ssh", paths)
        self.assertNotIn(".ssh/id_ed25519", paths)
        self.assertNotIn(".env", paths)
        self.assertEqual(result["counts"]["files"], 2)
        self.assertEqual(result["counts"]["directories"], 2)
        self.assertEqual(result["counts"]["excluded_directories"], 1)
        self.assertEqual(result["counts"]["excluded_files"], 1)
        self.assertTrue(all("content" not in item for item in result["items"]))

    def test_symlinked_directory_is_listed_but_not_followed(self):
        outside = Path(tempfile.mkdtemp())
        (outside / "outside.txt").write_text("outside", encoding="utf-8")
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        result = inventory_metadata(self.root)
        by_path = {item["relative_path"]: item for item in result["items"]}
        self.assertEqual(by_path["linked"]["type"], "symlink")
        self.assertNotIn("linked/outside.txt", by_path)

    def test_entry_limit_is_reported_and_deterministic(self):
        for index in range(5):
            (self.root / f"file-{index}.txt").write_text(str(index), encoding="utf-8")
        first = inventory_metadata(self.root, max_entries=3)
        second = inventory_metadata(self.root, max_entries=3)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(len(first["items"]), 3)

    def test_non_directory_root_is_rejected(self):
        file_path = self.root / "one.txt"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            inventory_metadata(file_path)


if __name__ == "__main__":
    unittest.main()
