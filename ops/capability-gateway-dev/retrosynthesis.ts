/** Independent single-step retrosynthesis MCP adapter. Deployment endpoints are fixed. */
import { timingSafeEqual, createHash } from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CallToolResultSchema } from "@modelcontextprotocol/sdk/types.js";
import type { CallToolResult, Tool } from "@modelcontextprotocol/sdk/types.js";
import { readPolytaoToken as readPrivateGatewayToken } from "./polytao";

export type RetrosynthesisAudience = "model" | "ui";
export const RETROSYNTHESIS_MODEL_TOOLS = [
  "get_retrosynthesis_capabilities", "resolve_retrosynthesis_target", "prepare_monomer_retrosynthesis",
  "get_retrosynthesis_draft", "get_retrosynthesis_job",
] as const;
export const RETROSYNTHESIS_UI_TOOLS = [
  "resolve_retrosynthesis_target", "get_retrosynthesis_draft", "update_retrosynthesis_draft",
  "confirm_monomer_retrosynthesis", "cancel_retrosynthesis_draft",
] as const;
export const RETROSYNTHESIS_MCP_URL = "http://nexpoly_retrosynthesis_mcp_dev-retrosynthesis-mcp-1:18211/mcp";
export const RETROSYNTHESIS_MODEL_TOKEN_FILE = "/run/capability-gateway/retrosynthesis-token";
export const RETROSYNTHESIS_UI_TOKEN_FILE = "/run/capability-gateway/retrosynthesis-ui-token";
export const RETROSYNTHESIS_GATEWAY_UI_TOKEN_FILE = "/run/capability-gateway/gateway-ui-token";
const FAILURE = "Monomer retrosynthesis is temporarily unavailable. Please retry later.";
const DENIED = "This gateway credential cannot access this tool.";
const names = (audience: RetrosynthesisAudience) => audience === "ui" ? RETROSYNTHESIS_UI_TOOLS : RETROSYNTHESIS_MODEL_TOOLS;
const sameSecret = (left: string, right: string) => timingSafeEqual(
  createHash("sha256").update(left).digest(), createHash("sha256").update(right).digest(),
);
export const readRetrosynthesisToken = readPrivateGatewayToken;
export function isRetrosynthesisTool(name: string) {
  return [...RETROSYNTHESIS_MODEL_TOOLS, ...RETROSYNTHESIS_UI_TOOLS].includes(name as never);
}
export function isRetrosynthesisAudienceTool(name: string, audience: RetrosynthesisAudience) {
  return names(audience).includes(name as never);
}
export function retrosynthesisConfiguration(env: Record<string, string | undefined> = process.env) {
  const enabled = env.RETROSYNTHESIS_MCP_ENABLED === "true";
  const url = env.RETROSYNTHESIS_MCP_URL ?? RETROSYNTHESIS_MCP_URL;
  const modelTokenFile = env.RETROSYNTHESIS_MCP_MODEL_TOKEN_FILE ?? RETROSYNTHESIS_MODEL_TOKEN_FILE;
  const uiTokenFile = env.RETROSYNTHESIS_MCP_UI_TOKEN_FILE ?? RETROSYNTHESIS_UI_TOKEN_FILE;
  const gatewayUiTokenFile = env.RETROSYNTHESIS_GATEWAY_UI_TOKEN_FILE ?? RETROSYNTHESIS_GATEWAY_UI_TOKEN_FILE;
  const timeout = Number(env.RETROSYNTHESIS_MCP_TIMEOUT_MS ?? "30000");
  if ((env.RETROSYNTHESIS_MCP_ENABLED !== undefined && !["true", "false"].includes(env.RETROSYNTHESIS_MCP_ENABLED))
      || url !== RETROSYNTHESIS_MCP_URL || modelTokenFile !== RETROSYNTHESIS_MODEL_TOKEN_FILE
      || uiTokenFile !== RETROSYNTHESIS_UI_TOKEN_FILE || gatewayUiTokenFile !== RETROSYNTHESIS_GATEWAY_UI_TOKEN_FILE
      || !Number.isSafeInteger(timeout) || timeout < 100 || timeout > 60000) {
    throw new Error("Invalid retrosynthesis gateway configuration");
  }
  return { enabled, url, modelTokenFile, uiTokenFile, gatewayUiTokenFile, timeout };
}
export async function authenticateRetrosynthesisUi(authorization: string | null,
  env: Record<string, string | undefined> = process.env,
  readToken: (filename: string) => Promise<string> = readRetrosynthesisToken): Promise<boolean> {
  try {
    const config = retrosynthesisConfiguration(env);
    if (!config.enabled) return false;
    const supplied = /^Bearer ([\x21-\x7e]{32,4096})$/.exec(authorization ?? "")?.[1];
    if (!supplied) return false;
    const expected = await readToken(config.gatewayUiTokenFile);
    if (!env.ZHIJU_CAPABILITY_TOKEN || sameSecret(expected, env.ZHIJU_CAPABILITY_TOKEN)) return false;
    return sameSecret(supplied, expected);
  } catch { return false; }
}

type Configuration = ReturnType<typeof retrosynthesisConfiguration>;
type Dependencies = { readToken?: (filename: string) => Promise<string>; fetch?: typeof fetch };
export class RetrosynthesisProxy {
  constructor(private readonly configuration: () => Configuration = retrosynthesisConfiguration,
    private readonly dependencies: Dependencies = {}) {}
  private async withClient<T>(audience: RetrosynthesisAudience,
    operation: (client: Client, signal: AbortSignal, timeout: number) => Promise<T>): Promise<T> {
    const config = this.configuration();
    if (!config.enabled) throw new Error(FAILURE);
    const readToken = this.dependencies.readToken ?? readRetrosynthesisToken;
    const [modelToken, uiToken] = await Promise.all([readToken(config.modelTokenFile), readToken(config.uiTokenFile)]);
    if (sameSecret(modelToken, uiToken)) throw new Error(FAILURE);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.timeout);
    const client = new Client({ name: `nexpoly-capability-retrosynthesis-${audience}-bridge`, version: "1.0.0" });
    const upstreamFetch = this.dependencies.fetch ?? fetch;
    const transport = new StreamableHTTPClientTransport(new URL(config.url), {
      requestInit: { headers: { Authorization: `Bearer ${audience === "ui" ? uiToken : modelToken}` } },
      fetch: async (url, init) => {
        const response = await upstreamFetch(url, { ...init, redirect: "error",
          signal: init?.signal ? AbortSignal.any([controller.signal, init.signal]) : controller.signal });
        if (!response.body) return response;
        let bytes = 0;
        return new Response(response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
          transform(chunk, stream) {
            bytes += chunk.byteLength;
            if (bytes > 8 * 1024 * 1024) { controller.abort(); stream.error(new Error("Response exceeds gateway limit")); }
            else stream.enqueue(chunk);
          },
        })), { status: response.status, statusText: response.statusText, headers: response.headers });
      },
      reconnectionOptions: { maxRetries: 0, initialReconnectionDelay: 100, maxReconnectionDelay: 100, reconnectionDelayGrowFactor: 1 },
    });
    try {
      await client.connect(transport, { timeout: config.timeout, signal: controller.signal });
      return await operation(client, controller.signal, config.timeout);
    } finally { clearTimeout(timer); await client.close().catch(() => {}); }
  }
  async listTools(audience: RetrosynthesisAudience = "model"): Promise<Tool[]> {
    if (!this.configuration().enabled) return [];
    try {
      return await this.withClient(audience, async (client, signal, timeout) => {
        const tools: Tool[] = [], cursors = new Set<string>();
        let cursor: string | undefined;
        do {
          const page = await client.listTools(cursor ? { cursor } : {}, { timeout, signal });
          for (const tool of page.tools) if (isRetrosynthesisAudienceTool(tool.name, audience)) tools.push(tool);
          cursor = page.nextCursor;
          if (cursor) {
            if (cursors.has(cursor) || cursors.size >= 10) throw new Error(FAILURE);
            cursors.add(cursor);
          }
        } while (cursor);
        if (tools.length !== names(audience).length || new Set(tools.map(tool => tool.name)).size !== tools.length) throw new Error(FAILURE);
        return names(audience).map(name => tools.find(tool => tool.name === name)!);
      });
    } catch { throw new Error(FAILURE); }
  }
  async callTool(name: string, args: Record<string, unknown>, audience: RetrosynthesisAudience = "model"): Promise<CallToolResult> {
    if (!isRetrosynthesisAudienceTool(name, audience)) return { isError: true, content: [{ type: "text", text: DENIED }] };
    try {
      return await this.withClient(audience, (client, signal, timeout) => client.callTool(
        { name, arguments: args }, CallToolResultSchema, { timeout, signal },
      ));
    } catch { return { isError: true, content: [{ type: "text", text: FAILURE }] }; }
  }
}
export const retrosynthesisProxy = new RetrosynthesisProxy();
