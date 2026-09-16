"""Tests for two-axis provenance classification.

Run: PYTHONPATH=src .venv/bin/python -m unittest tests.test_signal_policy -v
"""

from __future__ import annotations

import inspect
import unittest

from agent_diary.analytics import signal_policy as sp


class TestSourceAxis(unittest.TestCase):
    def test_non_conversation_sources_classify_directly(self):
        self.assertEqual(sp.classify_fact(["user_assertion"]), sp.CURATED)
        self.assertEqual(sp.classify_fact(["work_trace"]), sp.OBSERVED)
        self.assertEqual(sp.classify_fact(["overlay"]), sp.INFERRED)

    def test_unrecognised_source_kind_is_unknown(self):
        self.assertEqual(sp.classify_fact(["something_new"]), sp.UNKNOWN)

    def test_no_evidence_is_unknown(self):
        self.assertEqual(sp.classify_fact([]), sp.UNKNOWN)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(sp.classify_fact(["  USER_ASSERTION "]), sp.CURATED)


class TestAttributionAxis(unittest.TestCase):
    """The blocker: raw_entry is a container, not an origin."""

    def test_raw_entry_alone_is_not_knowledge(self):
        # No attribution available -> we cannot say the human said it.
        self.assertEqual(sp.classify_fact(["raw_entry"]), sp.UNATTRIBUTED)

    def test_human_authored_entry_is_knowledge(self):
        for role in ("human", "user", "HUMAN"):
            with self.subTest(role=role):
                self.assertEqual(
                    sp.classify_fact([("raw_entry", role)]), sp.HARVESTED
                )

    def test_agent_authored_entry_is_not_knowledge(self):
        for role in ("agent", "assistant"):
            with self.subTest(role=role):
                self.assertEqual(
                    sp.classify_fact([("raw_entry", role)]), sp.AGENT_DERIVED
                )

    def test_mixed_authored_entry_is_unattributed(self):
        self.assertEqual(sp.classify_fact([("raw_entry", "mixed")]), sp.UNATTRIBUTED)

    def test_unknown_author_role_is_unattributed_not_human(self):
        self.assertEqual(sp.classify_fact([("raw_entry", "who_knows")]), sp.UNATTRIBUTED)

    def test_attribution_does_not_affect_non_conversation_sources(self):
        self.assertEqual(sp.classify_fact([("work_trace", "agent")]), sp.OBSERVED)
        self.assertEqual(sp.classify_fact([("overlay", "mixed")]), sp.INFERRED)

    def test_human_attribution_wins_over_agent_narration(self):
        # If the human said it, it is knowledge even if the agent also said it.
        self.assertEqual(
            sp.classify_fact([("raw_entry", "agent"), ("raw_entry", "human")]),
            sp.HARVESTED,
        )

    def test_curated_trumps_everything(self):
        self.assertEqual(
            sp.classify_fact([
                ("raw_entry", "human"), ("raw_entry", "agent"),
                ("work_trace", None), ("overlay", None), ("user_assertion", None),
            ]),
            sp.CURATED,
        )

    def test_unattributed_sits_below_a_real_source(self):
        self.assertEqual(
            sp.classify_fact([("raw_entry", "mixed"), ("raw_entry", "human")]),
            sp.HARVESTED,
        )

    def test_agent_derived_outranks_unattributed(self):
        self.assertEqual(
            sp.classify_fact([("raw_entry", "agent"), ("raw_entry", "mixed")]),
            sp.AGENT_DERIVED,
        )


class TestRecallEligibility(unittest.TestCase):
    def test_only_human_attributable_knowledge_is_eligible(self):
        self.assertTrue(sp.is_recall_eligible(sp.CURATED))
        self.assertTrue(sp.is_recall_eligible(sp.HARVESTED))
        for cls in (sp.AGENT_DERIVED, sp.OBSERVED, sp.UNATTRIBUTED,
                    sp.INFERRED, sp.UNKNOWN):
            with self.subTest(cls=cls):
                self.assertFalse(sp.is_recall_eligible(cls))

    def test_agent_narrated_conversation_is_not_recallable(self):
        self.assertFalse(
            sp.is_recall_eligible(sp.classify_fact([("raw_entry", "assistant")]))
        )


class TestPortability(unittest.TestCase):
    FORBIDDEN = [
        "RUNS_ON", "USES", "LOCATED_IN", "CONNECTED_TO", "MEMBER_OF",
        "WORKS_ON", "HAS_RAM", "HAS_IP_ADDRESS", "HAS_OS", "OWNS",
    ]

    def test_module_contains_no_predicate_names(self):
        src = inspect.getsource(sp)
        for name in self.FORBIDDEN:
            with self.subTest(predicate=name):
                self.assertNotIn(name, src)

    def test_classify_does_not_accept_a_predicate_argument(self):
        for fn in (sp.classify_fact, sp.SignalPolicy.classify):
            params = set(inspect.signature(fn).parameters)
            with self.subTest(fn=fn.__qualname__):
                self.assertNotIn("predicate", params)
                self.assertNotIn("statement", params)

    def test_works_for_a_completely_unseen_domain(self):
        policy = sp.SignalPolicy(
            source_kind_class={
                "patient_statement": sp.CURATED,
                "clinician_note": sp.HARVESTED,
                "device_reading": sp.OBSERVED,
                "derived_summary": sp.INFERRED,
            },
            attribution_sensitive=frozenset(),
        )
        self.assertEqual(policy.classify(["clinician_note"]), sp.HARVESTED)
        self.assertEqual(policy.classify(["device_reading"]), sp.OBSERVED)
        self.assertFalse(sp.is_recall_eligible(policy.classify(["device_reading"])))

    def test_a_deployment_can_teach_it_its_own_author_labels(self):
        policy = sp.SignalPolicy(
            attribution_by_author_role={
                "caregiver": sp.HUMAN,
                "transcriber_bot": sp.AGENT,
                "household": sp.MIXED,
            }
        )
        self.assertEqual(policy.classify([("raw_entry", "caregiver")]), sp.HARVESTED)
        self.assertEqual(
            policy.classify([("raw_entry", "transcriber_bot")]), sp.AGENT_DERIVED
        )
        self.assertEqual(
            policy.classify([("raw_entry", "household")]), sp.UNATTRIBUTED
        )


if __name__ == "__main__":
    unittest.main()
