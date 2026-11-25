"""Development-only owner-bound budget and OpenAI-compatible gateway to the pinned CPU worker."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hmac
import json
import os
import time
from uuid import UUID

import anyio
from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from psycopg import OperationalError

from app.auth.openscience_m4_provider import MAX_OUTPUT, MODEL

BASE = "/internal/openscience/m4/model"
TABLE = "openscience_m4.model_requests"
PRIVATE = {"Cache-Control": "private, no-store"}
REQUEST_BINDING = (
    "environment",
    "user_id",
    "session_id",
    "workspace_user_id",
    "workspace_generation_fingerprint",
    "workspace_session",
    "turn_id",
    "turn_expires_ms",
    "request_id",
)


class ModelLedger:
    def __init__(self, connect, *, budget=131072, per_owner_streams=2, global_streams=3):
        self.connect = connect
        self.budget = budget
        self.per_owner_streams = per_owner_streams
        self.global_streams = global_streams

    @staticmethod
    def expire(conn):
        # Also account for a process that exited after admission and before finalization.
        # The gateway imposes a 285s upper bound on the live provider stream.
        conn.execute(f"""
            UPDATE {TABLE} SET state='estimated', estimated_tokens=reserved_tokens,
                   settled_at=clock_timestamp(), reason='reservation_timeout'
             WHERE state='reserved' AND created_at < clock_timestamp()-interval '6 minutes'
        """)

    @staticmethod
    def totals(conn, owner):
        return conn.execute(
            f"""
            SELECT coalesce(sum(reserved_tokens) FILTER (WHERE state='reserved'),0) AS reserved_tokens,
                   coalesce(sum(input_tokens+output_tokens) FILTER (WHERE state='actual'),0) AS used_tokens,
                   coalesce(sum(estimated_tokens),0) AS estimated_tokens
              FROM {TABLE} WHERE owner_id=%s AND budget_date=timezone('UTC',CURRENT_TIMESTAMP)::date
        """,
            (owner,),
        ).fetchone()

    def reserve(self, actor, protocol, input_tokens, max_output, request_tag=None):
        with self.connect() as conn:
            # All admissions in this deployment take this short transaction lock. No inference holds it.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('dev-m1-open-science-model-budget'))")
            self.expire(conn)
            existing = conn.execute(
                f"SELECT state,reason,request_tag,workspace_generation_fingerprint FROM {TABLE} WHERE request_id=%s",
                (actor["request_id"],),
            ).fetchone()
            if existing and not (
                existing["state"] == "released"
                and existing["reason"] in ("worker_busy", "worker_not_connected")
                and existing["workspace_generation_fingerprint"] == actor["workspace_generation_fingerprint"]
                and request_tag
                and existing["request_tag"]
                and hmac.compare_digest(request_tag, existing["request_tag"])
            ):
                raise HTTPException(409, "Model request already admitted")
            owner = actor["user_id"]
            limits = conn.execute(
                f"""
                SELECT count(*) AS active, count(*) FILTER (WHERE owner_id=%s) AS owner_active
                  FROM {TABLE} WHERE state='reserved'
            """,
                (owner,),
            ).fetchone()
            used = self.totals(conn, owner)
            if (
                sum(used.values()) + input_tokens + max_output > self.budget
                or limits["owner_active"] >= self.per_owner_streams
                or limits["active"] >= self.global_streams
            ):
                raise HTTPException(
                    429, "Model token budget or concurrent stream limit reached", headers={"Retry-After": "5"}
                )
            if existing:
                # This particular request was never started by the worker.
                # Re-reserve against today's capacity and restart the six-minute lease clock.
                conn.execute(
                    f"""UPDATE {TABLE} SET state='reserved',reason=NULL,settled_at=NULL,
                           created_at=clock_timestamp(),budget_date=timezone('UTC',CURRENT_TIMESTAMP)::date
                         WHERE request_id=%s""",
                    (actor["request_id"],),
                )
                return
            conn.execute(
                f"""
                INSERT INTO {TABLE}(request_id,environment,owner_id,login_id,workspace_user_id,
                           workspace_generation_fingerprint,workspace_session,turn_id,model,protocol,
                           reserved_input,max_output,request_tag)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
                (
                    actor["request_id"],
                    actor["environment"],
                    owner,
                    actor["session_id"],
                    actor["workspace_user_id"],
                    actor["workspace_generation_fingerprint"],
                    actor["workspace_session"],
                    actor["turn_id"],
                    MODEL,
                    protocol,
                    input_tokens,
                    max_output,
                    request_tag,
                ),
            )

    def release(self, request_id, reason="worker_busy"):
        # Used only while the gateway can prove that inference never started.
        with self.connect() as conn:
            conn.execute(
                f"""UPDATE {TABLE} SET state='released',reason=%s,settled_at=clock_timestamp()
                            WHERE request_id=%s AND state='reserved'""",
                (reason, request_id),
            )

    def settle(self, request_id, usage, reason):
        with self.connect() as conn:
            if usage is None:
                conn.execute(
                    f"""
                    UPDATE {TABLE} SET state='estimated', estimated_tokens=reserved_tokens,
                           reason=%s, settled_at=clock_timestamp()
                     WHERE request_id=%s AND state='reserved'
                """,
                    (reason, request_id),
                )
                return
            # Late verified receipts replace an estimate; an already-actual row remains immutable.
            result = conn.execute(
                f"""
                UPDATE {TABLE} SET state='actual', input_tokens=%s, output_tokens=%s,
                       estimated_tokens=NULL,reason=NULL,settled_at=clock_timestamp()
                 WHERE request_id=%s AND state IN ('reserved','estimated')
                   AND reserved_input=%s AND max_output>=%s
            """,
                (
                    usage["input_tokens"],
                    usage["output_tokens"],
                    request_id,
                    usage["input_tokens"],
                    usage["output_tokens"],
                ),
            )
            if not result.rowcount:
                row = conn.execute(
                    f"SELECT state,input_tokens,output_tokens FROM {TABLE} WHERE request_id=%s", (request_id,)
                ).fetchone()
                if not row or row != {"state": "actual", **usage}:
                    raise ValueError("Inconsistent model provider receipt")

    def pending(self, owner, after):
        with self.connect() as conn:
            self.expire(conn)
            return [
                str(row["request_id"])
                for row in conn.execute(
                    f"""
                SELECT request_id FROM {TABLE} WHERE state='estimated'
                 AND (%s::uuid IS NULL OR owner_id=%s) AND request_id>%s
                 ORDER BY request_id LIMIT 32
            """,
                    (owner, owner, after),
                ).fetchall()
            ]

    def summary(self, owner):
        with self.connect() as conn:
            self.expire(conn)
            totals = self.totals(conn, owner)
            rows = conn.execute(
                f"""
                SELECT request_id,environment,owner_id,login_id,workspace_user_id,workspace_generation_fingerprint,
                       workspace_session,turn_id,model,protocol,state,input_tokens,output_tokens,estimated_tokens,
                       reserved_tokens,reason,budget_date,created_at,settled_at
                  FROM {TABLE} WHERE owner_id=%s ORDER BY created_at DESC LIMIT 200
            """,
                (owner,),
            ).fetchall()
        return {"records": rows, "budget_tokens": self.budget, **totals}


def provider_input(body, protocol):
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise HTTPException(422, "Unknown development model")
    max_out = body.get(
        "max_output_tokens", body.get("max_completion_tokens", body.get("max_tokens", MAX_OUTPUT))
    )
    if type(max_out) is not int or max_out < 1:
        raise HTTPException(422, "Invalid output token limit")
    messages = body.get("messages") if protocol == "chat.completions" else body.get("input")
    if isinstance(messages, str) and protocol == "responses":
        messages = [{"role": "user", "content": messages}]
    if not isinstance(messages, list) or not messages:
        raise HTTPException(422, "Text model input required")
    if isinstance(body.get("instructions"), str):
        messages = [{"role": "system", "content": body["instructions"]}, *messages]
    parts = []
    for row in messages:
        if not isinstance(row, dict) or row.get("role") not in {
            "developer",
            "system",
            "user",
            "assistant",
            "tool",
        }:
            raise HTTPException(422, "Unknown model message role")
        content = row.get("content")
        if isinstance(content, list):
            if any(
                not isinstance(p, dict)
                or p.get("type") not in {"text", "input_text", "output_text"}
                or not isinstance(p.get("text"), str)
                for p in content
            ):
                raise HTTPException(422, "Local model accepts text messages")
            content = "\n".join(p["text"] for p in content)
        if not isinstance(content, str):
            raise HTTPException(422, "Local model accepts text messages")
        parts.append(f"<|im_start|>{row['role']}\n{content}<|im_end|>\n")
    prompt = "".join(parts) + "<|im_start|>assistant\n"
    if len(prompt) > 384000:
        raise HTTPException(413, "Model prompt size exceeded")
    return {"prompt": prompt, "max_output": min(max_out, MAX_OUTPUT)}


def sse(data, event=None):
    prefix = ("event: " + event + "\n") if event else ""
    return (prefix + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode()


class Output:
    def __init__(self, protocol, request_id):
        self.chat = protocol == "chat.completions"
        self.id = ("chatcmpl-" if self.chat else "resp_") + UUID(str(request_id)).hex
        self.item = "msg_" + UUID(str(request_id)).hex
        self.created = int(time.time())
        self.text = ""
        self.seq = 0

    def chunk(self, delta, finish=None):
        return {
            "id": self.id,
            "model": MODEL,
            "created": self.created,
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def event(self, name, **fields):
        event = sse({"type": name, "sequence_number": self.seq, **fields}, name)
        self.seq += 1
        return event

    def part(self):
        return {"type": "output_text", "text": self.text, "annotations": []}

    def message(self, status):
        return {
            "id": self.item,
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": [self.part()] if status != "in_progress" else [],
        }

    def result(self, usage, finish="stop"):
        counts = (
            {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": sum(usage.values()),
            }
            if usage is not None
            else None
        )
        if self.chat:
            return {
                "id": self.id,
                "created": self.created,
                "model": MODEL,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.text},
                        "finish_reason": finish,
                    }
                ],
                "usage": {
                    "prompt_tokens": counts["input_tokens"],
                    "completion_tokens": counts["output_tokens"],
                    "total_tokens": counts["total_tokens"],
                }
                if counts
                else None,
            }
        status = "incomplete" if finish == "length" else "completed"
        return {
            "id": self.id,
            "object": "response",
            "created_at": self.created,
            "model": MODEL,
            "status": status,
            "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
            "output": [self.message(status)],
            "usage": counts,
        }

    def start(self):
        if self.chat:
            return [sse(self.chunk({"role": "assistant", "content": ""}))]
        response = {**self.result(None), "status": "in_progress", "output": []}
        return [
            self.event("response.created", response=response),
            self.event("response.output_item.added", output_index=0, item=self.message("in_progress")),
            self.event(
                "response.content_part.added",
                output_index=0,
                content_index=0,
                item_id=self.item,
                part=self.part(),
            ),
        ]

    def delta(self, text):
        self.text += text
        if self.chat:
            return sse(self.chunk({"content": text}))
        return self.event(
            "response.output_text.delta", output_index=0, content_index=0, item_id=self.item, delta=text
        )

    def finish(self, usage, reason):
        result = self.result(usage, reason)
        if self.chat:
            return [
                sse(self.chunk({}, reason)),
                sse({**self.chunk({}), "choices": [], "usage": result["usage"]}),
                b"data: [DONE]\n\n",
            ]
        return [
            self.event(
                "response.output_text.done",
                output_index=0,
                content_index=0,
                item_id=self.item,
                text=self.text,
            ),
            self.event(
                "response.content_part.done",
                output_index=0,
                content_index=0,
                item_id=self.item,
                part=self.part(),
            ),
            self.event("response.output_item.done", output_index=0, item=result["output"][0]),
            self.event("response." + result["status"], response=result),
        ]


def install_model(app, principal, *, request_key, transport=None, ledger=None):
    ledger = ledger or ModelLedger(app.state.auth.connection)
    socket_path = os.environ.get("M4_MODEL_SOCKET", "/m4provider/model.sock")

    def scope(actor, expected):
        if actor.get("scope") != expected or actor.get("environment") != "dev-m1":
            raise HTTPException(403, "Model authorization scope mismatch")

    async def reconcile(owner=None, after="00000000-0000-0000-0000-000000000000"):
        pending = await anyio.to_thread.run_sync(ledger.pending, owner, after)
        if not pending:
            return None
        async with httpx.AsyncClient(
            base_url="http://local-worker",
            transport=transport or httpx.AsyncHTTPTransport(uds=socket_path),
            timeout=3,
            trust_env=False,
        ) as peer:
            response = await peer.post("/receipts", json={"requests": pending})
            response.raise_for_status()
            found = response.json()
            if not isinstance(found, dict):
                raise ValueError("Invalid provider receipt batch")
            for rid in pending:
                if rid in found:
                    receipt = found[rid]
                    if (
                        not isinstance(receipt, dict)
                        or set(receipt) != {"input_tokens", "output_tokens"}
                        or any(
                            type(value) is not int or not 0 <= value <= 16384 for value in receipt.values()
                        )
                    ):
                        raise ValueError("Invalid provider token count")
                    await anyio.to_thread.run_sync(ledger.settle, rid, receipt, None)
        return pending[-1] if len(pending) == 32 else None

    async def reconcile_loop():
        after = None
        while True:
            await asyncio.sleep(15)
            try:
                after = await reconcile(after=after or "00000000-0000-0000-0000-000000000000")
            except (HTTPException, httpx.HTTPError, OperationalError, ValueError):
                # Keep the bounded estimate and retry when the development worker recovers.
                pass

    async def start():
        app.state.model_reconciler = asyncio.create_task(reconcile_loop())

    async def stop():
        task = app.state.model_reconciler
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app.add_event_handler("startup", start)
    app.add_event_handler("shutdown", stop)

    @app.get(BASE + "/usage")
    async def usage(actor=Depends(principal)):
        scope(actor, "model.usage")
        with suppress(httpx.HTTPError, ValueError):
            await reconcile(actor["user_id"])
        summary = await anyio.to_thread.run_sync(ledger.summary, actor["user_id"])
        return JSONResponse(jsonable_encoder(summary), headers=PRIVATE)

    async def invoke(request, actor, protocol):
        scope(actor, "model.invoke")
        if actor["turn_expires_ms"] <= time.time_ns() // 1_000_000:
            raise HTTPException(401, "Workspace model turn expired")
        if request.query_params:
            raise HTTPException(422, "Unsupported model query parameters")
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise HTTPException(413, "Model request too large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "Invalid model JSON") from None
        work = provider_input(body, protocol)
        request_id = actor["request_id"]
        output = Output(protocol, request_id)
        client = httpx.AsyncClient(
            base_url="http://local-worker",
            timeout=httpx.Timeout(275, connect=3),
            transport=transport or httpx.AsyncHTTPTransport(uds=socket_path),
            trust_env=False,
        )
        # Count with the exact tokenizer before locking or holding a stream slot.
        try:
            tokenized = await client.post("/tokenize", json=work)
            if tokenized.status_code == 413:
                raise HTTPException(413, "Development model context exceeded")
            tokenized.raise_for_status()
            inputs = tokenized.json()["input_tokens"]
            if type(inputs) is not int or not 0 < inputs <= 16384:
                raise ValueError("Invalid token count")
            # Bind an unstarted retry to its verified actor, generation, turn, and exact inference input
            # without keeping a copy of the prompt or a publicly guessable hash.
            binding = [actor[field] for field in REQUEST_BINDING]
            message = json.dumps(
                [binding, MODEL, protocol, inputs, work], separators=(",", ":"), ensure_ascii=False
            )
            tag = hmac.digest(request_key, message.encode("utf-8"), "sha256")
            await anyio.to_thread.run_sync(ledger.reserve, actor, protocol, inputs, work["max_output"], tag)
        except HTTPException:
            await client.aclose()
            raise
        except (httpx.HTTPError, ValueError, KeyError):
            await client.aclose()
            raise HTTPException(503, "Local model admission unavailable") from None

        # Tokenization and budget admission can both wait. Check the original
        # handoff and generation again before handing new work to the CPU worker.
        try:
            current = await anyio.to_thread.run_sync(principal, request)
            scope(current, "model.invoke")
            if (
                any(current[field] != actor[field] for field in REQUEST_BINDING)
                or await request.is_disconnected()
                or actor["turn_expires_ms"] <= time.time_ns() // 1_000_000
            ):
                raise HTTPException(401, "Workspace model request expired")
        except BaseException:
            with anyio.CancelScope(shield=True):
                try:
                    await anyio.to_thread.run_sync(ledger.release, request_id, "admission_cancelled")
                finally:
                    await client.aclose()
            raise

        try:
            dispatch = {**work, "request_id": str(request_id), "turn_expires_ms": actor["turn_expires_ms"]}
            upstream = await client.send(client.build_request("POST", "/generate", json=dispatch), stream=True)
            upstream.raise_for_status()
        except BaseException as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            code = 503
            with anyio.CancelScope(shield=True):
                try:
                    if status == 429:
                        code = 429
                        await anyio.to_thread.run_sync(ledger.release, request_id)
                    elif status == 412:
                        code = 401
                        await anyio.to_thread.run_sync(ledger.release, request_id, "turn_expired")
                    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                        await anyio.to_thread.run_sync(ledger.release, request_id, "worker_not_connected")
                    else:
                        await anyio.to_thread.run_sync(ledger.settle, request_id, None, "upstream_unavailable")
                finally:
                    await client.aclose()
            if not isinstance(exc, Exception):
                raise
            if code == 401:
                raise HTTPException(401, "Workspace model turn expired") from None
            raise HTTPException(code, "Local model worker unavailable") from None

        actual = None
        finish_reason = "stop"
        settled = False

        async def events():
            nonlocal actual, finish_reason, settled
            uncertainty = "missing_usage"
            try:
                deadline = anyio.current_time() + 285
                for packet in output.start():
                    yield packet
                lines = upstream.aiter_lines()
                while True:
                    with anyio.fail_after(max(0, deadline - anyio.current_time())):
                        line = await anext(lines, None)
                    if line is None:
                        break
                    item = json.loads(line)
                    if actual is not None or not isinstance(item, dict):
                        raise ValueError("Unexpected worker event")
                    if set(item) == {"text"} and isinstance(item["text"], str):
                        yield output.delta(item["text"])
                        continue
                    usage = item.get("usage")
                    if (
                        not isinstance(usage, dict)
                        or set(usage) != {"input_tokens", "output_tokens"}
                        or type(usage["input_tokens"]) is not int
                        or usage["input_tokens"] != inputs
                        or type(usage["output_tokens"]) is not int
                        or not 0 <= usage["output_tokens"] <= work["max_output"]
                    ):
                        raise ValueError("Invalid worker receipt")
                    actual = usage
                    finish_reason = "length" if usage["output_tokens"] == work["max_output"] else "stop"
                # Commit the provider receipt before reporting completion to the caller.
                await anyio.to_thread.run_sync(ledger.settle, request_id, actual, uncertainty)
                settled = True
                for packet in output.finish(actual, finish_reason):
                    yield packet
            except BaseException:
                uncertainty = "stream_interrupted"
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        if not settled:
                            await anyio.to_thread.run_sync(ledger.settle, request_id, actual, uncertainty)
                    finally:
                        await upstream.aclose()
                        await client.aclose()

        headers = {**PRIVATE, "X-Model-Request-Id": str(request_id)}
        if body.get("stream") is True:
            return StreamingResponse(events(), media_type="text/event-stream", headers=headers)
        async for _ in events():
            pass
        return JSONResponse(output.result(actual, finish_reason), headers=headers)

    @app.post(BASE + "/v1/chat/completions")
    async def chat(request: Request, actor=Depends(principal)):
        return await invoke(request, actor, "chat.completions")

    @app.post(BASE + "/v1/responses")
    async def responses(request: Request, actor=Depends(principal)):
        return await invoke(request, actor, "responses")
