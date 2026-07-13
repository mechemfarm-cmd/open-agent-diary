#!/usr/bin/env bash
# hermes-to-diary.sh — Extract Hermes sessions and import into Agent Diary
# Runs as a daily cron job. Only imports sessions not yet seen.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIARY_ROOT="$SCRIPT_DIR/.."
HERMES_DB="$HOME/.hermes/state.db"
MARKER_DIR="$DIARY_ROOT/data/hermes-import-tracker"

mkdir -p "$MARKER_DIR" "$DIARY_ROOT/data/staging"

cd "$DIARY_ROOT"

# Find Hermes sessions not yet imported
for session_id in $(sqlite3 "$HERMES_DB" "SELECT id FROM sessions ORDER BY started_at ASC;"); do
    marker="$MARKER_DIR/$session_id.imported"
    if [ -f "$marker" ]; then
        continue
    fi

    # Get session info
    session_info=$(sqlite3 "$HERMES_DB" \
        "SELECT id, title, COALESCE(title, id), started_at FROM sessions WHERE id='$session_id';")
    title=$(sqlite3 "$HERMES_DB" "SELECT COALESCE(title, id) FROM sessions WHERE id='$session_id';")
    started_at=$(sqlite3 "$HERMES_DB" "SELECT datetime(started_at,'unixepoch') FROM sessions WHERE id='$session_id';")

    echo "--- Importing Hermes session: $session_id ($title) ---"

    staging_jsonl="$DIARY_ROOT/data/staging/transcript-$session_id.jsonl"
    session_jsonl="$DIARY_ROOT/data/staging/session-$session_id.jsonl"

    # Extract user + assistant messages as canonical transcript JSONL
    # Skip tool messages (raw tool output) and empty assistant content (tool-call-only responses)
    sqlite3 -json "$HERMES_DB" \
        "SELECT id as message_id, timestamp as created_at, role as author_role, content
         FROM messages
         WHERE session_id='$session_id' AND active=1 AND role IN ('user', 'assistant')
           AND content IS NOT NULL AND content != ''
         ORDER BY id ASC;" | python3 -c "
import json, sys
data = json.load(sys.stdin)
from datetime import datetime, timezone
out = []
for msg in data:
    ts = msg.get('created_at', 0)
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ''
    role = msg.get('author_role', '')
    speaker = 'sampleuser' if role == 'user' else 'assistant'
    content = msg.get('content', '') or ''
    # Skip messages that are just tool output payloads
    if content.startswith('{\"output\":'):
        continue
    out.append({
        'message_id': str(msg.get('message_id', '')),
        'created_at': iso,
        'author_role': role,
        'speaker': speaker,
        'content': content,
    })
for item in out:
    print(json.dumps(item))
" > "$staging_jsonl"

    msg_count=$(wc -l < "$staging_jsonl")
    if [ "$msg_count" -eq 0 ]; then
        echo "  No user/assistant messages, skipping."
        rm -f "$staging_jsonl"
        echo "$(date -Iseconds)" > "$marker"
        continue
    fi

    # Build session chunks
    agent-diary build-session-jsonl \
        --input-path "$staging_jsonl" \
        --output-path "$session_jsonl" \
        --source "hermes-session"

    chunk_count=$(wc -l < "$session_jsonl")
    echo "  $msg_count transcript messages → $chunk_count session chunks"

    # Import and analyze (produces conversation briefs, compressed memory, open loops)
    agent-diary import-session-and-analyze \
        --path "$session_jsonl" \
        --source-session-id "$session_id"

    # Mark imported
    echo "$(date -Iseconds)" > "$marker"

    # Clean up staging
    rm -f "$staging_jsonl" "$session_jsonl"

    echo "  ✓ Imported $chunk_count entries"
done

# ── Second pass: backfill work traces for sessions in the diary ──
echo ""
echo "--- Backfilling Hermes work traces ---"
cd "$DIARY_ROOT"
python3 scripts/backfill-hermes-work-traces.py \
    --diary-db data/index/memory.db \
    --hermes-db "$HERMES_DB" \
    --data-dir data 2>&1 || echo "  ⚠ Work trace backfill had issues (non-fatal)"

echo ""
echo "=== Hermes-to-diary import complete ==="