# Canonical Conversation Transcript Schema

## Purpose

Define one transport-independent conversation schema for truthful ingestion.

Telegram, WhatsApp, Signal, Discord, agent runtime session logs, and other message
surfaces should all normalize into this shape before chunking and import.

This keeps:
- the truth model stable
- chunking logic reusable
- derived layers independent of transport quirks

## Core principle

Channel-specific formats belong at the adapter edge.

The middle of the system should only see canonical conversation messages.

## Canonical message shape

Each normalized message should include:

- `message_id`
- `created_at`
- `author_role`
- `speaker`
- `content`
- `metadata`

These match the current transcript-builder shape on purpose.

## Required fields

### `message_id`

- stable within the source conversation
- should come from the source platform when possible
- if a source has no stable id, the adapter may synthesize one deterministically

### `created_at`

- ISO-8601 timestamp
- represents when the message was sent on the source system

### `author_role`

Allowed canonical values:
- `human`
- `agent`
- `system`
- `tool`

`mixed` should not be used at the per-message level.
It remains valid later at the chunk/entry level when one imported entry contains
both sides of the exchange.

### `speaker`

- human-readable display label
- should be preserved from the source when practical
- examples: `User`, `Assistant`, `Assistant`, `WhatsApp Bot`

### `content`

- normalized plain text payload used as the truthful raw message body
- adapters may flatten source-native rich text into readable plain text
- non-text-only events should be skipped unless they carry meaningful human- or
  agent-authored text

### `metadata`

- required as an object, even if empty
- carries source-specific facts that should not be promoted into the canonical
  top-level schema

## Recommended metadata keys

These are not all mandatory on every source, but they should be used
consistently when available.

- `transport`
  - `telegram`, `whatsapp`, `signal`, `discord`, `openclaw-session`, etc.
- `source_message_id`
- `source_parent_id`
- `source_session_id`
- `source_conversation_id`
- `source_thread_id`
- `channel_account_id`
- `channel_chat_id`
- `sender_id`
- `sender_username`
- `sender_display_name`
- `is_bot`

## Conversation identity

Adapters should preserve two different ideas:

### Session identity

The runtime or import batch a message came from.

Example:
- one agent runtime session file
- one exported Discord thread
- one WhatsApp chat export file

### Conversation identity

The user-facing thread or chat that should usually remain coherent across
multiple sessions or exports.

Example:
- Telegram direct chat with one person
- Discord thread id
- WhatsApp chat id

These should stay separate because a single conversation can span many runtime
sessions.

## Adapter rules

### Adapters should preserve truth

Do:
- keep real user/agent wording
- preserve timestamps
- preserve source ids
- keep source metadata attached

Do not:
- invent summaries during normalization
- merge separate messages into one canonical message
- rewrite the wording into cleaner prose

### Adapters should filter noise conservatively

Adapters may skip:
- empty messages
- tool-call-only payloads with no user-visible text
- obvious synthetic scheduler prompts that are not part of human/agent dialogue

Adapters should be cautious about skipping anything else.

## Chunking relationship

Chunking operates on canonical messages, not source-native records.

That means chunking should not care whether the source was Telegram or
WhatsApp. It should only use:
- timestamps
- author turns
- content
- canonical metadata like conversation/session/thread ids

## Immediate implementation order

1. Keep this canonical shape as the contract.
2. Treat existing agent runtime session adaptation as one adapter.
3. Build a Telegram transcript adapter that uses Telegram-side message logs as a
   better truth source than per-turn runtime session files.
4. Reuse the same chunking/import path after normalization.
5. Add later adapters for WhatsApp, Signal, Discord, and similar surfaces.

## Why this matters

Without this separation, each new messaging platform becomes a redesign.

With this separation, each new platform is just a new adapter into the same
truth model.
