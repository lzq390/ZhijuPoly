-- Append-only usage observations; no business-job foreign keys or historical backfill.
-- This expand must follow the exact 0012/0018/0019 contracts (manifest epoch 5).
-- Owner identity remains app.user_id; no existing auth/RLS/role membership changes.
CREATE SCHEMA usage;
REVOKE ALL ON SCHEMA usage FROM PUBLIC;

CREATE TABLE usage.events (
  owner_user_id uuid NOT NULL,
  module text NOT NULL CHECK (module ~ '^[a-z][a-z0-9_]{0,63}$'),
  task_id text NOT NULL CHECK (length(task_id) BETWEEN 1 AND 256),
  attempt_id text NOT NULL CHECK (length(attempt_id) BETWEEN 1 AND 128),
  kind text NOT NULL CHECK (kind IN ('task','execution','provider')),
  root_task_id text NOT NULL CHECK (length(root_task_id) BETWEEN 1 AND 256),
  parent_task_id text CHECK (length(parent_task_id) BETWEEN 1 AND 256),
  source text NOT NULL CHECK (source IN ('runtime','backfill','reconciliation')),
  status text NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled','interrupted','rejected','timed_out')),
  event_key text NOT NULL CHECK (length(event_key) BETWEEN 1 AND 256),
  payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  occurred_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  submitted_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  queue_ms bigint CHECK (queue_ms >= 0),
  execution_ms bigint CHECK (execution_ms >= 0),
  observed_wall_ms bigint CHECK (observed_wall_ms >= 0),
  provider_input_tokens bigint CHECK (provider_input_tokens >= 0),
  provider_output_tokens bigint CHECK (provider_output_tokens >= 0),
  provider_total_tokens bigint CHECK (provider_total_tokens >= 0),
  cached_input_tokens bigint CHECK (cached_input_tokens >= 0),
  reasoning_tokens bigint CHECK (reasoning_tokens >= 0),
  artifact_bytes bigint CHECK (artifact_bytes >= 0),
  PRIMARY KEY (owner_user_id,event_key),
  CHECK (parent_task_id IS NULL OR parent_task_id <> task_id),
  CHECK (parent_task_id IS NOT NULL OR root_task_id = task_id),
  CHECK (submitted_at IS NULL OR started_at IS NULL OR submitted_at <= started_at),
  CHECK (started_at IS NULL OR finished_at IS NULL OR started_at <= finished_at),
  CHECK (submitted_at IS NULL OR finished_at IS NULL OR submitted_at <= finished_at),
  CHECK (finished_at IS NULL OR status NOT IN ('queued','running'))
);

CREATE TABLE usage.attempts (
  owner_user_id uuid NOT NULL,
  module text NOT NULL CHECK (module ~ '^[a-z][a-z0-9_]{0,63}$'),
  task_id text NOT NULL CHECK (length(task_id) BETWEEN 1 AND 256),
  attempt_id text NOT NULL CHECK (length(attempt_id) BETWEEN 1 AND 128),
  kind text NOT NULL CHECK (kind IN ('task','execution','provider')),
  root_task_id text NOT NULL CHECK (length(root_task_id) BETWEEN 1 AND 256),
  parent_task_id text CHECK (length(parent_task_id) BETWEEN 1 AND 256),
  source text NOT NULL CHECK (source IN ('runtime','backfill','reconciliation')),
  status text NOT NULL CHECK (status IN ('queued','running','completed','failed','cancelled','interrupted','rejected','timed_out')),
  first_occurred_at timestamptz NOT NULL,
  last_occurred_at timestamptz NOT NULL,
  last_recorded_at timestamptz NOT NULL,
  last_source text NOT NULL CHECK (last_source IN ('runtime','backfill','reconciliation')),
  last_event_key text NOT NULL CHECK (length(last_event_key) BETWEEN 1 AND 256),
  submitted_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  queue_ms bigint CHECK (queue_ms >= 0),
  execution_ms bigint CHECK (execution_ms >= 0),
  observed_wall_ms bigint CHECK (observed_wall_ms >= 0),
  provider_input_tokens bigint CHECK (provider_input_tokens >= 0),
  provider_output_tokens bigint CHECK (provider_output_tokens >= 0),
  provider_total_tokens bigint CHECK (provider_total_tokens >= 0),
  cached_input_tokens bigint CHECK (cached_input_tokens >= 0),
  reasoning_tokens bigint CHECK (reasoning_tokens >= 0),
  artifact_bytes bigint CHECK (artifact_bytes >= 0),
  event_count bigint NOT NULL CHECK (event_count > 0),
  PRIMARY KEY (owner_user_id,module,task_id,attempt_id,kind),
  CHECK (first_occurred_at <= last_occurred_at),
  CHECK (parent_task_id IS NULL OR parent_task_id <> task_id),
  CHECK (parent_task_id IS NOT NULL OR root_task_id = task_id),
  CHECK (submitted_at IS NULL OR started_at IS NULL OR submitted_at <= started_at),
  CHECK (started_at IS NULL OR finished_at IS NULL OR started_at <= finished_at),
  CHECK (submitted_at IS NULL OR finished_at IS NULL OR submitted_at <= finished_at),
  CHECK (finished_at IS NULL OR status NOT IN ('queued','running'))
);
CREATE INDEX usage_attempts_owner_time ON usage.attempts(owner_user_id,first_occurred_at DESC,module,task_id);
CREATE INDEX usage_events_owner_task ON usage.events(owner_user_id,module,task_id,attempt_id,kind,occurred_at);

CREATE FUNCTION usage.reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage AS $$
BEGIN
  RAISE EXCEPTION 'usage events are append-only' USING ERRCODE='55000';
END;
$$;
CREATE TRIGGER usage_events_immutable BEFORE UPDATE OR DELETE ON usage.events
  FOR EACH ROW EXECUTE FUNCTION usage.reject_event_mutation();
CREATE TRIGGER usage_events_no_truncate BEFORE TRUNCATE ON usage.events
  FOR EACH STATEMENT EXECUTE FUNCTION usage.reject_event_mutation();

-- A trigger-depth check is not authority: TEMP triggers can create the same
-- nesting depth. Verify the complete new row against the immutable event and
-- old projection. Every update must advance the count exactly once and match
-- the visible event ledger; an already-projected key cannot be replayed.
CREATE FUNCTION usage.guard_projection() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage AS $$
DECLARE
  observed usage.events%ROWTYPE;
  expected usage.attempts%ROWTYPE;
  ledger_count bigint;
BEGIN
  IF TG_OP IN ('DELETE','TRUNCATE') THEN
    RAISE EXCEPTION 'usage projections are derived from events' USING ERRCODE='55000';
  END IF;
  SELECT * INTO observed FROM usage.events
    WHERE owner_user_id=NEW.owner_user_id AND event_key=NEW.last_event_key;
  IF NOT FOUND OR
     ROW(NEW.owner_user_id,NEW.module,NEW.task_id,NEW.attempt_id,NEW.kind,NEW.root_task_id,NEW.parent_task_id)
       IS DISTINCT FROM
     ROW(observed.owner_user_id,observed.module,observed.task_id,observed.attempt_id,observed.kind,observed.root_task_id,observed.parent_task_id)
  THEN
    RAISE EXCEPTION 'usage projection has no matching immutable event' USING ERRCODE='55000';
  END IF;
  IF TG_OP='INSERT' THEN
    -- ON CONFLICT executes this candidate check before the UPDATE check below.
    expected:=jsonb_populate_record(NULL::usage.attempts,to_jsonb(observed)||jsonb_build_object(
      'first_occurred_at',observed.occurred_at,'last_occurred_at',observed.occurred_at,
      'last_recorded_at',observed.recorded_at,'last_source',observed.source,
      'last_event_key',observed.event_key,'event_count',1));
  ELSE
    IF ROW(NEW.owner_user_id,NEW.module,NEW.task_id,NEW.attempt_id,NEW.kind)
        IS DISTINCT FROM ROW(OLD.owner_user_id,OLD.module,OLD.task_id,OLD.attempt_id,OLD.kind)
      OR NEW.last_event_key=OLD.last_event_key
      OR NEW.event_count<>OLD.event_count+1
    THEN
      RAISE EXCEPTION 'usage projection cannot replay or change identity' USING ERRCODE='55000';
    END IF;
    SELECT count(*) INTO ledger_count FROM usage.events
      WHERE owner_user_id=NEW.owner_user_id AND module=NEW.module AND task_id=NEW.task_id
        AND attempt_id=NEW.attempt_id AND kind=NEW.kind;
    IF NEW.event_count<>ledger_count THEN
      RAISE EXCEPTION 'usage projection has no new ledger event' USING ERRCODE='55000';
    END IF;
    expected:=OLD;
    expected.status:=CASE
      WHEN OLD.status NOT IN ('queued','running') THEN OLD.status
      WHEN observed.status NOT IN ('queued','running') THEN observed.status
      WHEN OLD.status='running' OR observed.status='running' THEN 'running'
      ELSE 'queued' END;
    expected.first_occurred_at:=least(OLD.first_occurred_at,observed.occurred_at);
    expected.last_occurred_at:=greatest(OLD.last_occurred_at,observed.occurred_at);
    expected.last_recorded_at:=greatest(OLD.last_recorded_at,observed.recorded_at);
    expected.last_source:=CASE WHEN observed.occurred_at>=OLD.last_occurred_at THEN observed.source ELSE OLD.last_source END;
    expected.submitted_at:=least(OLD.submitted_at,observed.submitted_at);
    expected.started_at:=least(OLD.started_at,observed.started_at);
    expected.finished_at:=least(OLD.finished_at,observed.finished_at);
    expected.queue_ms:=greatest(OLD.queue_ms,observed.queue_ms);
    expected.execution_ms:=greatest(OLD.execution_ms,observed.execution_ms);
    expected.observed_wall_ms:=greatest(OLD.observed_wall_ms,observed.observed_wall_ms);
    expected.provider_input_tokens:=greatest(OLD.provider_input_tokens,observed.provider_input_tokens);
    expected.provider_output_tokens:=greatest(OLD.provider_output_tokens,observed.provider_output_tokens);
    expected.provider_total_tokens:=greatest(OLD.provider_total_tokens,observed.provider_total_tokens);
    expected.cached_input_tokens:=greatest(OLD.cached_input_tokens,observed.cached_input_tokens);
    expected.reasoning_tokens:=greatest(OLD.reasoning_tokens,observed.reasoning_tokens);
    expected.artifact_bytes:=greatest(OLD.artifact_bytes,observed.artifact_bytes);
    expected.last_event_key:=observed.event_key;
    expected.event_count:=OLD.event_count+1;
  END IF;
  IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(expected) THEN
    RAISE EXCEPTION 'usage projection differs from immutable observations' USING ERRCODE='55000';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER usage_projection_derived BEFORE INSERT OR UPDATE OR DELETE ON usage.attempts
  FOR EACH ROW EXECUTE FUNCTION usage.guard_projection();
CREATE TRIGGER usage_projection_no_truncate BEFORE TRUNCATE ON usage.attempts
  FOR EACH STATEMENT EXECUTE FUNCTION usage.guard_projection();

CREATE FUNCTION usage.project_event() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage AS $$
BEGIN
  INSERT INTO usage.attempts AS prior (
    owner_user_id,module,task_id,attempt_id,kind,root_task_id,parent_task_id,source,status,submitted_at,started_at,finished_at,queue_ms,execution_ms,observed_wall_ms,provider_input_tokens,provider_output_tokens,provider_total_tokens,cached_input_tokens,reasoning_tokens,artifact_bytes,
    first_occurred_at,last_occurred_at,last_recorded_at,last_source,event_count,last_event_key
  ) VALUES (
    NEW.owner_user_id,NEW.module,NEW.task_id,NEW.attempt_id,NEW.kind,NEW.root_task_id,NEW.parent_task_id,NEW.source,NEW.status,NEW.submitted_at,NEW.started_at,NEW.finished_at,NEW.queue_ms,NEW.execution_ms,NEW.observed_wall_ms,NEW.provider_input_tokens,NEW.provider_output_tokens,NEW.provider_total_tokens,NEW.cached_input_tokens,NEW.reasoning_tokens,NEW.artifact_bytes,
    NEW.occurred_at,NEW.occurred_at,NEW.recorded_at,NEW.source,1,NEW.event_key
  ) ON CONFLICT (owner_user_id,module,task_id,attempt_id,kind) DO UPDATE SET
    status=CASE
      WHEN prior.status NOT IN ('queued','running') THEN prior.status
      WHEN EXCLUDED.status NOT IN ('queued','running') THEN EXCLUDED.status
      WHEN prior.status='running' OR EXCLUDED.status='running' THEN 'running'
      ELSE 'queued' END,
    first_occurred_at=least(prior.first_occurred_at,EXCLUDED.first_occurred_at),
    last_occurred_at=greatest(prior.last_occurred_at,EXCLUDED.last_occurred_at),
    last_recorded_at=greatest(prior.last_recorded_at,EXCLUDED.last_recorded_at),
    last_source=CASE WHEN EXCLUDED.last_occurred_at >= prior.last_occurred_at
      THEN EXCLUDED.last_source ELSE prior.last_source END,
    submitted_at=least(prior.submitted_at,EXCLUDED.submitted_at),
    started_at=least(prior.started_at,EXCLUDED.started_at),
    finished_at=least(prior.finished_at,EXCLUDED.finished_at),
    queue_ms=greatest(prior.queue_ms,EXCLUDED.queue_ms),
    execution_ms=greatest(prior.execution_ms,EXCLUDED.execution_ms),
    observed_wall_ms=greatest(prior.observed_wall_ms,EXCLUDED.observed_wall_ms),
    provider_input_tokens=greatest(prior.provider_input_tokens,EXCLUDED.provider_input_tokens),
    provider_output_tokens=greatest(prior.provider_output_tokens,EXCLUDED.provider_output_tokens),
    provider_total_tokens=greatest(prior.provider_total_tokens,EXCLUDED.provider_total_tokens),
    cached_input_tokens=greatest(prior.cached_input_tokens,EXCLUDED.cached_input_tokens),
    reasoning_tokens=greatest(prior.reasoning_tokens,EXCLUDED.reasoning_tokens),
    artifact_bytes=greatest(prior.artifact_bytes,EXCLUDED.artifact_bytes),
    last_event_key=EXCLUDED.last_event_key,
    event_count=prior.event_count+1
  WHERE prior.root_task_id=EXCLUDED.root_task_id
    AND prior.parent_task_id IS NOT DISTINCT FROM EXCLUDED.parent_task_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'usage attempt identity has conflicting ancestry' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER usage_event_projection AFTER INSERT ON usage.events
  FOR EACH ROW EXECUTE FUNCTION usage.project_event();

ALTER TABLE usage.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage.events FORCE ROW LEVEL SECURITY;
ALTER TABLE usage.attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage.attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_access ON usage.events TO nexpoly_api
  USING (owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid)
  WITH CHECK (owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY owner_access ON usage.attempts TO nexpoly_api
  USING (owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid)
  WITH CHECK (owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY service_access ON usage.events TO nexpoly_service USING (true) WITH CHECK (true);
CREATE POLICY service_access ON usage.attempts TO nexpoly_service USING (true) WITH CHECK (true);
CREATE POLICY audit_access ON usage.events FOR SELECT TO nexpoly_mutable_audit USING (true);
CREATE POLICY audit_access ON usage.attempts FOR SELECT TO nexpoly_mutable_audit USING (true);
REVOKE ALL ON usage.events,usage.attempts FROM PUBLIC;
GRANT USAGE ON SCHEMA usage TO nexpoly_api,nexpoly_service,nexpoly_mutable_audit;
GRANT SELECT,INSERT ON usage.events TO nexpoly_api,nexpoly_service;
GRANT SELECT,INSERT,UPDATE ON usage.attempts TO nexpoly_api,nexpoly_service;
GRANT SELECT ON usage.events,usage.attempts TO nexpoly_mutable_audit;
REVOKE ALL ON FUNCTION usage.reject_event_mutation(),usage.guard_projection(),usage.project_event() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION usage.reject_event_mutation(),usage.guard_projection(),usage.project_event()
  TO nexpoly_api,nexpoly_service;

COMMENT ON TABLE usage.events IS 'Immutable cumulative observations, no billing, quota, payloads, credentials or business FK';
COMMENT ON TABLE usage.attempts IS 'Invoker-derived projection; task/execution/provider metrics must not be blindly added together';
COMMENT ON COLUMN usage.attempts.artifact_bytes IS 'Observed generated artifact bytes, not current disk inventory';
