-- The migration ledger is the only idempotency authority.  Deliberately avoid
-- IF NOT EXISTS here: an unmanaged or partially-created monomer_dft schema
-- must fail closed instead of being adopted silently.
CREATE SCHEMA monomer_dft;

CREATE TABLE monomer_dft.jobs (
  job_id uuid PRIMARY KEY,
  enqueue_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  idempotency_key text NOT NULL UNIQUE
    CHECK (
      length(idempotency_key) BETWEEN 8 AND 128
      AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
    ),
  request_sha256 text NOT NULL
    CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  request_json jsonb NOT NULL,
  request_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  calculation_type text NOT NULL
    CHECK (calculation_type IN ('single_point', 'optimization')),
  model_name text NOT NULL
    CHECK (model_name IN (
      'aimnet2', 'aimnet2-2025', 'aimnet2-b973c',
      'aimnet2-nse', 'aimnet2-pd', 'aimnet2-rxn'
    )),
  input_smiles text NOT NULL,
  canonical_smiles text,
  effective_charge smallint
    CHECK (effective_charge IS NULL OR effective_charge BETWEEN -5 AND 5),
  multiplicity smallint NOT NULL
    CHECK (multiplicity BETWEEN 1 AND 7),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending', 'queued', 'running', 'cancel_requested',
      'completed', 'failed', 'cancelled'
    )),
  current_attempt integer NOT NULL DEFAULT 1
    CHECK (current_attempt >= 1),
  attempt_token text NOT NULL
    CHECK (attempt_token ~ '^[0-9a-f]{64}$'),
  worker_job_id text,
  worker_id text,
  worker_instance_id text,
  queue_position integer CHECK (queue_position IS NULL OR queue_position >= 1),
  stage text NOT NULL DEFAULT 'queued'
    CHECK (stage IN (
      'queued', 'validating', 'conformer', 'single_point',
      'optimization', 'hessian', 'frequency', 'artifacts'
    )),
  progress_percent double precision NOT NULL DEFAULT 0
    CHECK (progress_percent >= 0 AND progress_percent <= 100),
  scientific_status text,
  result_json jsonb,
  timings jsonb NOT NULL DEFAULT '{}'::jsonb,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_code text,
  error_message text,
  error_retryable boolean NOT NULL DEFAULT false,
  error_details jsonb NOT NULL DEFAULT '{}'::jsonb,
  artifacts_delete_requested_at timestamptz,
  artifacts_deleted_at timestamptz,
  cancel_requested_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  submitted_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  last_reconciled_at timestamptz
);

CREATE TABLE monomer_dft.job_attempts (
  job_id uuid NOT NULL REFERENCES monomer_dft.jobs(job_id) ON DELETE CASCADE,
  attempt integer NOT NULL CHECK (attempt >= 1),
  attempt_token text NOT NULL UNIQUE
    CHECK (attempt_token ~ '^[0-9a-f]{64}$'),
  request_sha256 text NOT NULL
    CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending', 'queued', 'running', 'cancel_requested',
      'completed', 'failed', 'cancelled'
    )),
  worker_job_id text,
  worker_id text,
  worker_instance_id text,
  heartbeat_at timestamptz,
  lease_expires_at timestamptz,
  outcome jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_code text,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  submitted_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  PRIMARY KEY (job_id, attempt)
);

CREATE TABLE monomer_dft.artifacts (
  job_id uuid NOT NULL REFERENCES monomer_dft.jobs(job_id) ON DELETE CASCADE,
  artifact_id text NOT NULL
    CHECK (artifact_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
  name text NOT NULL
    CHECK (
      length(name) BETWEEN 1 AND 255
      AND name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$'
      AND position('/' IN name) = 0
      AND position(E'\\' IN name) = 0
      AND name NOT IN ('.', '..')
      AND right(name, 1) <> '.'
      AND upper(split_part(name, '.', 1)) NOT IN (
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
      )
    ),
  relative_location text NOT NULL
    CHECK (
      length(relative_location) BETWEEN 11 AND 265
      AND relative_location ~ '^artifacts/[A-Za-z0-9][A-Za-z0-9._-]{0,254}$'
      AND relative_location = 'artifacts/' || name
      AND position(E'\\' IN relative_location) = 0
    ),
  media_type text NOT NULL
    CHECK (length(media_type) BETWEEN 1 AND 255),
  size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 0 AND 67108864),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  available boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  PRIMARY KEY (job_id, artifact_id)
);

CREATE INDEX idx_monomer_dft_jobs_status_created
ON monomer_dft.jobs(status, created_at DESC, job_id DESC);

CREATE INDEX idx_monomer_dft_jobs_type_created
ON monomer_dft.jobs(calculation_type, created_at DESC, job_id DESC);

CREATE INDEX idx_monomer_dft_jobs_model_created
ON monomer_dft.jobs(model_name, created_at DESC, job_id DESC);

CREATE INDEX idx_monomer_dft_jobs_active
ON monomer_dft.jobs(enqueue_sequence)
WHERE status IN ('pending', 'queued', 'running', 'cancel_requested');

CREATE INDEX idx_monomer_dft_jobs_pending_artifact_deletion
ON monomer_dft.jobs(artifacts_delete_requested_at, job_id)
WHERE artifacts_delete_requested_at IS NOT NULL
  AND artifacts_deleted_at IS NULL
  AND status IN ('completed', 'failed', 'cancelled');

CREATE UNIQUE INDEX uq_monomer_dft_artifact_name_ci
ON monomer_dft.artifacts(job_id, lower(name));

CREATE INDEX idx_monomer_dft_artifacts_available
ON monomer_dft.artifacts(job_id, available, artifact_id);
