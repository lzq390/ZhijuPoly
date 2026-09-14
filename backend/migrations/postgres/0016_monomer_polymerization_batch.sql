CREATE SCHEMA IF NOT EXISTS polymerization_batch;

CREATE TABLE polymerization_batch.imports (
  id text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
  files jsonb NOT NULL,
  preview_revision text,
  preview jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);

CREATE TABLE polymerization_batch.jobs (
  id text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{32}$'),
  import_id text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  request_hash text NOT NULL,
  options jsonb NOT NULL,
  engine jsonb NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN (
    'queued','running','cancelling','completed','completed_with_errors','failed','cancelled','expired')),
  stage text NOT NULL DEFAULT 'queued',
  summary jsonb NOT NULL DEFAULT '{}',
  artifacts jsonb NOT NULL DEFAULT '{}',
  terminal_intent text CHECK (terminal_intent IN ('failed','cancelled')),
  error_code text,
  message text,
  execution_token text,
  execution_started_at timestamptz,
  worker_id text,
  heartbeat_at timestamptz,
  consumed_seconds double precision NOT NULL DEFAULT 0 CHECK (consumed_seconds >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  expires_at timestamptz
);
CREATE INDEX polymerization_batch_jobs_queue ON polymerization_batch.jobs (created_at)
  WHERE status IN ('queued','running','cancelling');
CREATE INDEX polymerization_batch_jobs_expiry ON polymerization_batch.jobs (expires_at)
  WHERE status <> 'expired';

CREATE TABLE polymerization_batch.chunks (
  job_id text NOT NULL REFERENCES polymerization_batch.jobs(id) ON DELETE CASCADE,
  chunk_id text NOT NULL,
  phase integer NOT NULL,
  ordinal integer NOT NULL,
  kind text NOT NULL CHECK (kind IN ('classify','generate','export')),
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','skipped')),
  artifact jsonb,
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  attempt_token text,
  lease_expires_at timestamptz,
  counts jsonb NOT NULL DEFAULT '{}',
  completed_at timestamptz,
  PRIMARY KEY (job_id, chunk_id)
);
CREATE INDEX polymerization_batch_chunks_pending ON polymerization_batch.chunks (job_id, phase, ordinal)
  WHERE status = 'pending';

CREATE TABLE polymerization_batch.worker_status (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  worker_id text NOT NULL,
  available boolean NOT NULL,
  message text NOT NULL,
  engine jsonb NOT NULL DEFAULT '{}',
  heartbeat_at timestamptz NOT NULL DEFAULT now()
);

-- Preserve the existing read-only deployment audit boundary when provisioned.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexpoly_mutable_audit') THEN
    GRANT USAGE ON SCHEMA polymerization_batch TO nexpoly_mutable_audit;
    GRANT SELECT ON ALL TABLES IN SCHEMA polymerization_batch TO nexpoly_mutable_audit;
    ALTER DEFAULT PRIVILEGES IN SCHEMA polymerization_batch
      GRANT SELECT ON TABLES TO nexpoly_mutable_audit;
    ALTER DEFAULT PRIVILEGES IN SCHEMA polymerization_batch
      GRANT SELECT ON SEQUENCES TO nexpoly_mutable_audit;
  END IF;
END $$;
