-- Only the dev-m1 gateway verifier and dedicated PostgreSQL cluster use this schema.
BEGIN;
CREATE SCHEMA openscience_m3;
REVOKE ALL ON SCHEMA openscience_m3 FROM PUBLIC;
GRANT USAGE ON SCHEMA openscience_m3 TO nexpoly_auth;

CREATE TABLE openscience_m3.operations (
    operation_id uuid PRIMARY KEY,
    handoff_id uuid REFERENCES openscience_m2.handoffs ON DELETE SET NULL,
    environment text NOT NULL CHECK (environment = 'dev-m1'),
    owner_id uuid NOT NULL,
    login_id uuid NOT NULL,
    workspace_user_id text NOT NULL,
    workspace_session text,
    turn_id text,
    call_id text,
    tool text NOT NULL CHECK (tool IN ('predict_polymer_properties','polymerize_monomers',
        'dft_job_submit','dft_job_status','dft_job_list','dft_job_result')),
    arguments_sha256 text NOT NULL CHECK (arguments_sha256 ~ '^[0-9a-f]{64}$'),
    job_id text,
    confirmed boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (tool <> 'dft_job_submit' OR confirmed)
);
CREATE INDEX operations_owner ON openscience_m3.operations (owner_id, created_at);

CREATE TABLE openscience_m3.delegations (
    token_hash bytea PRIMARY KEY CHECK (octet_length(token_hash) = 32),
    operation_id uuid NOT NULL REFERENCES openscience_m3.operations,
    audience text NOT NULL CHECK (audience IN ('capability-gateway','nexpoly-native')),
    expires_at timestamptz NOT NULL,
    method text,
    path text,
    query text,
    request_sha256 text,
    used_at timestamptz,
    response_status integer,
    usage_request_id uuid,
    usage_operation_id uuid,
    CHECK ((audience='capability-gateway' AND method IS NULL AND path IS NULL AND query IS NULL
            AND request_sha256 IS NULL) OR
           (audience='nexpoly-native' AND method IN ('GET','POST') AND path IS NOT NULL
            AND query IS NOT NULL AND request_sha256 IS NOT NULL AND request_sha256 ~ '^[0-9a-f]{64}$'))
);
CREATE INDEX delegations_operation ON openscience_m3.delegations (operation_id);
CREATE INDEX delegations_expiry ON openscience_m3.delegations (expires_at);

ALTER TABLE openscience_m3.operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE openscience_m3.operations FORCE ROW LEVEL SECURITY;
ALTER TABLE openscience_m3.delegations ENABLE ROW LEVEL SECURITY;
ALTER TABLE openscience_m3.delegations FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_operations ON openscience_m3.operations TO nexpoly_auth USING (true) WITH CHECK (true);
CREATE POLICY authority_delegations ON openscience_m3.delegations TO nexpoly_auth USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON openscience_m3.operations, openscience_m3.delegations TO nexpoly_auth;
GRANT UPDATE (used_at,response_status,usage_request_id,usage_operation_id) ON openscience_m3.delegations TO nexpoly_auth;
COMMIT;
