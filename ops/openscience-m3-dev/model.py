"""Deterministic tool-choosing development model. OpenScience executes the actual MCP tool."""

import asyncio
import json
import runpy
import threading
import uuid
from http.server import ThreadingHTTPServer

fixture = runpy.run_path("/fixture.py")
transport = runpy.run_path("/m2-loopback.py")


class Model(fixture["Fixture"]):
    def completion(self, request):
        messages = request.get("messages", [])
        if not request.get("stream") or not messages or messages[-1]["role"] != "user":
            return super().completion(request)
        content = messages[-1].get("content", "")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
        prefix = "M3-TEST-CALL "
        line = next((line for line in content.splitlines() if line.startswith(prefix)), "")
        if not line:
            return super().completion(request)
        data, _ = json.JSONDecoder().raw_decode(line[len(prefix) :])
        name = "capability_gateway_" + data["tool"]
        if not any(t.get("function", {}).get("name") == name for t in request.get("tools", [])):
            return super().completion(request)
        call = {
            "index": 0,
            "id": "call_" + uuid.uuid4().hex,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(data["arguments"])},
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in [({"role": "assistant", "tool_calls": [call]}, None), ({}, "tool_calls")]:
            chunk = {
                "id": "m3-fixture",
                "model": "m0",
                "created": 1,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 18080), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    asyncio.run(transport["main"]())
