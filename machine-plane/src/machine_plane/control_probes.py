"""The control half of a paired capture.

A filtered capture is only evidence of narrowing if something was measured the same
way without the narrowing. That comparison is worthless if the control is a
hand-maintained second copy of the probes: two files that must stay identical
diverge, and the divergence is invisible precisely where it matters — in what the
probe reads.

So the control set is *derived*, not maintained. This module owns that derivation
and the proof that the two sets differ in nothing but the scope predicate:

* :func:`generate_control` strips the predicate from a filtered probe;
* :func:`compare_pair` refuses any pair that differs anywhere else — a different
  specifier, a different body, a different field, a different dereference.

The check is structural rather than textual: bodies are compared byte for byte, and
the only permitted difference is that the filtered probe carries ``cgroup == $1``
where the control does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The one term a control probe is allowed to lack.
SCOPE_PREDICATE = "cgroup == $1"

#: ``<specifier>`` optionally followed by ``/<filter>/`` then a body in braces.
#: Written to match how the probe set is actually laid out: the specifier, the
#: filter and the opening brace each on their own line.
_BLOCK = re.compile(
    r"^(?P<spec>[^\n{}]+?)[ \t]*\n"
    r"(?:/(?P<filter>[^\n]*)/[ \t]*\n)?"
    r"\{\n"
    r"(?P<body>.*?)\n"
    r"\}[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

_PRINTF = re.compile(r'printf\("(?P<text>(?:[^"\\]|\\.)*)"', re.DOTALL)
_FIELD = re.compile(r"([A-Za-z_]\w*)=")
_DEREF = re.compile(r"->\w+")


@dataclass(frozen=True)
class Block:
    spec: str
    filter: str
    body: str

    @property
    def fields(self) -> list[str]:
        """The named fields this block emits, in order."""
        for match in _PRINTF.finditer(self.body):
            return _FIELD.findall(match.group("text"))
        return []

    @property
    def dereferences(self) -> list[str]:
        return _DEREF.findall(self.body)


def blocks(source: str) -> tuple[Block, ...]:
    """Every block in a probe, in source order."""
    return tuple(
        Block(
            spec=match.group("spec").strip(),
            filter=(match.group("filter") or "").strip(),
            body=match.group("body").strip("\n"),
        )
        for match in _BLOCK.finditer(source)
    )


def strip_scope_predicate(filter_text: str) -> str:
    """Remove the scope predicate, leaving any other filter terms intact.

    ``/@seen[pid, tid] && cgroup == $1/`` becomes ``/@seen[pid, tid]/``;
    ``/cgroup == $1/`` becomes no filter at all. Rejoining with ``&&`` rather than
    string surgery is what keeps the surviving terms exactly as written.
    """
    if not filter_text.strip():
        return ""
    terms = [term.strip() for term in filter_text.split("&&")]
    kept = [term for term in terms if term != SCOPE_PREDICATE]
    return " && ".join(kept)


def generate_control(source: str) -> str:
    """Produce the control probe from a filtered one.

    Text-preserving on purpose: everything outside a filter is copied verbatim, so a
    review of the diff sees only predicate removals.
    """
    out: list[str] = []
    cursor = 0
    for match in _BLOCK.finditer(source):
        out.append(source[cursor : match.start()])
        spec = match.group("spec")
        filter_text = (match.group("filter") or "").strip()
        body = match.group("body")

        remaining = strip_scope_predicate(filter_text)
        if filter_text and remaining:
            out.append(f"{spec}\n/{remaining}/\n{{\n{body}\n}}")
        elif filter_text:
            out.append(f"{spec}\n{{\n{body}\n}}")
        else:
            out.append(f"{spec}\n/{filter_text}/\n{{\n{body}\n}}" if filter_text else f"{spec}\n{{\n{body}\n}}")
        cursor = match.end()
    out.append(source[cursor:])
    return "".join(out)


def compare_pair(filtered_source: str, control_source: str) -> list[str]:
    """Every way these two probes differ other than the scope predicate.

    An empty list means the pair is acceptable: same blocks, same order, identical
    bodies, and filters that differ only by the predicate. Anything else is a
    finding, because a control that reads different fields is not a control — it is
    a second experiment that happens to share a filename.
    """
    problems: list[str] = []
    filtered = blocks(filtered_source)
    control = blocks(control_source)

    if len(filtered) != len(control):
        problems.append(
            f"block count differs: filtered {len(filtered)}, control {len(control)}"
        )

    for index, (left, right) in enumerate(zip(filtered, control)):
        if left.spec != right.spec:
            problems.append(
                f"block {index} specifier differs: {left.spec!r} vs {right.spec!r}"
            )
            continue
        if left.body.strip() != right.body.strip():
            problems.append(
                f"block {index} ({left.spec}) body differs — the probes no longer "
                "read the same thing"
            )
        if left.fields != right.fields:
            problems.append(
                f"block {index} ({left.spec}) emits different fields: "
                f"{left.fields} vs {right.fields}"
            )
        if left.dereferences != right.dereferences:
            problems.append(
                f"block {index} ({left.spec}) dereferences differ: "
                f"{left.dereferences} vs {right.dereferences}"
            )

        expected = strip_scope_predicate(left.filter)
        actual = right.filter.strip()
        if actual != expected:
            problems.append(
                f"block {index} ({left.spec}) filter is not the filtered one minus the "
                f"predicate: expected {expected!r}, found {actual!r}"
            )

    return problems


def verify_pair(filtered_source: str, control_source: str, name: str = "probe") -> None:
    """Raise unless the control is exactly the filtered probe without the predicate."""
    problems = compare_pair(filtered_source, control_source)
    if problems:
        raise ValueError(
            f"{name}: control probe is not a faithful unfiltered twin:\n  "
            + "\n  ".join(problems)
        )
