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

        else:
            # Unknown method — but don't crash, the MCP inspector sends these
            pass


if __name__ == "__main__":
    main()