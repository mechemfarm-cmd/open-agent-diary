"""The view ranks what to look at, and says what it knows in plain words."""

from __future__ import annotations

import unittest

from machine_plane.correlation import (
    CorrelatedRecord,
    CorrelationStatus,
    CoverageMap,
    RunRegistry,
    correlate,
)
from machine_plane.events import EventType
from machine_plane.privacy import PathDisplay, PrivacyPolicy
from machine_plane.testing import (
    MockProducer,
    action_event,
    gap_event,
    make_claim,
    make_registration,
)
from machine_plane.view import build_view, describe

WALL = 1_700_000_000.0


def _event(
    event_type: EventType = EventType.WRITE_ATTEMPT,
    target: str | None = "/home/x/work/report.txt",
):
    raws = [action_event(event_type, target=target, wall_time=WALL)]
    return list(MockProducer(raws).events())[0]


def _record(status, *, event=None, claim=None, reason="test_reason"):
    return CorrelatedRecord(status=status, reason=reason, event=event, claim=claim)


class DescribeTest(unittest.TestCase):
    def test_a_confirmed_match_reads_as_agreement(self) -> None:
        sentence = describe(
            _record(
                CorrelationStatus.CONFIRMED_MATCH,
                event=_event(),
                claim=make_claim("write", target="/home/x/work/report.txt"),
            )
        )
        self.assertIn("and the machine saw it", sentence)

    def test_an_unclaimed_effect_names_the_absence_of_a_claim(self) -> None:
        sentence = describe(_record(CorrelationStatus.MACHINE_ONLY, event=_event()))
        self.assertIn("never mentioned it", sentence)

    def test_a_blind_period_is_stated_in_the_sentence(self) -> None:
        sentence = describe(
            _record(
                CorrelationStatus.UNMATCHED_DUE_TO_GAP,
                claim=make_claim("write", target="/home/x/work/report.txt"),
            )
        )
        self.assertIn("recorder was blind", sentence)

    def test_a_redacted_target_is_described_as_unrecorded(self) -> None:
        sentence = describe(_record(CorrelationStatus.MACHINE_ONLY, event=_event(target=None)))
        self.assertIn("the policy did not record", sentence)

    def test_every_status_has_a_sentence(self) -> None:
        for status in CorrelationStatus:
            with self.subTest(status=status):
                sentence = describe(_record(status, claim=make_claim("write")))
                self.assertTrue(sentence)
                self.assertNotIn("None", sentence)


class RankingTest(unittest.TestCase):
    def _report(self):
        policy = PrivacyPolicy(
            allowed_roots=("/home/x/work",), path_display=PathDisplay.BASENAME
        )
        events = list(
            MockProducer(
                [
                    gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                    # claimed and observed
                    action_event(
                        EventType.WRITE_ATTEMPT,
                        target="/home/x/work/confirmed.txt",
                        wall_time=WALL,
                    ),
                    # observed, never claimed
                    action_event(
                        EventType.WRITE_ATTEMPT,
                        target="/home/x/work/unclaimed.txt",
                        wall_time=WALL,
                    ),
                ],
                policy=policy,
            ).events()
        )
        claims = [
            make_claim("write", target="/home/x/work/confirmed.txt", wall_time=WALL),
            make_claim("write", target="/home/x/work/phantom.txt", wall_time=WALL),
        ]
        return correlate(events, claims, RunRegistry([make_registration()]), policy=policy)

    def test_an_unclaimed_effect_outranks_a_missing_one(self) -> None:
        view = build_view(self._report())
        statuses = [entry.status for entry in view.entries]
        self.assertEqual(statuses[0], CorrelationStatus.MACHINE_ONLY)
        self.assertLess(
            statuses.index(CorrelationStatus.MACHINE_ONLY),
            statuses.index(CorrelationStatus.AGENT_ONLY),
        )

    def test_confirmed_matches_are_not_listed(self) -> None:
        view = build_view(self._report())
        self.assertNotIn(
            CorrelationStatus.CONFIRMED_MATCH,
            [entry.status for entry in view.entries],
        )

    def test_ranks_are_contiguous_from_one(self) -> None:
        view = build_view(self._report())
        self.assertEqual(
            [entry.rank for entry in view.entries],
            list(range(1, len(view.entries) + 1)),
        )

    def test_the_counts_still_report_everything_compared(self) -> None:
        # Two action events and one unused claim: the gap event is not a
        # comparison, so it is not counted as one.
        view = build_view(self._report())
        self.assertEqual(view.compared, 3)
        self.assertEqual(
            view.counts.get(CorrelationStatus.CONFIRMED_MATCH.value), 1
        )


class TextTest(unittest.TestCase):
    def test_the_page_has_no_divergence_count(self) -> None:
        report = correlate([], [], RunRegistry())
        text = build_view(report).to_text()
        self.assertNotIn("divergence", text.lower())

    def test_an_empty_report_says_so_rather_than_showing_an_empty_list(self) -> None:
        text = build_view(correlate([], [], RunRegistry())).to_text()
        self.assertIn("Nothing to investigate", text)

    def test_coverage_appears_on_the_page(self) -> None:
        events = list(
            MockProducer(
                [
                    gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100),
                    gap_event(EventType.BUFFER_OVERFLOW, wall_time=WALL),
                ]
            ).events()
        )
        coverage = CoverageMap.from_events(events)
        text = build_view(correlate(events, [], RunRegistry()), coverage=coverage).to_text()
        self.assertIn("Coverage", text)
        self.assertIn("coverage gap", text)

    def test_absent_coverage_is_stated_not_implied(self) -> None:
        view = build_view(correlate([], [], RunRegistry()), coverage=None)
        self.assertIn("not reported", view.coverage_note)

    def test_a_clean_run_is_described_as_running_throughout(self) -> None:
        events = list(
            MockProducer(
                [gap_event(EventType.RECORDER_STARTED, wall_time=WALL - 100)]
            ).events()
        )
        view = build_view(
            correlate(events, [], RunRegistry()),
            coverage=CoverageMap.from_events(events),
        )
        self.assertIn("running throughout", view.coverage_note)


class DictTest(unittest.TestCase):
    def test_the_serialised_view_keeps_categories_and_coverage(self) -> None:
        payload = build_view(correlate([], [], RunRegistry())).to_dict()
        self.assertIn("exceptions", payload)
        self.assertIn("coverage", payload)
        self.assertIn("counts", payload)
        self.assertNotIn("divergences", payload)


if __name__ == "__main__":
    unittest.main()
