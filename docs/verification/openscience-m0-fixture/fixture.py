"""Loopback-only synthetic model, MCP services and probes for the M0 replay."""

import argparse
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode


BACKEND_URL = "http://127.0.0.1:4096"
MARKER = "M0-SYNTHETIC-OK"
TOKEN_USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def api(path, body=None, timeout=2):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        BACKEND_URL + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Leave stdout available for the machine-readable measurement output.
        pass

    def reply_json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/gateway/mcp", "/knowledge/mcp"):
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/chat/completions":
            self.completion(request)
        else:
            self.mcp(request)

    def completion(self, request):
        common = {"id": "m0-response", "model": "m0", "created": 1}
        if not request.get("stream"):
            choice = {
                "index": 0,
                "message": {"role": "assistant", "content": MARKER},
                "finish_reason": "stop",
            }
            self.reply_json({
                **common, "object": "chat.completion", "choices": [choice], "usage": TOKEN_USAGE
            })
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta in ({"role": "assistant"}, {"content": MARKER}):
            choice = {"index": 0, "delta": delta, "finish_reason": None}
            chunk = {**common, "object": "chat.completion.chunk", "choices": [choice]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.12)
        choice = {"index": 0, "delta": {}, "finish_reason": "stop"}
        final = {**common, "object": "chat.completion.chunk", "choices": [choice], "usage": TOKEN_USAGE}
        self.wfile.write(f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode())

    def mcp(self, request):
        role = self.path.split("/")[1]
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "m0-" + role, "version": "1.0"},
            }
        elif method == "tools/list":
            tool = {
                "name": "fixture_" + role + "_ping",
                "description": "Synthetic M0 tool",
                "inputSchema": {"type": "object", "properties": {}},
            }
            result = {"tools": [tool]}
        elif "id" not in request:
            self.send_response(202)
            self.end_headers()
            return
        else:
            result = {}
        self.reply_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


def probe(mcp_expected):
    deadline = time.monotonic() + 40
    health = workspace = None
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if health is None:
                response = api("/global/health", timeout=0.5)
                if response.get("healthy") is not True:
                    raise RuntimeError("health endpoint not ready")
                health = time.monotonic()
            if workspace is None:
                if not api("/project/current").get("id"):
                    raise RuntimeError("missing project ID")
                # Preserve the first usable workspace even if MCP needs more polls.
                workspace = time.monotonic()
            if mcp_expected:
                state = api("/mcp", timeout=5)
                for name in ("fixture_gateway", "fixture_knowledge"):
                    if state.get(name, {}).get("status") != "connected":
                        raise RuntimeError(f"{name} not ready")
            print(json.dumps({"health": health, "workspace": workspace, "full": time.monotonic()}), flush=True)
            return
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = str(exc)
            time.sleep(0.05)
    raise RuntimeError("readiness deadline; " + last_error)


def has_reply(message):
    return any(
        part.get("type") == "text" and MARKER in part.get("text", "")
        for part in message.get("parts", [])
    )


def chat(slot, rounds, files_root="/m0/files"):
    start = time.monotonic()
    sessions = []
    for kind, directory in (("general", f"{files_root}/general"), ("project", f"{files_root}/projects/m0-project")):
        query = "?" + urlencode({"directory": directory})
        session = api("/session" + query, {"title": f"M0 {kind} {slot}"}, timeout=20)
        path = f"/session/{session['id']}/message{query}"
        prompt = {
            "model": {"providerID": "m0fixture", "modelID": "m0"},
            "parts": [{"type": "text", "text": "Reply with the short M0 marker."}],
        }
        for _ in range(rounds):
            if not has_reply(api(path, prompt, timeout=90)):
                raise RuntimeError("fixture completion missing")
        completed = sum(has_reply(message) for message in api(path))
        if completed < rounds:
            raise RuntimeError("persisted readback incomplete")
        sessions.append({"type": kind, "replies_read_back": completed})
    print(json.dumps({"elapsed_s": round(time.monotonic() - start, 3), "sessions": sessions}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("probe").add_argument("mcp", nargs="?", choices=["mcp"])
    chat_args = commands.add_parser("chat")
    chat_args.add_argument("slot")
    chat_args.add_argument("rounds", type=int)
    chat_args.add_argument("--files-root", choices=("/m0/files", "/openscience/files"), default="/m0/files")
    args = parser.parse_args()
    if args.command == "serve":
        ThreadingHTTPServer(("127.0.0.1", 18080), Fixture).serve_forever()
    elif args.command == "probe":
        probe(args.mcp == "mcp")
    else:
        chat(args.slot, args.rounds, args.files_root)
