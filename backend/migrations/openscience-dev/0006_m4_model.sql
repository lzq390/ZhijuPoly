-- Token accounting for the local OpenScience model gateway in dev-m1 only.
BEGIN;
CREATE SCHEMA IF NOT EXISTS openscience_m4;
REVOKE ALL ON SCHEMA openscience_m4 FROM PUBLIC;
GRANT USAGE ON SCHEMA openscience_m4 TO nexpoly_auth;

CREATE TABLE openscience_m4.model_requests (
    request_id uuid PRIMARY KEY,
    environment text NOT NULL CHECK (environment = 'dev-m1'),
    owner_id uuid NOT NULL,
    login_id uuid NOT NULL,
    workspace_user_id text NOT NULL,
    workspace_session text NOT NULL,
    turn_id text NOT NULL,
    model text NOT NULL,
    protocol text NOT NULL CHECK (protocol IN ('chat.completions','responses')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    budget_date date NOT NULL DEFAULT (timezone('UTC', CURRENT_TIMESTAMP)::date),
    reserved_input integer NOT NULL CHECK (reserved_input > 0),
    max_output integer NOT NULL CHECK (max_output > 0),
    reserved_tokens integer GENERATED ALWAYS AS (reserved_input + max_output) STORED,
    state text NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved','actual','estimated','released')),
    input_tokens integer,
    output_tokens integer,
    estimated_tokens integer,
    reason text,
    settled_at timestamptz,
    CHECK (
        (state='reserved' AND settled_at IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND estimated_tokens IS NULL)
        OR (state='released' AND settled_at IS NOT NULL AND input_tokens IS NULL AND output_tokens IS NULL AND estimated_tokens IS NULL)
        OR (state='actual' AND settled_at IS NOT NULL AND input_tokens IS NOT NULL AND input_tokens=reserved_input
            AND output_tokens IS NOT NULL AND output_tokens BETWEEN 0 AND max_output AND estimated_tokens IS NULL)
        OR (state='estimated' AND settled_at IS NOT NULL AND input_tokens IS NULL AND output_tokens IS NULL
            AND estimated_tokens IS NOT NULL AND estimated_tokens=reserved_tokens)
    )
);
CREATE INDEX model_budget_owner_day ON openscience_m4.model_requests(owner_id,budget_date);
CREATE INDEX model_active ON openscience_m4.model_requests(created_at) WHERE state='reserved';
ALTER TABLE openscience_m4.model_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE openscience_m4.model_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY dev_authority ON openscience_m4.model_requests TO nexpoly_auth USING(true) WITH CHECK(true);
GRANT SELECT, INSERT ON openscience_m4.model_requests TO nexpoly_auth;
GRANT UPDATE(state,input_tokens,output_tokens,estimated_tokens,reason,settled_at) ON openscience_m4.model_requests TO nexpoly_auth;
COMMIT;
