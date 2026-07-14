CREATE TABLE IF NOT EXISTS governance.deployment_control (
  control_key text PRIMARY KEY CHECK (control_key = 'production'),
  drain_enabled boolean NOT NULL DEFAULT false,
  reason text,
  release_sha text,
  activated_at timestamptz,
  activated_by text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (release_sha IS NULL OR release_sha ~ '^[0-9a-f]{40}$')
);

INSERT INTO governance.deployment_control (control_key)
VALUES ('production')
ON CONFLICT (control_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS governance.database_analytics_snapshots (
  snapshot_key text PRIMARY KEY,
  generated_at timestamptz NOT NULL,
  source_sha text,
  datasets jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (source_sha IS NULL OR source_sha ~ '^[0-9a-f]{40}$'),
  CHECK (jsonb_typeof(datasets) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_database_analytics_snapshots_generated_at
ON governance.database_analytics_snapshots(generated_at DESC);
