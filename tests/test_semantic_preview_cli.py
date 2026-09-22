from __future__ import annotations

import subprocess
import sys
import unittest


class TestSemanticPreviewCli(unittest.TestCase):
    def test_module_help_describes_read_only_preview(self):
        result = subprocess.run(
            [sys.executable, "-m", "agent_diary.cli.semantic_preview", "--help"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("read-only", result.stdout.lower())
        self.assertIn("--topic", result.stdout)
        self.assertIn("--purpose", result.stdout)


if __name__ == "__main__":
    unittest.main()
