-- Independent M2 development authority, installed on the dedicated 0026 database only.
BEGIN;
CREATE SCHEMA openscience_m2;
REVOKE ALL ON SCHEMA openscience_m2 FROM PUBLIC;
GRANT USAGE ON SCHEMA openscience_m2 TO nexpoly_auth;

CREATE TABLE openscience_m2.environments (
    environment text PRIMARY KEY CHECK (environment LIKE 'dev-%'),
    enabled boolean NOT NULL DEFAULT false
);
CREATE TABLE openscience_m2.workspace_bindings (
    environment text NOT NULL REFERENCES openscience_m2.environments,
    user_id uuid NOT NULL REFERENCES auth.users,
    workspace_user_id text NOT NULL CHECK (length(workspace_user_id) BETWEEN 1 AND 128),
    enabled boolean NOT NULL DEFAULT false,
    PRIMARY KEY (environment, user_id),
    UNIQUE (environment, workspace_user_id)
);

-- A single-use handoff can create exactly one downstream session.
CREATE TABLE openscience_m2.handoffs (
    handoff_id uuid PRIMARY KEY,
    ticket_hash bytea NOT NULL UNIQUE CHECK (octet_length(ticket_hash) = 32),
    environment text NOT NULL,
    owner_id uuid NOT NULL,
    workspace_user_id text NOT NULL,
    login_id uuid NOT NULL REFERENCES auth.sessions ON DELETE CASCADE,
    parent_origin text NOT NULL,
    agent_origin text NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ticket_expires_at timestamptz NOT NULL,
    redeemed_at timestamptz,
    agent_hash bytea UNIQUE CHECK (octet_length(agent_hash) = 32),
    revoked_at timestamptz,
    FOREIGN KEY (environment, owner_id) REFERENCES openscience_m2.workspace_bindings ON DELETE CASCADE,
    CHECK ((agent_hash IS NULL) = (redeemed_at IS NULL))
);
CREATE INDEX handoffs_login ON openscience_m2.handoffs (login_id);
CREATE INDEX handoffs_owner ON openscience_m2.handoffs (environment, owner_id);
CREATE INDEX unused_ticket_expiry ON openscience_m2.handoffs (ticket_expires_at) WHERE redeemed_at IS NULL;
CREATE INDEX revoked_handoff_expiry ON openscience_m2.handoffs (revoked_at) WHERE revoked_at IS NOT NULL;
ALTER TABLE openscience_m2.handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE openscience_m2.handoffs FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_access ON openscience_m2.handoffs TO nexpoly_auth
    USING (true) WITH CHECK (true);
GRANT SELECT ON openscience_m2.environments, openscience_m2.workspace_bindings TO nexpoly_auth;
GRANT SELECT, INSERT, DELETE ON openscience_m2.handoffs TO nexpoly_auth;
GRANT UPDATE (redeemed_at, agent_hash, revoked_at) ON openscience_m2.handoffs TO nexpoly_auth;

-- Serialize ticket creation/consumption with membership of the deployment pilot.
-- Row locking requires UPDATE rights; expose the bounded read instead.
CREATE FUNCTION openscience_m2.lock_workspace(text, uuid) RETURNS text
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT b.workspace_user_id
      FROM openscience_m2.workspace_bindings b
      JOIN openscience_m2.environments e USING(environment)
     WHERE b.environment = $1 AND b.user_id = $2 AND b.enabled AND e.enabled
     FOR SHARE OF b,e;
$$;
REVOKE ALL ON FUNCTION openscience_m2.lock_workspace(text, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION openscience_m2.lock_workspace(text, uuid) TO nexpoly_auth;

CREATE FUNCTION openscience_m2.revoke_handoffs() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME = 'sessions' THEN
        UPDATE openscience_m2.handoffs SET revoked_at = clock_timestamp()
            WHERE login_id = OLD.session_id AND revoked_at IS NULL;
    ELSIF TG_TABLE_NAME = 'environments' THEN
        UPDATE openscience_m2.handoffs SET revoked_at = clock_timestamp()
            WHERE environment = OLD.environment AND revoked_at IS NULL;
    ELSE
        UPDATE openscience_m2.handoffs SET revoked_at = clock_timestamp()
            WHERE environment = OLD.environment AND owner_id = OLD.user_id AND revoked_at IS NULL;
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION openscience_m2.revoke_handoffs() FROM PUBLIC;
CREATE TRIGGER openscience_logout AFTER UPDATE OF revoked_at ON auth.sessions
    FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
    EXECUTE FUNCTION openscience_m2.revoke_handoffs();
CREATE TRIGGER openscience_disable_environment AFTER UPDATE ON openscience_m2.environments
    FOR EACH ROW WHEN (OLD.enabled AND NOT NEW.enabled)
    EXECUTE FUNCTION openscience_m2.revoke_handoffs();
CREATE TRIGGER openscience_rebind_or_disable AFTER UPDATE ON openscience_m2.workspace_bindings
    FOR EACH ROW WHEN ((OLD.enabled AND NOT NEW.enabled) OR OLD.workspace_user_id <> NEW.workspace_user_id)
    EXECUTE FUNCTION openscience_m2.revoke_handoffs();
COMMIT;
