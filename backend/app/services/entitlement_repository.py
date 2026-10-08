"""Backend connection adapter for worker-safe entitlement functions."""
from task_usage import entitlements as core
from app.postgres_database import postgres_connection
from task_usage.entitlements import EntitlementError, EntitlementConflict, EntitlementLimitExceeded, EntitlementPolicyError

class EntitlementRepository:
    def __init__(self, dsn=None):
        self.dsn = dsn
    def _call(self, name, connection=None, **kwargs):
        method = getattr(core, name)
        if not core.enabled():
            return method(None, **kwargs)
        if connection is not None:
            return method(connection, **kwargs)
        with postgres_connection(self.dsn) as active:
            return method(active, **kwargs)

def _method(name):
    def invoke(self, *, connection=None, **kwargs):
        return self._call(name, connection=connection, **kwargs)
    invoke.__name__ = name
    return invoke

for _name in ("lookup", "reserve", "bind_native", "begin_dispatch", "claim_running",
              "settle", "release", "mark_unknown", "get", "get_by_native",
              "list_operations", "summary", "set_owner_tier", "get_owner_tier", "get_policy", "record_request", "list_request_counts"):
    setattr(EntitlementRepository, _name, _method(_name))

def _resolve_access_tier(self, *, owner_user_id, connection=None):
    """Authorization is independent of the quota collection feature flag."""
    from app.auth.isolation_ledger import validate_application_migration_ledger, ENTITLEMENT_VERSION
    from task_usage.entitlements import resolve_access_tier as resolve_worker_tier
    def read(active):
        from psycopg.rows import dict_row
        with active.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT to_regclass('usage_entitlements.owner_entitlements') AS relation,to_regclass('governance.schema_migrations') AS ledger")
            objects=cursor.fetchone()
            if objects['relation'] is None:
                if objects['ledger'] is not None:
                    cursor.execute("SELECT 1 FROM governance.schema_migrations WHERE version=%s", (ENTITLEMENT_VERSION,))
                    if cursor.fetchone():
                        raise ValueError('Entitlement migration claims a missing access-tier table')
                return "member"
        ledger=validate_application_migration_ledger(active, require_usage=False, require_entitlements=False)
        if ledger[-1]["version"]!=ENTITLEMENT_VERSION:
            raise ValueError('Access-tier table has no exact entitlement migration')
        from app.services.entitlement_schema import validate_entitlement_schema
        validate_entitlement_schema(active)
        return resolve_worker_tier(active, owner_user_id=owner_user_id)
    if connection is not None: return read(connection)
    with postgres_connection(self.dsn) as active: return read(active)
EntitlementRepository.resolve_access_tier = _resolve_access_tier

def _saved_schema_available(active):
    """Historical private results remain readable when collection is disabled."""
    from psycopg.rows import dict_row
    from app.auth.isolation_ledger import validate_application_migration_ledger, ENTITLEMENT_VERSION
    from app.services.entitlement_schema import validate_entitlement_schema
    with active.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT to_regclass('usage_entitlements.operations') AS operations, to_regclass('usage_entitlements.owner_entitlements') AS tiers, to_regclass('governance.schema_migrations') AS ledger")
        objects=cursor.fetchone()
        if objects["operations"] is None and objects["tiers"] is None:
            if objects["ledger"] is not None:
                cursor.execute("SELECT 1 FROM governance.schema_migrations WHERE version=%s",(ENTITLEMENT_VERSION,))
                if cursor.fetchone():
                    raise ValueError("Entitlement migration claims missing saved-operation tables")
            return False
    ledger=validate_application_migration_ledger(active,require_usage=False,require_entitlements=False)
    if ledger[-1]["version"]!=ENTITLEMENT_VERSION:
        raise ValueError("Saved entitlement schema has no exact known migration")
    validate_entitlement_schema(active)
    return True

def _read_saved(self,name,*,owner_user_id,connection=None,**kwargs):
    from task_usage import _entitlement_store as store
    def read(active):
        if not _saved_schema_available(active):
            return [] if name=="list_operations" else None
        return getattr(store,name)(active,owner_user_id=owner_user_id,**kwargs)
    if connection is not None:
        return read(connection)
    with postgres_connection(self.dsn,readonly_snapshot=True) as active:
        return read(active)

def _read_saved_operation(self,*,owner_user_id,operation_id,connection=None):
    return _read_saved(self,"get",owner_user_id=owner_user_id,operation_id=operation_id,connection=connection)

def _read_saved_operations(self,*,owner_user_id,module=None,limit=50,offset=0,connection=None):
    return _read_saved(self,"list_operations",owner_user_id=owner_user_id,module=module,limit=limit,offset=offset,connection=connection)

EntitlementRepository.read_saved_operation = _read_saved_operation
EntitlementRepository.read_saved_operations = _read_saved_operations
