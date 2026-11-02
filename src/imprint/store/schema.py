"""SQLite schema for immutable events and bitemporal entity versions."""

SCHEMA_SQL = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  system_time TEXT NOT NULL,
  valid_time TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  prior_event_id TEXT,
  provenance_status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
  node_id TEXT PRIMARY KEY,
  node_type TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  created_event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS node_versions (
  version_id TEXT PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES nodes(node_id),
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  provenance_status TEXT NOT NULL,
  authority_tier TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  system_from TEXT NOT NULL,
  system_to TEXT,
  event_id TEXT NOT NULL REFERENCES events(event_id),
  prior_version_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_node_version
  ON node_versions(node_id) WHERE system_to IS NULL;
CREATE INDEX IF NOT EXISTS node_versions_by_node
  ON node_versions(node_id);
CREATE TABLE IF NOT EXISTS edges (
  edge_id TEXT PRIMARY KEY,
  edge_type TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES nodes(node_id),
  target_id TEXT NOT NULL REFERENCES nodes(node_id),
  operator_id TEXT NOT NULL,
  created_event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS edges_by_source
  ON edges(source_id);
CREATE INDEX IF NOT EXISTS edges_by_target
  ON edges(target_id);
CREATE TABLE IF NOT EXISTS edge_versions (
  version_id TEXT PRIMARY KEY,
  edge_id TEXT NOT NULL REFERENCES edges(edge_id),
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  provenance_status TEXT NOT NULL,
  authority_tier TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  system_from TEXT NOT NULL,
  system_to TEXT,
  event_id TEXT NOT NULL REFERENCES events(event_id),
  prior_version_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_edge_version
  ON edge_versions(edge_id) WHERE system_to IS NULL;
CREATE INDEX IF NOT EXISTS edge_versions_by_edge
  ON edge_versions(edge_id);
CREATE TABLE IF NOT EXISTS source_receipts (
  source_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  locator TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS ingest_rulings (
  ruling_id TEXT PRIMARY KEY,
  item_id TEXT NOT NULL,
  verdict TEXT NOT NULL,
  why TEXT,
  event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS ingest_items (
  item_id TEXT PRIMARY KEY,
  operator_id TEXT NOT NULL,
  session_id TEXT,
  node_id TEXT,
  source_id TEXT NOT NULL UNIQUE,
  source_kind TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  discovered_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('unruled','kept','killed')),
  kept_node_id TEXT,
  UNIQUE(source_kind, source_locator, source_sha256)
);
CREATE TABLE IF NOT EXISTS migrations (
  migration_id TEXT PRIMARY KEY,
  from_version TEXT NOT NULL,
  to_version TEXT NOT NULL,
  code_sha256 TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  backup_receipt TEXT NOT NULL,
  result_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumed_inputs (
  input_event_id TEXT PRIMARY KEY,
  payload_sha256 TEXT NOT NULL,
  consumed_at TEXT NOT NULL,
  source_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_state (
  projection TEXT PRIMARY KEY,
  snapshot_sha256 TEXT NOT NULL,
  generator_version TEXT NOT NULL,
  generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purge_receipts (
  operation_id TEXT PRIMARY KEY,
  purged_at TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  scope_class TEXT NOT NULL,
  counts_json TEXT NOT NULL
);
-- No foreign keys: a key on nodes or events would fail an older build's purge
-- after a rollback. CREATE TABLE IF NOT EXISTS cannot change a CHECK later.
CREATE TABLE IF NOT EXISTS record_scope (
  node_id TEXT PRIMARY KEY,
  scope_kind TEXT NOT NULL CHECK(scope_kind IN ('client','central','system','unresolved','held')),
  client_id TEXT,
  project_id TEXT,
  delivery TEXT CHECK(delivery IN ('general','specific')),
  delivery_rank INTEGER,
  speaker TEXT,
  endorser TEXT,
  overrides_json TEXT NOT NULL DEFAULT '[]',
  resolved_by TEXT NOT NULL,
  event_id TEXT NOT NULL,
  stamped_at TEXT NOT NULL,
  CHECK((scope_kind = 'client') = (client_id IS NOT NULL)),
  CHECK(delivery IS NULL OR scope_kind IN ('client','central')
        OR (scope_kind = 'system' AND delivery = 'specific'))
);
"""
