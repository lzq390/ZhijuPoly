-- A worker-busy reservation may be re-admitted under its original request ID.
-- Existing rows have no signed input binding and remain ineligible for replay.
BEGIN;
ALTER TABLE openscience_m4.model_requests ADD COLUMN request_tag bytea
    CHECK (octet_length(request_tag) = 32);
GRANT UPDATE (created_at, budget_date) ON openscience_m4.model_requests TO nexpoly_auth;
COMMIT;
