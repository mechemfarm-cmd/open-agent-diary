"""Gate 1 — static privacy inspection of the probes, before anything attaches.

A gate that runs after a capture proves what was recorded. It cannot un-run a
dereference. So this inspects the probe sources as artifacts, on the machine,
before installation and before every attach, and refuses when the sources ask the
kernel for more than the approved set.

**A dereference is judged by where it happens, not by how it is used.** The only
place a forbidden pointer may appear at all is inside a `printf` argument, and
only the one approved triple may appear there. Anywhere else in the program —
an assignment, a condition, an alias, a helper argument, a filter — is refused
regardless of whether its value is ever printed. Reading the pointer is the
thing being controlled, not publishing its value.

**This is a verifier for one fixed artifact grammar, not a bpftrace parser.**
It is written against probe sources that follow a single shape, and it refuses
anything outside that shape rather than guessing:

    <specifier> [ /filter/ ] {
        printf("<literal format>", <arg>, <arg>, ...);
    }

    specifier := BEGIN | END | tracepoint:<group>:<name> | kprobe:<symbol> ...
    filter    := [ <existing terms> && ] cgroup == $1     (REQUIRED on EV blocks)
    format    := "MARK probe=<name> phase=<phase> ts=%llu"
               | "EV <event> name=<value> ..."     (name=value pairs, %d/%s/%llu)
    arg       := identifier | args-><field> | args->ret | nsecs | comm | cgroup
               | str(<pointer>) | pid | tid

Anything with preprocessor directives, wildcard specifiers, or a printf whose
format is not a leading string literal is **rejected**, because a verifier that
guesses at syntax it does not understand is worse than one that refuses. If the
probe set ever needs a richer grammar, this module needs extending deliberately —
not silently absorbing whatever it is given.

Four rules, each from the design's path-privacy section:

1. **Every emitted field is enumerated from the probe source**, not from a
   document. ``printf`` format strings are the source of truth.
2. **No content-bearing pointer is dereferenced** — no ``filename``, ``argv``,
   ``envp``, or buffer read — with exactly one approved exception, matched by
   probe, tracepoint and expression, so a second one cannot slip in by looking
   similar.
3. **The fields the probes emit are exactly the fields the producer consumes.**
   A probe field nobody reads is data collected for no stated purpose; a producer
   field no probe emits is a field being invented somewhere.
4. **No field is free text, a path or a blob.** The only string-typed fields are
   ``label`` (``comm``, which truncates at 15 characters) and the approved
   ``path``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections.abc import Sequence
from pathlib import Path

#: The single approved pathname dereference, matched exactly.
#:
#: ``sched_process_exec`` fires on a successful image transition, so the string it
#: reads is the path of the process that actually started — a pathname, not
#: content. It is the one pointer read in this probe set, listed for review in the
#: Stage 1 run sheet, and it is approved only here.
APPROVED_DEREFERENCES: dict[tuple[str, str, str], str] = {
    (
        "exec.bt",
        "tracepoint:sched:sched_process_exec",
        "str(args->filename)",
    ): (
        "records the executable path of a successful image transition; "
        "pathname only, reviewed in the Stage 1 run sheet"
    )
}

#: Field names that may carry a string. Everything else must be numeric.
STRING_FIELDS = frozenset({"label", "path"})

#: Substrings that mean a probe is reading user memory or kernel strings.
FORBIDDEN_TOKENS = (
    "args->filename",
    "bprm->filename",
    "args->argv",
    "args->envp",
    "envp",
    "argv",
    "user_str",
    "buf(",
)

#: Constructs this verifier does not interpret. Presence means the source is
#: outside the artifact grammar, so its safety cannot be established here.
UNSUPPORTED_PATTERNS = {
    "preprocessor directive": re.compile(r"^\s*#\s*(define|include|pragma|import)\b", re.MULTILINE),
    "wildcard specifier": re.compile(r"^\s*(kprobe|kretprobe|tracepoint|uprobe|usdt):\S*\*", re.MULTILINE),
    "multiple statements per printf": re.compile(r"printf\s*\([^;]*\)\s*;\s*printf", re.DOTALL),
}

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_PRINTF = re.compile(r"printf\s*\((?P<args>.*?)\)\s*;", re.DOTALL)
_QUOTED = re.compile(r'^"(?P<text>(?:[^"\\]|\\.)*)"')
# A block opener on its own line, e.g. `BEGIN {` or
# `tracepoint:sched:sched_process_exec /filter/ {`.
_OPENER = re.compile(
    r"^\s*(?P<spec>[A-Za-z_][\w:./-]*|[A-Z]+)\s*(?:/\s*[^/\n]+\s*/\s*)?\{\s*$",
    re.MULTILINE,
)


def _enclosing_block(source: str, position: int) -> tuple[str, str]:
    """The specifier and filter of the block containing ``position``.

    bpftrace allows the specifier, the filter and the opening brace to be spread
    across three lines, which is how this probe set is written. Parsing the header
    as a single line therefore found no filter at all — and a verifier that cannot
    see a filter will happily report every probe as unfiltered, or worse, a
    filtered probe as unfiltered in one build and filtered in the next.

    Walking back from the opening brace is unambiguous: the brace ends the block
    header, and the one or two non-empty lines above it are the filter and the
    specifier.
    """
    lines = source[:position].splitlines()
    index = len(lines) - 1
    while index >= 0 and not lines[index].strip().endswith("{"):
        index -= 1
    if index < 0:
        return "<top-level>", ""

    header = lines[index].strip()[:-1].strip()
    filter_text = ""

    # `specifier /filter/ {`
    if "/" in header:
        spec, _, rest = header.partition("/")
        filter_text = "/" + rest
        return spec.strip() or "<unknown>", filter_text

    if header:
        return header, ""

    # `specifier` then `/filter/` then `{`, each on its own line.
    cursor = index - 1
    while cursor >= 0 and not lines[cursor].strip():
        cursor -= 1
    if cursor >= 0 and lines[cursor].strip().startswith("/"):
        filter_text = lines[cursor].strip()
        cursor -= 1
    while cursor >= 0 and not lines[cursor].strip():
        cursor -= 1
    spec = lines[cursor].strip() if cursor >= 0 else "<unknown>"
    return spec or "<unknown>", filter_text


def _enclosing_specifier(source: str, position: int) -> str:
    return _enclosing_block(source, position)[0]


@dataclass
class Finding:
    probe: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.probe}: {self.rule}: {self.detail}"


@dataclass
class ProbeInspection:
    """What one probe source emits, and what it reads to do it."""

    name: str
    fields: set[str] = field(default_factory=set)
    events: set[str] = field(default_factory=set)
    dereferences: set[tuple[str, str]] = field(default_factory=set)
    findings: list[Finding] = field(default_factory=list)


def _strip_comments(source: str) -> str:
    """Remove comments, so prose about a forbidden pointer does not trip the gate.

    ``exec.bt`` explains at length what it deliberately does *not* read. A gate
    that flags the explanation instead of the code would be unhelpful in exactly
    the case it matters.
    """
    return _LINE_COMMENT.sub("", _COMMENT.sub("", source))


def _split_arguments(body: str) -> list[str]:
    parts, depth, current = [], 0, []
    for character in body:
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def inspect_probe(path: str | Path, mode: str = "filtered") -> ProbeInspection:
    target = Path(path)
    raw = target.read_text(encoding="utf-8")
    source = _strip_comments(raw)
    if mode not in {"filtered", "control"}:
        raise ValueError(f"unknown probe mode {mode!r}; expected filtered or control")
    inspection = ProbeInspection(name=target.name)

    for label, pattern in UNSUPPORTED_PATTERNS.items():
        match = pattern.search(source)
        if match is not None:
            inspection.findings.append(
                Finding(
                    target.name,
                    "unsupported-syntax",
                    f"{label} at {match.group(0)[:40]!r} — outside the artifact grammar",
                )
            )

    # A dereference outside a printf argument never reaches the argument loop
    # below, so it would pass unexamined. Reading the pointer is the thing being
    # controlled, so any occurrence outside a printf is refused outright.
    spans = [(match.start(), match.end()) for match in _PRINTF.finditer(source)]
    for token in FORBIDDEN_TOKENS:
        for token_match in re.finditer(re.escape(token), source):
            position = token_match.start()
            if any(start <= position < end for start, end in spans):
                continue
            inspection.findings.append(
                Finding(
                    target.name,
                    "dereference-outside-printf",
                    f"{token!r} at offset {position} is not a printf argument — "
                    "reading the pointer is refused wherever it happens",
                )
            )

    for call in _PRINTF.finditer(source):
        specifier = _enclosing_specifier(source, call.start())
        arguments = _split_arguments(call.group("args"))
        if not arguments:
            continue

        literal = _QUOTED.match(arguments[0])
        if literal is None:
            inspection.findings.append(
                Finding(target.name, "unreadable-format", call.group("args")[:60])
            )
            continue

        text = literal.group("text")
        if text.startswith("EV "):
            _, filter_text = _enclosing_block(source, call.start())
            if mode == "control":
                # A control probe must NOT be narrowed. Carrying the predicate here
                # would make the control narrower than the filtered run and the
                # comparison would measure nothing.
                if "cgroup == $1" in filter_text:
                    inspection.findings.append(
                        Finding(
                            target.name,
                            "control-is-filtered",
                            f"block {specifier!r} carries the scope predicate but this "
                            "is a control probe — the control must be unfiltered",
                        )
                    )
            elif re.search(r"cgroup\s*!=", filter_text):
                inspection.findings.append(
                    Finding(
                        target.name,
                        "widening-predicate",
                        f"block {specifier!r} compares cgroup with '!=', which "
                        "widens rather than narrows",
                    )
                )
            elif "cgroup == $1" not in filter_text:
                inspection.findings.append(
                    Finding(
                        target.name,
                        "missing-scope-predicate",
                        f"block {specifier!r} prints an event but its filter is "
                        f"{filter_text or '(none)'!r} — every EV block requires "
                        "'cgroup == $1'",
                    )
                )

        names = re.findall(r"(\w+)=", text)
        if not names:
            inspection.findings.append(
                Finding(target.name, "unparsed-format", text[:60])
            )
            continue
        inspection.fields.update(names)
        for marker in re.findall(r"^EV\s+(\w+)", text):
            inspection.events.add(marker)

        # Rule 4: only the declared fields may be strings.
        for name in re.findall(r"(\w+)=%[diouxX]?s", text):
            if name not in STRING_FIELDS:
                inspection.findings.append(
                    Finding(
                        target.name,
                        "string-field",
                        f"{name!r} is emitted as a string and is not an approved "
                        "string field",
                    )
                )

        # Rule 2: what did it read to fill them?
        for expression in arguments[1:]:
            for token in FORBIDDEN_TOKENS:
                if token not in expression:
                    continue
                approved = (target.name, specifier, expression.strip())
                if approved in APPROVED_DEREFERENCES:
                    inspection.dereferences.add((specifier, expression.strip()))
                    continue
                inspection.findings.append(
                    Finding(
                        target.name,
                        "unapproved-dereference",
                        f"{expression.strip()!r} in {specifier}",
                    )
                )

    return inspection


def producer_fields(module_path: str | Path) -> set[str]:
    """Field names the producer actually reads back out of a probe line.

    Enumerated from the producer's source so the two sides cannot drift: adding a
    field to a probe without consuming it is how data gets collected for no stated
    purpose.
    """
    source = Path(module_path).read_text(encoding="utf-8")
    consumed = set(re.findall(r"values\[\"(\w+)\"\]", source))
    consumed |= set(re.findall(r"values\.get\(\"(\w+)\"", source))
    # Fields the parser handles generically rather than by name.
    consumed |= {"ts", "pid", "tid", "cgroup"}
    return consumed


@dataclass
class GateResult:
    passed: bool
    findings: list[Finding] = field(default_factory=list)
    fields: set[str] = field(default_factory=set)
    dereferences: set[tuple[str, str]] = field(default_factory=set)

    def report(self) -> str:
        lines = []
        if self.passed:
            lines.append("Gate 1: PASSED")
            lines.append(f"  fields emitted : {', '.join(sorted(self.fields))}")
            lines.append(
                f"  dereferences   : "
                + (", ".join(f"{spec} -> {expr}" for spec, expr in sorted(self.dereferences))
                   or "none")
            )
        else:
            lines.append("Gate 1: REFUSED — nothing may be installed or attached")
            for finding in self.findings:
                lines.append(f"  {finding}")
        return "\n".join(lines)


def run_gate(
    probes: Sequence[str | Path],
    *,
    producer_module: str | Path,
    expected_fields: set[str] | None = None,
    mode: str = "filtered",
) -> GateResult:
    findings: list[Finding] = []
    fields: set[str] = set()
    dereferences: set[tuple[str, str]] = set()

    if not probes:
        findings.append(Finding("<none>", "no-probes", "the gate has nothing to inspect"))

    for probe in probes:
        inspection = inspect_probe(probe, mode=mode)
        findings.extend(inspection.findings)
        fields |= inspection.fields
        dereferences |= inspection.dereferences

    consumed = producer_fields(producer_module)
    consumed |= {"probe", "phase"}  # MARK bookkeeping, not event fields
    if fields:
        unconsumed = fields - consumed
        invented = consumed - fields - {"ts", "pid", "tid", "cgroup", "probe", "phase"}
        for name in sorted(unconsumed):
            findings.append(
                Finding("<schema>", "unconsumed-field", f"{name!r} is emitted but never read")
            )
        for name in sorted(invented):
            findings.append(
                Finding("<schema>", "invented-field", f"{name!r} is read but never emitted")
            )

    if expected_fields is not None and fields != expected_fields:
        findings.append(
            Finding(
                "<schema>",
                "field-set-changed",
                f"emitted {sorted(fields)} differs from the approved {sorted(expected_fields)}",
            )
        )

    return GateResult(
        passed=not findings, findings=findings, fields=fields, dereferences=dereferences
    )
