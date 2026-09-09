#!/usr/bin/env python3
"""Hermes Graph Extractor — claims Agent Diary extraction jobs, sends source text
to an LLM, and submits structured facts back to the graph.

Decision summary (agreed with operator):
- Batches multiple source texts per LLM call to control cost.
- Only direct user statements establish facts; assistant-only text is skipped here.
- Calls the Agent Diary HTTP API directly (not MCP) for extraction flows.

Usage:
  python3 scripts/hermes-graph-extractor.py [--limit 5] [--batch 3] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

DIARY_BASE = os.environ.get("AGENT_DIARY_BASE", "http://127.0.0.1:8041")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("GRAPH_EXTRACTOR_MODEL", "deepseek/deepseek-v4-flash")
WORKER_ID = os.environ.get("GRAPH_EXTRACTOR_WORKER", "hermes-graph-extractor")
LEASE_SECONDS = int(os.environ.get("GRAPH_EXTRACTOR_LEASE_SECONDS", "600"))

SYSTEM_PROMPT = """You extract structured facts from diary conversations for a local knowledge graph.
Rules:
1. Only extract facts that are DIRECT, UNAMBIGUOUS user statements about persistent state:
   - Device relationships (RUNS_ON, OWNS, CONNECTED_TO, LOCATED_IN)
   - Software/services (USES, RUNS_ON)
   - Personal/project relationships (MEMBER_OF, WORKS_ON)
2. NEVER extract from assistant-only text. Ignore the assistant's own claims.
3. Ignore speculation ("I might...", "maybe..."), intentions, or transient chat.
4. Normalize predicate names to: OWNS, RUNS_ON, USES, CONNECTED_TO, MEMBER_OF, WORKS_ON, LOCATED_IN, HAS_RAM, HAS_IP_ADDRESS, HAS_OS.
5. Entity types: person, device, software_service, project, place, organization, other.
6. Confidence: high (explicit statement), medium (strong inference from context), low (unclear).

Respond with ONLY a JSON object:
{
  "no_facts": false,
  "entities": [{"name": "Lucy", "type": "device"}],
  "facts": [
    {"subject": "Pi-hole", "predicate": "RUNS_ON", "object": "Lucy", "confidence": "high", "timestamp": "2026-09-09T12:00:00Z"}
  ]
}
If there are no facts, return {"no_facts": true, "reason": "optional explanation"}.
"""


def _post(path: str, body: dict) -> dict:
    url = DIARY_BASE + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_llm(batch_text: str) -> dict:
    """Send batched source text to the LLM and parse the JSON result."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": batch_text[:24000]},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    # Strip markdown fences if present
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(content)


def process_job(job: dict) -> None:
    """Fetch a job's source, send to LLM, submit result."""
    job_id = job["job_id"]
    source = _post("/graph/fetch_source", {"job_id": job_id})
    content = source.get("content", "")
    author_role = source.get("author_role", "")
    if not content.strip():
        _post("/graph/submit_extraction", {"job_id": job_id, "result": {"no_facts": True, "reason": "empty source"}})
        return

    # Only process user-authored entries for fact establishment
    if author_role and author_role != "user":
        _post("/graph/submit_extraction", {"job_id": job_id, "result": {"no_facts": True, "reason": f"author_role={author_role} not user"}})
        return

    try:
        llm_result = call_llm(content)
        result_payload = {
            "no_facts": bool(llm_result.get("no_facts", False)),
            "entities": llm_result.get("entities", []),
            "facts": llm_result.get("facts", []),
            "source_kind": source.get("source_kind", "raw_entry"),
            "source_id": source.get("source_id", ""),
            "extractor_method": f"openrouter:{OPENROUTER_MODEL}",
            "extractor_version": "1.0",
            "reason": llm_result.get("reason", ""),
        }
        _post("/graph/submit_extraction", {"job_id": job_id, "result": result_payload})
    except Exception as e:
        _post("/graph/fail_extraction", {"job_id": job_id, "error": str(e), "retryable": True})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5, help="Max jobs to claim per run")
    parser.add_argument("--dry-run", action="store_true", help="Just show queue status, no processing")
    args = parser.parse_args()

    if args.dry_run:
        status = _post("/graph/queue_status", {})
        print(json.dumps(status, indent=2))
        return 0

    claimed = _post("/graph/claim_jobs", {
        "limit": args.limit,
        "worker_id": WORKER_ID,
        "lease_seconds": LEASE_SECONDS,
    })
    jobs = claimed.get("jobs", [])
    print(f"Claimed {len(jobs)} job(s)")
    for job in jobs:
        process_job(job)
        time.sleep(0.5)  # be gentle to the LLM API

    remaining = _post("/graph/queue_status", {})
    print(json.dumps(remaining, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())