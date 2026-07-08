CREATE SCHEMA IF NOT EXISTS generation;

CREATE TABLE IF NOT EXISTS generation.polytao_jobs (
  job_id text PRIMARY KEY,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'submitted', 'running', 'completed', 'failed', 'cancelled')),
  input_smiles text,
  canonical_smiles text,
  descriptor_prompt text NOT NULL,
  descriptors jsonb NOT NULL DEFAULT '{}'::jsonb,
  request_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  requested_count integer NOT NULL DEFAULT 10 CHECK (requested_count > 0),
  returned_count integer NOT NULL DEFAULT 0 CHECK (returned_count >= 0),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  progress_percent integer NOT NULL DEFAULT 0 CHECK (progress_percent >= 0 AND progress_percent <= 100),
  progress_stage text NOT NULL DEFAULT 'pending',
  progress_message text NOT NULL DEFAULT 'Waiting for the PolyTAO worker to start.',
  worker_id text,
  worker_job_id text,
  worker_version text,
  engine text NOT NULL DEFAULT 'polytao-worker',
  result_data jsonb,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_polytao_jobs_status
ON generation.polytao_jobs(status);

CREATE INDEX IF NOT EXISTS idx_polytao_jobs_created_at
ON generation.polytao_jobs(created_at DESC);
