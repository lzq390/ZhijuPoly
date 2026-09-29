from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException

from .context import Identity, SYSTEM_USER_ID
from .settings import AuthSettings

PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
_DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))
CUTOVER_VERSION = '0018_user_isolation_cutover'


def normalize_username(value: str) -> str:
    result = unicodedata.normalize('NFKC', value).strip().casefold()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', result):
        raise ValueError('用户名须为 3–64 位小写字母、数字、点、下划线或横线。')
    return result


def hash_password(value: str) -> str:
    if not 12 <= len(value) <= 256 or len(value.encode('utf-8')) > 1024:
        raise ValueError('密码须为 12–256 个字符。')
    return PASSWORDS.hash(value)


def verify_password(encoded: str, value: str) -> bool:
    if len(value) > 256:
        return False
    try:
        return PASSWORDS.verify(encoded, value)
    except (VerificationError, InvalidHashError):
        return False


def csrf_token(token: str) -> str:
    return hmac.new(token.encode(), b'nexpoly/session/csrf/v1', hashlib.sha256).hexdigest()


class LoginLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = {}

    def check(self, peer: str, username: str):
        now = time.monotonic()
        with self.lock:
            self.attempts = {key: events for key, events in self.attempts.items() if events[-1] > now - 900}
            # Bound attempts by source and account without recording credentials.
            for key, maximum in ((('ip', peer), 30), (('user', username), 10)):
                recent = [stamp for stamp in self.attempts.get(key, ()) if stamp > now - 900]
                if len(recent) >= maximum or len(self.attempts) >= 10000:
                    raise HTTPException(429, '登录尝试过于频繁，请稍后重试。', headers={'Retry-After': '900'})
                self.attempts[key] = [*recent, now]
        return peer,username,now

    def succeeded(self, attempt):
        peer,username,stamp = attempt
        with self.lock:
            for key in (('ip',peer),('user',username)):
                self.attempts[key] = [value for value in self.attempts.get(key,()) if value != stamp]
                if not self.attempts[key]:
                    del self.attempts[key]


class AuthService:
    def __init__(self, settings: AuthSettings):
        self.settings = settings
        self.limiter = LoginLimiter()

    @contextmanager
    def connection(self, *, service=False):
        import psycopg
        from psycopg.rows import dict_row
        try:
            with psycopg.connect(self.settings.service_dsn if service else self.settings.auth_dsn,
                                 row_factory=dict_row, connect_timeout=3) as connection:
                yield connection
        except psycopg.Error as exc:
            raise HTTPException(503, '认证服务暂不可用。') from exc

    def resolve(self, token: str | None):
        if not token or len(token) > 128:
            return None
        with self.connection() as connection:
            return connection.execute('''SELECT u.user_id,u.username,u.must_change_password,s.session_id
                FROM auth.sessions s JOIN auth.users u USING(user_id)
                WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now()
                  AND u.status='active' AND NOT u.is_system''', (hashlib.sha256(token.encode()).digest(),)).fetchone()

    @staticmethod
    def identity(session) -> Identity:
        return Identity(str(session['user_id']), str(session['session_id']),
                        request_id=str(uuid4()), must_change_password=session['must_change_password'])

    @staticmethod
    def response(session, token=None):
        from .policy import capabilities
        return {
            'authenticated': session is not None,
            'user': None if session is None else {'id': str(session['user_id']), 'username': session['username'],
                                                'must_change_password': session['must_change_password']},
            'session_id': str(session['session_id']) if session else None,
            'csrf_token': csrf_token(token) if session and token else None,
            'capabilities': capabilities(bool(session) and not session['must_change_password']),
        }

    def login(self, username: str, password: str, peer: str):
        try:
            username = normalize_username(username)
        except ValueError:
            username = '!invalid'
        attempt = self.limiter.check(peer, username)
        with self.connection() as connection:
            user = connection.execute('SELECT * FROM auth.users WHERE username=%s', (username,)).fetchone()
        verified = verify_password(user['password_hash'] if user else _DUMMY_HASH, password)
        if not verified or not user or user['status'] != 'active' or user['is_system']:
            raise HTTPException(401, '用户名或密码错误，或账号不可用。')
        token = secrets.token_urlsafe(32)
        session_id = uuid4()
        with self.connection() as connection:
            locked = connection.execute('SELECT * FROM auth.users WHERE user_id=%s FOR UPDATE', (user['user_id'],)).fetchone()
            if locked['status'] != 'active' or locked['password_hash'] != user['password_hash']:
                raise HTTPException(401, '用户名或密码错误，或账号不可用。')
            connection.execute('''INSERT INTO auth.sessions(session_id,user_id,token_hash,expires_at)
                VALUES(%s,%s,%s,%s)''', (session_id,user['user_id'],hashlib.sha256(token.encode()).digest(),
                                      datetime.now(timezone.utc)+timedelta(hours=self.settings.session_hours)))
        self.limiter.succeeded(attempt)
        return {**user, 'session_id': session_id}, token

    def logout(self, session_id):
        with self.connection() as connection:
            connection.execute('UPDATE auth.sessions SET revoked_at=now() WHERE session_id=%s AND revoked_at IS NULL', (session_id,))

    def change_password(self, session, current_password, new_password):
        if current_password == new_password:
            raise HTTPException(400, '新密码须与当前密码不同。')
        try:
            encoded = hash_password(new_password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with self.connection() as connection:
            user = connection.execute('SELECT * FROM auth.users WHERE user_id=%s', (session['user_id'],)).fetchone()
        if not verify_password(user['password_hash'], current_password):
            raise HTTPException(403, '当前密码错误。')
        with self.connection() as connection:
            locked = connection.execute('SELECT * FROM auth.users WHERE user_id=%s FOR UPDATE', (user['user_id'],)).fetchone()
            active_session = connection.execute('''SELECT 1 FROM auth.sessions WHERE session_id=%s
                AND revoked_at IS NULL AND expires_at>now()''', (session['session_id'],)).fetchone()
            if not active_session or locked['status'] != 'active' or locked['password_hash'] != user['password_hash']:
                raise HTTPException(401, '会话已失效，请重新登录。')
            connection.execute('''UPDATE auth.users SET password_hash=%s,must_change_password=false,
                password_changed_at=now(),updated_at=now() WHERE user_id=%s''', (encoded,user['user_id']))
            connection.execute('UPDATE auth.sessions SET revoked_at=now() WHERE user_id=%s AND revoked_at IS NULL', (user['user_id'],))

    def authorize_memory_start(self, owner_user_id: str) -> bool:
        # The commit on leaving this context is the logical start boundary.
        with self.connection(service=True) as connection:
            user = connection.execute('SELECT status,is_system FROM auth.users WHERE user_id=%s FOR UPDATE', (owner_user_id,)).fetchone()
            return bool(user and (user['status'] == 'active' or (str(owner_user_id) == SYSTEM_USER_ID and user['is_system'])))

    def assert_application_ready(self):
        import psycopg
        from psycopg.rows import dict_row
        from .schema import validate_runtime_role, validate_isolation_schema
        with psycopg.connect(self.settings.application_dsn, row_factory=dict_row, connect_timeout=3) as connection:
            validate_runtime_role(connection, 'nexpoly_api')
            if not connection.execute('SELECT 1 FROM governance.schema_migrations WHERE version=%s', (CUTOVER_VERSION,)).fetchone():
                raise RuntimeError('User isolation cutover has not completed')
            validate_isolation_schema(connection)
        for dsn,group in ((self.settings.auth_dsn,'nexpoly_auth'),(self.settings.service_dsn,'nexpoly_service')):
            with psycopg.connect(dsn,row_factory=dict_row,connect_timeout=3) as connection:
                validate_runtime_role(connection,group)
                other = 'nexpoly_service' if group == 'nexpoly_auth' else 'nexpoly_auth'
                if connection.execute("SELECT pg_has_role(current_user,%s,'MEMBER') AS mixed",(other,)).fetchone()['mixed']:
                    raise ValueError('Authentication and execution database roles must be separate')
