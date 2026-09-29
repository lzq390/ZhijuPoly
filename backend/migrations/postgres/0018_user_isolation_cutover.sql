-- Run only via app.auth.cutover, with writers stopped and an explicit existing owner.
DO $$
DECLARE target uuid := nullif(current_setting('nexpoly.legacy_owner', true),'')::uuid;
DECLARE relation text;
BEGIN
  IF target IS NULL OR NOT EXISTS (
    SELECT FROM auth.users WHERE user_id=target AND NOT is_system AND status='active'
  ) THEN RAISE EXCEPTION 'cutover requires an explicit existing active ordinary owner UUID'; END IF;
  IF EXISTS (SELECT FROM md.monomer_md_jobs WHERE status IN ('pending','submitted','queued','running','cancel_requested'))
    OR EXISTS (SELECT FROM monomer_dft.jobs WHERE status IN ('pending','queued','running','cancel_requested'))
    OR EXISTS (SELECT FROM polymerization_batch.jobs WHERE status IN ('queued','running','cancelling'))
    OR EXISTS (SELECT FROM online_knowledge.jobs WHERE status IN ('pending','queued','running'))
  THEN RAISE EXCEPTION 'private tasks must be drained before cutover'; END IF;
  FOREACH relation IN ARRAY ARRAY['online_knowledge.history','online_knowledge.jobs','md.monomer_md_jobs',
    'monomer_dft.jobs','polymerization_batch.imports','polymerization_batch.jobs'] LOOP
    EXECUTE format('UPDATE %s SET owner_user_id=$1 WHERE owner_user_id IS NULL',relation) USING target;
    EXECUTE format('ALTER TABLE %s ALTER COLUMN owner_user_id SET NOT NULL',relation);
    EXECUTE format('ALTER TABLE %s ADD CONSTRAINT owner_user_fk FOREIGN KEY(owner_user_id) REFERENCES auth.users(user_id)',relation);
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY',relation);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY',relation);
    EXECUTE format('CREATE POLICY owner_access ON %s TO nexpoly_api USING (owner_user_id = nullif(current_setting(''app.user_id'',true),'''')::uuid) WITH CHECK (owner_user_id = nullif(current_setting(''app.user_id'',true),'''')::uuid)',relation);
    EXECUTE format('CREATE POLICY service_access ON %s TO nexpoly_service USING (true) WITH CHECK (true)',relation);
    EXECUTE format('CREATE POLICY audit_access ON %s FOR SELECT TO nexpoly_mutable_audit USING (true)',relation);
  END LOOP;
END $$;
ALTER TABLE online_knowledge.history DROP CONSTRAINT history_material_mode_key,
  ADD CONSTRAINT history_owner_material_mode_key UNIQUE(owner_user_id,material,mode);
ALTER TABLE monomer_dft.jobs DROP CONSTRAINT jobs_idempotency_key_key,
  ADD CONSTRAINT jobs_owner_idempotency_key UNIQUE(owner_user_id,idempotency_key);
ALTER TABLE polymerization_batch.jobs DROP CONSTRAINT jobs_idempotency_key_key,
  ADD CONSTRAINT jobs_owner_idempotency_key UNIQUE(owner_user_id,idempotency_key);

ALTER TABLE monomer_dft.job_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE monomer_dft.job_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE monomer_dft.artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE monomer_dft.artifacts FORCE ROW LEVEL SECURITY;
ALTER TABLE polymerization_batch.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE polymerization_batch.chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON monomer_dft.job_attempts TO nexpoly_api
  USING (EXISTS (SELECT FROM monomer_dft.jobs p WHERE p.job_id=job_attempts.job_id AND p.owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid));
CREATE POLICY owner_access ON monomer_dft.artifacts TO nexpoly_api
  USING (EXISTS (SELECT FROM monomer_dft.jobs p WHERE p.job_id=artifacts.job_id AND p.owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid));
CREATE POLICY owner_access ON polymerization_batch.chunks TO nexpoly_api
  USING (EXISTS (SELECT FROM polymerization_batch.jobs p WHERE p.id=chunks.job_id AND p.owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid));
CREATE POLICY service_access ON monomer_dft.job_attempts TO nexpoly_service USING (true) WITH CHECK (true);
CREATE POLICY service_access ON monomer_dft.artifacts TO nexpoly_service USING (true) WITH CHECK (true);
CREATE POLICY service_access ON polymerization_batch.chunks TO nexpoly_service USING (true) WITH CHECK (true);
CREATE POLICY audit_access ON monomer_dft.job_attempts FOR SELECT TO nexpoly_mutable_audit USING (true);
CREATE POLICY audit_access ON monomer_dft.artifacts FOR SELECT TO nexpoly_mutable_audit USING (true);
CREATE POLICY audit_access ON polymerization_batch.chunks FOR SELECT TO nexpoly_mutable_audit USING (true);

GRANT USAGE ON SCHEMA governance,online_knowledge,md,monomer_dft,polymerization_batch,pi TO nexpoly_api,nexpoly_service;
GRANT SELECT ON ALL TABLES IN SCHEMA pi,governance TO nexpoly_api,nexpoly_service;
GRANT UPDATE(updated_at) ON governance.deployment_control TO nexpoly_service;
GRANT USAGE ON SCHEMA core,knowledge,dft,experimental,model_registry TO nexpoly_api,nexpoly_service;
GRANT SELECT ON ALL TABLES IN SCHEMA core,knowledge,dft,experimental,model_registry TO nexpoly_api,nexpoly_service;
GRANT USAGE ON SCHEMA lab TO nexpoly_service;
GRANT SELECT ON ALL TABLES IN SCHEMA lab TO nexpoly_service;
GRANT SELECT,INSERT,UPDATE,DELETE ON online_knowledge.history,online_knowledge.jobs,md.monomer_md_jobs,
  monomer_dft.jobs,monomer_dft.job_attempts,monomer_dft.artifacts,
  polymerization_batch.imports,polymerization_batch.jobs,polymerization_batch.chunks TO nexpoly_api,nexpoly_service;
GRANT SELECT ON polymerization_batch.worker_status TO nexpoly_api;
GRANT SELECT,INSERT,UPDATE,DELETE ON polymerization_batch.worker_status TO nexpoly_service;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA online_knowledge,md,monomer_dft,polymerization_batch TO nexpoly_api,nexpoly_service;
GRANT USAGE ON SCHEMA governance,online_knowledge,md,monomer_dft,polymerization_batch,pi TO nexpoly_mutable_audit;
GRANT SELECT ON ALL TABLES IN SCHEMA governance,online_knowledge,md,monomer_dft,polymerization_batch,pi TO nexpoly_mutable_audit;
