-- Apply only through app.auth.service_privileges after the exact 0018 ledger.
-- Preserve the UPDATE privilege needed to lock a user before start authorization.
REVOKE SELECT ON auth.users FROM nexpoly_service;
GRANT SELECT(user_id,status,is_system) ON auth.users TO nexpoly_service;
GRANT UPDATE(updated_at) ON auth.users TO nexpoly_service;
