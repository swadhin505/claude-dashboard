ALTER TABLE source_files ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(context_json));
ALTER TABLE source_files ADD COLUMN fingerprint_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_files ADD COLUMN tail_fingerprint TEXT;
CREATE TABLE ingestion_diagnostics (
    diagnostic_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
    source_path TEXT NOT NULL,
    source_line INTEGER,
    code TEXT NOT NULL
) STRICT;
CREATE TABLE replay_aliases (
    source_session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
    identity_key TEXT NOT NULL,
    owner_session_pk INTEGER NOT NULL REFERENCES sessions(session_pk),
    PRIMARY KEY(source_session_pk, identity_key)
) STRICT;
CREATE INDEX idx_requests_message ON requests(session_pk, message_id);
CREATE INDEX idx_requests_request ON requests(session_pk, request_id);
CREATE INDEX idx_observations_source ON request_observations(source_file_id, generation);
CREATE INDEX idx_diagnostics_run ON ingestion_diagnostics(run_id);
