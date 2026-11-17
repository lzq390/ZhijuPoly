import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { chmod, link, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import * as polytao from "./polytao";
import { propertyFilterProxy, PROPERTY_FILTER_TOOLS } from "./property-filter";
import { handleMcpRequest } from "./mcp";
import { createHandler } from "./server";
import { TOOLS } from "./tools";

const modelToken = "upstream-model-" + "m".repeat(40);
const uiToken = "upstream-ui-" + "u".repeat(40);
const gatewayToken = "inbound-model-" + "g".repeat(40);
const gatewayUiToken = "inbound-ui-" + "i".repeat(40);
const cleanups: Array<() => unknown | Promise<unknown>> = [];
afterEach(async () => { while (cleanups.length) await cleanups.pop()!(); });
const schemas = (names: readonly string[]) => names.map(name => ({
  name, description: name, inputSchema: { type: "object" as const, properties: { draft_id: { type: "string" } } },
  outputSchema: { type: "object" as const, properties: { ok: { type: "boolean" } } },
  annotations: { readOnlyHint: name.startsWith("get_"), destructiveHint: false },
  _meta: { "schema/kept": true },
}));
const modelTools = schemas(polytao.POLYTAO_MODEL_TOOLS);
const uiTools = schemas(polytao.POLYTAO_UI_TOOLS);
const filterTools = schemas(PROPERTY_FILTER_TOOLS);

async function fakeMcp(options: { mode?: "auth" | "timeout" | "redirect"; incomplete?: boolean; oversized?: boolean } = {}) {
  const calls: Array<{ method: string; authorization: string | null; params?: unknown }> = [];
  const result = {
    content: [{ type: "text", text: "form", annotations: { audience: ["assistant"] } }],
    structuredContent: { ok: true, draft_id: "draft.test", revision: 2 },
    _meta: { "result/kept": true }, customExtension: { kept: true },
  };
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(request) {
    const body = await request.clone().json().catch(() => ({}));
    const authorization = request.headers.get("authorization");
    calls.push({ method: body.method, authorization, params: body.params });
    if (options.mode === "redirect") return new Response(null, { status: 302, headers: { Location: "/redirect" } });
    if (options.mode === "auth" || ![`Bearer ${modelToken}`, `Bearer ${uiToken}`].includes(authorization ?? "")) {
      return Response.json({ error: "secret upstream diagnostic" }, { status: 401 });
    }
    if (options.mode === "timeout") {
      await new Promise(resolve => setTimeout(resolve, 300));
      return new Response("secret upstream diagnostic", { status: 503 });
    }
    const tools = authorization === `Bearer ${uiToken}` ? uiTools : modelTools;
    const mcp = new Server({ name: "fake-polytao", version: "1" }, { capabilities: { tools: {} } });
    mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: options.incomplete ? tools.slice(0, 1) : [...tools, ...schemas(["delete_everything"])],
    }));
    mcp.setRequestHandler(CallToolRequestSchema, async () => options.oversized
      ? { content: [{ type: "text", text: "x".repeat(9 * 1024 * 1024) }] } : result as never);
    const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    await mcp.connect(transport);
    try { return await transport.handleRequest(request); } finally { await mcp.close(); }
  } });
  cleanups.push(() => server.stop(true));
  const proxy = new polytao.PolytaoProxy(() => ({
    ...polytao.polytaoConfiguration({ POLYTAO_MCP_ENABLED: "true" }),
    url: `http://127.0.0.1:${server.port}/mcp`, timeout: options.mode === "timeout" ? 100 : 1000,
  }), { readToken: async filename => filename === polytao.POLYTAO_UI_TOKEN_FILE ? uiToken : modelToken });
  return { proxy, calls, result };
}

describe("PolyTAO MCP upstream", () => {
  test("preserves exact model/UI schemas and ignores unexpected upstream tools", async () => {
    const { proxy, calls } = await fakeMcp();
    expect(await proxy.listTools("model")).toEqual(modelTools);
    expect(await proxy.listTools("ui")).toEqual(uiTools);
    expect(calls.filter(call => call.method === "tools/list").map(call => call.authorization))
      .toEqual([`Bearer ${modelToken}`, `Bearer ${uiToken}`]);
  });

  test("preserves full structured MCP results and routes shared getdraft by audience", async () => {
    const { proxy, calls, result } = await fakeMcp();
    for (const audience of ["model", "ui"] as const) {
      expect(await proxy.callTool("get_polytao_draft", { conversation_id: "ses_test", draft_id: "draft.test" }, audience)).toEqual(result);
    }
    expect(calls.filter(call => call.method === "tools/call").map(call => call.authorization))
      .toEqual([`Bearer ${modelToken}`, `Bearer ${uiToken}`]);
    expect(calls.find(call => call.method === "tools/call")?.params).toEqual({
      name: "get_polytao_draft", arguments: { conversation_id: "ses_test", draft_id: "draft.test" },
    });
  });

  test("rejects cross-audience and unknown calls before credentials or network", async () => {
    const readToken = spyOn({ read: async () => modelToken }, "read");
    const proxy = new polytao.PolytaoProxy(() => polytao.polytaoConfiguration({ POLYTAO_MCP_ENABLED: "true" }), { readToken });
    for (const name of ["confirm_polytao_generation", "update_polytao_draft", "cancel_polytao_draft", "submit_polytao_generation", "delete_everything"]) {
      expect((await proxy.callTool(name, {}, "model")).isError).toBe(true);
    }
    for (const name of ["prepare_polytao_generation", "get_polytao_job", "predict_polymer_properties"]) {
      expect((await proxy.callTool(name, {}, "ui")).isError).toBe(true);
    }
    expect(readToken).not.toHaveBeenCalled();
  });

  test("rejects collapsed upstream model/UI identities", async () => {
    const proxy = new polytao.PolytaoProxy(() => polytao.polytaoConfiguration({ POLYTAO_MCP_ENABLED: "true" }), {
      readToken: async () => modelToken,
      fetch: async () => { throw new Error("must not contact network"); },
    });
    expect((await proxy.callTool("prepare_polytao_generation", {})).isError).toBe(true);
    await expect(proxy.listTools()).rejects.toThrow("temporarily unavailable");
  });

  test("sanitizes auth, timeout, partial registry, redirect and oversize failures", async () => {
    const failures: Array<{ mode?: "auth" | "timeout" | "redirect"; oversized?: boolean }> = [
      { mode: "auth" }, { mode: "timeout" }, { mode: "redirect" }, { oversized: true },
    ];
    for (const options of failures) {
      const { proxy, calls } = await fakeMcp(options);
      const started = Date.now();
      const result = await proxy.callTool("get_polytao_capabilities", {});
      expect(result.isError).toBe(true);
      expect(JSON.stringify(result)).not.toContain("secret");
      expect(JSON.stringify(result)).not.toContain(modelToken);
      expect(Date.now() - started).toBeLessThan(2000);
      if (options.mode === "redirect") expect(calls).toHaveLength(1);
    }
    const { proxy } = await fakeMcp({ incomplete: true });
    await expect(proxy.listTools()).rejects.toThrow("temporarily unavailable");
  });

  test("deployment config only permits reviewed URL, private mounts and bounded timeout", () => {
    for (const env of [
      { POLYTAO_MCP_ENABLED: "yes" }, { POLYTAO_MCP_URL: "http://untrusted.invalid/mcp" },
      { POLYTAO_MCP_MODEL_TOKEN_FILE: "/tmp/token" }, { POLYTAO_MCP_UI_TOKEN_FILE: "/tmp/token" },
      { POLYTAO_GATEWAY_UI_TOKEN_FILE: "/tmp/token" },
      { POLYTAO_MCP_TIMEOUT_MS: "0" }, { POLYTAO_MCP_TIMEOUT_MS: "60001" },
    ]) expect(() => polytao.polytaoConfiguration(env)).toThrow("Invalid PolyTAO gateway configuration");
    expect(polytao.polytaoConfiguration({}).enabled).toBe(false);
  });

  test("disabled integration preserves the previous gateway without reading credentials", async () => {
    const readToken = spyOn({ read: async () => modelToken }, "read");
    const proxy = new polytao.PolytaoProxy(() => polytao.polytaoConfiguration({}), { readToken });
    expect(await proxy.listTools()).toEqual([]);
    expect((await proxy.callTool("get_polytao_capabilities", {})).isError).toBe(true);
    expect(readToken).not.toHaveBeenCalled();
  });

  test("private credential file rejects permissions, symlinks, hardlinks and invalid content", async () => {
    const root = await mkdtemp(join(tmpdir(), "gateway-polytao-"));
    cleanups.push(() => rm(root, { recursive: true, force: true }));
    const target = join(root, "token");
    await writeFile(target, modelToken + "\n", { mode: 0o600 });
    expect(await polytao.readPolytaoToken(target)).toBe(modelToken);
    await chmod(target, 0o640);
    await expect(polytao.readPolytaoToken(target)).rejects.toThrow("credential file");
    await chmod(target, 0o600);
    await symlink(target, join(root, "symlink"));
    await expect(polytao.readPolytaoToken(join(root, "symlink"))).rejects.toThrow("credential file");
    await link(target, join(root, "hardlink"));
    await expect(polytao.readPolytaoToken(target)).rejects.toThrow("credential file");
    await rm(join(root, "hardlink"));
    await writeFile(target, "short");
    await expect(polytao.readPolytaoToken(target)).rejects.toThrow("credential");
    await writeFile(target, "x".repeat(4098));
    await expect(polytao.readPolytaoToken(target)).rejects.toThrow("credential file");
  });

  test("inbound UI auth is distinct, enabled explicitly and checked on every request", async () => {
    const env = { POLYTAO_MCP_ENABLED: "true", ZHIJU_CAPABILITY_TOKEN: gatewayToken };
    const read = spyOn({ read: async () => gatewayUiToken }, "read");
    expect(await polytao.authenticatePolytaoUi(`Bearer ${gatewayUiToken}`, env, read)).toBe(true);
    expect(await polytao.authenticatePolytaoUi(`Bearer ${gatewayUiToken}`, env, read)).toBe(true);
    expect(read).toHaveBeenCalledTimes(2);
    expect(await polytao.authenticatePolytaoUi(`Bearer ${gatewayToken}`, env, read)).toBe(false);
    expect(await polytao.authenticatePolytaoUi(`Bearer ${gatewayUiToken}`, {}, read)).toBe(false);
    expect(await polytao.authenticatePolytaoUi(`Bearer ${gatewayToken}`, env, async () => gatewayToken)).toBe(false);
    expect(await polytao.authenticatePolytaoUi("Bearer wrong", env, read)).toBe(false);
    expect(await polytao.authenticatePolytaoUi(`Bearer ${gatewayUiToken}`, env, async () => { throw Error("secret"); })).toBe(false);
  });
});

async function gatewayRpc(method: string, params: Record<string, unknown> = {}, audience: polytao.PolytaoAudience = "model") {
  const response = await handleMcpRequest(new Request("http://gateway/mcp", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  }), audience);
  const payload = await response.json() as { result?: Record<string, unknown>; error?: unknown };
  expect(payload.error).toBeUndefined();
  return payload.result!;
}

describe("PolyTAO capability gateway routing", () => {
  test("model lists exactly original six, property three and PolyTAO five", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const list = spyOn(polytao.polytaoProxy, "listTools").mockResolvedValue(modelTools);
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect(TOOLS.map(tool => tool.name)).toEqual([
      "predict_polymer_properties", "dft_job_submit", "dft_job_status", "dft_job_list", "dft_job_result", "polymerize_monomers",
    ]);
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...filterTools, ...modelTools]);
    expect(list).toHaveBeenCalledWith("model");
  });

  test("PolyTAO failure preserves all original six and property three", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const list = spyOn(polytao.polytaoProxy, "listTools").mockRejectedValue(Error("secret"));
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...filterTools]);
  });

  test("property failure preserves original six and PolyTAO five", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockRejectedValue(Error("secret"));
    const list = spyOn(polytao.polytaoProxy, "listTools").mockResolvedValue(modelTools);
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...modelTools]);
  });

  test("UI lists only UI tools without contacting property filtering", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const list = spyOn(polytao.polytaoProxy, "listTools").mockResolvedValue(uiTools);
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect((await gatewayRpc("tools/list", {}, "ui")).tools).toEqual(uiTools);
    expect(list).toHaveBeenCalledWith("ui");
    expect(filter).not.toHaveBeenCalled();
  });

  test("rejects model confirmation/mutations and UI access to model/native/property tools", async () => {
    const call = spyOn(polytao.polytaoProxy, "callTool");
    cleanups.push(() => call.mockRestore());
    for (const name of ["confirm_polytao_generation", "update_polytao_draft", "cancel_polytao_draft"]) {
      expect((await gatewayRpc("tools/call", { name, arguments: {} })).isError).toBe(true);
    }
    for (const name of ["prepare_polytao_generation", "get_polytao_job", "dft_job_submit", "search_polymers"]) {
      expect((await gatewayRpc("tools/call", { name, arguments: {} }, "ui")).isError).toBe(true);
    }
    expect(call).not.toHaveBeenCalled();
  });

  test("shared draft route retains structured output and forwards the authenticated audience", async () => {
    const expected = { content: [{ type: "text" as const, text: "form" }], structuredContent: { draft_id: "d", revision: 2 } };
    const call = spyOn(polytao.polytaoProxy, "callTool").mockResolvedValue(expected);
    cleanups.push(() => call.mockRestore());
    for (const audience of ["model", "ui"] as const) {
      expect(await gatewayRpc("tools/call", { name: "get_polytao_draft", arguments: { draft_id: "d" } }, audience)).toEqual(expected);
      expect(call).toHaveBeenLastCalledWith("get_polytao_draft", { draft_id: "d" }, audience);
    }
  });

  test("server authenticates each POST even with a reused MCP session header", async () => {
    const auth = spyOn(polytao, "authenticatePolytaoUi").mockImplementation(async authorization => authorization === `Bearer ${gatewayUiToken}`);
    const list = spyOn(polytao.polytaoProxy, "listTools").mockImplementation(async audience => audience === "ui" ? uiTools : modelTools);
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const call = spyOn(polytao.polytaoProxy, "callTool").mockResolvedValue({ content: [{ type: "text", text: "ok" }] });
    cleanups.push(() => { auth.mockRestore(); list.mockRestore(); filter.mockRestore(); call.mockRestore(); });
    const handler = createHandler({ ZHIJU_CAPABILITY_TOKEN: gatewayToken });
    const request = (authorization: string, params?: Record<string, unknown>) => handler(new Request("http://gateway/mcp", {
      method: "POST", headers: {
        "Content-Type": "application/json", Accept: "application/json, text/event-stream",
        Authorization: authorization, "mcp-session-id": "session-from-another-credential",
      }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: params ? "tools/call" : "tools/list", params: params ?? {} }),
    }));
    expect((await request("Bearer incorrect")).status).toBe(401);
    expect((await (await request(`Bearer ${gatewayUiToken}`)).json()).result.tools).toEqual(uiTools);
    expect((await (await request(`Bearer ${gatewayToken}`)).json()).result.tools).toEqual([...TOOLS, ...filterTools, ...modelTools]);
    const denied = await (await request(`Bearer ${gatewayToken}`, { name: "confirm_polytao_generation", arguments: {} })).json();
    expect(denied.result.isError).toBe(true);
    const reverse = await (await request(`Bearer ${gatewayUiToken}`, { name: "dft_job_submit", arguments: {} })).json();
    expect(reverse.result.isError).toBe(true);
    expect(call).not.toHaveBeenCalled();
  });
});
