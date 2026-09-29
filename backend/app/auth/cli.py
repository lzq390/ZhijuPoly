"""Run: python -m app.auth.cli --help. Passwords are read without echo."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from .service import hash_password, normalize_username


def manage_user(connection, action: str, *, username=None, user_id=None, password=None):
    if action == 'create':
        encoded = hash_password(password)
        row = connection.execute('''INSERT INTO auth.users(user_id,username,password_hash)
            VALUES(%s,%s,%s) RETURNING user_id,username,status,must_change_password''',
                                 (uuid4(),normalize_username(username),encoded)).fetchone()
    elif action == 'list':
        return connection.execute('''SELECT user_id,username,status,must_change_password
            FROM auth.users WHERE NOT is_system ORDER BY username''').fetchall()
    else:
        user_id = str(UUID(user_id))
        # Same user-first lock order as every first-start authorization.
        row = connection.execute('SELECT user_id FROM auth.users WHERE user_id=%s AND NOT is_system FOR UPDATE', (user_id,)).fetchone()
        if not row:
            raise ValueError('普通账号不存在。')
        if action == 'reset':
            connection.execute('''UPDATE auth.users SET password_hash=%s,must_change_password=true,
                password_changed_at=now(),updated_at=now() WHERE user_id=%s''', (hash_password(password),user_id))
        else:
            connection.execute('UPDATE auth.users SET status=%s,updated_at=now() WHERE user_id=%s',
                               ('active' if action == 'enable' else 'disabled',user_id))
        connection.execute('UPDATE auth.sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL', (user_id,))
        row = connection.execute('SELECT user_id,username,status,must_change_password FROM auth.users WHERE user_id=%s', (user_id,)).fetchone()
    return row


def main():
    parser = argparse.ArgumentParser(description='NexPoly 账号运维（密码通过终端读取，不进入参数或日志）')
    parser.add_argument('--dsn-env',default='AUTH_ADMIN_POSTGRES_DSN',help='含管理数据库 DSN 的环境变量名称')
    sub = parser.add_subparsers(dest='action',required=True)
    sub.add_parser('create').add_argument('username')
    sub.add_parser('list')
    for name in ('enable','disable','reset'):
        sub.add_parser(name).add_argument('user_id',type=lambda value:str(UUID(value)))
    args = parser.parse_args()
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        parser.error(f'请配置环境变量 {args.dsn_env}')
    password = None
    if args.action in {'create','reset'}:
        password = getpass.getpass('临时密码（至少 12 字符）: ')
        if password != getpass.getpass('再次输入: '):
            parser.error('两次密码不一致')
    with psycopg.connect(dsn,row_factory=dict_row) as connection:
        result = manage_user(connection,args.action,username=getattr(args,'username',None),
                             user_id=getattr(args,'user_id',None),password=password)
    print(json.dumps(result,default=str,ensure_ascii=False))


if __name__ == '__main__':
    main()
