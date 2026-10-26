-- Development-only polymer MD execution. Apply explicitly after exact 0019.
CREATE SCHEMA polymer_md;
REVOKE ALL ON SCHEMA polymer_md FROM PUBLIC;
GRANT USAGE ON SCHEMA polymer_md TO nexpoly_api,nexpoly_service,nexpoly_mutable_audit;

CREATE TABLE polymer_md.jobs (
 job_id uuid PRIMARY KEY,
 owner_user_id uuid NOT NULL REFERENCES auth.users(user_id),
 idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 128),
 input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[0-9a-f]{64}$'),
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
 scientific_request jsonb NOT NULL CHECK(jsonb_typeof(scientific_request)='object'),
 enqueue_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
 attempt_token text NOT NULL UNIQUE CHECK(attempt_token ~ '^[0-9a-f]{64}$'),
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','queued','running','completed','failed','cancel_requested','cancelled')),
 quality_status text NOT NULL DEFAULT 'not_evaluated' CHECK(quality_status IN ('not_evaluated','passed','warning','not_converged','failed')),
 stage text NOT NULL DEFAULT 'queued',
 results jsonb NOT NULL DEFAULT '{}',
 worker_instance_id text,
 snapshot_revision bigint NOT NULL DEFAULT 0 CHECK(snapshot_revision >= 0),
 dispatch_started_at timestamptz,
 start_authorized_at timestamptz,
 cancel_requested_at timestamptz,
 cleanup_confirmed boolean NOT NULL DEFAULT false,
 artifact_state text NOT NULL DEFAULT 'available' CHECK(artifact_state IN ('available','delete_requested','deleted')),
 delete_job_requested boolean NOT NULL DEFAULT false,
 error_code text,
 error_message text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz,
 finished_at timestamptz,
 UNIQUE(owner_user_id,idempotency_key),
 CHECK(status NOT IN ('completed','failed','cancelled') OR cleanup_confirmed)
);
CREATE UNIQUE INDEX polymer_md_one_active_owner ON polymer_md.jobs(owner_user_id)
 WHERE status IN ('pending','queued','running','cancel_requested');
CREATE INDEX polymer_md_active_fifo ON polymer_md.jobs(enqueue_sequence)
 WHERE status IN ('pending','queued','running','cancel_requested');

CREATE TABLE polymer_md.job_attempts (
 job_id uuid NOT NULL REFERENCES polymer_md.jobs(job_id) ON DELETE CASCADE,
 attempt integer NOT NULL CHECK(attempt > 0),
 attempt_token text NOT NULL UNIQUE CHECK(attempt_token ~ '^[0-9a-f]{64}$'),
 request_sha256 text NOT NULL,
 worker_instance_id text,
 start_authorized_at timestamptz,
 status text NOT NULL DEFAULT 'pending',
 snapshot jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(job_id,attempt),
 UNIQUE(job_id,attempt_token)
);
CREATE TABLE polymer_md.stage_runs (
 job_id uuid NOT NULL REFERENCES polymer_md.jobs(job_id) ON DELETE CASCADE,
 stage_run_id text NOT NULL CHECK(stage_run_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
 attempt_token text NOT NULL,
 stage text NOT NULL,
 attempt integer NOT NULL CHECK(attempt > 0),
 status text NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled')),
 quality_status text NOT NULL CHECK(quality_status IN ('not_evaluated','passed','warning','not_converged','failed')),
 results jsonb NOT NULL DEFAULT '{}',
 quality jsonb NOT NULL DEFAULT '{}',
 errors jsonb NOT NULL DEFAULT '[]',
 started_at timestamptz,
 finished_at timestamptz,
 PRIMARY KEY(job_id,stage_run_id),
 FOREIGN KEY(job_id,attempt_token) REFERENCES polymer_md.job_attempts(job_id,attempt_token)
);
CREATE TABLE polymer_md.artifacts (
 job_id uuid NOT NULL REFERENCES polymer_md.jobs(job_id) ON DELETE CASCADE,
 artifact_id text NOT NULL CHECK(artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
 stage_run_id text NOT NULL,
 attempt_token text NOT NULL,
 kind text NOT NULL,
 filename text NOT NULL CHECK(filename ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$'),
 media_type text NOT NULL,
 size_bytes bigint NOT NULL CHECK(size_bytes BETWEEN 0 AND 21474836480),
 sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 storage_ref text CHECK(storage_ref IS NULL OR (length(storage_ref) BETWEEN 1 AND 1024
   AND storage_ref ~ '^[A-Za-z0-9][A-Za-z0-9._/-]*$'
   AND storage_ref !~ '(^|/)\.\.?(/|$)'
   AND storage_ref LIKE job_id::text || '/' || attempt_token || '/%')),
 deleted_at timestamptz,
 PRIMARY KEY(job_id,artifact_id),
 FOREIGN KEY(job_id,stage_run_id) REFERENCES polymer_md.stage_runs(job_id,stage_run_id) ON DELETE CASCADE,
 FOREIGN KEY(job_id,attempt_token) REFERENCES polymer_md.job_attempts(job_id,attempt_token)
);

GRANT SELECT ON ALL TABLES IN SCHEMA polymer_md TO nexpoly_api,nexpoly_mutable_audit;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA polymer_md TO nexpoly_service;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA polymer_md TO nexpoly_service;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA polymer_md TO nexpoly_mutable_audit;

ALTER TABLE polymer_md.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE polymer_md.jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE polymer_md.job_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE polymer_md.job_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE polymer_md.stage_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE polymer_md.stage_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE polymer_md.artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE polymer_md.artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON polymer_md.jobs FOR ALL TO nexpoly_api
 USING(owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid)
 WITH CHECK(owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY owner_access ON polymer_md.job_attempts FOR ALL TO nexpoly_api
 USING(EXISTS(SELECT FROM polymer_md.jobs p WHERE p.job_id=job_attempts.job_id AND p.owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid));
CREATE POLICY owner_access ON polymer_md.stage_runs FOR ALL TO nexpoly_api
 USING(EXISTS(SELECT FROM polymer_md.jobs p WHERE p.job_id=stage_runs.job_id AND p.owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid));
CREATE POLICY owner_access ON polymer_md.artifacts FOR ALL TO nexpoly_api
 USING(EXISTS(SELECT FROM polymer_md.jobs p WHERE p.job_id=artifacts.job_id AND p.owner_user_id=NULLIF(current_setting('app.user_id',true),'')::uuid));
CREATE POLICY service_access ON polymer_md.jobs FOR ALL TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY service_access ON polymer_md.job_attempts FOR ALL TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY service_access ON polymer_md.stage_runs FOR ALL TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY service_access ON polymer_md.artifacts FOR ALL TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON polymer_md.jobs FOR SELECT TO nexpoly_mutable_audit USING(true);
CREATE POLICY audit_access ON polymer_md.job_attempts FOR SELECT TO nexpoly_mutable_audit USING(true);
CREATE POLICY audit_access ON polymer_md.stage_runs FOR SELECT TO nexpoly_mutable_audit USING(true);
CREATE POLICY audit_access ON polymer_md.artifacts FOR SELECT TO nexpoly_mutable_audit USING(true);
