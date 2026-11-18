"""Gate 1: the static inspection must refuse, not merely report.

A gate that only ever says PASSED is documentation. These tests are the refusals:
an unapproved dereference, an unforeseen string field, a field nobody consumes, and
a field the producer invents. Each is a probe that would otherwise have run.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from machine_plane.privacy_gate import (
    APPROVED_DEREFERENCES,
    inspect_probe,
    producer_fields,
    run_gate,
)

SHIPPED = Path("/usr/lib/machine-plane/probes")
PRODUCER = Path(__file__).resolve().parents[1] / "src/machine_plane/producers/linux_ebpf.py"

GOOD_PROBE = """
BEGIN { printf("MARK probe=test phase=start ts=%llu\\n", nsecs); }

tracepoint:syscalls:sys_enter_execve
{
    printf("EV exec_attempt pid=%d tid=%d rc=%d cgroup=%llu ts=%llu label=%s\\n",
           pid, tid, args->ret, cgroup, nsecs, comm);
}
"""

# Reads the filename in a block that is NOT the approved one.
BAD_DEREFERENCE = GOOD_PROBE.replace(
    "pid, tid, args->ret, cgroup, nsecs, comm);",
    "pid, tid, args->ret, cgroup, nsecs, str(args->filename));",
)

# Adds a string field nobody asked for.
BAD_STRING_FIELD = GOOD_PROBE.replace(
    'label=%s\\n",\n           pid, tid, args->ret, cgroup, nsecs, comm);',
    'label=%s cmdline=%s\\n",\n           pid, tid, args->ret, cgroup, nsecs, comm, comm);',
)

# Emits a field the producer never reads.
BAD_UNCONSUMED = GOOD_PROBE.replace("label=%s", "label=%s extra=%d").replace(
    "nsecs, comm);", "nsecs, comm, pid);"
)


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-gate-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def write(self, name: str, source: str) -> Path:
        path = self.directory / name
        path.write_text(source, encoding="utf-8")
        return path

    def gate(self, paths):
        return run_gate(paths, producer_module=PRODUCER)


class TestShippedProbes(GateTestCase):
    @unittest.skipUnless(SHIPPED.is_dir(), "shipped probes are not installed here")
    def test_the_deployed_probe_set_passes(self) -> None:
        result = self.gate(sorted(SHIPPED.glob("*.bt")))
        self.assertTrue(result.passed, result.report())

    @unittest.skipUnless(SHIPPED.is_dir(), "shipped probes are not installed here")
    def test_the_single_approved_dereference_is_recorded_not_flagged(self) -> None:
        result = self.gate(sorted(SHIPPED.glob("*.bt")))
        self.assertEqual(
            result.dereferences,
            {("tracepoint:sched:sched_process_exec", "str(args->filename)")},
        )


class TestRefusals(GateTestCase):
    def test_an_unapproved_filename_dereference_is_refused(self) -> None:
        result = self.gate([self.write("bad.bt", BAD_DEREFERENCE)])
        self.assertFalse(result.passed)
        self.assertTrue(
            any(f.rule == "unapproved-dereference" for f in result.findings),
            result.report(),
        )

    def test_a_new_string_field_is_refused(self) -> None:
        result = self.gate([self.write("bad.ft", BAD_STRING_FIELD)])
        self.assertFalse(result.passed)
        self.assertTrue(any(f.rule == "string-field" for f in result.findings))

    def test_a_field_nobody_consumes_is_refused(self) -> None:
        result = self.gate([self.write("bad.bt", BAD_UNCONSUMED)])
        self.assertFalse(result.passed)
        self.assertTrue(
            any(f.rule == "unconsumed-field" for f in result.findings), result.report()
        )

    def test_a_gate_with_no_probes_is_refused(self) -> None:
        self.assertFalse(self.gate([]).passed)


class TestCommentHandling(GateTestCase):
    def test_prose_about_a_forbidden_pointer_does_not_trip_the_gate(self) -> None:
        """The probes explain what they don't read; the explanation is not code.

        Asserted on the dereference rule specifically: a single-probe fixture is
        legitimately incomplete against the producer's field set, so overall
        passing is not the property under test.
        """
        source = GOOD_PROBE.replace(
            "BEGIN {",
            "/* deliberately does not read args->filename or argv, nor envp */\nBEGIN {",
        )
        result = self.gate([self.write("documented.bt", source)])
        self.assertFalse(
            any(f.rule == "unapproved-dereference" for f in result.findings),
            result.report(),
        )
        self.assertEqual(result.dereferences, set())


class TestArtifactGrammar(unittest.TestCase):
    """The verifier refuses syntax outside the grammar rather than guessing."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-grammar-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def write(self, name: str, source: str) -> Path:
        path = self.directory / name
        path.write_text(source, encoding="utf-8")
        return path

    def rules(self, source: str, name: str = "probe.bt") -> set[str]:
        return {finding.rule for finding in inspect_probe(self.write(name, source)).findings}

    def test_a_preprocessor_directive_is_refused(self) -> None:
        self.assertIn("unsupported-syntax", self.rules("#define N 10\n" + GOOD_PROBE))

    def test_a_wildcard_specifier_is_refused(self) -> None:
        source = 'tracepoint:syscalls:sys_enter_* { printf("EV x pid=%d\\n", pid); }'
        self.assertIn("unsupported-syntax", self.rules(source))

    def test_a_format_with_no_fields_is_refused(self) -> None:
        source = 'BEGIN { printf("hello\\n"); }'
        self.assertIn("unparsed-format", self.rules(source))

    def test_a_non_literal_format_is_refused(self) -> None:
        source = 'BEGIN { printf(fmt, pid); }'
        self.assertIn("unreadable-format", self.rules(source))


class TestApprovedDereferenceIsExact(unittest.TestCase):
    """The approval is for one expression in one block, not for a token.

    A test that only proves the approved token *exists* would pass even if the
    matcher were matching the token anywhere.
    """

    APPROVED_EXPRESSION = "str(args->filename)"

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-approved-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def write(self, name: str, source: str) -> Path:
        return Path(self.write_raw(name, source))

    def write_raw(self, name: str, source: str) -> str:
        path = self.directory / name
        path.write_text(source, encoding="utf-8")
        return str(path)

    def test_the_approved_expression_in_the_approved_block_is_allowed(self) -> None:
        source = (
            "tracepoint:sched:sched_process_exec\n{\n"
            f'    printf("EV exec_started pid=%d path=%s\\n", pid, {self.APPROVED_EXPRESSION});\n'
            "}\n"
        )
        inspection = inspect_probe(self.write("exec.bt", source))
        self.assertFalse(
            any(f.rule == "unapproved-dereference" for f in inspection.findings),
            [str(f) for f in inspection.findings],
        )
        self.assertEqual(
            inspection.dereferences,
            {("tracepoint:sched:sched_process_exec", self.APPROVED_EXPRESSION)},
        )

    def test_the_same_expression_in_a_different_tracepoint_is_refused(self) -> None:
        source = (
            "tracepoint:syscalls:sys_enter_openat\n{\n"
            f'    printf("EV open pid=%d path=%s\\n", pid, {self.APPROVED_EXPRESSION});\n'
            "}\n"
        )
        inspection = inspect_probe(self.write("exec.bt", source))
        self.assertTrue(
            any(f.rule == "unapproved-dereference" for f in inspection.findings),
            [str(f) for f in inspection.findings],
        )

    def test_the_approved_block_in_a_different_probe_file_is_refused(self) -> None:
        source = (
            "tracepoint:sched:sched_process_exec\n{\n"
            f'    printf("EV exec_started pid=%d path=%s\\n", pid, {self.APPROVED_EXPRESSION});\n'
            "}\n"
        )
        inspection = inspect_probe(self.write("file.bt", source))
        self.assertTrue(
            any(f.rule == "unapproved-dereference" for f in inspection.findings)
        )


class TestDereferenceOutsidePrintf(unittest.TestCase):
    """A pointer read is refused wherever it happens, not only when printed.

    The earlier verifier only inspected printf arguments, so an assignment, a
    condition, an alias or a helper argument could read the pointer and pass. The
    value never had to be published for the read to happen.
    """

    FORMS = {
        "assignment": "$read_name = args->filename;\n",
        "condition": "$flag = 0;\nif (args->filename) { $flag = 1; }\n",
        "alias": "alias leaked = args->filename;\n",
        "helper argument": "$path = str(args->filename);\n",
        "filter": "",
    }

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mp-outside-"))
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True)
        )

    def inspect(self, source: str):
        path = self.directory / "exec.bt"
        path.write_text(source, encoding="utf-8")
        return inspect_probe(path)

    def test_each_bypass_form_is_refused(self) -> None:
        for label, prologue in self.FORMS.items():
            with self.subTest(form=label):
                source = prologue + GOOD_PROBE if label != "filter" else (
                    "BEGIN { }\n"
                    "tracepoint:sched:sched_process_exec /args->filename/\n"
                    '{\n    printf("EV exec_started pid=%d\\n", pid);\n}\n'
                )
                inspection = self.inspect(source)
                self.assertTrue(
                    any(f.rule == "dereference-outside-printf" for f in inspection.findings),
                    f"{label}: {[str(f) for f in inspection.findings]}",
                )

    def test_the_approved_printf_dereference_still_passes_in_that_probe(self) -> None:
        """The fix must not make the one approved read impossible."""
        inspection = self.inspect(GOOD_PROBE)
        self.assertFalse(
            any(f.rule == "dereference-outside-printf" for f in inspection.findings),
            [str(f) for f in inspection.findings],
        )


class TestSchemaAgreement(GateTestCase):
    def test_the_producer_field_set_is_read_from_its_source(self) -> None:
        consumed = producer_fields(PRODUCER)
        self.assertIn("label", consumed)
        self.assertIn("rc", consumed)

    def test_an_expected_field_set_mismatch_is_refused(self) -> None:
        result = run_gate(
            [self.write("ok.bt", GOOD_PROBE)],
            producer_module=PRODUCER,
            expected_fields={"pid"},
        )
        self.assertFalse(result.passed)
        self.assertTrue(any(f.rule == "field-set-changed" for f in result.findings))


if __name__ == "__main__":
    unittest.main()
