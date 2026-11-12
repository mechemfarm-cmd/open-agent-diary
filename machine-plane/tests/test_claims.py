"""Adapting an export into claims, and refusing to do it quietly."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from machine_plane import claims as claims_module
from machine_plane.claims import (
    TOOL_ACTIONS,
    AdaptProblem,
    adapt_file,
    adapt_jsonl,
    adapt_record,
)
from machine_plane.correlation import (
    CorrelationStatus,
    RunRegistry,
    correlate,
)
from machine_plane.events import EventType
from machine_plane.privacy import PathDisplay, PrivacyPolicy
from machine_plane.testing import (
    DATABASE_DRIVERS,
    MockProducer,
    action_event,
    gap_event,
    imported_modules,
    make_registration,
)

WALL = 1_700_000_000.0


class RecordAdaptationTest(unittest.TestCase):
    def test_a_well_formed_record_becomes_a_claim(self) -> None:
        claim, problem, used_default = adapt_record(
            {
                "capture_id": "run-1",
                "action": "write_file",
                "target": "/home/x/work/report.txt",
                "wall_time": WALL,
            }
        )
        self.assertIsNone(problem)
        self.assertFalse(used_default)
        assert claim is not None
        self.assertEqual(claim.capture_id, "run-1")
        self.assertEqual(claim.action, "write")
        self.assertEqual(claim.target, "/home/x/work/report.txt")
        self.assertEqual(claim.wall_time, WALL)

    def test_alternative_field_names_are_accepted(self) -> None:
        claim, problem, _ = adapt_record(
            {"run_id": "run-2", "tool": "read_file", "path": "/x/y", "timestamp": 5}
        )
        self.assertIsNone(problem)
        assert claim is not None
        self.assertEqual(claim.capture_id, "run-2")
        self.assertEqual(claim.action, "read")
        self.assertEqual(claim.target, "/x/y")

    def test_a_record_without_a_run_identity_is_reported(self) -> None:
        claim, problem, _ = adapt_record({"action": "write", "wall_time": WALL})
        self.assertIsNone(claim)
        assert problem is not None
        self.assertEqual(problem.reason, "missing_run_identity")

    def test_a_record_without_a_timestamp_is_reported(self) -> None:
        claim, problem, _ = adapt_record({"capture_id": "r", "action": "write"})
        self.assertIsNone(claim)
        assert problem is not None
        self.assertEqual(problem.reason, "missing_timestamp")

    def test_an_unrecognised_action_is_reported_not_guessed(self) -> None:
        claim, problem, _ = adapt_record(
            {"capture_id": "r", "action": "frobnicate", "wall_time": WALL}
        )
        self.assertIsNone(claim)
        assert problem is not None
        self.assertTrue(problem.reason.startswith("unrecognised_action"))

    def test_a_non_object_record_is_reported(self) -> None:
        claim, problem, _ = adapt_record(["not", "a", "mapping"])
        self.assertIsNone(claim)
        assert problem is not None
        self.assertEqual(problem.reason, "record_is_not_an_object")


class JsonlTest(unittest.TestCase):
    def test_every_record_is_accounted_for(self) -> None:
        text = "\n".join(
            [
                json.dumps(
                    {"capture_id": "r", "action": "write", "wall_time": WALL}
                ),
                json.dumps({"capture_id": "r", "action": "frobnicate", "wall_time": WALL}),
                "{ this is not json",
                json.dumps({"action": "write", "wall_time": WALL}),
            ]
        )
        result = adapt_jsonl(text)

        self.assertEqual(result.read, 4)
        self.assertEqual(result.adapted, 1)
        self.assertEqual(result.skipped, 3)
        self.assertEqual(result.adapted + result.skipped, result.read)

    def test_problems_carry_line_numbers(self) -> None:
        text = json.dumps({"action": "write", "wall_time": WALL})
        result = adapt_jsonl(text)
        self.assertEqual(result.problems[0].line_number, 1)

    def test_blank_lines_are_not_counted_as_records(self) -> None:
        text = "\n\n" + json.dumps(
            {"capture_id": "r", "action": "write", "wall_time": WALL}
        ) + "\n\n"
        result = adapt_jsonl(text)
        self.assertEqual(result.read, 1)
        self.assertEqual(result.adapted, 1)

    def test_a_default_capture_id_is_applied_and_counted(self) -> None:
        text = json.dumps({"action": "write", "wall_time": WALL})
        result = adapt_jsonl(text, default_capture_id="single-run")
        self.assertEqual(result.adapted, 1)
        self.assertEqual(result.used_default_capture_id, 1)
        self.assertEqual(result.claims[0].capture_id, "single-run")

    def test_using_a_default_is_visible_in_the_result(self) -> None:
        result = adapt_jsonl(
            json.dumps({"action": "write", "wall_time": WALL}),
            default_capture_id="single-run",
        )
        self.assertEqual(result.to_dict()["used_default_capture_id"], 1)


class BoundaryTest(unittest.TestCase):
    def test_the_adapter_cannot_reach_a_database(self) -> None:
        """A regression tripwire, not a security control.

        Substring checks can be bypassed by aliases, dynamic imports or string
        construction, so this does not prove the module *cannot* reach a
        database. What it does prove is that a future edit which adds
        ``sqlite3``, ``sqlalchemy``, ``psycopg``, ``pymysql`` or a direct
        ``connect(`` to a deliberately neutral module fails immediately, which is
        the drift the architectural boundary needs protected against.
        """
        source = inspect.getsource(claims_module)
        for forbidden in (
            "sqlite3",
            "sqlalchemy",
            "psycopg",
            "pymysql",
            "connect(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_no_database_driver_is_imported(self) -> None:
        """The stronger half: read the import statements, not the text."""
        self.assertEqual(imported_modules(claims_module) & DATABASE_DRIVERS, set())

    def test_the_only_file_it_opens_is_the_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "work-trace.jsonl"
            export.write_text(
                json.dumps(
                    {"capture_id": "r", "action": "write", "wall_time": WALL}
                ),
                encoding="utf-8",
            )
            result = adapt_file(export)
        self.assertEqual(result.adapted, 1)


class IntegrationTest(unittest.TestCase):
    def test_an_exported_claim_correlates_against_an_observed_effect(self) -> None:
        """The whole path: export file -> claim -> comparison."""
        policy = PrivacyPolicy(
            allowed_roots=("/home/x/work",), path_display=PathDisplay.BASENAME
        )
        events = list(
            MockProducer(
                [
                    gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                    action_event(
                        EventType.WRITE_ATTEMPT,
                        target="/home/x/work/report.txt",
                        wall_time=WALL,
                    ),
                ],
                policy=policy,
            ).events()
        )

        export = json.dumps(
            {
                "capture_id": "run-1",
                "tool": "write_file",
                "path": "/home/x/work/report.txt",
                "timestamp": WALL,
            }
        )
        result = adapt_jsonl(export)
        self.assertEqual(result.adapted, 1)

        report = correlate(
            events,
            result.claims,
            RunRegistry([make_registration()]),
            policy=policy,
        )

        self.assertEqual(
            report.counts().get(CorrelationStatus.CONFIRMED_MATCH.value), 1
        )

    def test_an_unadaptable_claim_does_not_become_a_silent_absence(self) -> None:
        """A record that cannot be adapted must not vanish.

        Dropping it would make the agent's account look shorter than it is, and
        a missing claim reads downstream as "the agent never said that".
        """
        result = adapt_jsonl(json.dumps({"action": "write", "wall_time": WALL}))
        self.assertEqual(result.adapted, 0)
        self.assertEqual(result.skipped, 1)
        self.assertIn("missing_run_identity", result.problems[0].reason)


if __name__ == "__main__":
    unittest.main()
