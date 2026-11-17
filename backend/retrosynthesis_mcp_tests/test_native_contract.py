"""Optional real HTTP contract with the native and MCP runtimes in separate processes.

Run in the backend test image with disposable ISOLATION_TEST_ADMIN_DSN and
RETROSYNTHESIS_MCP_TEST_SITE_PACKAGES pointing to the locked MCP site-packages.
Keeping processes separate preserves each service's production dependency set.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
import time

import pytest

if not os.environ.get("ISOLATION_TEST_ADMIN_DSN") or not os.environ.get("RETROSYNTHESIS_MCP_TEST_SITE_PACKAGES"):
    if os.environ.get("RETROSYNTHESIS_MCP_REQUIRE_NATIVE_TEST") == "1":
        raise RuntimeError("Mandatory native HTTP contract requires disposable PG and the independent locked MCP runtime")
    pytest.skip("Native HTTP contract requires the disposable backend and locked MCP runtime", allow_module_level=True)

from fastapi import FastAPI
import uvicorn
from app.auth.context import Identity, user_context
from app.routers.monomer_retrosynthesis import router
from test_retrosynthesis_jobs import A, retrosynthesis_db, repo, make_manager, finish, response
from conftest import session_response


CHILD = r'''
import asyncio,json,sys
from contextlib import asynccontextmanager
from pathlib import Path
import httpx2 as httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from app.retrosynthesis_mcp import create_app
from app.retrosynthesis_mcp_security import RetrosynthesisMCPConfig

@asynccontextmanager
async def sdk(app,token):
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),headers={"Authorization":"Bearer "+token}) as http:
  @asynccontextmanager
  async def transport():
   async with streamable_http_client("http://127.0.0.1:18211/mcp",http_client=http) as streams: yield streams
  async with Client(transport(),read_timeout_seconds=25) as session: yield session

async def main():
 values=json.load(sys.stdin)
 drop_first=values.pop("simulate_lost_before_receive",False)
 for name in ("session_file","clients_file","journal_file"): values[name]=Path(values[name])
 class DropFirst(httpx.AsyncBaseTransport):
  def __init__(self): self.dropped=False; self.delegate=httpx.AsyncHTTPTransport(retries=0)
  async def handle_async_request(self,request):
   if drop_first and not self.dropped and request.method=="POST" and request.url.path.endswith("/jobs"):
    self.dropped=True
    raise httpx.ConnectError("Synthetic pre-delivery failure",request=request)
   return await self.delegate.handle_async_request(request)
  async def aclose(self): await self.delegate.aclose()
 app=create_app(RetrosynthesisMCPConfig(**values),transport=DropFirst())
 async with app.router.lifespan_context(app),sdk(app,"test-token") as model:
  caps=await model.call_tool("get_retrosynthesis_capabilities",{})
  assert not caps.is_error and caps.structured_content["available"],caps.structured_content
  draft=await model.call_tool("prepare_monomer_retrosynthesis",{"conversation_id":"ses_native","target":{"source":"smiles","smiles":"Nc1ccc(N)cc1"},"parameters":{"target_role":"other","num_return_sequences":3}})
  assert not draft.is_error,draft.structured_content
  assert draft.structured_content["resolved_target"]["inferred_target_role"]=="diamine"
  args={"conversation_id":"ses_native","draft_id":draft.structured_content["draft_id"],"revision":1}
  async with sdk(app,"ui-token") as ui:
   first=await ui.call_tool("confirm_monomer_retrosynthesis",args)
   if drop_first:
    assert first.is_error and first.structured_content["error"]["code"]=="SUBMISSION_NOT_RECEIVED",first.structured_content
    read=await model.call_tool("get_retrosynthesis_draft",{"conversation_id":"ses_native","draft_id":args["draft_id"]})
    assert read.structured_content["status"]=="submission_unknown" and read.structured_content["job_id"] is None
    first=await ui.call_tool("confirm_monomer_retrosynthesis",args)
   assert not first.is_error,first.structured_content
   job_id=first.structured_content["job_id"]
   second=await ui.call_tool("confirm_monomer_retrosynthesis",args)
   assert second.structured_content["job_id"]==job_id and second.structured_content["submission"]["replayed"]
  snapshot=await model.call_tool("get_retrosynthesis_job",{"conversation_id":"ses_native","job_id":job_id,"wait_seconds":20})
  assert not snapshot.is_error,snapshot.structured_content
  assert snapshot.structured_content["status"]=="completed"
  result=snapshot.structured_content["result"]
  assert result["target_role"]=="other" and result["inferred_target_role"]=="diamine"
  assert "device" not in result and "detected_target_role" not in result
  print("SDK_NATIVE_HTTP_OK")
asyncio.run(main())
'''


@pytest.mark.parametrize("lost_before_receive", [False, True])
def test_real_native_http_postgres_and_mcp_sdk_confirm_once(configuration, repo, monkeypatch, tmp_path, lost_before_receive):
    calls = []
    with user_context(Identity(A)):
        manager = make_manager(repo, monkeypatch, runner=lambda **kwargs: calls.append(1) or response(**kwargs))
        model = tmp_path / "model"
        model.mkdir()
        (model / "config.json").write_text("{}")
        (model / "model.safetensors").touch()
        manager.app.state.settings.retro_model_id = str(model)
        manager.app.state.settings.retro_model_enabled = True
        manager.app.state.settings.structure_library_enabled = False
        native = FastAPI()
        native.state = manager.app.state
        native.state.retrosynthesis_repository = repo
        native.state.retrosynthesis_job_manager = manager
        native.include_router(router)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        config = replace(configuration, account_uuid=A, backend_url="http://127.0.0.1:" + str(listener.getsockname()[1]))
        session = json.loads(config.session_file.read_text())
        session.update(account_uuid=A, backend_url=config.backend_url)
        config.session_file.write_text(json.dumps(session))
        native.get("/api/v1/auth/session")(lambda: session_response(config))
        @native.middleware("http")
        async def identity(request, call_next):
            with user_context(Identity(A)):
                return await call_next(request)
        monkeypatch.setattr("app.routers.monomer_retrosynthesis.validate_token_budget", lambda *_: None)
        server = uvicorn.Server(uvicorn.Config(native, log_level="error", access_log=False))
        worker = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            child_env = dict(os.environ, PYTHONPATH=os.environ["RETROSYNTHESIS_MCP_TEST_SITE_PACKAGES"]
                + os.pathsep + str(Path(__file__).resolve().parents[1]))
            settings = {"backend_url": config.backend_url, "account_uuid": A,
                "simulate_lost_before_receive": lost_before_receive,
                **{name: str(getattr(config, name)) for name in ("session_file", "clients_file", "journal_file")}}
            result = subprocess.run([sys.executable, "-c", CHILD], input=json.dumps(settings), text=True,
                capture_output=True, env=child_env, timeout=45)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "SDK_NATIVE_HTTP_OK" in result.stdout and calls == [1]
            with repo.connection() as connection:
                assert connection.execute("SELECT count(*) AS n FROM retrosynthesis.jobs").fetchone()["n"] == 1
        finally:
            server.should_exit = True
            worker.join(timeout=5)
            listener.close()
            finish(manager)
