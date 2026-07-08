ALTER TABLE md.monomer_md_jobs
  ADD COLUMN IF NOT EXISTS protocol text NOT NULL DEFAULT 'DensityDemo',
  ADD COLUMN IF NOT EXISTS run_mode text NOT NULL DEFAULT 'demo',
  ADD COLUMN IF NOT EXISTS config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS components jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS artifact_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS byteff2_git_sha text,
  ADD COLUMN IF NOT EXISTS gpu_device text,
  ADD COLUMN IF NOT EXISTS error_category text,
  ADD COLUMN IF NOT EXISTS artifact_deleted_at timestamptz,
  ADD COLUMN IF NOT EXISTS artifact_delete_message text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'monomer_md_jobs_protocol_check'
  ) THEN
    ALTER TABLE md.monomer_md_jobs
      ADD CONSTRAINT monomer_md_jobs_protocol_check
      CHECK (protocol IN ('DensityDemo', 'Density', 'Transport', 'HVap', 'Dielectric', 'Compressibility'));
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'monomer_md_jobs_run_mode_check'
  ) THEN
    ALTER TABLE md.monomer_md_jobs
      ADD CONSTRAINT monomer_md_jobs_run_mode_check
      CHECK (run_mode IN ('demo', 'formal'));
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_monomer_md_jobs_protocol
ON md.monomer_md_jobs(protocol);

CREATE INDEX IF NOT EXISTS idx_monomer_md_jobs_run_mode
ON md.monomer_md_jobs(run_mode);
