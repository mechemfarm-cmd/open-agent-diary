"""CLI surface for onboarding.

Deliberately small: two commands. The existing subcommands are the tools;
onboarding is the guided path through them. Keeping the surface narrow is the
point — a new user should not need to know the others exist.

Every emitted command includes the global ``--json`` flag and the explicit
``--step``, so an agent can copy what ``next_action`` returns and run it
verbatim rather than reconstructing argument order.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from agent_diary import onboarding
from agent_diary.onboarding_state import OnboardingError, OnboardingState, STEPS

# Steps that exist but are not built yet. Reported honestly rather than
# pretending to have run.
UNIMPLEMENTED: set[str] = set()


def add_onboarding_parsers(sub: argparse._SubParsersAction) -> None:
    p_init = sub.add_parser(
        "init",
        help="choose where the diary lives and create it",
    )
    p_init.add_argument(
        "--data-root",
        help="directory to hold the diary (you are asked if you omit this)",
    )
    p_init.add_argument(
        "--yes",
        action="store_true",
        help="accept the proposed data root without asking",
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help="reconfigure even if a config already exists",
    )

    p_onboard = sub.add_parser(
        "onboard",
        help="walk through first-time setup (human or agent driven)",
    )
    p_onboard.add_argument(
        "--step",
        choices=[s for s in STEPS],
        help="run a specific step instead of the current one",
    )
    p_onboard.add_argument(
        "--confirm",
        action="store_true",
        help="confirm a step that shows you something before advancing",
    )
    p_onboard.add_argument(
        "--answer",
        help="JSON object mapping source id to content|metadata|denied",
    )
    p_onboard.add_argument(
        "--approve",
        action="append",
        metavar="CANDIDATE_ID",
        help="approve one derived candidate (repeatable)",
    )
    p_onboard.add_argument(
        "--reject",
        action="append",
        metavar="CANDIDATE_ID",
        help="reject one derived candidate (repeatable)",
    )
    p_onboard.add_argument(
        "--auto-reject-unreviewed",
        action="store_true",
        help="complete review without importing any derived knowledge",
    )
    p_onboard.add_argument(
        "--status",
        action="store_true",
        help="report where onboarding is without changing anything",
    )


def handles(command: str) -> bool:
    return command in {"init", "onboard"}


def handle(
    args: argparse.Namespace,
    *,
    resolve_root: Callable[[], Any],
    printer: Callable[[Any, bool], None],
) -> None:
    json_out = bool(getattr(args, "json", False))

    if args.command == "init":
        out = onboarding.run_init(
            data_root=getattr(args, "data_root", None),
            accept_proposal=bool(getattr(args, "yes", False)),
            force=bool(getattr(args, "force", False)),
        )
        printer(out, json_out)
        return

    # -- onboard ------------------------------------------------------------
    state = OnboardingState.load(resolve_root())

    if getattr(args, "status", False):
        printer(onboarding.status(state), json_out)
        return

    step = getattr(args, "step", None) or state.step

    if step != state.step:
        printer(
            {
                "error": "out_of_order",
                "detail": f"onboarding is at {state.step!r}, not {step!r}",
                "action": onboarding.next_action(state),
            },
            json_out,
        )
        raise SystemExit(2)

    if step == "init":
        printer(onboarding.run_init(), json_out)
        return

    if step == "discover":
        out = onboarding.run_discover(state)
        _advance(state, "consent", out)
        printer(out, json_out)
        return

    if step == "consent":
        try:
            answers = json.loads(getattr(args, "answer", None) or "{}")
        except ValueError as exc:
            printer(
                {"error": "bad_answer", "detail": f"--answer is not valid JSON: {exc}"},
                json_out,
            )
            raise SystemExit(2)
        if not isinstance(answers, dict):
            printer(
                {"error": "bad_answer", "detail": "--answer must be a JSON object"},
                json_out,
            )
            raise SystemExit(2)

        out = onboarding.run_consent(state, answers)
        if out.get("status") in {"consented", "nothing_to_consent", "already_consented"}:
            _advance(state, "export", out)
        printer(out, json_out)
        return

    if step == "export":
        out = onboarding.run_export(state)
        if out.get("status") in {"exported", "nothing_new", "nothing_to_export"}:
            _advance(state, "preview", out)
        printer(out, json_out)
        return

    if step == "preview":
        out = onboarding.run_preview(state, confirm=bool(getattr(args, "confirm", False)))
        if out.get("status") == "confirmed":
            _advance(state, "import", out)
        elif out.get("status") == "nothing_to_preview":
            _advance(state, "import", out)
        printer(out, json_out)
        return

    if step == "import":
        out = onboarding.run_import(state)
        if out.get("status") in {"imported", "nothing_to_import"}:
            _advance(state, "review", out)
        printer(out, json_out)
        if out.get("status") == "partial_failure":
            raise SystemExit(4)
        return

    if step == "review":
        out = onboarding.run_review(
            state,
            approve=list(getattr(args, "approve", None) or []),
            reject=list(getattr(args, "reject", None) or []),
            auto_reject_unreviewed=bool(getattr(args, "auto_reject_unreviewed", False)),
        )
        # A run with no sources has nothing to review; that is a completion,
        # not a reason to sit at this step forever.
        if out.get("status") in {"reviewed", "nothing_to_review"}:
            _advance(state, "connect", out)
        printer(out, json_out)
        return

    if step == "connect":
        out = onboarding.run_connect(state)
        # Only a real connection completes onboarding. A human-action status
        # (missing config, no detected runtime, mismatched existing entry) must
        # leave the machine at `connect`, never produce done+needs_human.
        if out.get("status") in {"connected", "already_connected"}:
            _advance(state, "done", out)
        else:
            out["blocked_at"] = state.step
        printer(out, json_out)
        return

    if step in UNIMPLEMENTED:
        printer(
            {
                "status": "not_implemented",
                "step": step,
                "purpose": onboarding.STEP_PURPOSE.get(step, ""),
                "action": onboarding.next_action(state),
            },
            json_out,
        )
        raise SystemExit(3)

    printer(
        {"status": "nothing_to_do", "step": step, "action": onboarding.next_action(state)},
        json_out,
    )


def _advance(state: OnboardingState, to: str, out: dict) -> None:
    """Advance the persisted state and report where it landed."""
    try:
        state.advance(to)
    except OnboardingError as exc:
        out["advance_error"] = str(exc)
        return
    state.save()
    out["advanced_to"] = state.step
