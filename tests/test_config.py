import os
import tempfile
import unittest
from pathlib import Path

from agent_diary.config import default_paths


class ExternalDataRootTests(unittest.TestCase):
    def test_explicit_root_keeps_data_inside_that_root_even_when_env_is_set(self):
        with tempfile.TemporaryDirectory() as explicit, tempfile.TemporaryDirectory() as external:
            old = os.environ.get("AGENTDIARY_DATA")
            os.environ["AGENTDIARY_DATA"] = external
            try:
                paths = default_paths(Path(explicit))
            finally:
                if old is None:
                    os.environ.pop("AGENTDIARY_DATA", None)
                else:
                    os.environ["AGENTDIARY_DATA"] = old
            self.assertEqual(Path(explicit) / "data", paths.data_root)

    def test_environment_data_root_separates_runtime_state_from_checkout(self):
        with tempfile.TemporaryDirectory() as checkout, tempfile.TemporaryDirectory() as external:
            old = os.environ.get("AGENTDIARY_DATA")
            os.environ["AGENTDIARY_DATA"] = external
            try:
                paths = default_paths()
            finally:
                if old is None:
                    os.environ.pop("AGENTDIARY_DATA", None)
                else:
                    os.environ["AGENTDIARY_DATA"] = old
            self.assertEqual(Path(external), paths.data_root)
            self.assertEqual(Path.cwd(), paths.root)


if __name__ == "__main__":
    unittest.main()
