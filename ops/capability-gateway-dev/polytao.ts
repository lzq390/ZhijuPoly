import { timingSafeEqual, createHash } from "node:crypto";
import { constants } from "node:fs";
import { lstat, open } from "node:fs/promises";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CallToolResultSchema } from "@modelcontextprotocol/sdk/types.js";
import type { CallToolResult, Tool } from "@modelcontextprotocol/sdk/types.js";

export type PolytaoAudience = "model" | "ui";
export const POLYTAO_MODEL_TOOLS = [
  "get_polytao_capabilities", "calculate_polytao_descriptors", "prepare_polytao_generation",
  "get_polytao_draft", "get_polytao_job",
] as const;
export const POLYTAO_UI_TOOLS = [
  "get_polytao_draft", "update_polytao_draft", "confirm_polytao_generation", "cancel_polytao_draft",
] as const;
export const POLYTAO_MCP_URL = "http://nexpoly_polytao_mcp_dev-polytao-mcp-1:18111/mcp";
export const POLYTAO_MODEL_TOKEN_FILE = "/run/capability-gateway/polytao-token";
export const POLYTAO_UI_TOKEN_FILE = "/run/capability-gateway/polytao-ui-token";
export const POLYTAO_GATEWAY_UI_TOKEN_FILE = "/run/capability-gateway/gateway-ui-token";
const FAILURE = "Polymer generation is temporarily unavailable. Please retry later.";
const DENIED = "This gateway credential cannot access this tool.";
const MAX_UPSTREAM_BYTES = 8 * 1024 * 1024;
const names = (audience: PolytaoAudience) => audience === "ui" ? POLYTAO_UI_TOOLS : POLYTAO_MODEL_TOOLS;

export function isPolytaoTool(name: string): boolean {
  return [...POLYTAO_MODEL_TOOLS, ...POLYTAO_UI_TOOLS].includes(name as never);
}
export function isPolytaoAudienceTool(name: string, audience: PolytaoAudience): boolean {
  return names(audience).includes(name as never);
}

export function polytaoConfiguration(env: Record<string, string | undefined> = process.env) {
  const enabled = env.POLYTAO_MCP_ENABLED === "true";
  const url = env.POLYTAO_MCP_URL ?? POLYTAO_MCP_URL;
  const modelTokenFile = env.POLYTAO_MCP_MODEL_TOKEN_FILE ?? POLYTAO_MODEL_TOKEN_FILE;
  const uiTokenFile = env.POLYTAO_MCP_UI_TOKEN_FILE ?? POLYTAO_UI_TOKEN_FILE;
  const gatewayUiTokenFile = env.POLYTAO_GATEWAY_UI_TOKEN_FILE ?? POLYTAO_GATEWAY_UI_TOKEN_FILE;
  const timeout = Number(env.POLYTAO_MCP_TIMEOUT_MS ?? "30000");
  if ((env.POLYTAO_MCP_ENABLED !== undefined && !["true", "false"].includes(env.POLYTAO_MCP_ENABLED))
      || url !== POLYTAO_MCP_URL || modelTokenFile !== POLYTAO_MODEL_TOKEN_FILE
      || uiTokenFile !== POLYTAO_UI_TOKEN_FILE || gatewayUiTokenFile !== POLYTAO_GATEWAY_UI_TOKEN_FILE
      || !Number.isSafeInteger(timeout) || timeout < 100 || timeout > 60000) {
    throw new Error("Invalid PolyTAO gateway configuration");
  }
  return { enabled, url, modelTokenFile, uiTokenFile, gatewayUiTokenFile, timeout };
}

/** Reject links, permission changes and replacement between checking and opening. */
export async function readPolytaoToken(filename: string): Promise<string> {
  const before = await lstat(filename);
  const valid = (info: typeof before) => info.isFile() && (info.mode & 0o777) === 0o600
    && info.nlink === 1 && info.uid === process.getuid?.() && info.size <= 4097;
  if (!valid(before)) throw new Error("Invalid PolyTAO gateway credential file");
  const handle = await open(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const after = await handle.stat();
    if (!valid(after) || before.dev !== after.dev || before.ino !== after.ino) {
      throw new Error("Invalid PolyTAO gateway credential file");
    }
    const token = (await handle.readFile("utf8")).replace(/\r?\n$/, "");
    if (!/^[\x21-\x7e]{32,4096}$/.test(token)) throw new Error("Invalid PolyTAO gateway credential");
    return token;
  } finally { await handle.close(); }
}

const sameSecret = (left: string, right: string) => timingSafeEqual(
  createHash("sha256").update(left).digest(), createHash("sha256").update(right).digest(),
);

/** UI authentication runs for every POST; no MCP session can change its audience. */
export async function authenticatePolytaoUi(
  authorization: string | null,
  env: Record<string, string | undefined> = process.env,
  readToken: (filename: string) => Promise<string> = readPolytaoToken,
): Promise<boolean> {
  try {
    const config = polytaoConfiguration(env);
    if (!config.enabled) return false;
    const supplied = /^Bearer ([\x21-\x7e]{32,4096})$/.exec(authorization ?? "")?.[1];
    if (!supplied) return false;
    const expected = await readToken(config.gatewayUiTokenFile);
    if (!env.ZHIJU_CAPABILITY_TOKEN || sameSecret(expected, env.ZHIJU_CAPABILITY_TOKEN)) return false;
    return sameSecret(supplied, expected);
  } catch { return false; }
}

type Configuration = ReturnType<typeof polytaoConfiguration>;
type Dependencies = { readToken?: (filename: string) => Promise<string>; fetch?: typeof fetch };

export class PolytaoProxy {
  constructor(
    private readonly configuration: () => Configuration = polytaoConfiguration,
    private readonly dependencies: Dependencies = {},
  ) {}

  private async withClient<T>(audience: PolytaoAudience,
    operation: (client: Client, signal: AbortSignal, timeout: number) => Promise<T>): Promise<T> {
    const config = this.configuration();
    if (!config.enabled) throw new Error(FAILURE);
    const readToken = this.dependencies.readToken ?? readPolytaoToken;
    const [modelToken, uiToken] = await Promise.all([readToken(config.modelTokenFile), readToken(config.uiTokenFile)]);
    // A collapsed upstream identity would remove the human-confirmation boundary.
    if (sameSecret(modelToken, uiToken)) throw new Error(FAILURE);
    const token = audience === "ui" ? uiToken : modelToken;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.timeout);
    const client = new Client({ name: `nexpoly-capability-polytao-${audience}-bridge`, version: "1.0.0" });
    const upstreamFetch = this.dependencies.fetch ?? fetch;
    const transport = new StreamableHTTPClientTransport(new URL(config.url), {
      requestInit: { headers: { Authorization: `Bearer ${token}` } },
      fetch: async (url, init) => {
        const response = await upstreamFetch(url, {
          ...init, redirect: "error",
          signal: init?.signal ? AbortSignal.any([controller.signal, init.signal]) : controller.signal,
        });
        if (!response.body) return response;
        let bytes = 0;
        return new Response(response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
          transform(chunk, stream) {
            bytes += chunk.byteLength;
            if (bytes > MAX_UPSTREAM_BYTES) {
              controller.abort();
              stream.error(new Error("PolyTAO response exceeds the gateway limit"));
              return;
            }
            stream.enqueue(chunk);
          },
        })), { status: response.status, statusText: response.statusText, headers: response.headers });
      },
      reconnectionOptions: { maxRetries: 0, initialReconnectionDelay: 100, maxReconnectionDelay: 100, reconnectionDelayGrowFactor: 1 },
    });
    try {
      await client.connect(transport, { timeout: config.timeout, signal: controller.signal });
      return await operation(client, controller.signal, config.timeout);
    } finally {
      clearTimeout(timer);
      await client.close().catch(() => {});
    }
  }

  async listTools(audience: PolytaoAudience = "model"): Promise<Tool[]> {
    if (!this.configuration().enabled) return [];
    try {
      return await this.withClient(audience, async (client, signal, timeout) => {
        const tools: Tool[] = [];
        const cursors = new Set<string>();
        let cursor: string | undefined;
        do {
          const page = await client.listTools(cursor ? { cursor } : {}, { timeout, signal });
          for (const tool of page.tools) if (isPolytaoAudienceTool(tool.name, audience)) tools.push(tool);
          cursor = page.nextCursor;
          if (cursor) {
            if (cursors.has(cursor) || cursors.size >= 10) throw new Error(FAILURE);
            cursors.add(cursor);
          }
        } while (cursor);
        if (tools.length !== names(audience).length || new Set(tools.map(tool => tool.name)).size !== tools.length) {
          throw new Error(FAILURE);
        }
        return names(audience).map(name => tools.find(tool => tool.name === name)!);
      });
    } catch { throw new Error(FAILURE); }
  }

  async callTool(name: string, args: Record<string, unknown>, audience: PolytaoAudience = "model"): Promise<CallToolResult> {
    if (!isPolytaoAudienceTool(name, audience)) return { isError: true, content: [{ type: "text", text: DENIED }] };
    try {
      return await this.withClient(audience, (client, signal, timeout) => client.callTool(
        { name, arguments: args }, CallToolResultSchema, { timeout, signal },
      ));
    } catch { return { isError: true, content: [{ type: "text", text: FAILURE }] }; }
  }
}

export const polytaoProxy = new PolytaoProxy();
