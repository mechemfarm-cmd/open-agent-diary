# Hermes memory provider — Agent Diary

Makes Open Agent Diary the **primary durable memory** for a [Hermes Agent](https://github.com/NousResearch/hermes-agent), with Hermes' built-in memory demoted to a small, mirrored backup.

Without this plugin the diary is only a manually-queried archive: nothing writes to it automatically and nothing is injected from it. Installing the diary does **not** make it primary — this wiring does.

## What it provides

| Method | Job |
|---|---|
| `prefetch()` | Recalls relevant diary context **before every turn**. This is what makes the diary primary rather than merely searchable. |
| `on_memory_write()` | Mirrors every built-in memory write into the diary as a `diary_note` (fire-and-forget, so it never blocks the memory tool). |
| `system_prompt_block()` | Tells the agent the diary is its primary durable memory. |
| `get_tool_schemas()` | Exposes `agendiary_search_memory` as a first-class tool. |
| `backup_paths()` | Declares the diary data root so `hermes backup` preserves it. |

## Install

1. Copy `agentdiary/` into `$HERMES_HOME/plugins/`:

```
cp -r agentdiary ~/.hermes/plugins/agentdiary
```

2. Point Hermes at it and set the backup budget:

```
hermes config set memory.provider agentdiary
hermes config set memory.memory_char_limit 2200
```

3. Restart the gateway.

## Configuration

Both values are environment overrides, so **one copy of this plugin works on every machine** — no per-host edits.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTDIARY_URL` | `http://127.0.0.1:8041` | Where the diary server is listening |
| `AGENTDIARY_DATA` | `~/development/agent-diary/data` | Diary data root, used by `backup_paths()` |

If your gateway runs under systemd, set them on the unit rather than exporting them in a shell:

```
systemctl --user edit hermes-gateway.service
```

```ini
[Service]
Environment=AGENTDIARY_DATA=%h/open-agent-diary/data
```

## Verify it is actually primary

Installing is not proof. Check all three:

```
hermes config get memory        # provider must be 'agentdiary', not ''
```

Then perform any memory write and confirm a row appears:

```
sqlite3 <diary-data-root>/index/memory.db \
  "SELECT COUNT(*) FROM entries WHERE entry_type='diary_note';"
```

A host with zero `diary_note` rows has never mirrored a write.

The tell for **role** is `memory_char_limit`: `2200` means built-in memory is configured as the backup; the `5000` default means it is still acting as the primary.

## Pitfall: the budget must actually fit

Setting `memory_char_limit` to `2200` while the built-in `MEMORY.md` still holds several thousand characters of primary-role content will **reject every memory write**:

```
After applying all 1 operations, memory would be at 4,969/2,200 chars -- over the limit.
```

The agent then silently cannot remember anything new. Trim the built-in memory to fit its backup role *as part of the conversion* — a host that converted cleanly sits well under the limit (e.g. ~1,900/2,200). Keep the full text somewhere first (a backup file, or an `append_entry` snapshot into the diary) so nothing is lost.

## Pitfall: prompt-cache cost

`prefetch()` makes an HTTP call to the diary before every turn (~0.5 s warm against a local instance). That latency is the price of automatic recall — worth knowing if turns feel slow.
