"""CLI surface for onboarding.

Deliberately small: two commands. The existing 71 subcommands are the tools;
onboarding is the guided path through four of them. Keeping the surface narrow
is the point — a new user should not need to know the other sixty-eight exist.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from agent_diary import onboarding
from agent_diary.onboarding_state import OnboardingError, OnboardingState, STEPS


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
    data_root = resolve_root()
    state = OnboardingState.load(data_root)

    if getattr(args, "status", False):
        printer(onboarding.status(state), json_out)
        return

    step = getattr(args, "step", None) or state.step

    if step != state.step:
        # Running an explicit step out of order is a mistake worth naming.
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
        state.advance("consent")
        state.save()
        out["advanced_to"] = state.step
        printer(out, json_out)
        return

    # Remaining steps are implemented in their own tasks; report the contract
    # rather than pretending to have run them.
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
