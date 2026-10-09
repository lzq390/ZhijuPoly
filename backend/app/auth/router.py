from __future__ import annotations

from fastapi import APIRouter, Request, Response
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
        response["capabilities"] = capabilities(tier == "member" and not session["must_change_password"])
    response["account_type"] = tier
    return response


router = APIRouter(prefix='/api/v1/auth', tags=['authentication'])


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
    session, token = await run_in_threadpool(auth.login,body.username,body.password,request.client.host if request.client else 'unknown')
    response.set_cookie(auth.settings.cookie_name,token,max_age=auth.settings.session_hours*3600,
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
