import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { mkdtemp, chmod, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import {
  PROPERTY_FILTER_TOOLS, PropertyFilterProxy, propertyFilterConfiguration,
  propertyFilterProxy, readPropertyFilterToken,
} from "./property-filter";
import { handleMcpRequest } from "./mcp";
import { createHandler } from "./server";
import { ZhijuClient } from "./zhiju-client";
import { TOOLS } from "./tools";

const token = "independent-test-token-" + "a".repeat(40);
const cleanups: Array<() => unknown | Promise<unknown>> = [];
afterEach(async () => { while (cleanups.length) await cleanups.pop()!(); });

const filterTools = PROPERTY_FILTER_TOOLS.map(name => ({
  name, description: `Description of ${name}`,
  inputSchema: { type: "object" as const, properties: { limit: { type: "integer" } } },
  outputSchema: { type: "object" as const, properties: { count: { type: "integer" } } },
  annotations: { readOnlyHint: true, destructiveHint: false },
  _meta: { "test/schema": "preserved" },
}));

async function fakeMcp(options: {
  mode?: "auth" | "timeout" | "redirect"; result?: Record<string, unknown>; tools?: typeof filterTools;
} = {}) {
  const calls: Array<{ method: string; authorization: string | null; params?: unknown }> = [];
  const server = Bun.serve({
    hostname: "127.0.0.1", port: 0,
    async fetch(request) {
      const clone = request.clone();
      const body = await clone.json().catch(() => ({}));
      calls.push({ method: body.method, authorization: request.headers.get("authorization"), params: body.params });
      if (options.mode === "redirect") return new Response(null, { status: 302, headers: { Location: "/forbidden-redirect" } });
      if (options.mode === "auth" || request.headers.get("authorization") !== `Bearer ${token}`) {
        return Response.json({ error: "secret upstream diagnostic" }, { status: 401 });
      }
      if (options.mode === "timeout") {
        await new Promise(resolve => setTimeout(resolve, 300));
        return new Response("secret timeout diagnostic", { status: 503 });
      }
      const mcp = new Server({ name: "fake-filter", version: "1.0.0" }, { capabilities: { tools: {} } });
      mcp.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: options.tools ?? filterTools }));
      mcp.setRequestHandler(CallToolRequestSchema, async () => options.result as never ?? {
        content: [{ type: "text", text: "filtered" }], structuredContent: { count: 2 },
      });
      const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
      await mcp.connect(transport);
      try { return await transport.handleRequest(request); }
      finally { await mcp.close(); }
    },
  });
  cleanups.push(() => server.stop(true));
  const proxy = new PropertyFilterProxy(
    () => ({ url: `http://127.0.0.1:${server.port}/mcp`, tokenFile: "/unused", timeout: options.mode === "timeout" ? 100 : 1000 }),
    { readToken: async () => token },
  );
  return { proxy, calls };
}

describe("upstream property-filter MCP proxy", () => {
  test("passes original input/output schemas, annotations and metadata; excludes unexpected tools", async () => {
    const { proxy } = await fakeMcp({ tools: [...filterTools, { ...filterTools[0]!, name: "delete_everything" }] });
    expect(await proxy.listTools()).toEqual(filterTools);
  });

  test("passes structured results, content annotations, metadata and isError without rewrapping", async () => {
    const result = {
      content: [{ type: "text", text: "data", annotations: { audience: ["assistant"], priority: 0.6 } }],
      structuredContent: { count: 2, dataset_revision: "revision-test" },
      _meta: { "test/result": "preserved" }, isError: true, customExtension: { retained: true },
    };
    const { proxy, calls } = await fakeMcp({ result });
    expect(await proxy.callTool("search_polymers", { limit: 2 })).toEqual(result);
    expect(calls.every(call => call.authorization === `Bearer ${token}`)).toBe(true);
    expect(calls.find(call => call.method === "tools/call")?.params).toEqual({ name: "search_polymers", arguments: { limit: 2 } });
  });

  test("refuses unknown tool names before reading credentials or contacting upstream", async () => {
    const readToken = spyOn({ read: async () => token }, "read");
    const proxy = new PropertyFilterProxy(propertyFilterConfiguration, { readToken });
    expect((await proxy.callTool("delete_everything", {})).isError).toBe(true);
    expect(readToken).not.toHaveBeenCalled();
  });

  test("sanitizes auth, timeout and unavailable-schema failures", async () => {
    for (const mode of ["auth", "timeout"] as const) {
      const { proxy } = await fakeMcp({ mode });
      const started = Date.now();
      const result = await proxy.callTool("search_polymers", {});
      expect(result.isError).toBe(true);
      expect(JSON.stringify(result)).not.toContain("secret");
      expect(JSON.stringify(result)).not.toContain(token);
      expect(Date.now() - started).toBeLessThan(1500);
      await expect(proxy.listTools()).rejects.toThrow("Property filtering is temporarily unavailable");
    }
    const { proxy } = await fakeMcp({ tools: filterTools.slice(0, 2) });
    await expect(proxy.listTools()).rejects.toThrow("Property filtering is temporarily unavailable");
  });

  test("deployment configuration is restricted to fixed URL, token mount and bounded timeout", () => {
    for (const env of [
      { PROPERTY_FILTER_MCP_URL: "http://untrusted.invalid/mcp" },
      { PROPERTY_FILTER_MCP_TOKEN_FILE: "/outside/token" },
      { PROPERTY_FILTER_MCP_TIMEOUT_MS: "0" }, { PROPERTY_FILTER_MCP_TIMEOUT_MS: "60001" },
    ]) expect(() => propertyFilterConfiguration(env)).toThrow("Invalid property-filter gateway configuration");
    expect(propertyFilterConfiguration({}).timeout).toBe(30000);
  });

  test("does not follow upstream redirects or accept oversized responses", async () => {
    const redirect = await fakeMcp({ mode: "redirect" });
    expect((await redirect.proxy.callTool("search_polymers", {})).isError).toBe(true);
    expect(redirect.calls).toHaveLength(1);
    const oversized = await fakeMcp({ result: { content: [{ type: "text", text: "x".repeat(9 * 1024 * 1024) }] } });
    const result = await oversized.proxy.callTool("search_polymers", {});
    expect(result.isError).toBe(true);
    expect(JSON.stringify(result).length).toBeLessThan(200);
  });

  test("credential file must be owner-only, ordinary and a valid token", async () => {
    const root = await mkdtemp(join(tmpdir(), "gateway-filter-"));
    cleanups.push(() => rm(root, { recursive: true, force: true }));
    const target = join(root, "token");
    await writeFile(target, token + "\n", { mode: 0o600 });
    expect(await readPropertyFilterToken(target)).toBe(token);
    await chmod(target, 0o640);
    await expect(readPropertyFilterToken(target)).rejects.toThrow("Invalid property-filter gateway credential file");
    await chmod(target, 0o600);
    const link = join(root, "link");
    await symlink(target, link);
    await expect(readPropertyFilterToken(link)).rejects.toThrow("Invalid property-filter gateway credential file");
    await writeFile(target, "short");
    await expect(readPropertyFilterToken(target)).rejects.toThrow("Invalid property-filter gateway credential");
  });
});

async function gatewayRpc(method: string, params: Record<string, unknown> = {}) {
  const response = await handleMcpRequest(new Request("http://gateway/mcp", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  }));
  const payload = await response.json() as { result?: Record<string, unknown>; error?: unknown };
  expect(payload.error).toBeUndefined();
  return payload.result!;
}

describe("patched gateway routing", () => {
  test("the gateway still requires its own bearer credential before discovering upstream tools", async () => {
    const list = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    cleanups.push(() => list.mockRestore());
    const handler = createHandler({ ZHIJU_CAPABILITY_TOKEN: "independent-inbound-gateway-token" });
    for (const authorization of [undefined, "Bearer incorrect-token"]) {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (authorization) headers.Authorization = authorization;
      const response = await handler(new Request("http://gateway/mcp", {
        method: "POST", headers,
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }),
      }));
      expect(response.status).toBe(401);
    }
    expect(list).not.toHaveBeenCalled();
  });

  test("lists all original six tools plus three complete upstream tool schemas", async () => {
    const list = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    cleanups.push(() => list.mockRestore());
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...filterTools]);
  });

  test("upstream discovery failure leaves the six existing tools available", async () => {
    const list = spyOn(propertyFilterProxy, "listTools").mockRejectedValue(new Error("private diagnostics"));
    cleanups.push(() => list.mockRestore());
    expect((await gatewayRpc("tools/list")).tools).toEqual(TOOLS);
  });

  test("filter route returns the original structured result", async () => {
    const expected = { content: [{ type: "text" as const, text: "result" }], structuredContent: { count: 12 }, _meta: { test: true }, isError: true };
    const call = spyOn(propertyFilterProxy, "callTool").mockResolvedValue(expected);
    cleanups.push(() => call.mockRestore());
    expect(await gatewayRpc("tools/call", { name: "search_polymers", arguments: { limit: 2 } })).toEqual(expected);
    expect(call).toHaveBeenCalledWith("search_polymers", { limit: 2 });
  });

  test("all six pre-existing tool branches retain their original dispatch and response", async () => {
    const cases = [
      ["predict_polymer_properties", "predict", { smiles: "*CC*", properties: ["Glass transition temperature"] }],
      ["polymerize_monomers", "polymerizeMonomers", { monomer_a_smiles: "C=C", target_class: "polyolefin" }],
      ["dft_job_submit", "submitDftJob", { smiles: "C", calculation_type: "single_point" }],
      ["dft_job_status", "getDftJobStatus", { job_id: "fake-job" }],
      ["dft_job_list", "listDftJobs", { page_size: 2 }],
      ["dft_job_result", "getDftJobResult", { job_id: "fake-job" }],
    ] as const;
    for (const [name, method, args] of cases) {
      const mock = spyOn(ZhijuClient.prototype, method).mockResolvedValue({ retained: name } as never);
      try {
        expect(await gatewayRpc("tools/call", { name, arguments: args })).toEqual({ content: [{ type: "text", text: JSON.stringify({ retained: name }) }] });
        expect(mock).toHaveBeenCalledTimes(1);
      } finally { mock.mockRestore(); }
    }
  });
});
