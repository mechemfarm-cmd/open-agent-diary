from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  entry_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  title TEXT,
  source TEXT NOT NULL,
  author_role TEXT NOT NULL,
  raw_file_path TEXT NOT NULL,
  entry_type TEXT,
  source_session_id TEXT,
  source_conversation_id TEXT,
  import_id TEXT,
  truthful_source INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS work_trace_events (
  event_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  project TEXT,
  source_surface TEXT,
  actor TEXT,
  session_key TEXT,
  task_id TEXT,
  searchable_text TEXT NOT NULL,
  work_file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  producer TEXT NOT NULL,
  content TEXT NOT NULL,
  FOREIGN KEY (entry_id) REFERENCES entries(entry_id)
);

CREATE TABLE IF NOT EXISTS memory_index (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id TEXT NOT NULL,
  artifact_id TEXT,
  created_at TEXT NOT NULL,
  memory_text TEXT NOT NULL,
  tags TEXT,
  FOREIGN KEY (entry_id) REFERENCES entries(entry_id),
  FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_id)
);

CREATE TABLE IF NOT EXISTS work_trace_entry_links (
  event_id TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  PRIMARY KEY (event_id, entry_id),
  FOREIGN KEY (event_id) REFERENCES work_trace_events(event_id),
  FOREIGN KEY (entry_id) REFERENCES entries(entry_id)
) WITHOUT ROWID;

CREATE VIRTUAL TABLE IF NOT EXISTS memory_index_fts USING fts5(
  entry_id UNINDEXED,
  artifact_id UNINDEXED,
  created_at UNINDEXED,
  memory_text
);

CREATE VIRTUAL TABLE IF NOT EXISTS work_trace_fts USING fts5(
  event_id UNINDEXED,
  searchable_text
);

CREATE INDEX IF NOT EXISTS idx_wtel_entry_id ON work_trace_entry_links(entry_id);
CREATE INDEX IF NOT EXISTS idx_entries_created_at ON entries(created_at DESC, entry_id DESC);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source);
CREATE INDEX IF NOT EXISTS idx_work_trace_created_at ON work_trace_events(created_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_work_trace_project ON work_trace_events(project);
CREATE INDEX IF NOT EXISTS idx_work_trace_event_type ON work_trace_events(event_type);
"""


def bootstrap_sqlite(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)

        entry_cols = {row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}
        entry_migrations = {
            "entry_type": "ALTER TABLE entries ADD COLUMN entry_type TEXT",
            "source_session_id": "ALTER TABLE entries ADD COLUMN source_session_id TEXT",
            "source_conversation_id": "ALTER TABLE entries ADD COLUMN source_conversation_id TEXT",
            "import_id": "ALTER TABLE entries ADD COLUMN import_id TEXT",
            "truthful_source": "ALTER TABLE entries ADD COLUMN truthful_source INTEGER NOT NULL DEFAULT 0",
        }
        for column, ddl in entry_migrations.items():
            if column not in entry_cols:
                conn.execute(ddl)

        memory_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_index)").fetchall()}
        if "created_at" not in memory_cols:
            # Legacy scaffold compatibility: add with a safe constant default for existing rows.
            conn.execute("ALTER TABLE memory_index ADD COLUMN created_at TEXT NOT NULL DEFAULT \"\"")
        conn.execute("UPDATE memory_index SET created_at = ? WHERE created_at IS NULL", ("",))

        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_source_session_id ON entries(source_session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_source_conversation_id ON entries(source_conversation_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_import_id ON entries(import_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_truthful_source ON entries(truthful_source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_index_entry_created ON memory_index(entry_id, created_at DESC)")

        # FTS tables are an acceleration layer only. Raw JSON files plus normal
        # SQLite rows remain the user-visible source of truth.
        conn.execute("DELETE FROM memory_index_fts")
        conn.execute(
            """
            INSERT INTO memory_index_fts(rowid, entry_id, artifact_id, created_at, memory_text)
            SELECT id, entry_id, artifact_id, created_at, memory_text
            FROM memory_index
            """
        )
        conn.execute("DELETE FROM work_trace_fts")
        conn.execute(
            """
            INSERT INTO work_trace_fts(event_id, searchable_text)
            SELECT event_id, searchable_text
            FROM work_trace_events
            """
        )
        conn.commit()
