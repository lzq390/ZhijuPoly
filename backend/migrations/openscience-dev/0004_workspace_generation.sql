-- Preserve historical audit rows, but give those pre-generation turns a deadline in 1970.
BEGIN;
ALTER TABLE openscience_m3.operations
    ADD COLUMN workspace_generation text NOT NULL DEFAULT repeat('0',64)
        CHECK (workspace_generation ~ '^[0-9a-f]{64}$'),
    ADD COLUMN turn_expires_ms bigint NOT NULL DEFAULT 1 CHECK (turn_expires_ms > 0);
ALTER TABLE openscience_m3.operations
    ALTER COLUMN workspace_generation DROP DEFAULT,
    ALTER COLUMN turn_expires_ms DROP DEFAULT;
COMMIT;
