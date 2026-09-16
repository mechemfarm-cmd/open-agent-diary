"""Tests for evidence counting (independent sources, not rows).

Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_evidence -v
"""

from __future__ import annotations

import inspect
import unittest

from agent_diary.analytics import evidence as ev


def row(role="establishes", source_kind="raw_entry", source_id="e1"):
    return {"role": role, "source_kind": source_kind, "source_id": source_id}


class TestCounting(unittest.TestCase):
    def test_empty_is_zero(self):
        c = ev.count_evidence([])
        self.assertEqual((c.supporting, c.contradicting), (0, 0))

    def test_simple_support(self):
        c = ev.count_evidence([row(source_id="e1"), row(source_id="e2")])
        self.assertEqual(c.supporting, 2)
        self.assertEqual(c.contradicting, 0)

    def test_duplicate_rows_from_one_source_count_once(self):
        # The review finding: row count must not inflate certainty.
        c = ev.count_evidence([row(source_id="e1"), row(source_id="e1"), row(source_id="e1")])
        self.assertEqual(c.supporting, 1)
        self.assertEqual(c.raw_rows, 3)
        self.assertEqual(c.duplicate_rows, 2)

    def test_distinct_sources_all_count(self):
        c = ev.count_evidence([row(source_id=f"e{i}") for i in range(5)])
        self.assertEqual(c.supporting, 5)
        self.assertEqual(c.duplicate_rows, 0)

    def test_different_source_kinds_are_different_sources(self):
        c = ev.count_evidence([
            row(source_kind="raw_entry", source_id="x"),
            row(source_kind="work_trace", source_id="x"),
        ])
        self.assertEqual(c.supporting, 2)

    def test_contradicting_role_classifies(self):
        c = ev.count_evidence([row(source_id="e1"), row(role="contradicts", source_id="e2")])
        self.assertEqual(c.supporting, 1)
        self.assertEqual(c.contradicting, 1)

    def test_all_contradict_roles_recognised(self):
        for role in sorted(ev.CONTRADICT_ROLES):
            with self.subTest(role=role):
                c = ev.count_evidence([row(role=role, source_id="e1")])
                self.assertEqual(c.contradicting, 1)

    def test_a_source_that_ever_contradicts_stays_contradicting(self):
        c = ev.count_evidence([
            row(role="supports", source_id="e1"),
            row(role="contradicts", source_id="e1"),
        ])
        self.assertEqual(c.contradicting, 1)
        self.assertEqual(c.supporting, 0)

    def test_unrecognised_role_counts_as_support(self):
        # Refuse to invent a contradiction the schema does not name.
        c = ev.count_evidence([row(role="whatever", source_id="e1")])
        self.assertEqual(c.supporting, 1)

    def test_missing_fields_do_not_crash(self):
        c = ev.count_evidence([{}, {"role": None}])
        self.assertEqual(c.distinct_sources, 1)

    def test_role_is_case_insensitive(self):
        c = ev.count_evidence([row(role="CONTRADICTS", source_id="e1")])
        self.assertEqual(c.contradicting, 1)


class TestNoPredicateVocabulary(unittest.TestCase):
    def test_module_has_no_predicate_names(self):
        src = inspect.getsource(ev)
        for name in ["RUNS_ON", "USES", "LOCATED_IN", "HAS_RAM", "HAS_IP_ADDRESS"]:
            with self.subTest(predicate=name):
                self.assertNotIn(name, src)


if __name__ == "__main__":
    unittest.main()
