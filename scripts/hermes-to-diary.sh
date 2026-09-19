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

# ── Third pass: feed the optional knowledge graph ────────────────────
# Importing entries alone does not update the graph. Enqueue recent source
# entries and then drain a bounded number of extraction jobs. The queue/worker
# separation is intentional: extraction can call a hosted model and may take a
# long time for one transcript.
DIARY_API="${AGENT_DIARY_BASE:-http://127.0.0.1:8041}"
echo ""
echo "--- Feeding the knowledge graph ---"

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$HOME/.hermes/.env" ]; then
    OPENROUTER_API_KEY="$(grep -m1 '^OPENROUTER_API_KEY=' "$HOME/.hermes/.env" | cut -d= -f2- | tr -d '"'"'"' ')"
    export OPENROUTER_API_KEY
fi

# One long transcript can exceed the historical ten-minute job lease. Keep a
# generous lease and claim one job at a time so work is never re-claimed while
# still running. The bounded daily slice keeps a cron wrapper below its limit.
export GRAPH_EXTRACTOR_LEASE_SECONDS="${GRAPH_EXTRACTOR_LEASE_SECONDS:-3600}"
GRAPH_EXTRACTOR_DAILY_JOBS="${GRAPH_EXTRACTOR_DAILY_JOBS:-3}"
queue_json="$DIARY_ROOT/data/staging/graph-queue.json"

graph_pending() {
    curl -s -X POST "$DIARY_API/graph/queue_status" \
        -H 'Content-Type: application/json' -d '{}' -o "$queue_json" 2>/dev/null || true
    python3 -c "import json,sys
try:
    print(json.load(open(sys.argv[1]))['result']['pending'])
except Exception:
    print('?')
" "$queue_json" 2>/dev/null || echo "?"
}

# Idempotent: already-enqueued source entries are skipped.
curl -s -X POST "$DIARY_API/graph/enqueue_recent" \
    -H 'Content-Type: application/json' -d '{"limit": 200}' > /dev/null 2>&1 || true
pending_before=$(graph_pending)

if [ "$pending_before" = "?" ]; then
    echo "  ⚠ diary server unreachable at $DIARY_API — graph not fed"
elif [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "  ⚠ OPENROUTER_API_KEY not set — ${pending_before} job(s) pending"
elif [ "$pending_before" = "0" ]; then
    echo "  graph queue empty"
else
    jobs_done=0
    while [ "$(graph_pending)" != "0" ] && [ "$jobs_done" -lt "$GRAPH_EXTRACTOR_DAILY_JOBS" ]; do
        python3 scripts/hermes-graph-extractor.py --limit 1 2>&1 | grep -E '^Claimed' || true
        jobs_done=$((jobs_done + 1))
    done
    echo "  graph: ${pending_before} → $(graph_pending) job(s) pending"
fi

echo ""
echo "=== Hermes-to-diary import complete ==="