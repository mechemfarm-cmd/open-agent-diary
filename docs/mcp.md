# MCP setup

The repository includes `agent-diary-mcp.py`, an MCP wrapper for a running local Agent Diary server. MCP gives compatible agents direct tools for recall and inspection; it does not itself start the diary server or make the diary primary memory.

## Prerequisites

1. Install Open Agent Diary.
2. Start the server on loopback:

```bash
agent-diary serve --host 127.0.0.1 --port 8041
```

3. Install `uv` or otherwise provide the MCP Python dependency.

## Run the MCP server

From the repository root:

```bash
uv run --with mcp python agent-diary-mcp.py
```

The current wrapper uses a local `http://localhost:8041` base URL. Run it on the same machine as the diary server, or use SSH forwarding so that endpoint reaches the correct server.

## Configure your MCP host

Use your host application's stdio-MCP configuration to run:

```text
uv run --with mcp python /absolute/path/to/open-agent-diary/agent-diary-mcp.py
```

Do not put credentials in the command. The local diary API is unauthenticated in v0.1; access control comes from keeping it on loopback/private infrastructure.

## Tool use

The MCP surface supports memory search, entry detail, work-trace search, listing, health checks, and graph inspection. Use it as an evidence tool:

1. search memory,
2. inspect work traces when operational detail matters,
3. fetch raw entries for important claims,
4. inspect graph facts with their evidence.

See [Agent integration](agent-integration.md) for the recall order and [API reference](api-reference.md) for HTTP semantics.

## Verify

Before configuring a host, confirm the diary itself responds:

```bash
curl -sS http://127.0.0.1:8041/status
```

Then start the MCP wrapper and use the host's MCP inspection tool to confirm it lists Agent Diary tools. If the wrapper cannot connect, fix the local diary server first; MCP and HTTP are separate processes.