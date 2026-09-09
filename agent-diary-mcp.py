#!/usr/bin/env python3
"""MCP server that wraps Agent Diary's HTTP API as first-class Hermes tools.

Run with: uv run --with mcp python agent-diary-mcp.py
Or install: uv tool install agent-diary-mcp.py

Exposes tools:
  - agendiary_search_memory(query, limit=5)  — search entries for keywords
  - agendiary_get_entry(entry_id)            — full entry content
  - agendiary_search_work_trace(query, limit=5) — search work traces
  - agendiary_list_entries(source=None, limit=10) — recent entries

All tools proxy to http://localhost:8041
"""

import json
import sys
import urllib.request
import urllib.error

DIARY_BASE = "http://localhost:8041"


def _diary_post(path: str, body: dict) -> dict | None:
    """POST to Agent Diary and return parsed JSON."""
    url = f"{DIARY_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _diary_get(path: str) -> dict | None:
    """GET from Agent Diary."""
    url = f"{DIARY_BASE}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Tool implementations ─────────────────────────────────────────────

def search_memory(query: str, limit: int = 5) -> str:
    """Search Agent Diary memory entries.

    Args:
        query: Keywords or phrase to search for
        limit: Max results to return (default 5, max 20)
    """
    limit = min(max(1, limit), 20)
    result = _diary_post("/search_memory", {"query": query, "limit": limit})
    if not result or not result.get("ok"):
        return json.dumps({"error": "Agent Diary search failed", "detail": result})
    
    matches = result.get("result", {}).get("matches", [])
    if not matches:
        return json.dumps({"results": [], "message": "No matches found"})
    
    output = []
    for m in matches:
        entry = {
            "entry_id": m["entry_id"],
            "source": m.get("source", ""),
            "author_role": m.get("author_role", ""),
            "match_layer": m.get("match_layer", ""),
            "has_compressed_memory": "compressed_memory" in m.get("supporting_layers", []),
            "snippet": m.get("match_text", "")[:300],
        }
        output.append(entry)
    
    return json.dumps({"results": output, "count": len(output)})


def get_entry(entry_id: str) -> str:
    """Fetch a full Agent Diary entry by its entry_id.

    Args:
        entry_id: The entry ID (e.g. 'entry_f3b8541fb30d4f829b1950042cdc7d22')
    """
    result = _diary_post("/fetch_entry_detail", {"entry_id": entry_id})
    if not result or not result.get("ok"):
        return json.dumps({"error": f"Entry {entry_id} not found via API", "detail": result})
    raw = result.get("result", {})
    return json.dumps({"entry": raw})


def search_work_trace(query: str, limit: int = 5) -> str:
    """Search Agent Diary work traces.

    Args:
        query: Keywords to search for in work trace events
        limit: Max results (default 5, max 20)
    """
    limit = min(max(1, limit), 20)
    result = _diary_post("/search_work_trace", {"query": query, "limit": limit})
    if not result or not result.get("ok"):
        return json.dumps({"error": "Work trace search failed", "detail": result})
    
    matches = result.get("result", {}).get("matches", [])
    if not matches:
        return json.dumps({"results": [], "message": "No work trace matches found"})
    
    return json.dumps({"results": matches, "count": len(matches)})


def list_entries(limit: int = 10, source: str | None = None) -> str:
    """List recent Agent Diary entries.

    Args:
        limit: Max entries to return (default 10, max 50)
        source: Optional source filter (e.g. 'hermes-session')
    """
    limit = min(max(1, limit), 50)
    body = {"limit": limit}
    if source:
        body["source"] = source
    result = _diary_post("/list_entries", body)
    if not result or not result.get("ok"):
        return json.dumps({"error": "List entries failed", "detail": result})
    
    items = result.get("result", {}).get("items", result.get("result", {}).get("entries", []))
    if not items:
        return json.dumps({"results": [], "message": "No entries found"})
    
    output = []
    for item in items[:limit]:
        output.append({
            "entry_id": item.get("entry_id", ""),
            "created_at": item.get("created_at", ""),
            "source": item.get("source", ""),
            "author_role": item.get("author_role", ""),
            "title": item.get("title", ""),
        })
    
    return json.dumps({"results": output, "count": len(output)})


def health() -> str:
    """Check if Agent Diary server is running and healthy."""
    try:
        resp = _diary_get("/status")
        if resp:
            return json.dumps({"status": "healthy", "detail": resp})
        return json.dumps({"status": "healthy", "detail": "server up"})
    except Exception as e:
        return json.dumps({"status": "unhealthy", "error": str(e)})


def _graph_post(path: str, body: dict) -> str:
    """Call a /graph/* endpoint and return the result JSON."""
    result = _diary_post(path, body)
    if not result or not result.get("ok"):
        return json.dumps({"error": "Graph query failed", "detail": result})
    return json.dumps(result.get("result", {}))


def graph_find_entity(query: str, limit: int = 5) -> str:
    """Search for entities by name or alias."""
    return _graph_post("/graph/find_entity", {"query": query, "limit": limit})


def graph_get_entity(entity_id: str, include_history: bool = False) -> str:
    """Get entity details with current facts and aliases."""
    return _graph_post("/graph/get_entity", {"entity_id": entity_id, "include_history": include_history})


def graph_neighbors(entity_id: str, states: str = "current", depth: int = 1) -> str:
    """Get neighboring entities and facts around an entity."""
    return _graph_post("/graph/neighbors", {
        "entity_id": entity_id,
        "states": [s.strip() for s in states.split(",")],
        "depth": depth,
    })


def graph_search(query: str, states: str = "current") -> str:
    """Search across entities and facts."""
    return _graph_post("/graph/search", {
        "query": query,
        "states": [s.strip() for s in states.split(",")],
    })


def graph_explain_fact(fact_id: str) -> str:
    """Get full details of a fact including all evidence."""
    return _graph_post("/graph/explain_fact", {"fact_id": fact_id})


def graph_get_subgraph(entity_id: str, depth: int = 1, max_nodes: int = 20, states: str = "current") -> str:
    """Get a connected subgraph around an entity."""
    return _graph_post("/graph/get_subgraph", {
        "entity_id": entity_id,
        "depth": depth,
        "max_nodes": max_nodes,
        "states": [s.strip() for s in states.split(",")],
    })


def graph_add_fact(subject_id: str, predicate: str, object_entity_id: str | None = None,
                    object_value: str | None = None, object_value_type: str | None = None,
                    source_kind: str = "user_assertion", source_id: str = "mcp", reason: str = "") -> str:
    """Manually add a fact to the knowledge graph."""
    body = {
        "subject_id": subject_id,
        "predicate": predicate,
        "source_kind": source_kind,
        "source_id": source_id,
        "reason": reason,
    }
    if object_entity_id:
        body["object_kind"] = "entity"
        body["object_entity_id"] = object_entity_id
    else:
        body["object_kind"] = "value"
        body["object_value"] = object_value or ""
        body["object_value_type"] = object_value_type or "text"
    return _graph_post("/graph/add_fact", body)


def graph_correct_fact(fact_id: str, correction: str, reason: str) -> str:
    """Correct a current fact with a new value. Old fact becomes historical."""
    return _graph_post("/graph/correct_fact", {
        "fact_id": fact_id,
        "correction": correction,
        "reason": reason,
    })


def graph_queue_status() -> str:
    """Check the extraction queue status."""
    return _graph_post("/graph/queue_status", {})


# ── MCP server entry point ───────────────────────────────────────────

TOOLS = {
    "agendiary_search_memory": {
        "description": "Search Agent Diary memory for past conversations and decisions. Use this for recall questions about past work, discussions, or decisions. Returns entries with snippets and indicates if compressed memory is available.",
        "fn": search_memory,
        "parameters": {
            "query": {"type": "string", "description": "Keywords or phrase to search for"},
            "limit": {"type": "number", "description": "Max results (1-20, default 5)", "default": 5},
        },
    },
    "agendiary_get_entry": {
        "description": "Fetch the full content of a specific Agent Diary entry by its ID. Use this when a search result's snippet isn't enough — it returns the complete entry content including full chat text.",
        "fn": get_entry,
        "parameters": {
            "entry_id": {"type": "string", "description": "Entry ID (e.g. 'entry_f3b8541fb30d4f829b1950042cdc7d22')"},
        },
    },
    "agendiary_search_work_trace": {
        "description": "Search Agent Diary work traces — terminal commands, file edits, and tool calls from past sessions. Good for finding what commands were run, what files were changed, or debugging steps taken.",
        "fn": search_work_trace,
        "parameters": {
            "query": {"type": "string", "description": "Keywords to search for in work traces"},
            "limit": {"type": "number", "description": "Max results (1-20, default 5)", "default": 5},
        },
    },
    "agendiary_list_entries": {
        "description": "List recent Agent Diary entries. Useful for browsing what's been recorded recently or filtering by source.",
        "fn": list_entries,
        "parameters": {
            "limit": {"type": "number", "description": "Max entries (1-50, default 10)", "default": 10},
            "source": {"type": "string", "description": "Optional source filter (e.g. 'hermes-session')"},
        },
    },
    "agendiary_health": {
        "description": "Check if Agent Diary server is healthy and reachable.",
        "fn": health,
        "parameters": {},
    },
    "graph_find_entity": {
        "description": "Search the knowledge graph for entities by name or alias. Returns matching entities with their canonical names and types.",
        "fn": graph_find_entity,
        "parameters": {
            "query": {"type": "string", "description": "Name or alias to search for"},
            "limit": {"type": "number", "description": "Max results (1-20, default 5)", "default": 5},
        },
    },
    "graph_get_entity": {
        "description": "Get full entity details including current facts, history, and aliases from the knowledge graph.",
        "fn": graph_get_entity,
        "parameters": {
            "entity_id": {"type": "string", "description": "Entity ID (ge_...) or canonical name"},
            "include_history": {"type": "boolean", "description": "Include historical facts", "default": False},
        },
    },
    "graph_neighbors": {
        "description": "Explore connections around an entity in the knowledge graph — see what it runs on, who owns it, etc.",
        "fn": graph_neighbors,
        "parameters": {
            "entity_id": {"type": "string", "description": "Entity ID or canonical name"},
            "states": {"type": "string", "description": "Comma-separated states (current,historical,planned,retracted)", "default": "current"},
            "depth": {"type": "number", "description": "Connection depth (default 1)", "default": 1},
        },
    },
    "graph_search": {
        "description": "Search the knowledge graph across both entities and facts. Good for questions like 'what runs on Lucy?' or 'what does Bill own?'",
        "fn": graph_search,
        "parameters": {
            "query": {"type": "string", "description": "Search query"},
            "states": {"type": "string", "description": "Comma-separated states", "default": "current"},
        },
    },
    "graph_explain_fact": {
        "description": "Get full details of a fact including all evidence sources, validity dates, and correction history.",
        "fn": graph_explain_fact,
        "parameters": {
            "fact_id": {"type": "string", "description": "Fact ID (gf_...)"},
        },
    },
    "graph_get_subgraph": {
        "description": "Get a connected subgraph centered on an entity, showing its direct relationships.",
        "fn": graph_get_subgraph,
        "parameters": {
            "entity_id": {"type": "string", "description": "Entity ID or canonical name"},
            "depth": {"type": "number", "description": "Connection depth (default 1)", "default": 1},
            "max_nodes": {"type": "number", "description": "Max nodes to return (default 20)", "default": 20},
            "states": {"type": "string", "description": "Comma-separated states", "default": "current"},
        },
    },
    "graph_add_fact": {
        "description": "Manually add a fact to the knowledge graph. For single-value predicates like RUNS_ON, this auto-closes any previous current fact.",
        "fn": graph_add_fact,
        "parameters": {
            "subject_id": {"type": "string", "description": "Subject entity ID or name"},
            "predicate": {"type": "string", "description": "Predicate (RUNS_ON, OWNS, USES, etc.)"},
            "object_entity_id": {"type": "string", "description": "Object entity ID or name (for entity facts)"},
            "object_value": {"type": "string", "description": "Object value (for value facts like IP addresses)"},
            "object_value_type": {"type": "string", "description": "Value type (text, integer, ip_address)", "default": "text"},
            "reason": {"type": "string", "description": "Why this fact was added", "default": ""},
        },
    },
    "graph_correct_fact": {
        "description": "Correct a current fact. The old fact becomes historical and a new current fact is created with the correction.",
        "fn": graph_correct_fact,
        "parameters": {
            "fact_id": {"type": "string", "description": "Fact ID to correct"},
            "correction": {"type": "string", "description": "New value or entity name"},
            "reason": {"type": "string", "description": "Why the correction was made"},
        },
    },
    "graph_queue_status": {
        "description": "Check the status of the extraction queue — how many pending, claimed, succeeded, failed jobs.",
        "fn": graph_queue_status,
        "parameters": {},
    },
}


def main():
    """Simple stdio MCP protocol server."""
    # Read initialization request
    init_line = sys.stdin.readline()
    try:
        init = json.loads(init_line)
    except json.JSONDecodeError:
        init = {}

    # Send initialization response with capabilities
    capabilities = {
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {},
        },
        "serverInfo": {
            "name": "agent-diary-mcp",
            "version": "1.0.0",
        },
    }
    response = {"jsonrpc": "2.0", "id": init.get("id", 1), "result": capabilities}
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()

    # Main request loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        if method == "tools/list":
            tool_list = []
            for name, spec in TOOLS.items():
                schema = {
                    "name": name,
                    "description": spec["description"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                    },
                }
                for pname, pinfo in spec["parameters"].items():
                    pschema = {"type": pinfo["type"], "description": pinfo["description"]}
                    if "default" in pinfo:
                        pschema["default"] = pinfo["default"]
                    schema["inputSchema"]["properties"][pname] = pschema
                    if "default" not in pinfo:
                        schema["inputSchema"].setdefault("required", []).append(pname)
                tool_list.append(schema)

            result = {"tools": tool_list}
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
            sys.stdout.flush()

        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            tool = TOOLS.get(tool_name)
            if not tool:
                sys.stdout.write(
                    json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Tool not found: {tool_name}"}}) + "\n"
                )
                sys.stdout.flush()
                continue

            try:
                # Build kwargs from arguments
                kwargs = {}
                for pname in tool["parameters"]:
                    if pname in arguments:
                        kwargs[pname] = arguments[pname]
                    elif "default" in tool["parameters"][pname]:
                        kwargs[pname] = tool["parameters"][pname]["default"]

                content = tool["fn"](**kwargs)
                sys.stdout.write(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": content}],
                        },
                    }) + "\n"
                )
                sys.stdout.flush()
            except Exception as e:
                sys.stdout.write(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32603, "message": str(e)},
                    }) + "\n"
                )
                sys.stdout.flush()

        elif method == "initialized":
            # No-op, just acknowledge
            pass

        elif method == "ping":
            # MCP keepalive: respond so the client doesn't time out and
            # reconnect-loop the server (this was causing ~8.5min restarts).
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}}) + "\n")
            sys.stdout.flush()

        elif method == "resources/list":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}) + "\n")
            sys.stdout.flush()

        elif method == "prompts/list":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}) + "\n")
            sys.stdout.flush()

        else:
            # Unknown method. Requests (with an id) must get a JSON-RPC error
            # response or the client hangs and its keepalive times out; only
            # notifications (no id) may be silently ignored.
            if req_id is not None:
                sys.stdout.write(
                    json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}) + "\n"
                )
                sys.stdout.flush()


if __name__ == "__main__":
    main()