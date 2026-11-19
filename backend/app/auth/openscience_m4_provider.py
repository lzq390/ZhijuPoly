"""Read-only offline Qwen CPU worker. Only the private development model gateway mounts its socket."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from uuid import UUID
from pathlib import Path
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

MODEL = "qwen2.5-7b"
MANIFEST = "2cdb0c3afafb408c85ae429636bfb4e19f2f0342fbd8d6fa0491b6db92f8d04a"
CONTEXT = 16384
MAX_OUTPUT = 12


class Prompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=384000)
    max_output: int = Field(ge=1, le=MAX_OUTPUT, strict=True)


class Generate(Prompt):
    request_id: UUID


class ReceiptQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[UUID] = Field(min_length=1, max_length=32)


def verified_weights(directory):
    manifest = (directory / "asset_manifest.json").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != MANIFEST:
        raise ValueError("Unexpected Qwen development release manifest")
    release = json.loads(manifest)
    names = (*release["expected_files_sha256"], "generation_config.json", "merges.txt", "vocab.json")
    for name in names:
        expected = release["local_files"][name]["sha256"]
        with (directory / name).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                raise ValueError("Unexpected Qwen development asset: " + name)


def create_app():
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, StoppingCriteria, StoppingCriteriaList
    from transformers.generation.streamers import BaseStreamer

    directory = Path("/model")
    verified_weights(directory)
    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(
        directory, dtype=torch.bfloat16, local_files_only=True, trust_remote_code=False
    ).eval()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # A cancelled request may still be finishing its current CPU forward pass.
    slots = threading.BoundedSemaphore(3)

    receipt_file = os.environ.get("M4_MODEL_RECEIPTS", "/m4provider/receipts.sqlite")
    with closing(sqlite3.connect(receipt_file)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS receipts(request_id TEXT PRIMARY KEY, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL)"
        )

    @app.post("/receipts")
    def receipts(query: ReceiptQuery):
        ids = [str(request_id) for request_id in query.requests]
        query_params = ",".join("?" for _ in ids)
        with closing(sqlite3.connect(receipt_file)) as db:
            rows = db.execute(
                "SELECT request_id,input_tokens,output_tokens FROM receipts WHERE request_id IN ("
                + query_params
                + ")",
                ids,
            )
            return {rid: {"input_tokens": i, "output_tokens": o} for rid, i, o in rows}

    def encode(body):
        ids = tokenizer.encode(body.prompt, add_special_tokens=False, return_tensors="pt")
        if ids.shape[1] + body.max_output > CONTEXT:
            raise HTTPException(413, "Development model context exceeded")
        return ids

    @app.post("/tokenize")
    def tokenize(body: Prompt):
        return {"input_tokens": encode(body).shape[1]}

    @app.post("/generate")
    async def generate(body: Generate):
        ids = encode(body)
        stopped = threading.Event()
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()

        def emit(item):
            loop.call_soon_threadsafe(queue.put_nowait, item)

        class Stop(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return stopped.is_set()

        class Stream(BaseStreamer):
            def __init__(self):
                self.prompt = True
                self.ids = []
                self.text = ""

            def put(self, value):
                if self.prompt:
                    self.prompt = False
                    return
                self.ids.extend(value.view(-1).tolist())
                text = tokenizer.decode(
                    self.ids, skip_special_tokens=True, clean_up_tokenization_spaces=False
                )
                if text.startswith(self.text) and not text.endswith("\ufffd") and len(text) > len(self.text):
                    emit({"text": text[len(self.text) :]})
                    self.text = text

            def end(self):
                pass

        def infer():
            try:
                with torch.inference_mode():
                    generated = model.generate(
                        ids,
                        attention_mask=torch.ones_like(ids),
                        max_new_tokens=body.max_output,
                        do_sample=False,
                        pad_token_id=tokenizer.eos_token_id,
                        streamer=Stream(),
                        stopping_criteria=StoppingCriteriaList([Stop()]),
                    )
                inputs = ids.shape[1]
                outputs = generated.shape[1] - inputs
                # The upstream can produce its receipt after the gateway has lost its stream.
                # Persist counts before returning them; never write the prompt or generation.
                with closing(sqlite3.connect(receipt_file)) as db, db:
                    db.execute("INSERT INTO receipts VALUES (?,?,?)", (str(body.request_id), inputs, outputs))
                emit({"usage": {"input_tokens": inputs, "output_tokens": outputs}})
            except Exception:
                emit({"error": "inference_failed"})
            finally:
                slots.release()
                emit(None)

        async def events():
            try:
                while (item := await queue.get()) is not None:
                    yield (json.dumps(item, ensure_ascii=False) + "\n").encode()
            finally:
                stopped.set()

        if not slots.acquire(blocking=False):
            raise HTTPException(429, "Local model concurrency reached")
        threading.Thread(target=infer, daemon=True).start()
        return StreamingResponse(events(), media_type="application/x-ndjson")

    return app
