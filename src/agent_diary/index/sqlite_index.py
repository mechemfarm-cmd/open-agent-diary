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

CREATE INDEX IF NOT EXISTS idx_entries_created_at ON entries(created_at DESC, entry_id DESC);
CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source);
CREATE INDEX IF NOT EXISTS idx_entries_source_session_id ON entries(source_session_id);
CREATE INDEX IF NOT EXISTS idx_entries_source_conversation_id ON entries(source_conversation_id);
CREATE INDEX IF NOT EXISTS idx_entries_import_id ON entries(import_id);
CREATE INDEX IF NOT EXISTS idx_entries_truthful_source ON entries(truthful_source);

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

CREATE INDEX IF NOT EXISTS idx_wtel_entry_id ON work_trace_entry_links(entry_id);
"""

GRAPH_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS graph_entities (
  entity_id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL DEFAULT 'active',
  merged_into_entity_id TEXT REFERENCES graph_entities(entity_id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT DEFAULT '{}',
  UNIQUE(normalized_name, entity_type)
);

CREATE TABLE IF NOT EXISTS graph_entity_aliases (
  alias_id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  confidence TEXT DEFAULT 'high',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gea_entity_id ON graph_entity_aliases(entity_id);
CREATE INDEX IF NOT EXISTS idx_gea_normalized ON graph_entity_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS graph_facts (
  fact_id TEXT PRIMARY KEY,
  subject_entity_id TEXT NOT NULL REFERENCES graph_entities(entity_id),
  predicate TEXT NOT NULL,
  object_kind TEXT NOT NULL,
  object_entity_id TEXT REFERENCES graph_entities(entity_id),
  object_value TEXT,
  object_value_type TEXT,
  state TEXT NOT NULL DEFAULT 'current',
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  time_precision TEXT DEFAULT 'source',
  time_basis TEXT DEFAULT 'source_timestamp',
  confidence TEXT DEFAULT 'medium',
  superseded_by_fact_id TEXT REFERENCES graph_facts(fact_id),
  retracted_by_event_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_gf_subject ON graph_facts(subject_entity_id, predicate, state);
CREATE INDEX IF NOT EXISTS idx_gf_object_entity ON graph_facts(object_entity_id) WHERE object_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_gf_superseded ON graph_facts(superseded_by_fact_id) WHERE superseded_by_fact_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS graph_fact_evidence (
  evidence_id TEXT PRIMARY KEY,
  fact_id TEXT NOT NULL REFERENCES graph_facts(fact_id),
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_timestamp TEXT NOT NULL,
  role TEXT NOT NULL,
  weight REAL DEFAULT 1.0,
  extractor_method TEXT,
  extractor_version TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gfe_fact_id ON graph_fact_evidence(fact_id);

CREATE TABLE IF NOT EXISTS graph_assertion_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  author TEXT NOT NULL,
  reason TEXT,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gae_event_type ON graph_assertion_events(event_type);

CREATE TABLE IF NOT EXISTS graph_extraction_jobs (
  job_id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_timestamp TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expiry TEXT,
  last_error TEXT,
  extractor_method TEXT,
  result_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source_kind, source_id)
);

CREATE INDEX IF NOT EXISTS idx_gej_status ON graph_extraction_jobs(status);
"""


def bootstrap_sqlite(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.executescript(GRAPH_SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_index)").fetchall()}
        if "created_at" not in cols:
            # Legacy scaffold compatibility: add with a safe constant default for existing rows.
            conn.execute("ALTER TABLE memory_index ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE memory_index SET created_at = '' WHERE created_at IS NULL")
        conn.commit()