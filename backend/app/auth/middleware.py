from __future__ import annotations

import hmac
import logging
import ipaddress

import anyio
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException

from .context import user_context, service_context
from .policy import route_policy
from .service import csrf_token

logger = logging.getLogger(__name__)


class AuthenticationMiddleware:
    """Runs before routing/body parsing; credentials never come from request data."""
    def __init__(self, app, auth_service):
        self.app, self.auth = app, auth_service
        self._ready = False

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path, method = scope['path'], scope['method']
        policy = route_policy(path, method)
        if method == 'OPTIONS':
            return await self.app(scope, receive, send)
        if policy == 'internal':
            if path.startswith('/internal/monomer-dft/'):
                expected = getattr(scope['app'].state.settings,'monomer_dft_start_authorization_token','')
                supplied = Headers(scope=scope).get('authorization','')
                if not expected or not hmac.compare_digest(supplied.encode('utf-8'),('Bearer '+expected).encode('utf-8')):
                    return await JSONResponse({'detail':'内部服务身份未获授权。'},status_code=403)(scope,receive,send)
            else:
                # Other existing maintenance endpoints are direct loopback only.
                peer = (scope.get('client') or ('',0))[0]
                try:
                    local = ipaddress.ip_address(peer).is_loopback
                except ValueError:
                    local = False
                names = {name.lower() for name,_value in scope.get('headers',())}
                forwarded = {b'forwarded',b'x-forwarded-for',b'x-forwarded-host',b'x-forwarded-proto',b'x-real-ip'}
                if not local or names & forwarded:
                    return await JSONResponse({'detail':'内部运维接口仅允许直接 loopback 访问。'},status_code=403)(scope,receive,send)
            with service_context(self.auth.settings):
                return await self.app(scope, receive, send)
        headers = Headers(scope=scope)

        async def reject(status, message, code):
            await JSONResponse({'detail': message, 'code': code}, status_code=status,
                               headers={'Cache-Control': 'private, no-store'})(scope, receive, send)

        if policy == 'public' and method not in {'GET','HEAD','OPTIONS'} and headers.get('origin') not in self.auth.settings.allowed_origins:
            return await reject(403, '请求来源未获授权。', 'untrusted_origin')
        token = Request(scope).cookies.get(self.auth.settings.cookie_name)
        try:
            session = await anyio.to_thread.run_sync(self.auth.resolve, token)
        except Exception:
            logger.warning('Authentication backend unavailable', exc_info=False)
            return await reject(503, '认证服务暂不可用。', 'authentication_unavailable')
        if policy != 'public':
            if session is None:
                return await reject(401, '请先登录。', 'authentication_required')
            if method not in {'GET','HEAD','OPTIONS'} and headers.get('origin') not in self.auth.settings.allowed_origins:
                return await reject(403, '请求来源未获授权。', 'untrusted_origin')
            if policy == 'operations':
                return await reject(403, '此操作仅供运维使用。', 'operation_forbidden')
            if headers.get('x-session-context') != str(session['session_id']):
                return await reject(409, '页面身份已变化，请重新确认登录状态。', 'session_context_mismatch')
            if method not in {'GET','HEAD','OPTIONS'} and not hmac.compare_digest(headers.get('x-csrf-token', '').encode('utf-8'), csrf_token(token).encode('ascii')):
                return await reject(403, '会话校验失败。', 'csrf_mismatch')
            if not path.startswith('/api/v1/auth/'):
                if session['must_change_password']:
                    return await reject(403, '请先修改初始密码。', 'password_change_required')
                if not self._ready:
                    try:
                        await anyio.to_thread.run_sync(self.auth.assert_application_ready)
                        self._ready = True
                    except Exception:
                        logger.warning('Private API is closed: isolation preflight failed', exc_info=False)
                        return await reject(503, '用户隔离数据库尚未准备好。', 'isolation_not_ready')
        scope.setdefault('state', {})['auth_session'] = session
        scope['state']['auth_token'] = token
        if path.startswith('/api/v1/auth/') and method not in {'GET','HEAD','OPTIONS'}:
            original_receive = receive
            received_bytes = 0
            async def bounded_auth_receive():
                nonlocal received_bytes
                message = await original_receive()
                received_bytes += len(message.get('body',b''))
                if received_bytes > 8192:
                    raise HTTPException(413,'Authentication request is too large')
                return message
            receive = bounded_auth_receive
        started = False
        streaming = False
        completed = False
        delivery_revoked = False
        send_lock = anyio.Lock()

        async def private_send(message):
            nonlocal started, streaming, completed
            async with send_lock:
                if delivery_revoked:
                    return
                if message['type'] == 'http.response.start':
                    started = True
                    response_headers = [(k,v) for k,v in message.get('headers', []) if k.lower() != b'cache-control']
                    response_headers += [(b'cache-control', b'private, no-store'), (b'vary', b'Cookie, X-Session-Context')]
                    message['headers'] = response_headers
                    streaming = any(k.lower() == b'content-type' and b'text/event-stream' in v for k,v in response_headers)
                elif message['type'] == 'http.response.body' and not message.get('more_body', False):
                    completed = True
                await send(message)

        async def run():
            # SSE revocation stops delivery, never releases the executor's lease.
            async with anyio.create_task_group() as group:
                async def watch_session():
                    nonlocal delivery_revoked, completed
                    while True:
                        await anyio.sleep(5)
                        if not streaming or completed:
                            continue
                        try:
                            active = await anyio.to_thread.run_sync(self.auth.resolve, token)
                        except Exception:
                            active = None
                        if not active or str(active['session_id']) != str(session['session_id']):
                            delivery_revoked = True
                            async with send_lock:
                                if started and not completed:
                                    await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
                                    completed = True
                            group.cancel_scope.cancel()
                            return
                if session:
                    group.start_soon(watch_session)
                try:
                    await self.app(scope, receive, private_send)
                finally:
                    group.cancel_scope.cancel()

        if session:
            with user_context(self.auth.identity(session), self.auth.settings):
                return await run()
        return await run()
