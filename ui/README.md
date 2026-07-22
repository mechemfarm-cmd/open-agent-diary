# Open Agent Diary UI

## Release status

The browser UI in this directory is included in the supported v0.1 release path. It is served by the Python backend and is the current human-facing UI. The separate Tauri desktop shell is experimental until it manages the backend lifecycle itself.


The UI is intentionally organized around a simple human loop:

1. **Search or browse** for a memory.
2. **Read the source record** as the primary evidence.
3. **Correct or inspect** the record without hiding the original.

The goal is not to expose every internal layer at once. Transparency remains available, but advanced provenance and generated artifacts stay behind clear labels.

## Main surfaces

### Search your diary

Prominent first action. Searches generated memory first and falls back to source entries when needed.

### Recent entries

A simple browse list for people who do not know what to search for. On a new, empty installation it shows exact commands for importing the repository's synthetic demo data. Advanced filters and recent imports are collapsed by default.

### First-use orientation

The page begins with a compact three-step explanation: find a record, read the raw source record, then correct or inspect supporting layers. It also makes the local/private data model visible without promoting network exposure.

### Source record

The center panel. This is the authority. If a summary and the source disagree, the source wins.

### Correct or inspect

The right panel has three tabs:

| Tab | Purpose |
|---|---|
| **Correct** | Add an annotation or correction. The original source record remains visible. |
| **Summary** | See generated summaries, possible follow-ups, and recorded agent work. |
| **Advanced** | Inspect search-memory artifacts, provenance, support artifacts, and regeneration tools. |

## Run locally

The backend serves the UI directly:

```bash
cd /path/to/open-agent-diary
PYTHONPATH=src python3 -m agent_diary.cli.main serve --host 127.0.0.1 --port 8041
```

Open:

```text
http://127.0.0.1:8041
```

## Manual UI check

1. Load with an empty `data/` directory and confirm the timeline presents the synthetic-demo commands and local/private data note.
2. Import `examples/synthetic-session-import.jsonl`.
3. Search for `privacy review`.
4. Open a result and confirm the center panel clearly presents the source record.
5. Add an annotation in the **Correct** tab.
6. Open **Summary** and confirm generated layers feel secondary.
7. Open **Advanced** and confirm provenance is available but not visually dominant.
8. Test at 1440px, 1280px, 1024px, and a narrow/mobile width.
