from __future__ import annotations

import argparse
import json

from agent_diary.analytics.semantic_situations import ALLOWED_PURPOSES, compile_situation, render_situation_view
from agent_diary.config import default_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_diary.cli.semantic_preview",
        description="Read-only semantic situation preview over existing local Agent Diary data.",
    )
    parser.add_argument("--topic", required=True, help="topic or entity anchor to preview")
    parser.add_argument("--purpose", required=True, choices=sorted(ALLOWED_PURPOSES), help="retrieval purpose")
    parser.add_argument("--limit", type=int, default=20, help="maximum source candidates/items to inspect")
    parser.add_argument("--char-budget", type=int, default=4000, help="maximum rendered characters")
    parser.add_argument("--json", action="store_true", help="print serialized situation JSON instead of text")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    paths = default_paths()
    situation = compile_situation(
        paths,
        topic=args.topic,
        purpose=args.purpose,
        limit=args.limit,
        char_budget=args.char_budget,
    )
    if args.json:
        print(json.dumps(situation.to_dict(), indent=2))
    else:
        print(render_situation_view(situation, purpose=args.purpose, char_budget=args.char_budget))


if __name__ == "__main__":
    main()
