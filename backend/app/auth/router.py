from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

def _account_response(request, session, token=None):
    response = request.app.state.auth.response(session, token)
    tier = "free"
    if session is not None:
        repository = getattr(request.app.state, "entitlement_repository", None)
        if repository is None:
            tier = "member"  # Legacy standalone auth apps do not define plans.
        else:
            from app.auth.context import Identity, user_context
            with user_context(Identity(str(session["user_id"])), request.app.state.auth.settings):
                tier = repository.resolve_access_tier(owner_user_id=str(session["user_id"]))
        from app.auth.policy import capabilities
        from app.auth.acceptance import entry_matches
        settings = request.app.state.auth.settings
        acceptance = getattr(settings, "isolated_acceptance_policy", None) if entry_matches(request.scope, settings) else None
        response["capabilities"] = capabilities(tier == "member" and not session["must_change_password"],
                                                acceptance=acceptance, owner_id=str(session["user_id"]))
    response["account_type"] = tier
    return response


router = APIRouter(prefix='/api/v1/auth', tags=['authentication'])


def _isolated_login_origin(request: Request, auth):
    policy = auth.settings.isolated_ab_session_policy
    if policy is None:
        return None
    peer = request.client.host if request.client else ''
    headers = request.headers
    # Docker sees its bridge peer and internal listener, not the host's
    # loopback publishing address. Pin that actual peer; forwarded headers
    # never establish or override the trusted entry identity.
    raw_headers = request.scope.get('headers', ())
    unique_entry = all(sum(name.lower() == expected for name, _ in raw_headers) == 1
                       for expected in (b'host', b'origin'))
    if (peer != policy.proxy_peer or not unique_entry or request.url.scheme != 'http'
            or headers.get('origin') != policy.origin
            or headers.get('host') != urlsplit(policy.origin).netloc):
        raise HTTPException(403, '隔离验收会话仅允许专属 loopback 代理入口。')
    return policy.origin


class LoginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1,max_length=256)
    password: str = Field(min_length=1,max_length=256)


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    current_password: str = Field(min_length=1,max_length=256)
    new_password: str = Field(min_length=12,max_length=256)


@router.get('/session')
def session(request: Request):
    return _account_response(request, request.state.auth_session, request.state.auth_token)


@router.post('/login')
async def login(body: LoginBody, request: Request, response: Response):
    auth = request.app.state.auth
    isolated_origin = _isolated_login_origin(request, auth)
    session, token = await run_in_threadpool(auth.login,body.username,body.password,
        request.client.host if request.client else 'unknown', isolated_origin=isolated_origin)
    response.set_cookie(auth.settings.cookie_name,token,max_age=session['session_max_age'],
                        secure=auth.settings.cookie_secure,httponly=True,samesite='lax',path='/')
    return await run_in_threadpool(_account_response, request, session, token)


@router.post('/logout',status_code=204)
async def logout(request: Request):
    auth = request.app.state.auth
    await run_in_threadpool(auth.logout,request.state.auth_session['session_id'])
    # Revocation is authoritative. A delayed Delete-Cookie response could erase
    # a newer login in another tab; leave the inert token to expire/overwrite.
    return Response(status_code=204)


@router.post('/password',status_code=204)
async def password(body: PasswordBody,request: Request):
    auth = request.app.state.auth
    await run_in_threadpool(auth.change_password,request.state.auth_session,body.current_password,body.new_password)
    return Response(status_code=204)
