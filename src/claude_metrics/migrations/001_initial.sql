-- All timestamps are UTC microseconds. NULL counters mean unknown, not zero.
CREATE TABLE ingestion_runs (
    run_id TEXT PRIMARY KEY,
    started_at_us INTEGER NOT NULL,
    completed_at_us INTEGER,
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'complete', 'failed')),
    files_seen INTEGER NOT NULL DEFAULT 0 CHECK(files_seen >= 0),
    rows_added INTEGER NOT NULL DEFAULT 0 CHECK(rows_added >= 0),
    rows_revised INTEGER NOT NULL DEFAULT 0 CHECK(rows_revised >= 0),
    malformed_rows INTEGER NOT NULL DEFAULT 0 CHECK(malformed_rows >= 0)
) STRICT;

CREATE TABLE source_files (
    file_id INTEGER PRIMARY KEY,
    agent_type TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    file_identity TEXT,
    generation INTEGER NOT NULL DEFAULT 1 CHECK(generation > 0),
    size INTEGER NOT NULL DEFAULT 0 CHECK(size >= 0),
    mtime_ns INTEGER,
    byte_offset INTEGER NOT NULL DEFAULT 0 CHECK(byte_offset >= 0),
    line_number INTEGER NOT NULL DEFAULT 0 CHECK(line_number >= 0),
    prefix_fingerprint TEXT,
    parser_version TEXT NOT NULL,
    last_scan_at_us INTEGER,
    UNIQUE(agent_type, canonical_path)
) STRICT;

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    canonical_root TEXT NOT NULL,
    display_name TEXT NOT NULL,
    repository_identity TEXT,
    UNIQUE(agent_type, canonical_root)
) STRICT;

CREATE TABLE sessions (
    session_pk INTEGER PRIMARY KEY,
    agent_type TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    parent_session_pk INTEGER REFERENCES sessions(session_pk),
    lineage_evidence TEXT,
    started_at_us INTEGER,
    ended_at_us INTEGER,
    cwd TEXT,
    branch TEXT,
    worktree TEXT,
    UNIQUE(agent_type, session_id),
    CHECK(ended_at_us IS NULL OR started_at_us IS NULL OR ended_at_us >= started_at_us)
) STRICT;

CREATE TABLE turns (
    turn_pk INTEGER PRIMARY KEY,
    session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
    turn_id TEXT NOT NULL,
    prompt_id TEXT,
    started_at_us INTEGER,
    ended_at_us INTEGER,
    source_file_id INTEGER REFERENCES source_files(file_id),
    source_line INTEGER CHECK(source_line > 0),
    UNIQUE(session_pk, turn_id),
    UNIQUE(turn_pk, session_pk)
) STRICT;

CREATE TABLE requests (
    request_pk INTEGER PRIMARY KEY,
    session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
    turn_pk INTEGER,
    dedup_key TEXT NOT NULL,
    request_id TEXT,
    message_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    occurred_at_us INTEGER NOT NULL,
    model_raw TEXT NOT NULL,
    model_resolved TEXT,
    provider TEXT NOT NULL DEFAULT 'unknown',
    provider_region TEXT,
    inference_geo TEXT,
    query_source TEXT NOT NULL DEFAULT 'unknown'
        CHECK(query_source IN ('main', 'subagent', 'auxiliary', 'unknown')),
    agent_id TEXT,
    parent_agent_id TEXT,
    speed TEXT,
    service_tier TEXT,
    effort TEXT,
    billing_surface TEXT NOT NULL DEFAULT 'unknown'
        CHECK(billing_surface IN ('api', 'subscription', 'unknown')),
    input_uncached_tokens INTEGER CHECK(input_uncached_tokens >= 0),
    cache_read_tokens INTEGER CHECK(cache_read_tokens >= 0),
    cache_write_5m_tokens INTEGER CHECK(cache_write_5m_tokens >= 0),
    cache_write_1h_tokens INTEGER CHECK(cache_write_1h_tokens >= 0),
    cache_write_unknown_tokens INTEGER CHECK(cache_write_unknown_tokens >= 0),
    output_tokens INTEGER CHECK(output_tokens >= 0),
    reasoning_tokens INTEGER CHECK(reasoning_tokens >= 0),
    reported_cost_nanos INTEGER CHECK(reported_cost_nanos >= 0),
    reported_cost_original TEXT,
    reported_cost_unit TEXT,
    identity_confidence TEXT NOT NULL DEFAULT 'low' CHECK(identity_confidence IN ('high', 'low')),
    UNIQUE(session_pk, dedup_key),
    UNIQUE(request_pk, session_pk),
    FOREIGN KEY(turn_pk, session_pk) REFERENCES turns(turn_pk, session_pk),
    CHECK(reasoning_tokens IS NULL OR output_tokens IS NULL OR reasoning_tokens <= output_tokens)
) STRICT;

CREATE TABLE request_observations (
    observation_id INTEGER PRIMARY KEY,
    request_pk INTEGER NOT NULL REFERENCES requests(request_pk),
    request_revision INTEGER NOT NULL CHECK(request_revision > 0),
    source_file_id INTEGER NOT NULL REFERENCES source_files(file_id),
    source_session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
    generation INTEGER NOT NULL CHECK(generation > 0),
    source_line INTEGER NOT NULL CHECK(source_line > 0),
    byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
    parser_version TEXT NOT NULL,
    -- Only a serialized normalized Request allowlist, never a raw SourceRecord.
    normalized_candidate_json TEXT NOT NULL CHECK(json_valid(normalized_candidate_json)),
    observed_request_id TEXT,
    observed_message_id TEXT,
    finality INTEGER CHECK(finality IN (0, 1)),
    usage_rank INTEGER CHECK(usage_rank >= 0),
    superseded INTEGER NOT NULL DEFAULT 0 CHECK(superseded IN (0, 1)),
    source_available INTEGER NOT NULL DEFAULT 1 CHECK(source_available IN (0, 1)),
    is_current_winner INTEGER NOT NULL DEFAULT 0 CHECK(is_current_winner IN (0, 1)),
    UNIQUE(source_file_id, generation, byte_offset, parser_version),
    UNIQUE(observation_id, request_pk)
) STRICT;

CREATE TABLE tool_calls (
    tool_pk INTEGER PRIMARY KEY,
    session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
    tool_call_id TEXT NOT NULL,
    request_pk INTEGER,
    turn_pk INTEGER,
    tool_name TEXT NOT NULL,
    success INTEGER CHECK(success IN (0, 1)),
    duration_ms INTEGER CHECK(duration_ms >= 0),
    source_file_id INTEGER REFERENCES source_files(file_id),
    source_line INTEGER CHECK(source_line > 0),
    UNIQUE(session_pk, tool_call_id),
    FOREIGN KEY(request_pk, session_pk) REFERENCES requests(request_pk, session_pk),
    FOREIGN KEY(turn_pk, session_pk) REFERENCES turns(turn_pk, session_pk)
) STRICT;

CREATE TABLE billable_units (
    unit_id INTEGER PRIMARY KEY,
    request_pk INTEGER NOT NULL REFERENCES requests(request_pk),
    observation_id INTEGER NOT NULL,
    unit_type TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    UNIQUE(observation_id, unit_type),
    FOREIGN KEY(observation_id, request_pk) REFERENCES request_observations(observation_id, request_pk)
) STRICT;

CREATE TABLE price_catalog_versions (
    version TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    retrieved_at TEXT NOT NULL,
    effective_from_us INTEGER,
    effective_to_us INTEGER
) STRICT;

CREATE TABLE cost_receipts (
    receipt_id INTEGER PRIMARY KEY,
    request_pk INTEGER NOT NULL REFERENCES requests(request_pk),
    request_revision INTEGER NOT NULL CHECK(request_revision > 0),
    observation_id INTEGER NOT NULL,
    catalog_version TEXT NOT NULL REFERENCES price_catalog_versions(version),
    formula_version TEXT NOT NULL,
    price_key TEXT,
    resolution_method TEXT NOT NULL,
    rates_json TEXT NOT NULL CHECK(json_valid(rates_json)),
    rules_json TEXT NOT NULL CHECK(json_valid(rules_json)),
    calculated_cost_nanos INTEGER CHECK(calculated_cost_nanos >= 0),
    reported_cost_nanos INTEGER CHECK(reported_cost_nanos >= 0),
    effective_cost_nanos INTEGER CHECK(effective_cost_nanos >= 0),
    effective_source TEXT CHECK(effective_source IN ('reported', 'calculated')),
    status TEXT NOT NULL CHECK(status IN ('COMPLETE', 'PARTIAL', 'UNPRICED')),
    estimates_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(estimates_json)),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0, 1)),
    UNIQUE(request_pk, request_revision, catalog_version, formula_version),
    FOREIGN KEY(observation_id, request_pk) REFERENCES request_observations(observation_id, request_pk),
    CHECK(status != 'UNPRICED' OR effective_cost_nanos IS NULL),
    CHECK(status != 'COMPLETE' OR effective_cost_nanos IS NOT NULL),
    CHECK((effective_cost_nanos IS NULL) = (effective_source IS NULL))
) STRICT;

CREATE INDEX idx_requests_time ON requests(occurred_at_us);
CREATE INDEX idx_requests_session_time ON requests(session_pk, occurred_at_us);
CREATE INDEX idx_requests_model_time ON requests(model_raw, occurred_at_us);
CREATE INDEX idx_sessions_project ON sessions(project_id);
CREATE INDEX idx_observations_request ON request_observations(request_pk);
CREATE UNIQUE INDEX idx_observation_winner ON request_observations(request_pk)
    WHERE is_current_winner = 1;
CREATE UNIQUE INDEX idx_current_receipt ON cost_receipts(request_pk) WHERE is_current = 1;
