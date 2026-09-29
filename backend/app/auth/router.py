from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

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
    return request.app.state.auth.response(request.state.auth_session, request.state.auth_token)


@router.post('/login')
async def login(body: LoginBody, request: Request, response: Response):
    auth = request.app.state.auth
    session, token = await run_in_threadpool(auth.login,body.username,body.password,request.client.host if request.client else 'unknown')
    response.set_cookie(auth.settings.cookie_name,token,max_age=auth.settings.session_hours*3600,
                        secure=auth.settings.cookie_secure,httponly=True,samesite='lax',path='/')
    return auth.response(session, token)


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
