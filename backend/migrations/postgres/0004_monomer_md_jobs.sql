CREATE SCHEMA IF NOT EXISTS md;

CREATE TABLE IF NOT EXISTS md.monomer_md_jobs (
  job_id text PRIMARY KEY,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'submitted', 'running', 'completed', 'failed', 'cancelled')),
  input_smiles text NOT NULL,
  canonical_smiles text NOT NULL,
  requested_steps integer NOT NULL DEFAULT 1000 CHECK (requested_steps > 0),
  completed_steps integer NOT NULL DEFAULT 0 CHECK (completed_steps >= 0),
  progress_percent integer NOT NULL DEFAULT 0 CHECK (progress_percent >= 0 AND progress_percent <= 100),
  progress_stage text NOT NULL DEFAULT 'pending',
  progress_message text NOT NULL DEFAULT 'Waiting for the monomer MD worker to start.',
  worker_id text,
  worker_job_id text,
  worker_version text,
  engine text NOT NULL DEFAULT 'byteff2-density-demo-worker',
  artifact_root text,
  artifacts jsonb NOT NULL DEFAULT '{}'::jsonb,
  result_data jsonb,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_monomer_md_jobs_status
ON md.monomer_md_jobs(status);

CREATE INDEX IF NOT EXISTS idx_monomer_md_jobs_canonical_smiles
ON md.monomer_md_jobs(canonical_smiles);

CREATE INDEX IF NOT EXISTS idx_monomer_md_jobs_created_at
ON md.monomer_md_jobs(created_at DESC);
