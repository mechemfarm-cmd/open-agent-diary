"""Tests for measured volatility (churn, not a predicate table).

Task 4 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md
Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_volatility -v
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone

from agent_diary.analytics import volatility as vol

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def series(values, step_days=30):
    """Build an observation series of (timestamp, value)."""
    return [(T0 + timedelta(days=i * step_days), v) for i, v in enumerate(values)]


class TestParseTs(unittest.TestCase):
    def test_junk_values_are_not_timestamps(self):
        for junk in ["", "unknown", "NULL", "n/a", "  ", None, "-"]:
            with self.subTest(junk=junk):
                self.assertIsNone(vol.parse_ts(junk))

    def test_parses_iso(self):
        self.assertIsNotNone(vol.parse_ts("2026-09-15T12:00:00+00:00"))

    def test_parses_zulu(self):
        self.assertIsNotNone(vol.parse_ts("2026-09-15T12:00:00Z"))

    def test_naive_datetime_is_treated_as_utc(self):
        parsed = vol.parse_ts("2026-09-15T12:00:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_garbage_is_ignored_not_crashed(self):
        self.assertIsNone(vol.parse_ts("not a date"))


class TestClassifyChurn(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(vol.classify_churn(0.0), "stable")
        self.assertEqual(vol.classify_churn(1.0), "slow")
        self.assertEqual(vol.classify_churn(10.0), "medium")
        self.assertEqual(vol.classify_churn(100.0), "fast")


class TestMeasureChurn(unittest.TestCase):
    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            vol.measure_churn([])

    def test_single_observation_is_unmeasured_not_stable(self):
        c = vol.measure_churn(series(["a"]))
        self.assertEqual(c.volatility, vol.DEFAULT_UNMEASURED_VOLATILITY)
        self.assertTrue(c.provisional)

    def test_short_span_is_unmeasured_not_extrapolated(self):
        # many observations, tiny span: a burst says nothing about a year
        c = vol.measure_churn(series(["a", "b", "a", "b", "a"], step_days=1))
        self.assertEqual(c.volatility, vol.DEFAULT_UNMEASURED_VOLATILITY)
        self.assertEqual(c.churn_per_year, 0.0)

    def test_stable_series_over_a_year_is_stable(self):
        c = vol.measure_churn(series(["a"] * 12))
        self.assertEqual(c.changes, 0)
        self.assertEqual(c.volatility, "stable")

    def test_changing_series_is_volatile(self):
        # alternates every 30 days -> ~12 changes/year
        c = vol.measure_churn(series(["a", "b"] * 12))
        self.assertGreater(c.changes, 5)
        self.assertIn(c.volatility, {"medium", "fast"})

    def test_distinct_values_counted(self):
        c = vol.measure_churn(series(["a", "b", "a", "c"]))
        self.assertEqual(c.distinct_values, 3)

    def test_retrospective_is_provisional_and_observed_is_not(self):
        r = vol.measure_churn(series(["a"] * 12), source=vol.RETROSPECTIVE)
        o = vol.measure_churn(series(["a"] * 12), source=vol.OBSERVED)
        self.assertTrue(r.provisional)
        self.assertFalse(o.provisional)

    def test_missing_timestamps_do_not_crash(self):
        c = vol.measure_churn([(None, "a"), (None, "b")])
        self.assertEqual(c.observations, 2)
        self.assertIsInstance(c.volatility, str)


class TestNoPredicateVocabulary(unittest.TestCase):
    def test_module_has_no_predicate_names(self):
        src = inspect.getsource(vol)
        for name in ["RUNS_ON", "USES", "LOCATED_IN", "HAS_RAM", "HAS_IP_ADDRESS",
                     "CONNECTED_TO", "MEMBER_OF", "WORKS_ON", "OWNS", "HAS_OS"]:
            with self.subTest(predicate=name):
                self.assertNotIn(name, src)


if __name__ == "__main__":
    unittest.main()
