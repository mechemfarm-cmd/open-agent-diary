"""Path-resolution regression tests for the Hermes work-trace backfill.

``--data-dir`` is the data root *itself* — the same directory ``--diary-db``
points inside. The backfill used to hand its parent to ``default_paths()`` as a
project root, and ``default_paths(root)`` resolves data to ``<root>/data``. That
coincidence held only for the in-repo layout (``<checkout>/data``). With a live
root such as ``~/.local/share/agent-diary/<agent>`` it produced
``<parent>/data/index/memory.db``, which does not exist, so every run died with
``sqlite3.OperationalError: unable to open database file`` — and because the
caller exits 0 regardless, the scheduled job kept reporting success.

These tests pin the contract: the argument is the root, never its sibling.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "backfill-hermes-work-traces.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_hermes_work_traces", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResolvePathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script_module()
        self._saved_env = os.environ.pop("AGENTDIARY_DATA", None)

    def tearDown(self) -> None:
        if self._saved_env is not None:
            os.environ["AGENTDIARY_DATA"] = self._saved_env

    def test_data_dir_is_used_as_the_data_root(self) -> None:
        """The live layout: the argument is the root, not a '<project>/data' child."""
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp).resolve() / "share" / "agent-diary" / "emily"

            paths = self.module.resolve_paths(str(data_root))

            self.assertEqual(
                paths.sqlite_path, data_root.resolve() / "index" / "memory.db"
            )
            self.assertEqual(paths.data_root, data_root.resolve())

    def test_no_sibling_data_directory_is_invented(self) -> None:
        """The exact bug: <parent>/data must never be the resolved root."""
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp).resolve() / "custom-root"

            paths = self.module.resolve_paths(str(data_root))

            self.assertNotEqual(
                paths.sqlite_path,
                data_root.parent / "data" / "index" / "memory.db",
            )
            self.assertEqual(paths.data_root, data_root)

    def test_explicit_data_dir_beats_ambient_env(self) -> None:
        """The explicit argument is authoritative; the env var is a fallback only."""
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp).resolve() / "explicit"
            ambient = Path(tmp).resolve() / "ambient"
            os.environ["AGENTDIARY_DATA"] = str(ambient)

            paths = self.module.resolve_paths(str(explicit))

            self.assertEqual(
                paths.sqlite_path, explicit / "index" / "memory.db"
            )

    def test_empty_data_dir_falls_back_without_raising(self) -> None:
        """An unset argument must not crash; it degrades to the env/default root."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENTDIARY_DATA"] = str(Path(tmp).resolve() / "from-env")

            paths = self.module.resolve_paths("")

            self.assertEqual(
                paths.data_root, (Path(tmp).resolve() / "from-env").resolve()
            )


if __name__ == "__main__":
    unittest.main()
