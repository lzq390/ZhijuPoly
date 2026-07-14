ALTER TABLE md.monomer_md_jobs
  ADD COLUMN IF NOT EXISTS worker_instance_id text,
  ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_monomer_md_jobs_active_lease
ON md.monomer_md_jobs(lease_expires_at)
WHERE status IN ('pending', 'submitted', 'running');
