"""Read-only exact contract of the new entitlement schema; no corrective SQL."""
from pathlib import Path
import re
from psycopg.rows import dict_row
from task_usage.entitlements import MIGRATION_VERSION as VERSION
from app.services.usage_schema import _require, _expression
_ROLES=('nexpoly_api','nexpoly_service','nexpoly_mutable_audit')
_ACL={
 'policy_versions':{'nexpoly_api':{'SELECT'},'nexpoly_service':{'SELECT','INSERT'},'nexpoly_mutable_audit':{'SELECT'}},
 'owner_entitlements':{'nexpoly_api':{'SELECT'},'nexpoly_service':{'SELECT','INSERT','UPDATE'},'nexpoly_mutable_audit':{'SELECT'}},
 'operations':{'nexpoly_api':{'SELECT','INSERT','UPDATE'},'nexpoly_service':{'SELECT','INSERT','UPDATE'},'nexpoly_mutable_audit':{'SELECT'}},
 'operation_events':{'nexpoly_api':{'SELECT','INSERT'},'nexpoly_service':{'SELECT','INSERT'},'nexpoly_mutable_audit':{'SELECT'}},
 'request_observations':{'nexpoly_api':{'SELECT','INSERT'},'nexpoly_service':{'SELECT','INSERT'},'nexpoly_mutable_audit':{'SELECT'}},
}
_PKS={'policy_versions':['version'],'owner_entitlements':['owner_user_id'],
 'operations':['owner_user_id','operation_id'],'operation_events':['owner_user_id','event_key'],
 'request_observations':['owner_user_id','request_id','phase']}
_TRIGGERS={
 'policy_versions':{'entitlement_policy_immutable':('reject_mutation',27),'entitlement_policy_no_truncate':('reject_mutation',34)},
 'owner_entitlements':{},
 'operations':{'entitlement_projection_guard':('guard_projection',31),'entitlement_projection_no_truncate':('guard_projection',34)},
 'operation_events':{'entitlement_events_immutable':('reject_mutation',27),'entitlement_events_no_truncate':('reject_mutation',34),
   'entitlement_event_validate':('validate_event',7),'entitlement_event_projection':('project_event',5)},
 'request_observations':{'entitlement_request_immutable':('reject_mutation',27),'entitlement_request_no_truncate':('reject_mutation',34)},
}

def validate_entitlement_schema(connection):
 source=(Path(__file__).resolve().parents[2]/'migrations/postgres'/f'{VERSION}.sql').read_text()
 functions=dict(re.findall(r'CREATE FUNCTION usage_entitlements\.(\w+)\(\) RETURNS trigger\s+LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog,usage_entitlements AS \$\$(.*?)\$\$;',source,re.S))
 _require(set(functions)=={'reject_mutation','validate_event','guard_projection','project_event'},'entitlement release functions')
 with connection.cursor(row_factory=dict_row) as c:
  c.execute("SELECT oid,nspowner FROM pg_namespace WHERE nspname='usage_entitlements'")
  schema=c.fetchone();_require(schema is not None,'entitlement schema missing')
  c.execute("""SELECT r.rolname,has_schema_privilege(r.oid,'usage_entitlements','USAGE') AS can_use,
    has_schema_privilege(r.oid,'usage_entitlements','CREATE') AS can_create,
    pg_has_role(r.oid,%s,'MEMBER') AS owns_schema FROM pg_roles r WHERE r.rolname=ANY(%s)""",(schema['nspowner'],list(_ROLES)))
  roles=c.fetchall()
  _require(len(roles)==3 and all(r['can_use'] and not r['can_create'] and not r['owns_schema'] for r in roles),'entitlement schema authority')
  c.execute("""SELECT r.rolname,a.privilege_type,a.is_grantable FROM pg_namespace n
    CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a
    LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE n.oid=%s AND a.grantee<>n.nspowner""",(schema['oid'],))
  _require({(r['rolname'],r['privilege_type'],r['is_grantable']) for r in c}=={(r,'USAGE',False) for r in _ROLES},'entitlement exact schema ACL')
  c.execute("SELECT oid,relname,relowner,relkind,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace=%s AND relkind IN ('r','p','v','m','f')",(schema['oid'],))
  tables=c.fetchall();_require({r['relname'] for r in tables}==set(_ACL),'entitlement relations')
  for table in tables:
   name,oid=table['relname'],table['oid']
   _require(table['relkind']=='r' and table['relrowsecurity'] and table['relforcerowsecurity'],name+' forced RLS')
   c.execute("SELECT rolname FROM pg_roles WHERE rolname=ANY(%s) AND pg_has_role(oid,%s,'MEMBER')",(list(_ROLES),table['relowner']))
   _require(not c.fetchall(),name+' ownership')
   c.execute("""SELECT p.polname,p.polcmd,p.polpermissive,
    ARRAY(SELECT r.rolname FROM unnest(p.polroles) x JOIN pg_roles r ON r.oid=x ORDER BY r.rolname) AS roles,
    pg_get_expr(p.polqual,p.polrelid) AS using_expr,pg_get_expr(p.polwithcheck,p.polrelid) AS check_expr
    FROM pg_policy p WHERE p.polrelid=%s""",(oid,))
   policies={r['polname']:r for r in c};_require(set(policies)=={'owner_access','service_access','audit_access'},name+' policy names')
   owner_expr=_expression("owner_user_id=nullif(current_setting('app.user_id',true),'')::uuid")
   for key,role,cmd,expr in [('owner_access','nexpoly_api','r' if name=='policy_versions' else '*','true' if name=='policy_versions' else owner_expr),
     ('service_access','nexpoly_service','*','true'),('audit_access','nexpoly_mutable_audit','r','true')]:
    r=policies[key]
    _require(r['roles']==[role] and r['polcmd']==cmd and r['polpermissive'] and _expression(r['using_expr'])==expr
      and (r['check_expr'] is None if cmd=='r' else _expression(r['check_expr'])==expr),name+' '+key)
   c.execute("""SELECT r.rolname,a.privilege_type,a.is_grantable FROM pg_class c
    CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
    LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE c.oid=%s AND a.grantee<>c.relowner""",(oid,))
   _require({(r['rolname'],r['privilege_type'],r['is_grantable']) for r in c}
     =={(role,p,False) for role,privileges in _ACL[name].items() for p in privileges},name+' exact ACL')
   c.execute("SELECT 1 FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND attacl IS NOT NULL",(oid,))
   _require(not c.fetchone(),name+' column ACL')
   c.execute("""SELECT ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum,n)
     JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum ORDER BY k.n) AS columns
     FROM pg_constraint c WHERE c.conrelid=%s AND c.contype='p'""",(oid,))
   keys=c.fetchall();_require(len(keys)==1 and keys[0]['columns']==_PKS[name],name+' primary key')
   c.execute("SELECT 1 FROM pg_constraint WHERE conrelid=%s AND contype='f'",(oid,))
   _require(not c.fetchone(),name+' no native FK')
   c.execute("""SELECT t.tgname,t.tgtype,t.tgenabled,t.tgnargs,t.tgqual,p.proname,n.nspname
     FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace n ON n.oid=p.pronamespace
     WHERE t.tgrelid=%s AND NOT t.tgisinternal""",(oid,))
   triggers=c.fetchall();_require(len(triggers)==len(_TRIGGERS[name]),name+' trigger count')
   for t in triggers:
    _require((t['proname'],t['tgtype'])==_TRIGGERS[name].get(t['tgname']) and t['nspname']=='usage_entitlements'
      and t['tgenabled']=='O' and t['tgnargs']==0 and t['tgqual'] is None,name+' '+t['tgname'])
  c.execute("""SELECT p.oid,p.proname,p.pronargs,p.prosecdef,p.proconfig,p.prosrc,p.proowner,
    l.lanname,p.prorettype='pg_catalog.trigger'::regtype AS returns_trigger
    FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang WHERE p.pronamespace=%s""",(schema['oid'],))
  actual=c.fetchall();_require(len(actual)==len(functions) and {r['proname'] for r in actual}==set(functions),'entitlement functions')
  for f in actual:
   _require(f['pronargs']==0 and not f['prosecdef'] and f['lanname']=='plpgsql' and f['returns_trigger']
     and f['prosrc'].strip()==functions[f['proname']].strip()
     and [s.replace(' ','') for s in f['proconfig'] or []]==['search_path=pg_catalog,usage_entitlements'],f['proname']+' invoker/body')
   c.execute("""SELECT r.rolname,a.privilege_type,a.is_grantable FROM pg_proc p
    CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
    LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE p.oid=%s AND a.grantee<>p.proowner""",(f['oid'],))
   _require({(r['rolname'],r['privilege_type'],r['is_grantable']) for r in c}=={(r,'EXECUTE',False) for r in ('nexpoly_api','nexpoly_service')},f['proname']+' exact ACL')
 return {'version':VERSION,'validated':True,'relations':['usage_entitlements.'+n for n in sorted(_ACL)]}
