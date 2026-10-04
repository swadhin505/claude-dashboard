-- Preserve archived request identities when late replay evidence arrives after pricing.
ALTER TABLE requests ADD COLUMN superseded_by INTEGER REFERENCES requests(request_pk);
ALTER TABLE cost_receipts RENAME TO cost_receipts_v2;
CREATE TABLE cost_receipts (
    receipt_id INTEGER PRIMARY KEY,
    request_pk INTEGER NOT NULL REFERENCES requests(request_pk),
    request_revision INTEGER NOT NULL CHECK(request_revision > 0),
    -- Ownership can be reconciled later; the frozen snapshot preserves the original evidence.
    observation_id INTEGER NOT NULL REFERENCES request_observations(observation_id),
    catalog_version TEXT NOT NULL REFERENCES price_catalog_versions(version),
    formula_version TEXT NOT NULL,
    policy_key TEXT NOT NULL DEFAULT 'legacy',
    price_key TEXT,
    resolution_method TEXT NOT NULL,
    rates_json TEXT NOT NULL CHECK(json_valid(rates_json)),
    rules_json TEXT NOT NULL CHECK(json_valid(rules_json)),
    calculated_cost_nanos INTEGER CHECK(calculated_cost_nanos >= 0),
    reported_cost_nanos INTEGER CHECK(reported_cost_nanos >= 0),
    effective_cost_nanos INTEGER CHECK(effective_cost_nanos >= 0),
    effective_source TEXT CHECK(effective_source IN ('reported','calculated')),
    status TEXT NOT NULL CHECK(status IN ('COMPLETE','PARTIAL','UNPRICED')),
    estimates_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(estimates_json)),
    snapshot_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(snapshot_json)),
    is_current INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0,1)),
    UNIQUE(request_pk,request_revision,catalog_version,formula_version,policy_key),
    CHECK(status != 'UNPRICED' OR effective_cost_nanos IS NULL),
    CHECK(status != 'COMPLETE' OR effective_cost_nanos IS NOT NULL),
    CHECK((effective_cost_nanos IS NULL) = (effective_source IS NULL))
) STRICT;
INSERT INTO cost_receipts
    (receipt_id,request_pk,request_revision,observation_id,catalog_version,formula_version,
     price_key,resolution_method,rates_json,rules_json,calculated_cost_nanos,reported_cost_nanos,
     effective_cost_nanos,effective_source,status,estimates_json,is_current)
SELECT receipt_id,request_pk,request_revision,observation_id,catalog_version,formula_version,
     price_key,resolution_method,rates_json,rules_json,calculated_cost_nanos,reported_cost_nanos,
     effective_cost_nanos,effective_source,status,estimates_json,is_current
FROM cost_receipts_v2;
DROP TABLE cost_receipts_v2;
CREATE UNIQUE INDEX idx_current_receipt ON cost_receipts(request_pk) WHERE is_current=1;
CREATE INDEX idx_receipts_status ON cost_receipts(status) WHERE is_current=1;
CREATE TRIGGER immutable_receipt BEFORE UPDATE OF
    receipt_id,request_pk,request_revision,observation_id,catalog_version,formula_version,policy_key,
    price_key,resolution_method,rates_json,rules_json,calculated_cost_nanos,reported_cost_nanos,
    effective_cost_nanos,effective_source,status,estimates_json,snapshot_json
ON cost_receipts BEGIN
    SELECT RAISE(ABORT, 'receipt payload is immutable');
END;
CREATE TRIGGER retain_receipt BEFORE DELETE ON cost_receipts BEGIN
    SELECT RAISE(ABORT, 'historical receipts must be retained');
END;
