-- Windows paths are case-insensitive. Re-key Claude projects using the same
-- cross-platform canonicalization as the parser, preserving every session.
CREATE TEMP TABLE project_identity_migration (
    old_id TEXT PRIMARY KEY,
    new_id TEXT NOT NULL,
    canonical_root TEXT NOT NULL
) STRICT;

INSERT INTO project_identity_migration(old_id, new_id, canonical_root)
SELECT project_id,
       sha256_hex('claude:' || canonical_project_root(canonical_root)),
       canonical_project_root(canonical_root)
FROM projects
WHERE agent_type = 'claude';

INSERT OR IGNORE INTO projects(
    project_id, agent_type, canonical_root, display_name, repository_identity
)
SELECT m.new_id, 'claude', m.canonical_root, min(p.display_name), max(p.repository_identity)
FROM project_identity_migration AS m
JOIN projects AS p ON p.project_id = m.old_id
GROUP BY m.new_id, m.canonical_root;

UPDATE sessions
SET project_id = (
    SELECT m.new_id
    FROM project_identity_migration AS m
    WHERE m.old_id = sessions.project_id
)
WHERE project_id IN (SELECT old_id FROM project_identity_migration);

DELETE FROM projects
WHERE project_id IN (
    SELECT old_id
    FROM project_identity_migration
    WHERE old_id != new_id
);

DROP TABLE project_identity_migration;
