"""Read-only membership storage, registration and least-privilege contracts."""
from pathlib import Path
import hashlib
import re

from psycopg.rows import dict_row
from task_usage.billing_contract import BILLING_VERSION, BILLING_CHECKSUM

_ROLES = ('nexpoly_api', 'nexpoly_auth', 'nexpoly_service', 'nexpoly_mutable_audit')
_COLUMNS = {
    'accounts': 'owner_user_id legacy_member override_tier created_at updated_at',
    'subscriptions': 'owner_user_id paid_through updated_at',
    'orders': 'id owner_user_id request_key provider merchant_id app_id amount_fen currency months status payment_id created_at expires_at paid_at last_checked_at access_starts_at access_ends_at checkout_data',
    'payment_events': 'provider event_key order_id payload_digest created_at',
}
_NULLABLE = {'accounts': {'override_tier'}, 'subscriptions': set(),
             'orders': {'payment_id', 'paid_at', 'last_checked_at', 'access_starts_at', 'access_ends_at'}, 'payment_events': set()}
_CONSTRAINTS = {
    'accounts': ["CHECK (override_tier = ANY(ARRAY['free','member']))", 'PRIMARY KEY(owner_user_id)',
                 'FOREIGN KEY(owner_user_id) REFERENCES auth.users(user_id)'],
    'subscriptions': ['PRIMARY KEY(owner_user_id)', 'FOREIGN KEY(owner_user_id) REFERENCES billing.accounts(owner_user_id)'],
    'orders': ['PRIMARY KEY(id)', 'FOREIGN KEY(owner_user_id) REFERENCES billing.accounts(owner_user_id)',
               'UNIQUE(owner_user_id,request_key)', 'UNIQUE(provider,payment_id)',
               "CHECK(provider = ANY(ARRAY['wechat','alipay']))", "CHECK(status = ANY(ARRAY['pending','paid','closed']))",
               'CHECK(length(request_key)>=1 AND length(request_key)<=128)',
               'CHECK(length(payment_id)>=1 AND length(payment_id)<=128)',
               'CHECK(length(merchant_id)>=1 AND length(merchant_id)<=64)',
               'CHECK(length(app_id)>=1 AND length(app_id)<=64)',
               'CHECK(amount_fen>0)', "CHECK(currency='CNY')", 'CHECK(months=1)', 'CHECK(expires_at>created_at)',
               "CHECK((status='paid')=(paid_at IS NOT NULL))", "CHECK((status='paid')=(payment_id IS NOT NULL))",
               "CHECK((status='paid')=(access_starts_at IS NOT NULL AND access_ends_at IS NOT NULL))",
               'CHECK(access_starts_at IS NULL OR access_starts_at<access_ends_at)',
               "CHECK(jsonb_typeof(checkout_data)='object' AND octet_length(checkout_data::text)<=65536)"],
    'payment_events': ['PRIMARY KEY(provider,event_key)', 'FOREIGN KEY(order_id) REFERENCES billing.orders(id)',
                       "CHECK(provider = ANY(ARRAY['wechat','alipay']))",
                       'CHECK(length(event_key)>=1 AND length(event_key)<=256)', "CHECK(payload_digest ~ '^[0-9a-f]{64}$')"],
}


def _require(value, reason):
    if not value:
        raise ValueError('Membership schema contract mismatch: ' + reason)


def _expression(value):
    return re.sub(r'[\s()]', '', value or '').replace('::text', '').lower()


def validate_billing_schema(connection):
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT current_setting('search_path') AS value")
            previous = cursor.fetchone()['value']
            cursor.execute('SET LOCAL search_path=pg_catalog')
            result = _validate_billing_schema(connection)
            cursor.execute("SELECT set_config('search_path',%s,true)", (previous,))
            return result


def _validate_billing_schema(connection):
    source = (Path(__file__).resolve().parents[2] / 'migrations/postgres' / (BILLING_VERSION + '.sql')).read_bytes()
    _require(hashlib.sha256(source).hexdigest() == BILLING_CHECKSUM, 'release source checksum')
    functions = dict(re.findall(r'CREATE FUNCTION billing\.(\w+)\(\) RETURNS trigger\s+LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,billing AS \$\$(.*?)\$\$;', source.decode(), re.S))
    with connection.cursor(row_factory=dict_row) as c:
        c.execute("SELECT oid,nspowner FROM pg_namespace WHERE nspname='billing'")
        schema = c.fetchone()
        _require(schema is not None, 'missing schema')
        c.execute("""SELECT r.rolname,a.privilege_type,a.is_grantable FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a
            LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE n.oid=%s AND a.grantee<>n.nspowner""", (schema['oid'],))
        _require({(r['rolname'], r['privilege_type'], r['is_grantable']) for r in c}
                 == {(r, 'USAGE', False) for r in _ROLES}, 'schema ACL')
        c.execute("SELECT oid,relname,relowner,relkind,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace=%s AND relkind IN ('r','p','v','m','f')", (schema['oid'],))
        tables = c.fetchall()
        _require({r['relname'] for r in tables} == set(_COLUMNS), 'table inventory')
        for table in tables:
            name, oid = table['relname'], table['oid']
            _require(table['relkind'] == 'r' and table['relrowsecurity'] and table['relforcerowsecurity'], name + ' forced RLS')
            c.execute("SELECT rolname FROM pg_roles WHERE rolname=ANY(%s) AND (pg_has_role(oid,%s,'MEMBER') OR pg_has_role(oid,%s,'MEMBER'))", (list(_ROLES), table['relowner'], schema['nspowner']))
            _require(not c.fetchall(), name + ' runtime ownership')
            c.execute("SELECT attname,attnotnull,format_type(atttypid,atttypmod) AS type FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum", (oid,))
            columns = c.fetchall()
            _require([r['attname'] for r in columns] == _COLUMNS[name].split(), name + ' columns')
            _require({r['attname'] for r in columns if not r['attnotnull']} == _NULLABLE[name], name + ' nullable columns')
            for column in columns:
                field = column['attname']
                expected_type = ('uuid' if field in {'id', 'owner_user_id', 'order_id'} else
                                 'integer' if field in {'amount_fen', 'months'} else
                                 'boolean' if field == 'legacy_member' else
                                 'jsonb' if field == 'checkout_data' else
                                 'timestamp with time zone' if field.endswith('_at') or field == 'paid_through' else 'text')
                _require(column['type'] == expected_type, name + '.' + field + ' type')
            c.execute('SELECT convalidated,condeferrable,pg_get_constraintdef(oid,true) AS definition FROM pg_constraint WHERE conrelid=%s', (oid,))
            constraints = c.fetchall()
            _require(len(constraints) == len(_CONSTRAINTS[name]) and all(r['convalidated'] and not r['condeferrable'] for r in constraints)
                     and {_expression(r['definition']) for r in constraints} == {_expression(r) for r in _CONSTRAINTS[name]}, name + ' constraints')
            c.execute("""SELECT r.rolname,a.privilege_type,a.is_grantable FROM pg_class t
                CROSS JOIN LATERAL aclexplode(coalesce(t.relacl,acldefault('r',t.relowner))) a
                LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE t.oid=%s AND a.grantee<>t.relowner""", (oid,))
            acl = {('nexpoly_service', 'SELECT', False), ('nexpoly_service', 'INSERT', False), ('nexpoly_mutable_audit', 'SELECT', False)}
            if name != 'payment_events':
                acl |= {('nexpoly_service', 'UPDATE', False), ('nexpoly_api', 'SELECT', False)}
            _require({(r['rolname'], r['privilege_type'], r['is_grantable']) for r in c} == acl, name + ' table ACL')
            c.execute("""SELECT a.attname,r.rolname,x.privilege_type,x.is_grantable FROM pg_attribute a
                CROSS JOIN LATERAL aclexplode(a.attacl) x LEFT JOIN pg_roles r ON r.oid=x.grantee
                WHERE a.attrelid=%s AND a.attnum>0""", (oid,))
            expected_columns = {('owner_user_id', 'nexpoly_auth', 'INSERT', False)} if name == 'accounts' else set()
            _require({tuple(r.values()) for r in c} == expected_columns, name + ' column ACL')
            c.execute("""SELECT p.polname,p.polcmd,p.polpermissive,
                ARRAY(SELECT r.rolname FROM unnest(p.polroles) x JOIN pg_roles r ON r.oid=x ORDER BY r.rolname) AS roles,
                pg_get_expr(p.polqual,p.polrelid) AS using_expr,pg_get_expr(p.polwithcheck,p.polrelid) AS check_expr
                FROM pg_policy p WHERE p.polrelid=%s""", (oid,))
            actual = {r['polname']: r for r in c}
            expected = {
                'service_access': ('*', ['nexpoly_service'], 'true', 'true'),
                'audit_access': ('r', ['nexpoly_mutable_audit'], 'true', None),
            }
            if name != 'payment_events':
                expected['owner_access'] = ('r', ['nexpoly_api'], "owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid", None)
            if name == 'accounts':
                expected['auth_insert'] = ('a', ['nexpoly_auth'], None, 'NOT legacy_member AND override_tier IS NULL AND EXISTS(SELECT FROM auth.users u WHERE u.user_id=accounts.owner_user_id AND NOT u.is_system)')
            _require(set(actual) == set(expected), name + ' policies')
            for key, (command, roles, using, check) in expected.items():
                row = actual[key]
                _require(row['polcmd'] == command and row['roles'] == roles and row['polpermissive']
                         and _expression(row['using_expr']) == _expression(using)
                         and _expression(row['check_expr']) == _expression(check), name + ' ' + key)
        c.execute("""SELECT a.attname,pg_get_expr(d.adbin,d.adrelid) AS value FROM pg_attribute a
            JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE a.attrelid='billing.accounts'::regclass""")
        defaults = {r['attname']: r['value'] for r in c}
        _require(defaults == {'legacy_member': 'false', 'created_at': 'clock_timestamp()', 'updated_at': 'clock_timestamp()'}, 'free registration defaults')
        c.execute("""SELECT p.oid,p.proname,p.pronargs,p.prosecdef,p.proconfig,p.prosrc,p.proowner,l.lanname,
            p.prorettype='pg_catalog.trigger'::regtype AS returns_trigger FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
            WHERE p.pronamespace=%s""", (schema['oid'],))
        actual = c.fetchall()
        _require({r['proname'] for r in actual} == set(functions) == {'initialize_account', 'reject_event_mutation'}, 'functions')
        for row in actual:
            _require(row['pronargs'] == 0 and not row['prosecdef'] and row['lanname'] == 'plpgsql' and row['returns_trigger']
                     and row['prosrc'].strip() == functions[row['proname']].strip()
                     and [s.replace(' ', '') for s in row['proconfig'] or []] == ['search_path=pg_catalog,billing'], row['proname'] + ' invoker body')
            c.execute("""SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                WHERE p.oid=%s AND a.grantee<>p.proowner""", (row['oid'],))
            _require(c.fetchone() is None, row['proname'] + ' function ACL')
        c.execute("""SELECT t.tgname,t.tgtype,t.tgenabled,t.tgnargs,t.tgqual,p.proname,t.tgrelid::regclass::text AS relation
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid WHERE p.pronamespace=%s AND NOT t.tgisinternal""", (schema['oid'],))
        triggers = c.fetchall()
        expected = {('billing_initialize_account', 5, 'initialize_account', 'auth.users'),
                    ('billing_events_immutable', 27, 'reject_event_mutation', 'billing.payment_events'),
                    ('billing_events_no_truncate', 34, 'reject_event_mutation', 'billing.payment_events')}
        _require({(r['tgname'], r['tgtype'], r['proname'], r['relation']) for r in triggers} == expected
                 and all(r['tgenabled'] == 'O' and r['tgnargs'] == 0 and r['tgqual'] is None for r in triggers), 'registration/event triggers')
        c.execute("""SELECT a.attname FROM pg_attribute a WHERE a.attrelid=(SELECT t.oid FROM pg_class t
            JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='auth' AND t.relname='users') AND a.attnum>0
            AND has_column_privilege('nexpoly_auth',a.attrelid,a.attnum,'INSERT')""")
        _require({r['attname'] for r in c} == {'user_id', 'username', 'password_hash', 'must_change_password'}, 'registration INSERT columns')
    return {'version': BILLING_VERSION, 'validated': True}
