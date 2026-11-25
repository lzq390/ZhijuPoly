-- Earlier model receipts have no authenticated generation evidence.
BEGIN;
ALTER TABLE openscience_m4.model_requests ADD COLUMN workspace_generation_fingerprint text
    CHECK (workspace_generation_fingerprint ~ '^[0-9a-f]{64}$');
COMMIT;
