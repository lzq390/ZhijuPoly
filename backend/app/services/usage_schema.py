"""Read-only validation of the usage schema's isolation and immutable write path."""
from __future__ import annotations

from pathlib import Path
import re

VERSION = '0020_usage_metering'
_ROLES = ('nexpoly_api', 'nexpoly_service', 'nexpoly_mutable_audit')
_ACL = {
    'events': {'nexpoly_api': {'SELECT','INSERT'}, 'nexpoly_service': {'SELECT','INSERT'},
               'nexpoly_mutable_audit': {'SELECT'}},
    'attempts': {'nexpoly_api': {'SELECT','INSERT','UPDATE'}, 'nexpoly_service': {'SELECT','INSERT','UPDATE'},
                 'nexpoly_mutable_audit': {'SELECT'}},
}
_TRIGGERS = {
    'events': {'usage_events_immutable': ('reject_event_mutation',27),
               'usage_events_no_truncate': ('reject_event_mutation',34),
               'usage_event_projection': ('project_event',5)},
    'attempts': {'usage_projection_derived': ('guard_projection',31),
                 'usage_projection_no_truncate': ('guard_projection',34)},
}


def _require(condition, label):
    if not condition:
        raise ValueError('Usage schema contract mismatch: '+label)


def _expression(value):
    return re.sub(r'[\s()]', '', value or '').replace('::text','').lower()


def validate_usage_schema(connection):
    """Check exact usage objects and privileges; no migration or corrective SQL.

    The application ledger validator separately pins the entire migration chain.
    Migration source is used to pin the invoker trigger bodies, never secrets.
    """
    from psycopg.rows import dict_row
    migration = Path(__file__).resolve().parents[2]/'migrations/postgres'/f'{VERSION}.sql'
    source = migration.read_text(encoding='utf-8')
    expected_functions = {
        name: body.strip() for name, body in re.findall(
            r'CREATE FUNCTION usage\.(\w+)\(\) RETURNS trigger\s+LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage AS \$\$(.*?)\$\$;',
            source, re.S)
    }
    _require(set(expected_functions)=={'reject_event_mutation','guard_projection','project_event'}, 'release function definitions')
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SELECT oid,nspowner FROM pg_namespace WHERE nspname='usage'")
        schema = cursor.fetchone()
        _require(schema is not None, 'missing usage schema')
        cursor.execute("""SELECT r.rolname,has_schema_privilege(r.oid,'usage','USAGE') AS can_use,
            has_schema_privilege(r.oid,'usage','CREATE') AS can_create,
            pg_has_role(r.oid,%s,'MEMBER') AS owns_schema
            FROM pg_roles r WHERE r.rolname=ANY(%s)""",(schema['nspowner'],list(_ROLES)))
        roles=cursor.fetchall()
        _require(len(roles)==3 and all(row['can_use'] and not row['can_create'] and not row['owns_schema'] for row in roles), 'schema privileges')
        cursor.execute("""SELECT a.grantee,r.rolname,a.privilege_type,a.is_grantable
            FROM pg_namespace n CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a
            LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE n.nspname='usage' AND a.grantee<>n.nspowner""")
        schema_acl=cursor.fetchall()
        _require(len(schema_acl)==3 and {(r['rolname'],r['privilege_type'],r['is_grantable']) for r in schema_acl}
                 =={(role,'USAGE',False) for role in _ROLES}, 'exact schema ACL')
        cursor.execute("""SELECT c.oid,c.relname,c.relowner,c.relkind,c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c WHERE c.relnamespace=%s AND c.relkind IN ('r','p','v','m','f')""",(schema['oid'],))
        tables=cursor.fetchall()
        _require({r['relname'] for r in tables}=={'events','attempts'}, 'relations')
        for table in tables:
            name,oid=table['relname'],table['oid']
            _require(table['relkind']=='r' and table['relrowsecurity'] and table['relforcerowsecurity'],name+' forced RLS')
            cursor.execute("SELECT rolname FROM pg_roles WHERE rolname=ANY(%s) AND pg_has_role(oid,%s,'MEMBER')",(list(_ROLES),table['relowner']))
            _require(not cursor.fetchall(),name+' ownership')
            cursor.execute("""SELECT p.polname,p.polcmd,p.polpermissive,
                ARRAY(SELECT r.rolname FROM unnest(p.polroles) x JOIN pg_roles r ON r.oid=x ORDER BY r.rolname) AS roles,
                pg_get_expr(p.polqual,p.polrelid) AS using_expr,
                pg_get_expr(p.polwithcheck,p.polrelid) AS check_expr
                FROM pg_policy p WHERE p.polrelid=%s""",(oid,))
            policies={row['polname']:row for row in cursor.fetchall()}
            _require(set(policies)=={'owner_access','service_access','audit_access'},name+' policy names')
            owner_expr=_expression("owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid")
            for key,role,command,expression in [('owner_access','nexpoly_api','*',owner_expr),
                ('service_access','nexpoly_service','*','true'),('audit_access','nexpoly_mutable_audit','r','true')]:
                row=policies[key]
                _require(row['roles']==[role] and row['polcmd']==command and row['polpermissive']
                    and _expression(row['using_expr'])==expression
                    and (row['check_expr'] is None if key=='audit_access' else _expression(row['check_expr'])==expression),name+' '+key)
            cursor.execute("""SELECT r.rolname,a.privilege_type,a.is_grantable
                FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
                LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE c.oid=%s AND a.grantee<>c.relowner""",(oid,))
            actual={(r['rolname'],r['privilege_type'],r['is_grantable']) for r in cursor.fetchall()}
            expected={(role,privilege,False) for role,privileges in _ACL[name].items() for privilege in privileges}
            _require(actual==expected,name+' exact table ACL')
            cursor.execute("SELECT 1 FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND attacl IS NOT NULL",(oid,))
            _require(not cursor.fetchone(),name+' unexpected column grants')
            cursor.execute("""SELECT ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum,n)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum ORDER BY k.n) AS columns
                FROM pg_constraint c WHERE c.conrelid=%s AND c.contype='p'""",(oid,))
            primary=cursor.fetchall()
            expected_pk=['owner_user_id','event_key'] if name=='events' else ['owner_user_id','module','task_id','attempt_id','kind']
            _require(len(primary)==1 and primary[0]['columns']==expected_pk,name+' primary key')
            cursor.execute("SELECT 1 FROM pg_constraint WHERE conrelid=%s AND contype='f'",(oid,))
            _require(not cursor.fetchone(),name+' unexpected foreign key')
            cursor.execute("""SELECT t.tgname,t.tgtype,t.tgenabled,t.tgnargs,t.tgqual,
                p.proname,n.nspname FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
                JOIN pg_namespace n ON n.oid=p.pronamespace WHERE t.tgrelid=%s AND NOT t.tgisinternal""",(oid,))
            triggers=cursor.fetchall()
            _require(len(triggers)==len(_TRIGGERS[name]),name+' trigger count')
            for trigger in triggers:
                _require((trigger['proname'],trigger['tgtype'])==_TRIGGERS[name].get(trigger['tgname'])
                    and trigger['nspname']=='usage' and trigger['tgenabled']=='O'
                    and trigger['tgnargs']==0 and trigger['tgqual'] is None,name+' trigger '+trigger['tgname'])
        cursor.execute("""SELECT p.oid,p.proname,p.pronargs,p.prosecdef,p.proconfig,p.prosrc,p.proowner,
            l.lanname,p.prorettype='pg_catalog.trigger'::regtype AS returns_trigger
            FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang WHERE p.pronamespace=%s""",(schema['oid'],))
        functions=cursor.fetchall()
        _require(len(functions)==3 and {f['proname'] for f in functions}==set(expected_functions),'functions')
        for function in functions:
            _require(function['pronargs']==0 and not function['prosecdef'] and function['lanname']=='plpgsql'
                and function['returns_trigger'] and function['prosrc'].strip()==expected_functions[function['proname']]
                and [s.replace(' ','') for s in function['proconfig'] or []]==['search_path=pg_catalog,usage'],function['proname']+' invoker/body/search_path')
            cursor.execute("""SELECT r.rolname,a.privilege_type,a.is_grantable
                FROM pg_proc p CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
                LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid=%s AND a.grantee<>p.proowner""",(function['oid'],))
            _require({(r['rolname'],r['privilege_type'],r['is_grantable']) for r in cursor.fetchall()}
                =={('nexpoly_api','EXECUTE',False),('nexpoly_service','EXECUTE',False)},function['proname']+' exact ACL')
    return {'version':VERSION,'validated':True,'relations':['usage.events','usage.attempts']}
