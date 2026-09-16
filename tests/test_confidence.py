"""Tests for computed strength / confidence.

Task 2 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md
Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_confidence -v
"""

from __future__ import annotations

import inspect
import unittest

from agent_diary.analytics import confidence as cf
from agent_diary.analytics.signal_policy import (
    CURATED,
    HARVESTED,
    OBSERVED,
    UNKNOWN,
)


class TestStrength(unittest.TestCase):
    def test_strength_is_supporting_over_total(self):
        b = cf.compute_belief(n_supporting=3, n_contradicting=1)
        self.assertAlmostEqual(b.strength, 0.75)

    def test_no_evidence_gives_zero_strength_and_confidence(self):
        b = cf.compute_belief(n_supporting=0)
        self.assertEqual(b.strength, 0.0)
        self.assertEqual(b.confidence, 0.0)

    def test_contradiction_lowers_strength(self):
        clean = cf.compute_belief(n_supporting=4)
        muddied = cf.compute_belief(n_supporting=4, n_contradicting=2)
        self.assertLess(muddied.strength, clean.strength)


class TestEvidenceSaturation(unittest.TestCase):
    def test_confidence_rises_with_independent_entries(self):
        one = cf.compute_belief(n_supporting=1)
        five = cf.compute_belief(n_supporting=5)
        self.assertLess(one.confidence, five.confidence)

    def test_one_supporting_entry_is_low_confidence(self):
        b = cf.compute_belief(n_supporting=1, provenance=CURATED)
        self.assertLess(b.confidence, 0.30)

    def test_confidence_never_reaches_certainty(self):
        b = cf.compute_belief(n_supporting=100000, provenance=CURATED)
        self.assertLess(b.confidence, 1.0)
        self.assertGreater(b.confidence, 0.99)

    def test_contradiction_lowers_confidence(self):
        clean = cf.compute_belief(n_supporting=5)
        muddied = cf.compute_belief(n_supporting=5, n_contradicting=2)
        self.assertLess(muddied.confidence, clean.confidence)


class TestDecay(unittest.TestCase):
    def test_fresh_evidence_does_not_decay(self):
        self.assertEqual(cf.decay(0.0, "fast"), 1.0)

    def test_half_life_halves(self):
        self.assertAlmostEqual(cf.decay(7, "fast"), 0.5, places=6)

    def test_fast_volatility_decays_faster_than_stable(self):
        self.assertLess(cf.decay(30, "fast"), cf.decay(30, "stable"))

    def test_unknown_volatility_falls_back_to_default(self):
        self.assertEqual(cf.decay(10, "not-a-class"), cf.decay(10, cf.DEFAULT_VOLATILITY))

    def test_age_reduces_confidence(self):
        fresh = cf.compute_belief(n_supporting=5, age_days=0)
        old = cf.compute_belief(n_supporting=5, age_days=365)
        self.assertLess(old.confidence, fresh.confidence)


class TestClassCeiling(unittest.TestCase):
    def test_observed_fact_is_capped(self):
        b = cf.compute_belief(n_supporting=1000, provenance=OBSERVED)
        self.assertLessEqual(b.confidence, cf.CLASS_CEILING[OBSERVED])

    def test_unknown_provenance_is_most_restricted(self):
        b = cf.compute_belief(n_supporting=1000, provenance=UNKNOWN)
        self.assertLessEqual(b.confidence, cf.CLASS_CEILING[UNKNOWN])

    def test_curated_outranks_harvested_at_equal_evidence(self):
        c = cf.compute_belief(n_supporting=20, provenance=CURATED)
        h = cf.compute_belief(n_supporting=20, provenance=HARVESTED)
        self.assertGreater(c.confidence, h.confidence)

    def test_provenance_is_carried_through(self):
        self.assertEqual(
            cf.compute_belief(n_supporting=1, provenance=CURATED).provenance, CURATED
        )


class TestValidation(unittest.TestCase):
    def test_negative_counts_rejected(self):
        with self.assertRaises(ValueError):
            cf.compute_belief(n_supporting=-1)
        with self.assertRaises(ValueError):
            cf.compute_belief(n_supporting=1, n_contradicting=-1)
        with self.assertRaises(ValueError):
            cf.compute_belief(n_supporting=1, age_days=-1)


class TestNonUseDecay(unittest.TestCase):
    """Salience by usage, not by taxonomy."""

    def test_unknown_usage_is_neutral_not_punitive(self):
        self.assertEqual(cf.non_use_decay(None), 1.0)

    def test_recent_use_is_neutral(self):
        self.assertEqual(cf.non_use_decay(0.0), 1.0)
        self.assertEqual(cf.non_use_decay(cf.NON_USE_GRACE_DAYS), 1.0)

    def test_past_grace_fades_by_half_life(self):
        one_half_life_past_grace = cf.NON_USE_GRACE_DAYS + cf.NON_USE_HALF_LIFE_DAYS
        self.assertAlmostEqual(cf.non_use_decay(one_half_life_past_grace), 0.5, places=6)

    def test_longer_unused_fades_further(self):
        self.assertLess(cf.non_use_decay(400), cf.non_use_decay(35))

    def test_negative_usage_age_rejected(self):
        with self.assertRaises(ValueError):
            cf.non_use_decay(-1)

    def test_unknown_usage_does_not_change_confidence(self):
        a = cf.compute_belief(n_supporting=3)
        b = cf.compute_belief(n_supporting=3, days_since_last_use=None)
        self.assertEqual(a.confidence, b.confidence)

    def test_the_corpus_today_is_neutral_because_recall_is_not_tracked(self):
        # Everything currently stored must NOT be penalised for non-use.
        b = cf.compute_belief(n_supporting=2, age_days=0, days_since_last_use=None)
        self.assertAlmostEqual(b.confidence, cf.saturating_evidence(2), places=6)


class TestNoPredicateVocabulary(unittest.TestCase):
    def test_module_has_no_predicate_names(self):
        src = inspect.getsource(cf)
        for name in ["RUNS_ON", "USES", "LOCATED_IN", "HAS_RAM", "HAS_IP_ADDRESS"]:
            with self.subTest(predicate=name):
                self.assertNotIn(name, src)


if __name__ == "__main__":
    unittest.main()
