"""The export format: writing it, checking it, and going round the loop."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from machine_plane import export as export_module
from machine_plane.claims import adapt_jsonl
from machine_plane.export import (
    EXPORT_VERSION,
    to_record,
    validate_export,
    validate_file,
    write_claims,
    write_export,
)
from machine_plane.testing import make_claim

WALL = 1_700_000_000.0


class RecordShapeTest(unittest.TestCase):
    def test_unset_optional_fields_are_omitted_not_nulled(self) -> None:
        """A null target would read as "recorded as nothing"."""
        record = to_record(make_claim("write", target=None, monotonic_time=None, tool=None))
        self.assertNotIn("target", record)
        self.assertNotIn("monotonic_time", record)
        self.assertNotIn("tool", record)

    def test_set_optional_fields_are_written(self) -> None:
        record = to_record(make_claim("write", target="/x/y.txt"))
        self.assertEqual(record["target"], "/x/y.txt")

    def test_the_required_fields_are_always_present(self) -> None:
        record = to_record(make_claim("write"))
        for key in ("capture_id", "action", "wall_time"):
            self.assertIn(key, record)


class WriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "export.jsonl"

    def test_claims_are_written_as_json_lines(self) -> None:
        summary = write_claims([make_claim("write"), make_claim("read")], self.path)

        self.assertEqual(summary.written, 2)
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["action"], "write")

    def test_the_write_leaves_no_temporary_file_behind(self) -> None:
        write_claims([make_claim("write")], self.path)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class RoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "export.jsonl"

    def test_claims_survive_a_write_and_an_adapt(self) -> None:
        """The loop a real user takes: write an export, then read it back."""
        original = [
            make_claim("write", capture_id="run-1", target="/home/x/work/a.txt", wall_time=WALL),
            make_claim("read", capture_id="run-1", target="/home/x/work/b.txt", wall_time=WALL + 1),
        ]
        write_claims(original, self.path)

        result = adapt_jsonl(self.path.read_text(encoding="utf-8"))

        self.assertEqual(result.adapted, 2)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(
            [claim.capture_id for claim in result.claims], ["run-1", "run-1"]
        )
        self.assertEqual(
            [claim.target for claim in result.claims],
            ["/home/x/work/a.txt", "/home/x/work/b.txt"],
        )

    def test_a_file_we_wrote_validates(self) -> None:
        write_claims([make_claim("write", target="/x/y.txt")], self.path)
        validation = validate_file(self.path)
        self.assertTrue(validation.ok)
        self.assertEqual(validation.records, 1)


class ValidationTest(unittest.TestCase):
    def test_a_missing_run_identity_is_caught_here_not_downstream(self) -> None:
        validation = validate_export(
            json.dumps({"action": "write", "wall_time": WALL})
        )
        self.assertFalse(validation.ok)
        self.assertIn("missing_capture_id", validation.problems[0].reason)

    def test_a_non_numeric_timestamp_is_caught(self) -> None:
        validation = validate_export(
            json.dumps({"capture_id": "r", "action": "write", "wall_time": "yesterday"})
        )
        self.assertIn("wall_time_not_a_number", validation.problems[0].reason)

    def test_an_unrecognised_action_is_caught(self) -> None:
        validation = validate_export(
            json.dumps({"capture_id": "r", "action": "frobnicate", "wall_time": WALL})
        )
        self.assertIn("unrecognised_action", validation.problems[0].reason)

    def test_unknown_fields_are_reported_but_not_rejected(self) -> None:
        """Refusing them would just mean being bypassed."""
        validation = validate_export(
            json.dumps(
                {
                    "capture_id": "r",
                    "action": "write",
                    "wall_time": WALL,
                    "session": "abc",
                }
            )
        )
        self.assertTrue(validation.ok)
        self.assertEqual(validation.unknown_fields, {"session": 1})

    def test_unreadable_json_is_reported_with_its_line(self) -> None:
        validation = validate_export("{ not json")
        self.assertEqual(validation.problems[0].line_number, 1)

    def test_field_coverage_is_reported(self) -> None:
        text = "\n".join(
            [
                json.dumps({"capture_id": "r", "action": "write", "wall_time": WALL}),
                json.dumps(
                    {
                        "capture_id": "r",
                        "action": "read",
                        "wall_time": WALL,
                        "target": "/x",
                    }
                ),
            ]
        )
        validation = validate_export(text)
        self.assertEqual(validation.records, 2)
        self.assertEqual(validation.field_counts["target"], 1)
        self.assertEqual(validation.field_counts["capture_id"], 2)

    def test_an_empty_export_validates_with_no_records(self) -> None:
        validation = validate_export("")
        self.assertTrue(validation.ok)
        self.assertEqual(validation.records, 0)


class BoundaryTest(unittest.TestCase):
    def test_the_exporter_cannot_reach_a_database(self) -> None:
        source = inspect.getsource(export_module)
        for forbidden in ("sqlite3", "sqlalchemy", "psycopg", "pymysql", "connect("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_format_version_is_declared(self) -> None:
        self.assertEqual(EXPORT_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
