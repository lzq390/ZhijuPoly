CREATE SCHEMA auth;
CREATE TABLE auth.users (
  user_id uuid PRIMARY KEY,
  username text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,63}$'),
  password_hash text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  must_change_password boolean NOT NULL DEFAULT true,
  is_system boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  password_changed_at timestamptz,
  CHECK (NOT is_system OR status = 'disabled')
);
INSERT INTO auth.users(user_id,username,password_hash,status,must_change_password,is_system)
VALUES ('00000000-0000-0000-0000-000000000001','system-canary','!','disabled',false,true);
CREATE TABLE auth.sessions (
  session_id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES auth.users(user_id),
  token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash)=32),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz
);
CREATE INDEX auth_sessions_user_active ON auth.sessions(user_id) WHERE revoked_at IS NULL;
CREATE INDEX auth_sessions_expiry ON auth.sessions(expires_at);

ALTER TABLE online_knowledge.history ADD COLUMN owner_user_id uuid;
ALTER TABLE online_knowledge.jobs ADD COLUMN owner_user_id uuid,
  ADD COLUMN start_authorized_at timestamptz;
ALTER TABLE md.monomer_md_jobs ADD COLUMN owner_user_id uuid,
  ADD COLUMN start_authorized_at timestamptz;
ALTER TABLE monomer_dft.jobs ADD COLUMN owner_user_id uuid;
ALTER TABLE monomer_dft.job_attempts ADD COLUMN start_authorized_at timestamptz;
ALTER TABLE polymerization_batch.imports ADD COLUMN owner_user_id uuid;
ALTER TABLE polymerization_batch.jobs ADD COLUMN owner_user_id uuid,
  ADD COLUMN start_authorized_at timestamptz;
CREATE INDEX online_history_owner_created ON online_knowledge.history(owner_user_id,created_at DESC,history_id DESC);
CREATE INDEX online_jobs_owner_created ON online_knowledge.jobs(owner_user_id,created_at DESC,job_id DESC);
CREATE INDEX md_jobs_owner_created ON md.monomer_md_jobs(owner_user_id,created_at DESC,job_id DESC);
CREATE INDEX dft_jobs_owner_created ON monomer_dft.jobs(owner_user_id,created_at DESC,job_id DESC);
CREATE INDEX batch_imports_owner_created ON polymerization_batch.imports(owner_user_id,created_at DESC,id DESC);
CREATE INDEX batch_jobs_owner_created ON polymerization_batch.jobs(owner_user_id,created_at DESC,id DESC);

-- Deployment credentials are provisioned separately; these are privilege groups.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='nexpoly_api') THEN CREATE ROLE nexpoly_api NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='nexpoly_auth') THEN CREATE ROLE nexpoly_auth NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='nexpoly_service') THEN CREATE ROLE nexpoly_service NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='nexpoly_mutable_audit') THEN CREATE ROLE nexpoly_mutable_audit NOLOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA auth TO nexpoly_auth,nexpoly_service,nexpoly_mutable_audit;
GRANT SELECT,INSERT,UPDATE,DELETE ON auth.sessions TO nexpoly_auth;
GRANT SELECT,UPDATE ON auth.users TO nexpoly_auth;
GRANT SELECT ON auth.users TO nexpoly_service;
-- SELECT FOR UPDATE requires an UPDATE privilege; services cannot change status/passwords.
GRANT UPDATE(updated_at) ON auth.users TO nexpoly_service;
GRANT SELECT(user_id,username,status,must_change_password,is_system,created_at,updated_at,password_changed_at)
  ON auth.users TO nexpoly_mutable_audit;
GRANT SELECT(session_id,user_id,created_at,expires_at,revoked_at)
  ON auth.sessions TO nexpoly_mutable_audit;
