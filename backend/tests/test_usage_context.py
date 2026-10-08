from __future__ import annotations
import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from app.auth.context import Identity, current_owner_id, user_context
from app.usage_catalog import ROUTES, catalog_modules, match_usage_route
from app.usage_context import (UsageCollector, UsageContext, UsageSpan, UsageMeteringUnavailable,
    current_usage_context, current_usage_collector, emit_usage_event, usage_scope, usage_span)
from app.usage_middleware import UsageMeteringMiddleware

OWNER = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"

class Repository:
    def __init__(self):
        self.events, self.attempts = [], []
        self.fail = lambda event: False
    def record_event(self, connection=None, **event):
        self.attempts.append(event)
        if self.fail(event):
            raise RuntimeError("secret-dsn-and-prompt-must-not-escape")
        if connection is None:
            assert current_owner_id() == event["owner_user_id"]
        self.events.append(event)
        return {"inserted": True}


def collector():
    repo = Repository()
    return UsageCollector(enabled=True, repository=repo), repo


def root(usage, owner=OWNER, task_id=None):
    return UsageSpan(UsageContext(owner, "predict", task_id or "request:" + str(uuid4())), usage)


def test_disabled_does_not_touch_database_or_create_context(monkeypatch):
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)
    usage = UsageCollector(repository=object())
    span = root(usage)
    with span:
        with usage_span("predict"):
            pass
    assert usage.snapshot() == {"enabled": False, "degraded": False,
                                "pending_terminal_events": 0, "write_failures": 0}


def test_keys_unique_per_task_kind_attempt_and_stable_on_retry():
    usage, repo = collector()
    a = UsageContext(OWNER, "predict", "request:a")
    b = UsageContext(OWNER, "predict", "request:b")
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc)
    with usage_scope(a, collector=usage):
        for context in (a, a, b, UsageContext(OWNER,"predict","request:a",attempt_id="2"),
                        UsageContext(OWNER,"predict","request:a",kind="execution")):
            emit_usage_event(context, status="running", event_key="running", occurred_at=stamp)
    keys = [event["event_key"] for event in repo.events]
    assert keys[0] == keys[1]
    assert len(set(keys)) == 4
    assert all(len(key) <= 256 for key in keys)


def test_root_waits_for_real_child_and_cross_context_close():
    usage, repo = collector()
    span = root(usage)
    with usage_scope(span.context, collector=usage):
        span.start()
        child = usage_span("predict")
        child.start()
        assert not span.finish("interrupted")
    assert [event["status"] for event in repo.events] == ["running", "running"]
    # Complete in an unrelated thread with no request ContextVars.
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(child.finish).result(timeout=3)
    events = repo.events
    assert [event["status"] for event in events] == ["running", "running", "completed", "interrupted"]
    assert events[2]["parent_task_id"] == span.context.task_id
    assert events[2]["root_task_id"] == span.context.task_id
    assert "execution_ms" in events[2]["metrics"]
    assert "observed_wall_ms" in events[3]["metrics"]
    assert current_usage_context() is None


def test_provider_wall_is_not_execution_time():
    usage, repo = collector()
    with root(usage):
        with usage_span(kind="provider") as child:
            child.set_metrics({"provider_total_tokens": None})
    event = next(event for event in repo.events if event["kind"] == "provider" and event["status"] == "completed")
    assert event["metrics"]["provider_total_tokens"] is None
    assert "execution_ms" not in event["metrics"]
    assert "observed_wall_ms" in event["metrics"]


def test_terminal_failure_is_visible_and_replays_exact_metadata(caplog):
    usage, repo = collector()
    span = root(usage)
    span.start()
    repo.fail = lambda event: event["status"] == "completed"
    assert not span.finish()
    assert usage.snapshot()["pending_terminal_events"] == 1
    assert usage.snapshot()["degraded"]
    failed = repo.attempts[-1]
    assert "secret-dsn" not in caplog.text
    assert "usage_write_failed" in caplog.text
    repo.fail = lambda event: False
    second = root(usage, OTHER)
    second.start()
    assert repo.attempts[-2] == failed
    assert usage.snapshot()["pending_terminal_events"] == 0
    assert not usage.snapshot()["degraded"]
    second.finish()


def test_caller_transaction_failure_propagates_without_deferred_replay():
    usage, repo = collector()
    context = UsageContext(OWNER, "monomer_md", "monomer_md:test")
    repo.fail = lambda event: True
    with usage_scope(context, collector=usage), pytest.raises(UsageMeteringUnavailable):
        emit_usage_event(context, status="completed", event_key="completed", connection=object())
    assert usage.snapshot()["pending_terminal_events"] == 0


def run_request(app, usage, method="POST", path="/api/v1/predict", owner=OWNER):
    output = []
    async def receive():
        return {"type": "http.request", "body": b"private request content must not be inspected", "more_body": False}
    async def send(message):
        output.append(message)
    async def run():
        with user_context(Identity(owner)):
            await UsageMeteringMiddleware(app, collector=usage)(
                {"type": "http", "method": method, "path": path}, receive, send)
    asyncio.run(run())
    return output


def test_begin_failure_503_prevents_business_and_body_read():
    usage, repo = collector()
    repo.fail = lambda event: True
    async def app(scope, receive, send):
        pytest.fail("business must not execute when begin cannot persist")
    output = run_request(app, usage)
    assert output[0]["status"] == 503
    assert output[1]["body"] == b'{"detail":"usage_metering_unavailable"}'
    assert not repo.events


@pytest.mark.parametrize("code,terminal", [(200,"completed"),(422,"rejected"),(503,"failed"),(504,"timed_out")])
def test_middleware_outcome_and_owner_are_server_captured(code, terminal):
    usage, repo = collector()
    async def app(scope, receive, send):
        context = current_usage_context()
        assert context.owner_user_id == OWNER
        await send({"type":"http.response.start", "status":code, "headers":[]})
        await send({"type":"http.response.body", "body":b"private model output"})
    run_request(app,usage)
    assert [event["status"] for event in repo.events] == ["running",terminal]
    assert all("private" not in str(event) for event in repo.events)


def test_native_submit_only_binds_collector_without_http_root():
    usage, repo = collector()
    async def app(scope, receive, send):
        assert current_usage_context() is None
        assert current_usage_collector() is usage
        await send({"type":"http.response.start", "status":202, "headers":[]})
        await send({"type":"http.response.body", "body":b"{}"})
    run_request(app, usage, path="/api/v1/monomer-md/jobs")
    assert repo.events == []


@pytest.mark.parametrize("method,path", [("GET","/api/v1/task-events"),
    ("GET","/api/v1/monomer-md/jobs/abc"),("GET","/api/v1/monomer-dft/jobs/abc/bundle"),
    ("POST","/api/v1/monomer-md/jobs/abc/cancel"),("GET","/api/v1/usage/summary"),
    ("POST","/api/v1/auth/login"),("GET","/health")])
def test_observers_and_operations_do_not_count(method,path):
    usage, repo = collector()
    async def app(scope, receive, send):
        assert current_usage_context() is None
    run_request(app,usage,method,path)
    assert repo.events == []


def test_private_stream_retains_root_until_blocking_pull_and_close_end(monkeypatch):
    from app.services import private_execution as private
    class Lease:
        context = None
        released = False
        def release(self): self.released = True
    lease = Lease()
    monkeypatch.setattr(private,"acquire_admission",lambda *a,**kw:lease)
    monkeypatch.setattr(private,"authorize_memory_start",lambda owner:True)
    monkeypatch.setattr(private,"log_task_event",lambda *a,**kw:None)
    entered, release, finalized = Event(),Event(),Event()
    usage, repo = collector()
    parent = root(usage)
    @private.bounded_stream("cpu")
    def producer():
        assert current_usage_context() == parent.context
        try:
            yield "first"
            entered.set()
            assert release.wait(3)
            yield "second"
        finally:
            assert current_usage_context() == parent.context
            finalized.set()
    with user_context(Identity(OWNER)), usage_scope(parent.context, collector=usage):
        parent.start()
        stream = producer()
    assert next(stream) == "first"
    with ThreadPoolExecutor(2) as pool:
        pull = pool.submit(next,stream)
        assert entered.wait(3)
        closed = pool.submit(stream.close)
        assert not parent.finish("interrupted")
        assert len(repo.events) == 1
        assert not lease.released
        release.set()
        assert pull.result(3) == "second"
        closed.result(3)
    assert finalized.is_set() and lease.released
    assert repo.events[-1]["status"] == "interrupted"


def _declared_routes(path):
    tree = ast.parse(path.read_text())
    prefixes = {}
    for node in ast.walk(tree):
        if isinstance(node,ast.Assign) and isinstance(node.value,ast.Call) and getattr(node.value.func,"id","") == "APIRouter":
            for target in node.targets:
                if isinstance(target,ast.Name):
                    prefixes[target.id] = next((ast.literal_eval(kw.value) for kw in node.value.keywords if kw.arg == "prefix"), "")
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Attribute) and decorator.func.attr in {"get","post","put","patch","delete"}:
                    yield decorator.func.attr.upper(), prefixes.get(getattr(decorator.func.value,"id",""),"") + ast.literal_eval(decorator.args[0])


def test_catalog_classifies_every_registered_router_endpoint():
    app = Path(__file__).parents[1] / "app"
    main = ast.parse((app/"main.py").read_text())
    included = {call.args[0].id for call in ast.walk(main)
                if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute)
                and call.func.attr == "include_router" and call.args and isinstance(call.args[0],ast.Name)}
    sources = set()
    for node in main.body:
        if isinstance(node,ast.ImportFrom) and node.module and node.module.startswith("app."):
            if any((alias.asname or alias.name) in included for alias in node.names):
                sources.add(app.parent / (node.module.replace(".","/") + ".py"))
    assert len(sources) >= 20
    declared = [(method,path) for source in sources for method,path in _declared_routes(source)]
    # Main-level API registration is also part of the coverage contract.
    for call in ast.walk(main):
        if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and call.func.attr == "add_api_route":
            path = ast.literal_eval(call.args[0])
            methods = next((ast.literal_eval(kw.value) for kw in call.keywords if kw.arg == "methods"), ["GET"])
            declared.extend((method,path) for method in methods)
    missing = [(method,path) for method,path in declared if match_usage_route(method,path) is None]
    assert missing == []
    assert match_usage_route("POST","/api/v1/new-unclassified-computation") is None
    library_routes = [route for route in ROUTES if route.path.startswith("/api/v1/structure-library/")]
    assert len(library_routes) == 6
    assert all(route.mode == "observe" and route.module is None for route in library_routes)
    assert len({(route.method,route.path) for route in ROUTES}) == len(ROUTES)
    assert all(item["coverage"] in {"measured","partial","external"} for item in catalog_modules())


def test_authentication_is_outermost_to_usage_and_drain_rejects_before_usage():
    source = (Path(__file__).parents[1]/"app/main.py").read_text()
    assert source.index("app.add_middleware(UsageMeteringMiddleware") < source.index("app.add_middleware(DeploymentDrainMiddleware") < source.index("app.add_middleware(AuthenticationMiddleware")


def test_local_model_execution_excludes_ledger_begin_latency(monkeypatch):
    import app.usage_context as context_module
    usage, repo = collector()
    ticks = [0.0]
    monkeypatch.setattr(context_module,"monotonic",lambda:ticks[0])
    prior = repo.record_event
    def record_event(**event):
        ticks[0] += 2.0
        return prior(**event)
    repo.record_event = record_event
    with root(usage):
        with usage_span("predict"):
            ticks[0] += 0.5
    event = next(event for event in repo.events if event["kind"] == "execution" and event["status"] == "completed")
    assert event["metrics"]["execution_ms"] == 500


def test_cpu_prediction_records_each_actual_model_call(monkeypatch):
    from app.services import predictor
    usage, repo = collector()
    calls = []
    class Model:
        def predict(self, features):
            calls.append(True)
            return [1.5]
    monkeypatch.setattr(predictor,"smiles_to_features",lambda value:object())
    monkeypatch.setattr(predictor,"get_available_properties",lambda path:["one","two"])
    monkeypatch.setattr(predictor,"load_model",lambda name,path:Model())
    with root(usage):
        assert predictor._predict_unbounded("unrecorded-input",["one","two"]) == {"one":1.5,"two":1.5}
    completed = [event for event in repo.events if event["kind"] == "execution" and event["status"] == "completed"]
    assert len(calls) == len(completed) == 2
    assert len({event["task_id"] for event in completed}) == 2
    assert "unrecorded-input" not in str(repo.events)


def test_generation_filtering_still_records_each_model_invocation():
    from app.services.conditional_generation import GeneratedSmiles,run_conditional_generation
    usage, repo = collector()
    class Runtime:
        calls = 0
        def generate_once(self,**kwargs):
            self.calls += 1
            return GeneratedSmiles(raw_smiles="" if self.calls == 1 else "*CCC*",rdkit_smiles=None if self.calls == 1 else "*CCC*")
        def predict_tg(self,smiles):return 100.0
    with root(usage):
        result = run_conditional_generation(input_smiles="*CC*",delta_tg=20,candidate_count=1,
            top_k=5,temperature=1,runtime=Runtime(),max_attempts=2)
    assert len(result.candidates) == 1
    completed = [event for event in repo.events if event["kind"] == "execution" and event["status"] == "completed"]
    assert len(completed) == 3  # filtered generate + accepted generate + evaluator
    assert all(event["module"] == "conditional_generation" for event in completed)


def test_failed_model_span_cannot_claim_completed(monkeypatch):
    from app.services import predictor
    usage, repo = collector()
    class Model:
        def predict(self,features):raise ValueError("unrecorded-model-result")
    monkeypatch.setattr(predictor,"smiles_to_features",lambda value:object())
    monkeypatch.setattr(predictor,"get_available_properties",lambda path:["one"])
    monkeypatch.setattr(predictor,"load_model",lambda name,path:Model())
    with pytest.raises(ValueError), root(usage):
        predictor._predict_unbounded("unrecorded-input",["one"])
    assert [event["status"] for event in repo.events] == ["running","running","failed","failed"]
    assert "unrecorded" not in str(repo.events)



def test_explicit_sse_business_failure_overrides_http_200():
    from app.usage_context import mark_current_usage_outcome
    usage, repo = collector()
    async def app(scope, receive, send):
        await send({"type":"http.response.start", "status":200, "headers":[]})
        mark_current_usage_outcome("failed")
        await send({"type":"http.response.body", "body":b"business error remains private"})
    output = run_request(app,usage,path="/api/v1/assistant/chat/stream")
    assert output[0]["status"] == 200
    assert repo.events[-1]["status"] == "failed"


def test_recovered_child_failure_does_not_override_root_success():
    usage, repo = collector()
    with root(usage):
        try:
            with usage_span(kind="provider"):
                raise ValueError("first provider transport failed")
        except ValueError:
            with usage_span(kind="provider"):
                pass
    assert [event["status"] for event in repo.events] == ["running","running","failed","running","completed","completed"]


def test_late_business_outcome_after_http_end_is_kept_until_producer_end():
    from app.usage_context import mark_current_usage_outcome,retain_usage_execution
    usage, repo = collector()
    span = root(usage)
    with usage_scope(span.context,collector=usage):
        span.start()
        retention = retain_usage_execution()
        span.finish("completed")
    assert repo.events[-1]["status"] == "running"
    with usage_scope(span.context,collector=usage):
        mark_current_usage_outcome("interrupted")
    retention.release()
    assert repo.events[-1]["status"] == "interrupted"



def test_tg_worker_whole_lifetime_survives_http_close_between_provider_calls(monkeypatch):
    from types import SimpleNamespace
    from threading import current_thread
    from app.models import TgAssistantChatStreamRequest
    from app.routers import assistant
    from app.services.tg_assistant import TgAssistantEvent
    entered, release = Event(), Event()
    workers = []
    usage, repo = collector()
    parent = root(usage)
    def worker(**kwargs):
        workers.append(current_thread())
        assert current_usage_context() == parent.context
        yield TgAssistantEvent("token", {"content":"private"})
        entered.set()
        assert release.wait(3)
        # No child provider span is active during this wait. Whole-producer
        # retention is required; otherwise HTTP close would terminate the root.
        yield TgAssistantEvent("done", {})
    monkeypatch.setattr(assistant,"stream_tg_assistant_events",worker)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(
        tg_assistant_enabled=True,tg_assistant_api_key="synthetic-test-only",
        tg_assistant_base_url="https://invalid.example/v1",tg_assistant_model="fixture",
        ai_proxy_url=""))))
    body = TgAssistantChatStreamRequest(messages=[{"role":"user","content":"fixture"}])
    with user_context(Identity(OWNER)),usage_scope(parent.context,collector=usage):
        parent.start()
        response = assistant._stream_tg_chat_response(request_body=body,request=request)
        stream = response.private_content
        assert next(stream).startswith("event: meta")
        assert next(stream).startswith("event: token")
        assert entered.wait(3)
        stream.close()
        assert not parent.finish("interrupted")
        assert repo.events[-1]["status"] == "running"
    release.set()
    workers[0].join(3)
    assert not workers[0].is_alive()
    assert repo.events[-1]["status"] == "interrupted"
    assert "private" not in str(repo.events)
