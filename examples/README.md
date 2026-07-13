# Synthetic examples

These fixtures are invented demo data. They are not exported from a real user's chat history.

Import the session example:

```bash
agent-diary import-session-jsonl --path examples/synthetic-session-import.jsonl --import-id demo
agent-diary produce-conversation-briefs --import-id demo --force
agent-diary produce-compressed-memory --import-id demo --force
agent-diary search-memory --query "privacy review" --json
```

The work-trace JSONL demonstrates the shape consumed by importer/adapters. Current CLI work-trace import paths are adapter-specific, so this file is primarily documentation/test data.
