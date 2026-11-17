-- Durable single-target retrosynthesis; explicitly applied, never at API startup.
CREATE SCHEMA retrosynthesis;
REVOKE ALL ON SCHEMA retrosynthesis FROM PUBLIC;
GRANT USAGE ON SCHEMA retrosynthesis TO nexpoly_api,nexpoly_service,nexpoly_mutable_audit;
CREATE TABLE retrosynthesis.jobs (
 job_id uuid PRIMARY KEY,
 owner_user_id uuid NOT NULL REFERENCES auth.users(user_id),
 idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 128),
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
 request_json jsonb NOT NULL CHECK(jsonb_typeof(request_json)='object'),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','queued','running','completed','failed','timed_out','interrupted','unknown')),
 stage text NOT NULL DEFAULT 'pending',
 executor_id text,
 claim_token uuid NOT NULL,
 start_authorized_at timestamptz,
 result_json jsonb CHECK(result_json IS NULL OR jsonb_typeof(result_json)='object'),
 error_code text,
 storage_bytes bigint NOT NULL CHECK(storage_bytes BETWEEN 1 AND 1048576),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz,
 finished_at timestamptz,
 UNIQUE(owner_user_id,idempotency_key),
 CHECK(status NOT IN ('completed','failed','timed_out','interrupted') OR finished_at IS NOT NULL),
 CHECK(status <> 'completed' OR result_json IS NOT NULL)
);
CREATE INDEX retrosynthesis_jobs_owner_created ON retrosynthesis.jobs(owner_user_id,created_at DESC,job_id);
CREATE INDEX retrosynthesis_jobs_live ON retrosynthesis.jobs(status) WHERE status IN ('pending','queued','running','unknown');
ALTER TABLE retrosynthesis.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrosynthesis.jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON retrosynthesis.jobs TO nexpoly_api
 USING(owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid)
 WITH CHECK(owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY service_access ON retrosynthesis.jobs TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON retrosynthesis.jobs FOR SELECT TO nexpoly_mutable_audit USING(true);
GRANT SELECT ON retrosynthesis.jobs TO nexpoly_api;
GRANT SELECT,INSERT,UPDATE ON retrosynthesis.jobs TO nexpoly_service;
GRANT SELECT ON retrosynthesis.jobs TO nexpoly_mutable_audit;
