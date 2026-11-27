-- Invalidate existing device authorizations when effective membership lapses.
-- A continuous extension of a paid subscription keeps the same login usable.
BEGIN;
CREATE FUNCTION openscience_m2.membership_transition() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    owner uuid := coalesce(NEW.owner_user_id, OLD.owner_user_id);
    paid boolean;
    tier text;
    legacy boolean;
    was_member boolean;
    is_member boolean;
    as_of timestamptz;
BEGIN
    -- Ticket creation and validation hold shared locks on these same bindings.
    -- An existing authorization cannot slip past a simultaneous revoke.
    PERFORM 1 FROM openscience_m2.workspace_bindings
        WHERE user_id=owner ORDER BY environment FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    as_of := clock_timestamp();
    IF TG_TABLE_NAME = 'accounts' THEN
        SELECT EXISTS (SELECT FROM billing.subscriptions WHERE owner_user_id=owner AND paid_through>as_of)
            INTO paid;
        was_member := coalesce(OLD.override_tier='member', OLD.legacy_member OR paid);
        is_member := coalesce(NEW.override_tier='member', NEW.legacy_member OR paid);
    ELSE
        SELECT a.override_tier,a.legacy_member INTO tier,legacy FROM billing.accounts a WHERE a.owner_user_id=owner;
        was_member := coalesce(tier='member', legacy OR coalesce(OLD.paid_through>as_of,false),false);
        is_member := coalesce(tier='member', legacy OR coalesce(NEW.paid_through>as_of,false),false);
    END IF;
    IF NOT was_member OR NOT is_member THEN
        UPDATE openscience_m2.handoffs SET revoked_at=as_of WHERE owner_id=owner AND revoked_at IS NULL;
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION openscience_m2.membership_transition() FROM PUBLIC;
CREATE TRIGGER openscience_member_access AFTER UPDATE ON billing.accounts
    FOR EACH ROW WHEN ((OLD.override_tier,OLD.legacy_member) IS DISTINCT FROM (NEW.override_tier,NEW.legacy_member))
    EXECUTE FUNCTION openscience_m2.membership_transition();
CREATE TRIGGER openscience_paid_access AFTER INSERT OR UPDATE OF paid_through OR DELETE ON billing.subscriptions
    FOR EACH ROW EXECUTE FUNCTION openscience_m2.membership_transition();
COMMIT;
