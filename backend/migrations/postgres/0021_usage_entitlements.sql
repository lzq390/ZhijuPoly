-- Success-count entitlements. New isolated schema only; 0020 remains immutable.
-- Example policy is not an activated plan. No policy/tier seed or auth mutation.
CREATE SCHEMA usage_entitlements;
REVOKE ALL ON SCHEMA usage_entitlements FROM PUBLIC;

CREATE TABLE usage_entitlements.policy_versions (
 version text PRIMARY KEY CHECK(length(version) BETWEEN 1 AND 128),
 policy_sha256 text NOT NULL CHECK(policy_sha256 ~ '^[0-9a-f]{64}$'),
 document jsonb NOT NULL CHECK(jsonb_typeof(document)='object' AND document->>'version'=version),
 installed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE usage_entitlements.owner_entitlements (
 owner_user_id uuid PRIMARY KEY,
 tier text NOT NULL CHECK(tier IN ('member','free')),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE usage_entitlements.operations (
 owner_user_id uuid NOT NULL,
 operation_id uuid NOT NULL,
 module text NOT NULL CHECK (module ~ '^[a-z][a-z0-9_]{0,63}$'),
 key_hash text NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
 request_fingerprint text NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
 policy_version text NOT NULL,
 tier text NOT NULL CHECK (tier IN ('member','free')),
 pool_id text NOT NULL,
 spec_id text NOT NULL,
 spec jsonb NOT NULL CHECK (jsonb_typeof(spec)='object'),
 period_start timestamptz NOT NULL,
 period_end timestamptz NOT NULL,
 generation bigint NOT NULL CHECK (generation > 0),
 native_task_id text CHECK (length(native_task_id) BETWEEN 1 AND 256),
 executor_id text CHECK (length(executor_id) BETWEEN 1 AND 256),
 state text NOT NULL CHECK (state IN ('reserved','dispatching','running','unknown','succeeded','released')),
 outcome text CHECK (outcome ~ '^[a-z][a-z0-9_]{0,63}$'),
 qualified boolean,
 result_json jsonb,
 result_ref jsonb,
 created_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 settled_at timestamptz,
 event_count bigint NOT NULL CHECK (event_count > 0),
 last_event_key text NOT NULL,
 PRIMARY KEY(owner_user_id,operation_id),
 UNIQUE(owner_user_id,module,key_hash),
 CHECK(period_start<period_end AND created_at<=updated_at),
 CHECK((state IN ('succeeded','released'))=(settled_at IS NOT NULL)),
 CHECK(state<>'succeeded' OR qualified IS TRUE),
 CHECK(state<>'released' OR qualified IS FALSE),
 CHECK(state<>'succeeded' OR result_json IS NOT NULL OR result_ref IS NOT NULL)
);
CREATE UNIQUE INDEX entitlement_native_identity ON usage_entitlements.operations(owner_user_id,module,native_task_id) WHERE native_task_id IS NOT NULL;
CREATE INDEX entitlement_pool_period ON usage_entitlements.operations(owner_user_id,pool_id,period_start,state);
CREATE INDEX entitlement_owner_created ON usage_entitlements.operations(owner_user_id,created_at DESC);
CREATE TABLE usage_entitlements.operation_events (
 owner_user_id uuid NOT NULL,
 operation_id uuid NOT NULL,
 event_key text NOT NULL CHECK(length(event_key) BETWEEN 1 AND 256),
 action text NOT NULL CHECK(action IN ('reserve','bind_native','begin_dispatch','claim_running','settle','release','mark_unknown')),
 expected_generation bigint NOT NULL CHECK(expected_generation>=0),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(owner_user_id,event_key)
);
CREATE INDEX entitlement_event_operation ON usage_entitlements.operation_events(owner_user_id,operation_id);

CREATE FUNCTION usage_entitlements.reject_mutation() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage_entitlements AS $$
BEGIN
 RAISE EXCEPTION 'entitlement observations/policies are immutable' USING ERRCODE='55000';
END;
$$;
CREATE TRIGGER entitlement_policy_immutable BEFORE UPDATE OR DELETE ON usage_entitlements.policy_versions FOR EACH ROW EXECUTE FUNCTION usage_entitlements.reject_mutation();
CREATE TRIGGER entitlement_policy_no_truncate BEFORE TRUNCATE ON usage_entitlements.policy_versions FOR EACH STATEMENT EXECUTE FUNCTION usage_entitlements.reject_mutation();
CREATE TRIGGER entitlement_events_immutable BEFORE UPDATE OR DELETE ON usage_entitlements.operation_events FOR EACH ROW EXECUTE FUNCTION usage_entitlements.reject_mutation();
CREATE TRIGGER entitlement_events_no_truncate BEFORE TRUNCATE ON usage_entitlements.operation_events FOR EACH STATEMENT EXECUTE FUNCTION usage_entitlements.reject_mutation();

CREATE FUNCTION usage_entitlements.validate_event() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage_entitlements AS $$
DECLARE
 old_row usage_entitlements.operations%ROWTYPE;
 proposed usage_entitlements.operations%ROWTYPE;
 prior_event usage_entitlements.operation_events%ROWTYPE;
 policy jsonb; variant jsonb; proof jsonb; selected_tier text; selected_variant text;
 cap bigint; occupied bigint; owner_rows bigint; owner_bytes bigint; result_bytes bigint;
BEGIN
 IF NEW.payload_sha256<>encode(sha256(convert_to(NEW.payload::text,'UTF8')),'hex') THEN
  RAISE EXCEPTION 'entitlement event digest mismatch' USING ERRCODE='23514';
 END IF;
 SELECT * INTO prior_event FROM usage_entitlements.operation_events
 WHERE owner_user_id=NEW.owner_user_id AND event_key=NEW.event_key;
 IF FOUND THEN
  IF prior_event.operation_id<>NEW.operation_id OR prior_event.action<>NEW.action
   OR prior_event.expected_generation<>NEW.expected_generation OR prior_event.payload<>NEW.payload THEN
   RAISE EXCEPTION 'entitlement event conflict' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 proposed:=jsonb_populate_record(NULL::usage_entitlements.operations,NEW.payload->'operation');
 IF proposed.owner_user_id IS DISTINCT FROM NEW.owner_user_id OR proposed.operation_id IS DISTINCT FROM NEW.operation_id THEN
  RAISE EXCEPTION 'entitlement owner/operation mismatch' USING ERRCODE='23514';
 END IF;
 -- This is an entitlement-owned storage lock, never an auth/account row lock.
 PERFORM pg_advisory_xact_lock(hashtextextended('entitlement-owner:'||NEW.owner_user_id::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('entitlement-pool:'||NEW.owner_user_id::text||':'||proposed.pool_id||':'||extract(epoch FROM proposed.period_start)::text,0));
 SELECT * INTO old_row FROM usage_entitlements.operations
 WHERE owner_user_id=NEW.owner_user_id AND operation_id=NEW.operation_id FOR UPDATE;
 SELECT document INTO policy FROM usage_entitlements.policy_versions WHERE version=proposed.policy_version;
 IF policy IS NULL THEN RAISE EXCEPTION 'entitlement policy missing' USING ERRCODE='23514'; END IF;
 IF NEW.action='reserve' THEN
  IF old_row.operation_id IS NOT NULL OR NEW.expected_generation<>0 OR proposed.state<>'reserved'
   OR proposed.generation<>1 OR proposed.executor_id IS NOT NULL OR proposed.outcome IS NOT NULL
   OR proposed.qualified IS NOT NULL OR proposed.result_json IS NOT NULL OR proposed.result_ref IS NOT NULL
   OR proposed.settled_at IS NOT NULL OR proposed.updated_at<>proposed.created_at THEN
   RAISE EXCEPTION 'invalid entitlement reservation' USING ERRCODE='23514';
  END IF;
  SELECT tier INTO selected_tier FROM usage_entitlements.owner_entitlements WHERE owner_user_id=NEW.owner_user_id;
  selected_tier:=coalesce(selected_tier,'member');
  IF proposed.tier<>selected_tier OR policy->>'default_tier'<>'member' OR policy->>'period' NOT IN ('day','week','month') THEN
   RAISE EXCEPTION 'entitlement tier/period mismatch' USING ERRCODE='23514';
  END IF;
  selected_variant:=CASE WHEN policy#>>ARRAY['modules',proposed.module,'selector'] IS NULL THEN 'default'
   ELSE proposed.spec->>(policy#>>ARRAY['modules',proposed.module,'selector']) END;
  variant:=policy#>ARRAY['modules',proposed.module,'variants',selected_variant];
  IF variant IS NULL OR variant->>'mode'<>'count' OR variant->>'pool_id' IS DISTINCT FROM proposed.pool_id THEN
   RAISE EXCEPTION 'entitlement module/spec pool mismatch' USING ERRCODE='23514';
  END IF;
  IF proposed.period_start<>(date_trunc(policy->>'period',proposed.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
   OR proposed.period_end<>(((proposed.period_start AT TIME ZONE 'UTC')+
      (CASE policy->>'period' WHEN 'day' THEN interval '1 day' WHEN 'week' THEN interval '7 days' ELSE interval '1 month' END)) AT TIME ZONE 'UTC') THEN
   RAISE EXCEPTION 'entitlement period is not the frozen submission UTC window' USING ERRCODE='23514';
  END IF;
  IF EXISTS(SELECT 1 FROM usage_entitlements.operations WHERE owner_user_id=NEW.owner_user_id
    AND pool_id=proposed.pool_id AND state<>'released' AND period_end>proposed.created_at
    AND period_start<proposed.period_end AND period_end>proposed.period_start
    AND (period_start<>proposed.period_start OR period_end<>proposed.period_end)) THEN
   RAISE EXCEPTION 'entitlement period policy conflict' USING ERRCODE='23514';
  END IF;
  IF NOT (policy#>ARRAY['tiers',proposed.tier,'pools']) ? proposed.pool_id THEN
   RAISE EXCEPTION 'entitlement pool is absent from tier' USING ERRCODE='23514';
  END IF;
  cap:=(policy#>>ARRAY['tiers',proposed.tier,'pools',proposed.pool_id])::bigint;
  SELECT count(*) INTO occupied FROM usage_entitlements.operations
  WHERE owner_user_id=NEW.owner_user_id AND pool_id=proposed.pool_id AND period_start=proposed.period_start AND period_end=proposed.period_end
    AND state<>'released';
  IF cap IS NOT NULL AND occupied>=cap THEN
   RAISE EXCEPTION 'entitlement pool exhausted' USING ERRCODE='P0001';
  END IF;
  SELECT count(*) INTO occupied FROM usage_entitlements.operations
  WHERE owner_user_id=NEW.owner_user_id AND state IN ('reserved','dispatching','running','unknown');
  IF occupied>=(policy->>'active_operations_per_owner')::bigint THEN
   RAISE EXCEPTION 'entitlement owner active capacity exhausted' USING ERRCODE='P0001';
  END IF;
  SELECT count(*),coalesce(sum(octet_length(to_jsonb(o)::text)
    + CASE WHEN state NOT IN ('succeeded','released') THEN
      (SELECT (document->>'result_max_bytes')::bigint FROM usage_entitlements.policy_versions WHERE version=o.policy_version)
      ELSE 0 END),0)
  INTO owner_rows,owner_bytes FROM usage_entitlements.operations o WHERE owner_user_id=NEW.owner_user_id;
  IF owner_rows>=(policy->>'operation_max_rows')::bigint
   OR owner_bytes+octet_length(to_jsonb(proposed)::text)+(policy->>'result_max_bytes')::bigint
       >(policy->>'operation_max_bytes')::bigint THEN
   RAISE EXCEPTION 'entitlement operation storage exhausted' USING ERRCODE='P0001';
  END IF;
 ELSE
  IF old_row.operation_id IS NULL OR old_row.generation<>NEW.expected_generation THEN
   RAISE EXCEPTION 'entitlement stale generation' USING ERRCODE='23514';
  END IF;
  IF old_row.state IN ('succeeded','released') THEN
   RAISE EXCEPTION 'entitlement terminal operation is immutable' USING ERRCODE='23514';
  END IF;
  IF (to_jsonb(proposed)-ARRAY['generation','native_task_id','executor_id','state','outcome','qualified','result_json','result_ref','updated_at','settled_at','event_count','last_event_key'])
     IS DISTINCT FROM
     (to_jsonb(old_row)-ARRAY['generation','native_task_id','executor_id','state','outcome','qualified','result_json','result_ref','updated_at','settled_at','event_count','last_event_key']) THEN
   RAISE EXCEPTION 'entitlement immutable identity changed' USING ERRCODE='23514';
  END IF;
  IF proposed.updated_at<old_row.updated_at THEN RAISE EXCEPTION 'entitlement time regression' USING ERRCODE='23514'; END IF;
  IF NEW.action='bind_native' THEN
   IF old_row.state<>'reserved' OR old_row.native_task_id IS NOT NULL OR proposed.native_task_id IS NULL
    OR (to_jsonb(proposed)-ARRAY['native_task_id','updated_at','event_count','last_event_key'])
       IS DISTINCT FROM (to_jsonb(old_row)-ARRAY['native_task_id','updated_at','event_count','last_event_key']) THEN
    RAISE EXCEPTION 'invalid native binding' USING ERRCODE='23514';
   END IF;
  ELSIF NEW.action='begin_dispatch' THEN
   IF old_row.state<>'reserved' OR proposed.state<>'dispatching' OR proposed.generation<>old_row.generation+1
    OR proposed.executor_id IS NULL OR old_row.executor_id IS NOT NULL THEN
    RAISE EXCEPTION 'invalid dispatch fence' USING ERRCODE='23514';
   END IF;
  ELSIF NEW.action='claim_running' THEN
   IF old_row.state<>'dispatching' OR proposed.state<>'running' OR proposed.executor_id IS DISTINCT FROM old_row.executor_id
    OR proposed.generation<>old_row.generation THEN RAISE EXCEPTION 'invalid execution claim' USING ERRCODE='23514'; END IF;
  ELSIF NEW.action='mark_unknown' THEN
   IF proposed.state<>'unknown' OR proposed.generation<>old_row.generation
    OR proposed.qualified IS NOT NULL OR proposed.settled_at IS NOT NULL THEN
    RAISE EXCEPTION 'invalid unknown transition' USING ERRCODE='23514';
   END IF;
  ELSIF NEW.action='settle' THEN
   IF proposed.generation<>old_row.generation OR proposed.qualified IS NULL
    OR proposed.outcome IS NULL OR proposed.outcome IN ('unknown','pending','running','cancel_requested')
    OR proposed.state<>(CASE WHEN proposed.qualified THEN 'succeeded' ELSE 'released' END)
    OR proposed.settled_at IS DISTINCT FROM proposed.updated_at
    OR (proposed.qualified AND old_row.state NOT IN ('running','unknown'))
    OR (old_row.state='unknown' AND proposed.result_json IS NULL AND proposed.result_ref IS NULL) THEN
    RAISE EXCEPTION 'invalid entitlement settlement' USING ERRCODE='23514';
   END IF;
  ELSIF NEW.action='release' THEN
   proof:=NEW.payload->'proof';
   IF proposed.state<>'released' OR proposed.qualified IS DISTINCT FROM false
    OR proposed.generation<>old_row.generation OR proposed.settled_at IS DISTINCT FROM proposed.updated_at THEN
    RAISE EXCEPTION 'invalid entitlement release' USING ERRCODE='23514';
   END IF;
   IF proof->>'kind'='not_dispatched' THEN
    IF old_row.state<>'reserved' OR old_row.executor_id IS NOT NULL THEN RAISE EXCEPTION 'not-dispatched proof contradicted' USING ERRCODE='23514'; END IF;
   ELSIF proof->>'kind'='dispatch_rejected_before_execution' THEN
    IF old_row.state<>'dispatching' OR proof->>'executor_id' IS DISTINCT FROM old_row.executor_id THEN RAISE EXCEPTION 'dispatch rejection proof contradicted' USING ERRCODE='23514'; END IF;
   ELSIF proof->>'kind' IN ('native_terminal','native_never_dispatched') THEN
    IF proof->>'module' IS DISTINCT FROM old_row.module OR proof->>'native_task_id' IS DISTINCT FROM old_row.native_task_id
     OR old_row.native_task_id IS NULL
     OR NOT (proof->>'native_status'=ANY(ARRAY['completed','failed','cancelled','interrupted','rejected','timed_out','completed_with_errors']))
     OR NOT (proof->>'source'=ANY(ARRAY['md_submit_unclaimed_cas','md_worker_terminal','md_native_recovery','md_pending_lease_expired','dft_unsubmitted_cancel','dft_disabled_before_dispatch','dft_validated_worker_terminal','batch_native_terminal','online_native_terminal','online_native_recovery','online_not_scheduled'])) THEN
     RAISE EXCEPTION 'native termination proof mismatch' USING ERRCODE='23514';
    END IF;
   ELSE RAISE EXCEPTION 'unsupported release proof' USING ERRCODE='23514';
   END IF;
  END IF;
  IF NEW.action<>'bind_native' AND proposed.native_task_id IS DISTINCT FROM old_row.native_task_id THEN
   RAISE EXCEPTION 'native identity cannot change' USING ERRCODE='23514';
  END IF;
  IF NEW.action<>'begin_dispatch' AND proposed.executor_id IS DISTINCT FROM old_row.executor_id THEN
   RAISE EXCEPTION 'executor identity cannot change' USING ERRCODE='23514';
  END IF;
  IF NEW.action NOT IN ('settle','release') AND
    (proposed.result_json IS DISTINCT FROM old_row.result_json OR proposed.result_ref IS DISTINCT FROM old_row.result_ref
     OR proposed.qualified IS DISTINCT FROM old_row.qualified OR proposed.settled_at IS DISTINCT FROM old_row.settled_at) THEN
   RAISE EXCEPTION 'nonterminal transition changed result' USING ERRCODE='23514';
  END IF;
 END IF;
 result_bytes:=coalesce(octet_length(proposed.result_json::text),0)+coalesce(octet_length(proposed.result_ref::text),0);
 IF result_bytes>(policy->>'result_max_bytes')::bigint THEN
  RAISE EXCEPTION 'entitlement result storage exhausted' USING ERRCODE='P0001';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER entitlement_event_validate BEFORE INSERT ON usage_entitlements.operation_events FOR EACH ROW EXECUTE FUNCTION usage_entitlements.validate_event();

CREATE FUNCTION usage_entitlements.guard_projection() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage_entitlements AS $$
DECLARE observed usage_entitlements.operation_events%ROWTYPE; expected usage_entitlements.operations%ROWTYPE; n bigint;
BEGIN
 IF TG_OP NOT IN ('INSERT','UPDATE') OR pg_trigger_depth()<>2 THEN
  RAISE EXCEPTION 'entitlement projection must derive from new immutable event' USING ERRCODE='55000';
 END IF;
 SELECT * INTO observed FROM usage_entitlements.operation_events
 WHERE owner_user_id=NEW.owner_user_id AND operation_id=NEW.operation_id AND event_key=NEW.last_event_key;
 IF NOT FOUND THEN RAISE EXCEPTION 'entitlement projection missing observation' USING ERRCODE='55000'; END IF;
 SELECT count(*) INTO n FROM usage_entitlements.operation_events
 WHERE owner_user_id=NEW.owner_user_id AND operation_id=NEW.operation_id;
 IF NEW.event_count<>n OR (TG_OP='INSERT' AND n<>1)
  OR (TG_OP='UPDATE' AND (NEW.last_event_key=OLD.last_event_key OR n<>OLD.event_count+1)) THEN
  RAISE EXCEPTION 'entitlement projection cannot replay observations' USING ERRCODE='55000';
 END IF;
 expected:=jsonb_populate_record(NULL::usage_entitlements.operations,
  observed.payload->'operation'||jsonb_build_object('event_count',n,'last_event_key',observed.event_key));
 IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(expected) THEN
  RAISE EXCEPTION 'entitlement projection differs from event' USING ERRCODE='55000';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER entitlement_projection_guard BEFORE INSERT OR UPDATE OR DELETE ON usage_entitlements.operations FOR EACH ROW EXECUTE FUNCTION usage_entitlements.guard_projection();
CREATE TRIGGER entitlement_projection_no_truncate BEFORE TRUNCATE ON usage_entitlements.operations FOR EACH STATEMENT EXECUTE FUNCTION usage_entitlements.guard_projection();

CREATE FUNCTION usage_entitlements.project_event() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage_entitlements AS $$
DECLARE n bigint; projected usage_entitlements.operations%ROWTYPE;
BEGIN
 SELECT count(*) INTO n FROM usage_entitlements.operation_events WHERE owner_user_id=NEW.owner_user_id AND operation_id=NEW.operation_id;
 projected:=jsonb_populate_record(NULL::usage_entitlements.operations,
  NEW.payload->'operation'||jsonb_build_object('event_count',n,'last_event_key',NEW.event_key));
 UPDATE usage_entitlements.operations SET
 module=projected.module,
 key_hash=projected.key_hash,
 request_fingerprint=projected.request_fingerprint,
 policy_version=projected.policy_version,
 tier=projected.tier,
 pool_id=projected.pool_id,
 spec_id=projected.spec_id,
 spec=projected.spec,
 period_start=projected.period_start,
 period_end=projected.period_end,
 generation=projected.generation,
 native_task_id=projected.native_task_id,
 executor_id=projected.executor_id,
 state=projected.state,
 outcome=projected.outcome,
 qualified=projected.qualified,
 result_json=projected.result_json,
 result_ref=projected.result_ref,
 created_at=projected.created_at,
 updated_at=projected.updated_at,
 settled_at=projected.settled_at,
 event_count=projected.event_count,
 last_event_key=projected.last_event_key
 WHERE owner_user_id=NEW.owner_user_id AND operation_id=NEW.operation_id;
 IF NOT FOUND THEN INSERT INTO usage_entitlements.operations SELECT projected.*; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER entitlement_event_projection AFTER INSERT ON usage_entitlements.operation_events FOR EACH ROW EXECUTE FUNCTION usage_entitlements.project_event();

ALTER TABLE usage_entitlements.policy_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_entitlements.policy_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON usage_entitlements.policy_versions FOR SELECT TO nexpoly_api USING(true);
CREATE POLICY service_access ON usage_entitlements.policy_versions TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON usage_entitlements.policy_versions FOR SELECT TO nexpoly_mutable_audit USING(true);
REVOKE ALL ON usage_entitlements.policy_versions FROM PUBLIC;

ALTER TABLE usage_entitlements.owner_entitlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_entitlements.owner_entitlements FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON usage_entitlements.owner_entitlements TO nexpoly_api USING(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid) WITH CHECK(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY service_access ON usage_entitlements.owner_entitlements TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON usage_entitlements.owner_entitlements FOR SELECT TO nexpoly_mutable_audit USING(true);
REVOKE ALL ON usage_entitlements.owner_entitlements FROM PUBLIC;

ALTER TABLE usage_entitlements.operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_entitlements.operations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON usage_entitlements.operations TO nexpoly_api USING(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid) WITH CHECK(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY service_access ON usage_entitlements.operations TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON usage_entitlements.operations FOR SELECT TO nexpoly_mutable_audit USING(true);
REVOKE ALL ON usage_entitlements.operations FROM PUBLIC;

ALTER TABLE usage_entitlements.operation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_entitlements.operation_events FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON usage_entitlements.operation_events TO nexpoly_api USING(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid) WITH CHECK(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY service_access ON usage_entitlements.operation_events TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON usage_entitlements.operation_events FOR SELECT TO nexpoly_mutable_audit USING(true);
REVOKE ALL ON usage_entitlements.operation_events FROM PUBLIC;

GRANT USAGE ON SCHEMA usage_entitlements TO nexpoly_api,nexpoly_service,nexpoly_mutable_audit;
GRANT SELECT ON ALL TABLES IN SCHEMA usage_entitlements TO nexpoly_api,nexpoly_service,nexpoly_mutable_audit;
GRANT INSERT ON usage_entitlements.policy_versions TO nexpoly_service;
GRANT INSERT,UPDATE ON usage_entitlements.owner_entitlements TO nexpoly_service;
GRANT INSERT ON usage_entitlements.operation_events TO nexpoly_api,nexpoly_service;
GRANT INSERT,UPDATE ON usage_entitlements.operations TO nexpoly_api,nexpoly_service;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA usage_entitlements FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA usage_entitlements TO nexpoly_api,nexpoly_service;
COMMENT ON TABLE usage_entitlements.operations IS 'Success-count reservations and retained owner results; no native-job FK or automatic unknown refund';

CREATE TABLE usage_entitlements.request_observations (
 owner_user_id uuid NOT NULL,
 request_id uuid NOT NULL,
 phase text NOT NULL CHECK(phase IN ('received','finished')),
 module text NOT NULL CHECK(module ~ '^[a-z][a-z0-9_]{0,63}$'),
 operation_id uuid,
 http_status integer CHECK(http_status BETWEEN 100 AND 599),
 outcome text CHECK(outcome ~ '^[a-z][a-z0-9_]{0,63}$'),
 occurred_at timestamptz NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(owner_user_id,request_id,phase),
 CHECK(phase<>'received' OR (http_status IS NULL AND outcome IS NULL))
);
CREATE INDEX entitlement_request_owner_time ON usage_entitlements.request_observations(owner_user_id,occurred_at,module);
CREATE TRIGGER entitlement_request_immutable BEFORE UPDATE OR DELETE ON usage_entitlements.request_observations FOR EACH ROW EXECUTE FUNCTION usage_entitlements.reject_mutation();
CREATE TRIGGER entitlement_request_no_truncate BEFORE TRUNCATE ON usage_entitlements.request_observations FOR EACH STATEMENT EXECUTE FUNCTION usage_entitlements.reject_mutation();
ALTER TABLE usage_entitlements.request_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_entitlements.request_observations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON usage_entitlements.request_observations TO nexpoly_api USING(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid) WITH CHECK(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY service_access ON usage_entitlements.request_observations TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON usage_entitlements.request_observations FOR SELECT TO nexpoly_mutable_audit USING(true);
REVOKE ALL ON usage_entitlements.request_observations FROM PUBLIC;
GRANT SELECT,INSERT ON usage_entitlements.request_observations TO nexpoly_api,nexpoly_service;
GRANT SELECT ON usage_entitlements.request_observations TO nexpoly_mutable_audit;
