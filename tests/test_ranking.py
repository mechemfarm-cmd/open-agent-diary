"""Tests for belief ranking, spend/replenish salience, and recall selection.

Task 5 + 6 of .hermes/plans/2026-09-15_evidential-belief-layer-plan.md
Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_ranking -v
"""

from __future__ import annotations

import inspect
import unittest

from agent_diary.analytics import ranking as rk
from agent_diary.analytics.signal_policy import CURATED, HARVESTED, OBSERVED, UNKNOWN


def cand(statement, confidence, provenance=HARVESTED, strength=1.0,
         pressure=None, provisional=False):
    return rk.Candidate(
        fact_id=f"f_{statement[:12]}",
        statement=statement,
        provenance=provenance,
        strength=strength,
        confidence=confidence,
        surfaced_since_credit=pressure,
        provisional=provisional,
    )


class TestSalienceOutstandingPressure(unittest.TestCase):
    """Surfacing SPENDS. Acting on a fact pays the debt down at write time."""

    def test_unknown_usage_is_neutral(self):
        self.assertEqual(rk.salience(), 1.0)
        self.assertEqual(rk.salience(surfaced_since_credit=None), 1.0)

    def test_no_outstanding_pressure_is_full_value(self):
        self.assertEqual(rk.salience(surfaced_since_credit=0), 1.0)

    def test_more_pressure_lowers_salience(self):
        self.assertGreater(
            rk.salience(surfaced_since_credit=1),
            rk.salience(surfaced_since_credit=20),
        )

    def test_salience_is_monotonic_in_pressure(self):
        values = [rk.salience(surfaced_since_credit=n) for n in range(0, 60, 5)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_acted_on_count_is_not_a_scoring_input(self):
        # Lifetime credit used to offset pressure, which let a fact that was
        # useful once immunise itself against all future unearned exposure.
        params = set(inspect.signature(rk.salience).parameters)
        self.assertNotIn("acted_on_count", params)

    def test_unbounded_exposure_converges_above_the_floor(self):
        # Documented honestly: SPEND_WEIGHT caps the penalty, so infinite
        # unearned exposure bottoms out near 0.40, not at SALIENCE_FLOOR. The
        # spend is a nudge, not a guarantee of rotation.
        s = rk.salience(surfaced_since_credit=10 ** 6)
        self.assertAlmostEqual(s, 1.0 - rk.SPEND_WEIGHT, places=2)
        self.assertGreater(s, rk.SALIENCE_FLOOR)

    def test_negative_pressure_rejected(self):
        with self.assertRaises(ValueError):
            rk.salience(surfaced_since_credit=-1)

    def test_salience_never_exceeds_the_ceiling(self):
        self.assertLessEqual(rk.salience(surfaced_since_credit=0), rk.SALIENCE_CEILING)


class TestScore(unittest.TestCase):
    def test_ineligible_classes_score_zero(self):
        for cls in (OBSERVED, UNKNOWN):
            with self.subTest(cls=cls):
                self.assertEqual(rk.score(cand("x", 1.0, provenance=cls)), 0.0)

    def test_eligible_classes_can_score(self):
        self.assertGreater(rk.score(cand("x", 0.5, provenance=CURATED)), 0.0)
        self.assertGreater(rk.score(cand("x", 0.5, provenance=HARVESTED)), 0.0)

    def test_pressure_reduces_score(self):
        self.assertLess(
            rk.score(cand("a", 0.6, pressure=30)),
            rk.score(cand("a", 0.6, pressure=0)),
        )

    def test_confidence_and_strength_both_matter(self):
        high = rk.score(cand("a", 0.8, strength=1.0))
        low_conf = rk.score(cand("a", 0.2, strength=1.0))
        low_str = rk.score(cand("a", 0.8, strength=0.2))
        self.assertGreater(high, low_conf)
        self.assertGreater(high, low_str)


class TestRanking(unittest.TestCase):
    def test_higher_confidence_ranks_first(self):
        out = rk.rank([cand("weak", 0.2), cand("strong", 0.8)])
        self.assertEqual(out[0][0].statement, "strong")

    def test_surfacing_never_raises_the_score(self):
        never = cand("never surfaced", 0.6, pressure=0)
        lots = cand("surfaced a lot", 0.6, pressure=50)
        self.assertLess(rk.score(lots), rk.score(never))

    def test_heavy_surfacing_cannot_overturn_a_large_confidence_gap(self):
        out = rk.rank([
            cand("certain but unsurfaced", 0.80, pressure=0),
            cand("weak but surfaced", 0.45, pressure=50),
        ])
        self.assertEqual(out[0][0].statement, "certain but unsurfaced")

    def test_known_limitation_a_very_strong_fact_survives_any_exposure(self):
        # Honest documentation of the limit Codex identified: because the spend
        # tops out at SPEND_WEIGHT, a ~2.2x better baseline wins even after
        # unbounded unearned exposure. Asserted so the claim is visible rather
        # than implied.
        out = rk.rank([
            cand("strong, hammered", 0.90, pressure=50),
            cand("weak, never shown", 0.35, pressure=0),
        ])
        self.assertEqual(out[0][0].statement, "strong, hammered")

    def test_a_never_surfaced_fact_outranks_an_equal_one_heavily_surfaced(self):
        out = rk.rank([
            cand("shown fifty times", 0.60, pressure=50),
            cand("never shown", 0.60, pressure=0),
        ])
        self.assertEqual(out[0][0].statement, "never shown")

    def test_ineligible_candidates_never_appear(self):
        out = rk.rank([cand("telemetry", 0.99, provenance=OBSERVED)])
        self.assertEqual(out, [])

    def test_respects_limit(self):
        items = [cand(f"s{i}", 0.5) for i in range(20)]
        self.assertEqual(len(rk.rank(items, limit=5)), 5)

    def test_respects_min_score(self):
        self.assertEqual(rk.rank([cand("tiny", 0.01)], min_score=0.15), [])

    def test_ordering_is_deterministic_for_ties(self):
        a, b = cand("tie a", 0.5), cand("tie b", 0.5)
        first = [c.fact_id for c, _ in rk.rank([a, b])]
        second = [c.fact_id for c, _ in rk.rank([b, a])]
        self.assertEqual(first, second)


class TestRenderBudget(unittest.TestCase):
    def test_empty_selection_renders_nothing(self):
        self.assertEqual(rk.render_block([]), "")

    def test_hard_character_budget_is_enforced(self):
        items = [cand("x" * 40, 0.9) for _ in range(20)]
        self.assertLessEqual(
            len(rk.render_block(rk.rank(items, limit=20), char_budget=200)), 200
        )

    def test_block_contains_the_statements(self):
        self.assertIn("Art runs Alice", rk.render_block(rk.rank([cand("Art runs Alice", 0.9)])))

    def test_provisional_is_marked(self):
        block = rk.render_block(rk.rank([cand("bulk derived", 0.9, provisional=True)]))
        self.assertIn("[provisional]", block)

    def test_first_hand_is_not_marked(self):
        block = rk.render_block(rk.rank([cand("observed live", 0.9, provisional=False)]))
        self.assertNotIn("[provisional]", block)

    def test_an_oversized_statement_does_not_starve_the_rest(self):
        # Breaking on the first unrenderable line made the block PERMANENTLY
        # empty when the top fact had a long statement: never rendered, so never
        # charged, so never demoted, so it blocked everything behind it forever.
        long_one = cand("x" * 500, 0.9)
        short_one = cand("short", 0.5)
        block, shown = rk.render_selection(
            rk.rank([long_one, short_one]), char_budget=60
        )
        self.assertNotEqual(block, "")
        self.assertIn("short", block)
        self.assertEqual(shown, [short_one.fact_id])

    def test_render_selection_reports_only_what_was_shown(self):
        items = [cand("a", 0.9), cand("b", 0.8), cand("c", 0.7)]
        block, shown = rk.render_selection(rk.rank(items), char_budget=10 ** 6)
        self.assertEqual(len(shown), 3)
        self.assertEqual(len(block.splitlines()), 3)


class TestNoPredicateVocabulary(unittest.TestCase):
    def test_module_has_no_predicate_names(self):
        src = inspect.getsource(rk)
        for name in ["RUNS_ON", "USES", "LOCATED_IN", "HAS_RAM", "HAS_IP_ADDRESS"]:
            with self.subTest(predicate=name):
                self.assertNotIn(name, src)


if __name__ == "__main__":
    unittest.main()