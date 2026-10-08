"""A separate 0020 usage seal; historical identity/cutover seals stay unchanged."""
from __future__ import annotations

import hashlib
import json

from psycopg import sql
from psycopg.rows import dict_row

from app.services.usage_schema import validate_usage_schema

_TIMES = {
    'events': ('occurred_at','recorded_at','submitted_at','started_at','finished_at'),
    'attempts': ('first_occurred_at','last_occurred_at','last_recorded_at','submitted_at','started_at','finished_at'),
}


def _metadata(connection, name):
    relation='usage.'+name
    result={}
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""SELECT attname AS name,format_type(atttypid,atttypmod) AS type,
            attnotnull AS required,pg_get_expr(d.adbin,d.adrelid) AS default_expression
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""",(relation,))
        result['columns']=[dict(row) for row in cursor]
        cursor.execute("SELECT conname AS name,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid=%s::regclass ORDER BY conname",(relation,))
        result['constraints']=[dict(row) for row in cursor]
        cursor.execute("SELECT pg_get_indexdef(indexrelid) AS definition FROM pg_index WHERE indrelid=%s::regclass ORDER BY pg_get_indexdef(indexrelid)",(relation,))
        result['indexes']=[dict(row) for row in cursor]
        cursor.execute("SELECT pg_get_triggerdef(oid) AS definition FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal ORDER BY tgname",(relation,))
        result['triggers']=[dict(row) for row in cursor]
    return hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()


def usage_backup_seal(connection):
    """Return only counts/digests. Row values never leave PostgreSQL.

    All timestamps are hashed as UTC epoch numerics so different session time
    zones cannot make an otherwise identical restore appear different.
    """
    validate_usage_schema(connection)
    tables={}
    for name,times in _TIMES.items():
        # Both the table and timestamp identifiers are fixed allowlisted source.
        pairs=[]
        for field in times:
            pairs.extend([sql.Literal(field),sql.SQL('extract(epoch FROM t.{})').format(sql.Identifier(field))])
        canonical=sql.SQL('to_jsonb(t)||jsonb_build_object({})').format(sql.SQL(',').join(pairs))
        query=sql.SQL("SELECT encode(sha256(convert_to(({})::text,'UTF8')),'hex') AS digest FROM {} t ORDER BY digest").format(
            canonical,sql.Identifier('usage',name))
        digest,count=hashlib.sha256(),0
        with connection.cursor(name='usage_backup_'+name,row_factory=dict_row) as cursor:
            cursor.execute(query)
            for row in cursor:
                digest.update(row['digest'].encode('ascii')+b'\n')
                count+=1
        tables['usage.'+name]={'rows':count,'sha256':digest.hexdigest(),
                              'metadata_sha256':_metadata(connection,name)}
    return {'schema_version':1,'tables':tables}
