// @vitest-environment node
import { createServer as createHttpServer, request } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createLogger, createServer } from "vite";
import type { ViteDevServer } from "vite";
import { developmentMcpProxy } from "./development-mcp-proxy.ts";

const cleanups: (() => Promise<unknown>)[] = [];
afterEach(async () => {
  vi.restoreAllMocks();
  while (cleanups.length) await cleanups.pop()!();
});

async function listen(server: Server): Promise<number> {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return (server.address() as { port: number }).port;
}

async function fixture(handler: (request: IncomingMessage, response: ServerResponse) => void) {
  const upstream = createHttpServer(handler);
  const upstreamPort = await listen(upstream);
  cleanups.push(() => new Promise<void>((resolve) => { upstream.closeAllConnections(); upstream.close(() => resolve()); }));
  const directory = await mkdtemp(join(tmpdir(), "nexpoly-mcp-proxy-"));
  cleanups.push(() => rm(directory, { recursive: true, force: true }));
  const logger = createLogger("info", { allowClearScreen: false });
  const logs = [vi.spyOn(logger, "error"), vi.spyOn(logger, "warn"), vi.spyOn(logger, "info")];
  for (const spy of logs) spy.mockImplementation(() => {});
  const vite: ViteDevServer = await createServer({
    configFile: false, root: directory, appType: "custom", customLogger: logger,
    server: { host: "127.0.0.1", port: 0, hmr: false },
    plugins: [developmentMcpProxy(`http://127.0.0.1:${upstreamPort}`, "localhost:9001")]
  });
  await vite.listen();
  cleanups.push(() => vite.close());
  const port = (vite.httpServer!.address() as { port: number }).port;
  return { port, upstreamPort, logs };
}

function call(port: number, path = "/mcp", headers: Record<string, string> = {}, body = "{}") {
  return new Promise<{ status: number; headers: IncomingMessage["headers"]; body: string }>((resolve, reject) => {
    const outgoing = request({ hostname: "127.0.0.1", port, path, method: "POST", headers: {
      Host: "localhost:9001", "Content-Type": "application/json", ...headers
    } }, (response) => {
      let payload = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { payload += chunk; });
      response.on("end", () => resolve({ status: response.statusCode!, headers: response.headers, body: payload }));
      response.on("error", reject);
    });
    outgoing.on("error", reject);
    outgoing.end(body);
  });
}

describe("development MCP proxy", () => {
  it("is opt-in and rejects targets containing credentials without echoing them", () => {
    expect(developmentMcpProxy()).toBeUndefined();
    for (const target of ["http://user:private-token@localhost", "http://localhost/mcp", "http://localhost?token=private-token", "https://localhost", "private-token"]) {
      try {
        developmentMcpProxy(target);
        expect.fail("Unsafe target accepted");
      } catch (error) {
        expect(String(error)).not.toContain("private-token");
      }
    }
    expect(() => developmentMcpProxy("http://localhost", "*")).toThrow("explicit host:port");
    const plugin = developmentMcpProxy("http://localhost")!;
    expect(plugin.apply).toBe("serve");
    expect(plugin.configurePreviewServer).toBeUndefined();
  });

  it("streams actual JSON-RPC bodies and preserves authentication, Origin and MCP headers", async () => {
    let captured: IncomingMessage["headers"] = {};
    let capturedBody = "";
    const service = await fixture((incoming, response) => {
      captured = incoming.headers;
      incoming.setEncoding("utf8");
      incoming.on("data", (chunk) => { capturedBody += chunk; });
      incoming.on("end", () => {
        response.writeHead(200, { "Content-Type": "application/json", "Mcp-Session-Id": "test-session", Connection: "x-hop-response", "X-Hop-Response": "private-hop" });
        response.end('{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}');
      });
    });
    const body = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}';
    const result = await call(service.port, "/mcp", {
      Authorization: "Bearer synthetic-token", Origin: "https://untrusted.example",
      Accept: "application/json, text/event-stream", "Mcp-Session-Id": "test-session",
      "Mcp-Protocol-Version": "2025-11-25", "Last-Event-ID": "event-2", Connection: "x-hop-request", "X-Hop-Request": "private-hop"
    }, body);
    expect(captured.host).toBe(`127.0.0.1:${service.upstreamPort}`);
    expect(captured.authorization).toBe("Bearer synthetic-token");
    expect(captured.origin).toBe("https://untrusted.example");
    expect(captured["mcp-session-id"]).toBe("test-session");
    expect(captured["mcp-protocol-version"]).toBe("2025-11-25");
    expect(captured["last-event-id"]).toBe("event-2");
    expect(captured["x-hop-request"]).toBeUndefined();
    expect(captured["content-length"]).toBe(String(Buffer.byteLength(body)));
    expect(captured["transfer-encoding"]).toBeUndefined();
    expect(capturedBody).toBe(body);
    expect(result.status).toBe(200);
    expect(JSON.parse(result.body).result).toEqual({ tools: [] });
    expect(result.headers["mcp-session-id"]).toBe("test-session");
    expect(result.headers["x-hop-response"]).toBeUndefined();
  });

  it("does not inject credentials and preserves upstream rejection responses", async () => {
    const service = await fixture((incoming, response) => {
      expect(incoming.headers.authorization).toBeUndefined();
      response.writeHead(401, { "WWW-Authenticate": "Bearer", "X-Request-Id": "synthetic-request" });
      response.end('{"error":"invalid_token"}');
    });
    const result = await call(service.port);
    expect(result.status).toBe(401);
    expect(result.headers["www-authenticate"]).toBe("Bearer");
    expect(result.headers["x-request-id"]).toBe("synthetic-request");
  });

  it("accepts only the exact route, preserves queries and checks Host before rewriting", async () => {
    const paths: string[] = [];
    const service = await fixture((incoming, response) => {
      paths.push(incoming.url!);
      response.writeHead(400);
      response.end('{"error":"query_parameters_not_supported"}');
    });
    expect((await call(service.port, "/mcp/?bad=1")).status).toBe(400);
    expect(paths).toEqual(["/mcp?bad=1"]);
    expect((await call(service.port, "/mcp", { Host: "attacker.example:9001" })).status).toBe(403);
    expect((await call(service.port, "/mcp", { Host: "127.0.0.2:9001" })).status).toBe(421);
    expect((await call(service.port, "/mcp", { Connection: "Origin", Origin: "https://untrusted.example" })).status).toBe(400);
    for (const path of ["/mcp-other", "/mcp/healthz", "/mcp//", "/healthz", "/api", "/%6dcp"]) {
      await call(service.port, path);
    }
    expect(paths).toEqual(["/mcp?bad=1"]);
  });

  it("keeps the upstream 32 KiB limit and error payload without buffering or truncation", async () => {
    let size = 0;
    const service = await fixture((incoming, response) => {
      incoming.on("data", (chunk) => { size += chunk.length; });
      incoming.on("end", () => {
        response.writeHead(size > 32768 ? 413 : 200);
        response.end('{"error":"body_too_large"}');
      });
    });
    const result = await call(service.port, "/mcp", {}, "x".repeat(32769));
    expect(size).toBe(32769);
    expect(result.status).toBe(413);
    expect(result.body).toBe('{"error":"body_too_large"}');
  });

  it("returns a fixed error without logging secret query values or headers", async () => {
    const service = await fixture((incoming) => incoming.socket.destroy());
    const result = await call(service.port, "/mcp?token=synthetic-query-secret", { Authorization: "Bearer synthetic-header-secret" });
    expect(result.status).toBe(502);
    expect(result.body).toBe('{"error":"mcp_upstream_unavailable"}');
    const logs = service.logs.flatMap((spy) => spy.mock.calls).flat().join(" ");
    expect(logs).not.toContain("synthetic-query-secret");
    expect(logs).not.toContain("synthetic-header-secret");
    expect(logs).not.toContain(`127.0.0.1:${service.upstreamPort}`);
  });

  it("streams SSE immediately and closes the upstream when the caller disconnects", async () => {
    let upstreamClosed = false;
    const service = await fixture((incoming, response) => {
      incoming.resume();
      response.on("close", () => { upstreamClosed = true; });
      response.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
      response.write('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n');
      // Leave the upstream response open: the proxy must not wait for its end.
    });
    const firstChunk = await new Promise<string>((resolve, reject) => {
      const outgoing = request({ hostname: "127.0.0.1", port: service.port, path: "/mcp", method: "POST",
        headers: { Host: "localhost:9001", Accept: "text/event-stream" } }, (response) => {
        expect(response.headers["content-type"]).toBe("text/event-stream");
        response.once("data", (chunk) => {
          resolve(chunk.toString());
          response.destroy();
        });
        response.on("error", reject);
      });
      outgoing.on("error", reject);
      outgoing.end("{}");
    });
    expect(firstChunk).toContain('data: {"jsonrpc":"2.0"');
    await vi.waitFor(() => expect(upstreamClosed).toBe(true));
  });

  it("bounds a stalled upstream and closes its request", async () => {
    let closed = false;
    const service = await fixture((incoming) => incoming.on("close", () => { closed = true; }));
    const actualTimeout = globalThis.setTimeout;
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((handler, duration, ...args) =>
      actualTimeout(handler, duration === 35_000 ? 40 : duration, ...args)) as typeof setTimeout);
    const result = await call(service.port);
    expect(result.status).toBe(504);
    expect(result.body).toBe('{"error":"mcp_upstream_timeout"}');
    await vi.waitFor(() => expect(closed).toBe(true));
  });
});
