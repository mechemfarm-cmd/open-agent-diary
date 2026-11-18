"""The control probe must be the filtered probe minus the predicate, and nothing else."""

from __future__ import annotations

import unittest
from pathlib import Path

from machine_plane.control_probes import (
    SCOPE_PREDICATE,
    blocks,
    compare_pair,
    generate_control,
    strip_scope_predicate,
    verify_pair,
)

PROBES = Path(__file__).resolve().parents[1] / "probes"

SHIPPED = ("exec.bt", "file.bt", "net.bt")


class TestStripScopePredicate(unittest.TestCase):
    def test_a_lone_predicate_becomes_no_filter(self) -> None:
        self.assertEqual(strip_scope_predicate(SCOPE_PREDICATE), "")

    def test_a_combined_predicate_keeps_the_other_terms(self) -> None:
        self.assertEqual(
            strip_scope_predicate("@seen[pid, tid] && " + SCOPE_PREDICATE),
            "@seen[pid, tid]",
        )

    def test_order_does_not_matter(self) -> None:
        self.assertEqual(
            strip_scope_predicate(SCOPE_PREDICATE + " && @seen[pid, tid]"),
            "@seen[pid, tid]",
        )

    def test_a_filter_without_the_predicate_is_untouched(self) -> None:
        self.assertEqual(strip_scope_predicate("@seen[pid, tid]"), "@seen[pid, tid]")


class TestShippedPairs(unittest.TestCase):
    """The checked-in control set must be what the generator produces."""

    def test_every_shipped_probe_generates_a_clean_twin(self) -> None:
        for name in SHIPPED:
            with self.subTest(probe=name):
                filtered = (PROBES / name).read_text(encoding="utf-8")
                control = generate_control(filtered)
                self.assertEqual(
                    compare_pair(filtered, control),
                    [],
                    f"{name}: generated control is not a faithful twin",
                )

    def test_the_control_has_no_predicate_anywhere(self) -> None:
        for name in SHIPPED:
            with self.subTest(probe=name):
                control = generate_control((PROBES / name).read_text(encoding="utf-8"))
                self.assertNotIn(SCOPE_PREDICATE, control)

    def test_the_control_keeps_the_same_blocks_and_fields(self) -> None:
        for name in SHIPPED:
            with self.subTest(probe=name):
                filtered = (PROBES / name).read_text(encoding="utf-8")
                control = generate_control(filtered)
                left = [(b.spec, b.fields, b.dereferences) for b in blocks(filtered)]
                right = [(b.spec, b.fields, b.dereferences) for b in blocks(control)]
                self.assertEqual(left, right)


class TestTamperingIsCaught(unittest.TestCase):
    """The comparison exists to fail. These are the ways it must."""

    def setUp(self) -> None:
        self.filtered = (PROBES / "exec.bt").read_text(encoding="utf-8")
        self.control = generate_control(self.filtered)

    def test_a_changed_field_is_refused(self) -> None:
        tampered = self.control.replace("label=%s", "label=%s extra=%d", 1)
        self.assertTrue(compare_pair(self.filtered, tampered))

    def test_a_changed_dereference_is_refused(self) -> None:
        tampered = self.control.replace("args->ret", "args->fd", 1)
        self.assertTrue(compare_pair(self.filtered, tampered))

    def test_a_surviving_predicate_is_refused(self) -> None:
        tampered = self.control.replace(
            "tracepoint:sched:sched_process_exec\n{",
            "tracepoint:sched:sched_process_exec\n/" + SCOPE_PREDICATE + "/\n{",
            1,
        )
        self.assertTrue(compare_pair(self.filtered, tampered))

    def test_an_extra_filter_term_is_refused(self) -> None:
        tampered = self.control.replace(
            "tracepoint:sched:sched_process_exec\n{",
            "tracepoint:sched:sched_process_exec\n/@other[pid]/\n{",
            1,
        )
        self.assertTrue(compare_pair(self.filtered, tampered))

    def test_a_missing_block_is_refused(self) -> None:
        # Drop the END block entirely: the control would no longer seal the same way.
        tampered = self.control[: self.control.rindex("END")]
        self.assertTrue(compare_pair(self.filtered, tampered))

    def test_verify_pair_raises_with_the_reason(self) -> None:
        tampered = self.control.replace("args->ret", "args->fd", 1)
        with self.assertRaises(ValueError) as caught:
            verify_pair(self.filtered, tampered, "exec.bt")
        self.assertIn("exec.bt", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class TestCheckedInControlSet(unittest.TestCase):
    """The control set is derived at build time and checked in; it must never drift."""

    def test_the_control_directory_mirrors_the_filtered_one(self) -> None:
        filtered = {p.name for p in PROBES.glob("*.bt")}
        control = {p.name for p in (PROBES / "control").glob("*.bt")}
        self.assertEqual(filtered, control)

    def test_every_checked_in_control_file_matches_the_generator(self) -> None:
        for path in sorted(PROBES.glob("*.bt")):
            with self.subTest(probe=path.name):
                expected = generate_control(path.read_text(encoding="utf-8"))
                actual = (PROBES / "control" / path.name).read_text(encoding="utf-8")
                self.assertEqual(
                    actual,
                    expected,
                    f"{path.name}: the checked-in control probe is not what the "
                    "generator produces — regenerate it rather than editing it",
                )


class TestGateModes(unittest.TestCase):
    def test_a_control_probe_is_refused_in_filtered_mode(self) -> None:
        from machine_plane.privacy_gate import inspect_probe

        for path in sorted((PROBES / "control").glob("*.bt")):
            with self.subTest(probe=path.name):
                rules = {f.rule for f in inspect_probe(path, mode="filtered").findings}
                self.assertTrue(rules, "a control probe passed as a filtered probe")

    def test_a_filtered_probe_is_refused_in_control_mode(self) -> None:
        from machine_plane.privacy_gate import inspect_probe

        for path in sorted(PROBES.glob("*.bt")):
            with self.subTest(probe=path.name):
                rules = {f.rule for f in inspect_probe(path, mode="control").findings}
                self.assertIn("control-is-filtered", rules)

    def test_a_control_probe_passes_as_a_control(self) -> None:
        from machine_plane.privacy_gate import inspect_probe

        for path in sorted((PROBES / "control").glob("*.bt")):
            with self.subTest(probe=path.name):
                self.assertEqual(inspect_probe(path, mode="control").findings, [])


class TestCaptureModeIsExplicit(unittest.TestCase):
    def test_an_unknown_mode_is_rejected(self) -> None:
        from pathlib import Path as _P

        from machine_plane.recorder import RecorderConfig

        with self.assertRaises(ValueError):
            RecorderConfig(
                capture_id="x",
                spool_dir=_P("/tmp"),
                probes=(_P("/tmp/p.bt"),),
                workload=("true",),
                duration_s=1,
                capture_mode="both",
            )

    def test_the_default_is_filtered(self) -> None:
        from pathlib import Path as _P

        from machine_plane.recorder import RecorderConfig

        config = RecorderConfig(
            capture_id="x",
            spool_dir=_P("/tmp"),
            probes=(_P("/tmp/p.bt"),),
            workload=("true",),
            duration_s=1,
        )
        self.assertEqual(config.capture_mode, "filtered")
