import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { chmod, link, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import * as retrosynthesis from "./retrosynthesis";
import { propertyFilterProxy, PROPERTY_FILTER_TOOLS } from "./property-filter";
import { handleMcpRequest } from "./mcp";
import { createHandler } from "./server";
import { TOOLS } from "./tools";
import { polytaoProxy, POLYTAO_MODEL_TOOLS, POLYTAO_UI_TOOLS } from "./polytao";

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
const modelTools = schemas(retrosynthesis.RETROSYNTHESIS_MODEL_TOOLS);
const uiTools = schemas(retrosynthesis.RETROSYNTHESIS_UI_TOOLS);
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
    const mcp = new Server({ name: "fake-retrosynthesis", version: "1" }, { capabilities: { tools: {} } });
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
  const proxy = new retrosynthesis.RetrosynthesisProxy(() => ({
    ...retrosynthesis.retrosynthesisConfiguration({ RETROSYNTHESIS_MCP_ENABLED: "true" }),
    url: `http://127.0.0.1:${server.port}/mcp`, timeout: options.mode === "timeout" ? 100 : 1000,
  }), { readToken: async filename => filename === retrosynthesis.RETROSYNTHESIS_UI_TOKEN_FILE ? uiToken : modelToken });
  return { proxy, calls, result };
}

describe("Retrosynthesis MCP upstream", () => {
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
      expect(await proxy.callTool("get_retrosynthesis_draft", { conversation_id: "ses_test", draft_id: "draft.test" }, audience)).toEqual(result);
    }
    expect(calls.filter(call => call.method === "tools/call").map(call => call.authorization))
      .toEqual([`Bearer ${modelToken}`, `Bearer ${uiToken}`]);
    expect(calls.find(call => call.method === "tools/call")?.params).toEqual({
      name: "get_retrosynthesis_draft", arguments: { conversation_id: "ses_test", draft_id: "draft.test" },
    });
  });

  test("rejects cross-audience and unknown calls before credentials or network", async () => {
    const readToken = spyOn({ read: async () => modelToken }, "read");
    const proxy = new retrosynthesis.RetrosynthesisProxy(() => retrosynthesis.retrosynthesisConfiguration({ RETROSYNTHESIS_MCP_ENABLED: "true" }), { readToken });
    for (const name of ["confirm_monomer_retrosynthesis", "update_retrosynthesis_draft", "cancel_retrosynthesis_draft", "submit_monomer_retrosynthesis", "delete_everything"]) {
      expect((await proxy.callTool(name, {}, "model")).isError).toBe(true);
    }
    for (const name of ["prepare_monomer_retrosynthesis", "get_retrosynthesis_job", "predict_polymer_properties"]) {
      expect((await proxy.callTool(name, {}, "ui")).isError).toBe(true);
    }
    expect(readToken).not.toHaveBeenCalled();
  });

  test("rejects collapsed upstream model/UI identities", async () => {
    const proxy = new retrosynthesis.RetrosynthesisProxy(() => retrosynthesis.retrosynthesisConfiguration({ RETROSYNTHESIS_MCP_ENABLED: "true" }), {
      readToken: async () => modelToken,
      fetch: async () => { throw new Error("must not contact network"); },
    });
    expect((await proxy.callTool("prepare_monomer_retrosynthesis", {})).isError).toBe(true);
    await expect(proxy.listTools()).rejects.toThrow("temporarily unavailable");
  });

  test("sanitizes auth, timeout, partial registry, redirect and oversize failures", async () => {
    const failures: Array<{ mode?: "auth" | "timeout" | "redirect"; oversized?: boolean }> = [
      { mode: "auth" }, { mode: "timeout" }, { mode: "redirect" }, { oversized: true },
    ];
    for (const options of failures) {
      const { proxy, calls } = await fakeMcp(options);
      const started = Date.now();
      const result = await proxy.callTool("get_retrosynthesis_capabilities", {});
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
      { RETROSYNTHESIS_MCP_ENABLED: "yes" }, { RETROSYNTHESIS_MCP_URL: "http://untrusted.invalid/mcp" },
      { RETROSYNTHESIS_MCP_MODEL_TOKEN_FILE: "/tmp/token" }, { RETROSYNTHESIS_MCP_UI_TOKEN_FILE: "/tmp/token" },
      { RETROSYNTHESIS_GATEWAY_UI_TOKEN_FILE: "/tmp/token" },
      { RETROSYNTHESIS_MCP_TIMEOUT_MS: "0" }, { RETROSYNTHESIS_MCP_TIMEOUT_MS: "60001" },
    ]) expect(() => retrosynthesis.retrosynthesisConfiguration(env)).toThrow("Invalid retrosynthesis gateway configuration");
    expect(retrosynthesis.retrosynthesisConfiguration({}).enabled).toBe(false);
  });

  test("disabled integration preserves the previous gateway without reading credentials", async () => {
    const readToken = spyOn({ read: async () => modelToken }, "read");
    const proxy = new retrosynthesis.RetrosynthesisProxy(() => retrosynthesis.retrosynthesisConfiguration({}), { readToken });
    expect(await proxy.listTools()).toEqual([]);
    expect((await proxy.callTool("get_retrosynthesis_capabilities", {})).isError).toBe(true);
    expect(readToken).not.toHaveBeenCalled();
  });

  test("private credential file rejects permissions, symlinks, hardlinks and invalid content", async () => {
    const root = await mkdtemp(join(tmpdir(), "gateway-retrosynthesis-"));
    cleanups.push(() => rm(root, { recursive: true, force: true }));
    const target = join(root, "token");
    await writeFile(target, modelToken + "\n", { mode: 0o600 });
    expect(await retrosynthesis.readRetrosynthesisToken(target)).toBe(modelToken);
    await chmod(target, 0o640);
    await expect(retrosynthesis.readRetrosynthesisToken(target)).rejects.toThrow("credential file");
    await chmod(target, 0o600);
    await symlink(target, join(root, "symlink"));
    await expect(retrosynthesis.readRetrosynthesisToken(join(root, "symlink"))).rejects.toThrow("credential file");
    await link(target, join(root, "hardlink"));
    await expect(retrosynthesis.readRetrosynthesisToken(target)).rejects.toThrow("credential file");
    await rm(join(root, "hardlink"));
    await writeFile(target, "short");
    await expect(retrosynthesis.readRetrosynthesisToken(target)).rejects.toThrow("credential");
    await writeFile(target, "x".repeat(4098));
    await expect(retrosynthesis.readRetrosynthesisToken(target)).rejects.toThrow("credential file");
  });

  test("inbound UI auth is distinct, enabled explicitly and checked on every request", async () => {
    const env = { RETROSYNTHESIS_MCP_ENABLED: "true", ZHIJU_CAPABILITY_TOKEN: gatewayToken };
    const read = spyOn({ read: async () => gatewayUiToken }, "read");
    expect(await retrosynthesis.authenticateRetrosynthesisUi(`Bearer ${gatewayUiToken}`, env, read)).toBe(true);
    expect(await retrosynthesis.authenticateRetrosynthesisUi(`Bearer ${gatewayUiToken}`, env, read)).toBe(true);
    expect(read).toHaveBeenCalledTimes(2);
    expect(await retrosynthesis.authenticateRetrosynthesisUi(`Bearer ${gatewayToken}`, env, read)).toBe(false);
    expect(await retrosynthesis.authenticateRetrosynthesisUi(`Bearer ${gatewayUiToken}`, {}, read)).toBe(false);
    expect(await retrosynthesis.authenticateRetrosynthesisUi(`Bearer ${gatewayToken}`, env, async () => gatewayToken)).toBe(false);
    expect(await retrosynthesis.authenticateRetrosynthesisUi("Bearer wrong", env, read)).toBe(false);
    expect(await retrosynthesis.authenticateRetrosynthesisUi(`Bearer ${gatewayUiToken}`, env, async () => { throw Error("secret"); })).toBe(false);
  });
});

async function gatewayRpc(method: string, params: Record<string, unknown> = {}, audience: retrosynthesis.RetrosynthesisAudience = "model") {
  const response = await handleMcpRequest(new Request("http://gateway/mcp", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  }), audience);
  const payload = await response.json() as { result?: Record<string, unknown>; error?: unknown };
  expect(payload.error).toBeUndefined();
  return payload.result!;
}

describe("Retrosynthesis capability gateway routing", () => {
  test("model lists exactly original six, property three and Retrosynthesis five", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const list = spyOn(retrosynthesis.retrosynthesisProxy, "listTools").mockResolvedValue(modelTools);
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect(TOOLS.map(tool => tool.name)).toEqual([
      "predict_polymer_properties", "dft_job_submit", "dft_job_status", "dft_job_list", "dft_job_result", "polymerize_monomers",
    ]);
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...filterTools, ...modelTools]);
    expect(list).toHaveBeenCalledWith("model");
  });

  test("Retrosynthesis failure preserves all original six and property three", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const list = spyOn(retrosynthesis.retrosynthesisProxy, "listTools").mockRejectedValue(Error("secret"));
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...filterTools]);
  });

  test("property failure preserves original six and Retrosynthesis five", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockRejectedValue(Error("secret"));
    const list = spyOn(retrosynthesis.retrosynthesisProxy, "listTools").mockResolvedValue(modelTools);
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...modelTools]);
  });

  test("UI lists only UI tools without contacting property filtering", async () => {
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const list = spyOn(retrosynthesis.retrosynthesisProxy, "listTools").mockResolvedValue(uiTools);
    cleanups.push(() => { filter.mockRestore(); list.mockRestore(); });
    expect((await gatewayRpc("tools/list", {}, "ui")).tools).toEqual(uiTools);
    expect(list).toHaveBeenCalledWith("ui");
    expect(filter).not.toHaveBeenCalled();
  });

  test("rejects model confirmation/mutations and UI access to model/native/property tools", async () => {
    const call = spyOn(retrosynthesis.retrosynthesisProxy, "callTool");
    cleanups.push(() => call.mockRestore());
    for (const name of ["confirm_monomer_retrosynthesis", "update_retrosynthesis_draft", "cancel_retrosynthesis_draft"]) {
      expect((await gatewayRpc("tools/call", { name, arguments: {} })).isError).toBe(true);
    }
    for (const name of ["prepare_monomer_retrosynthesis", "get_retrosynthesis_job", "dft_job_submit", "search_polymers"]) {
      expect((await gatewayRpc("tools/call", { name, arguments: {} }, "ui")).isError).toBe(true);
    }
    expect(call).not.toHaveBeenCalled();
  });

  test("shared draft route retains structured output and forwards the authenticated audience", async () => {
    const expected = { content: [{ type: "text" as const, text: "form" }], structuredContent: { draft_id: "d", revision: 2 } };
    const call = spyOn(retrosynthesis.retrosynthesisProxy, "callTool").mockResolvedValue(expected);
    cleanups.push(() => call.mockRestore());
    for (const audience of ["model", "ui"] as const) {
      expect(await gatewayRpc("tools/call", { name: "get_retrosynthesis_draft", arguments: { draft_id: "d" } }, audience)).toEqual(expected);
      expect(call).toHaveBeenLastCalledWith("get_retrosynthesis_draft", { draft_id: "d" }, audience);
    }
  });

  test("server authenticates each POST even with a reused MCP session header", async () => {
    const auth = spyOn(retrosynthesis, "authenticateRetrosynthesisUi").mockImplementation(async authorization => authorization === `Bearer ${gatewayUiToken}`);
    const list = spyOn(retrosynthesis.retrosynthesisProxy, "listTools").mockImplementation(async audience => audience === "ui" ? uiTools : modelTools);
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    const call = spyOn(retrosynthesis.retrosynthesisProxy, "callTool").mockResolvedValue({ content: [{ type: "text", text: "ok" }] });
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
    const denied = await (await request(`Bearer ${gatewayToken}`, { name: "confirm_monomer_retrosynthesis", arguments: {} })).json();
    expect(denied.result.isError).toBe(true);
    const reverse = await (await request(`Bearer ${gatewayUiToken}`, { name: "dft_job_submit", arguments: {} })).json();
    expect(reverse.result.isError).toBe(true);
    expect(call).not.toHaveBeenCalled();
  });
});


describe("combined capability inventory", () => {
  test("enabling retrosynthesis adds exactly five model and five UI tools to the PolyTAO baseline", async () => {
    const polyModel = schemas(POLYTAO_MODEL_TOOLS), polyUi = schemas(POLYTAO_UI_TOOLS);
    const poly = spyOn(polytaoProxy, "listTools").mockImplementation(async audience => audience === "ui" ? polyUi : polyModel);
    const retro = spyOn(retrosynthesis.retrosynthesisProxy, "listTools").mockImplementation(async audience => audience === "ui" ? uiTools : modelTools);
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    cleanups.push(() => { poly.mockRestore(); retro.mockRestore(); filter.mockRestore(); });
    const model = (await gatewayRpc("tools/list")).tools as Array<{name: string}>;
    const ui = (await gatewayRpc("tools/list", {}, "ui")).tools as Array<{name: string}>;
    expect(model).toEqual([...TOOLS, ...filterTools, ...polyModel, ...modelTools]);
    expect(model).toHaveLength(19); expect(new Set(model.map(tool => tool.name)).size).toBe(19);
    expect(ui).toEqual([...polyUi, ...uiTools]); expect(ui).toHaveLength(9);
  });
  test("failed retrosynthesis discovery preserves fourteen model and four UI tools", async () => {
    const polyModel = schemas(POLYTAO_MODEL_TOOLS), polyUi = schemas(POLYTAO_UI_TOOLS);
    const poly = spyOn(polytaoProxy, "listTools").mockImplementation(async audience => audience === "ui" ? polyUi : polyModel);
    const retro = spyOn(retrosynthesis.retrosynthesisProxy, "listTools").mockRejectedValue(Error("private failure"));
    const filter = spyOn(propertyFilterProxy, "listTools").mockResolvedValue(filterTools);
    cleanups.push(() => { poly.mockRestore(); retro.mockRestore(); filter.mockRestore(); });
    expect((await gatewayRpc("tools/list")).tools).toEqual([...TOOLS, ...filterTools, ...polyModel]);
    expect((await gatewayRpc("tools/list", {}, "ui")).tools).toEqual(polyUi);
  });
});
