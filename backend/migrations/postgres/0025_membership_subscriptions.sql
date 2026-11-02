-- Calendar-month memberships; independent of usage collection and success limits.
-- Existing explicit free accounts stay free; pre-cutover members retain access.
CREATE SCHEMA billing;
REVOKE ALL ON SCHEMA billing FROM PUBLIC;

CREATE TABLE billing.accounts (
 owner_user_id uuid PRIMARY KEY REFERENCES auth.users(user_id),
 legacy_member boolean NOT NULL DEFAULT false,
 override_tier text CHECK (override_tier IN ('free','member')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO billing.accounts(owner_user_id,legacy_member)
 SELECT u.user_id,coalesce(e.tier,'member')='member'
 FROM auth.users u LEFT JOIN usage_entitlements.owner_entitlements e ON e.owner_user_id=u.user_id
 WHERE NOT u.is_system;

CREATE TABLE billing.subscriptions (
 owner_user_id uuid PRIMARY KEY REFERENCES billing.accounts(owner_user_id),
 paid_through timestamptz NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE billing.orders (
 id uuid PRIMARY KEY,
 owner_user_id uuid NOT NULL REFERENCES billing.accounts(owner_user_id),
 request_key text NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
 provider text NOT NULL CHECK (provider IN ('wechat','alipay')),
 merchant_id text NOT NULL CHECK (length(merchant_id) BETWEEN 1 AND 64),
 app_id text NOT NULL CHECK (length(app_id) BETWEEN 1 AND 64),
 amount_fen integer NOT NULL CHECK (amount_fen > 0),
 currency text NOT NULL DEFAULT 'CNY' CHECK (currency='CNY'),
 months integer NOT NULL DEFAULT 1 CHECK (months=1),
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','closed')),
 payment_id text CHECK (length(payment_id) BETWEEN 1 AND 128),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL,
 paid_at timestamptz,
 last_checked_at timestamptz,
 access_starts_at timestamptz,
 access_ends_at timestamptz,
 checkout_data jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(checkout_data)='object' AND octet_length(checkout_data::text)<=65536),
 UNIQUE(owner_user_id,request_key),
 UNIQUE(provider,payment_id),
 CHECK (expires_at>created_at),
 CHECK ((status='paid')=(paid_at IS NOT NULL)),
 CHECK ((status='paid')=(payment_id IS NOT NULL)),
 CHECK ((status='paid')=(access_starts_at IS NOT NULL AND access_ends_at IS NOT NULL)),
 CHECK (access_starts_at IS NULL OR access_starts_at<access_ends_at)
);
CREATE INDEX billing_orders_owner_created ON billing.orders(owner_user_id,created_at DESC);
CREATE TABLE billing.payment_events (
 provider text NOT NULL CHECK (provider IN ('wechat','alipay')),
 event_key text NOT NULL CHECK (length(event_key) BETWEEN 1 AND 256),
 order_id uuid NOT NULL REFERENCES billing.orders(id),
 payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(provider,event_key)
);

ALTER TABLE billing.accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing.accounts FORCE ROW LEVEL SECURITY;
ALTER TABLE billing.subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing.subscriptions FORCE ROW LEVEL SECURITY;
ALTER TABLE billing.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing.orders FORCE ROW LEVEL SECURITY;
ALTER TABLE billing.payment_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing.payment_events FORCE ROW LEVEL SECURITY;

CREATE POLICY owner_access ON billing.accounts FOR SELECT TO nexpoly_api USING(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY owner_access ON billing.subscriptions FOR SELECT TO nexpoly_api USING(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY owner_access ON billing.orders FOR SELECT TO nexpoly_api USING(owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid);
CREATE POLICY auth_insert ON billing.accounts FOR INSERT TO nexpoly_auth WITH CHECK (
 NOT legacy_member AND override_tier IS NULL AND EXISTS (
  SELECT FROM auth.users u WHERE u.user_id=owner_user_id AND NOT u.is_system));
CREATE POLICY service_access ON billing.accounts TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY service_access ON billing.subscriptions TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY service_access ON billing.orders TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY service_access ON billing.payment_events TO nexpoly_service USING(true) WITH CHECK(true);
CREATE POLICY audit_access ON billing.accounts FOR SELECT TO nexpoly_mutable_audit USING(true);
CREATE POLICY audit_access ON billing.subscriptions FOR SELECT TO nexpoly_mutable_audit USING(true);
CREATE POLICY audit_access ON billing.orders FOR SELECT TO nexpoly_mutable_audit USING(true);
CREATE POLICY audit_access ON billing.payment_events FOR SELECT TO nexpoly_mutable_audit USING(true);
REVOKE ALL ON ALL TABLES IN SCHEMA billing FROM PUBLIC;
GRANT USAGE ON SCHEMA billing TO nexpoly_api,nexpoly_service,nexpoly_mutable_audit;
GRANT SELECT ON billing.accounts,billing.subscriptions,billing.orders TO nexpoly_api;
GRANT SELECT ON ALL TABLES IN SCHEMA billing TO nexpoly_service,nexpoly_mutable_audit;
GRANT INSERT,UPDATE ON billing.accounts,billing.subscriptions,billing.orders TO nexpoly_service;
GRANT INSERT ON billing.payment_events TO nexpoly_service;
-- Registration has no ability to grant membership or change billing history.
GRANT INSERT(user_id,username,password_hash,must_change_password) ON auth.users TO nexpoly_auth;
GRANT USAGE ON SCHEMA billing TO nexpoly_auth;
GRANT INSERT(owner_user_id) ON billing.accounts TO nexpoly_auth;

CREATE FUNCTION billing.initialize_account() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,billing AS $$
BEGIN
 IF NOT NEW.is_system THEN
  INSERT INTO billing.accounts(owner_user_id) VALUES(NEW.user_id);
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER billing_initialize_account AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION billing.initialize_account();
REVOKE ALL ON FUNCTION billing.initialize_account() FROM PUBLIC;

CREATE FUNCTION billing.reject_event_mutation() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,billing AS $$
BEGIN
 RAISE EXCEPTION 'payment event receipts are immutable' USING ERRCODE='55000';
END;
$$;
CREATE TRIGGER billing_events_immutable BEFORE UPDATE OR DELETE ON billing.payment_events FOR EACH ROW EXECUTE FUNCTION billing.reject_event_mutation();
CREATE TRIGGER billing_events_no_truncate BEFORE TRUNCATE ON billing.payment_events FOR EACH STATEMENT EXECUTE FUNCTION billing.reject_event_mutation();
REVOKE ALL ON FUNCTION billing.reject_event_mutation() FROM PUBLIC;
