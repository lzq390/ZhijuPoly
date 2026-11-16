import { lstat, readFile } from "node:fs/promises";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CallToolResultSchema } from "@modelcontextprotocol/sdk/types.js";
import type { CallToolResult, Tool } from "@modelcontextprotocol/sdk/types.js";

export const PROPERTY_FILTER_TOOLS = [
  "list_filter_properties", "search_polymers", "get_property_histogram",
] as const;
export const PROPERTY_FILTER_MCP_URL = "http://nexpoly_mcp_dev-property-filter-mcp-1:18110/mcp";
export const PROPERTY_FILTER_TOKEN_FILE = "/run/capability-gateway/property-filter-token";
const TOOL_NAMES: ReadonlySet<string> = new Set(PROPERTY_FILTER_TOOLS);
const FAILURE = "Property filtering is temporarily unavailable. Please retry later.";
const MAX_UPSTREAM_BYTES = 8 * 1024 * 1024;

export function isPropertyFilterTool(name: string): boolean {
  return TOOL_NAMES.has(name);
}

export function propertyFilterConfiguration(env: Record<string, string | undefined> = process.env) {
  const url = env.PROPERTY_FILTER_MCP_URL ?? PROPERTY_FILTER_MCP_URL;
  const tokenFile = env.PROPERTY_FILTER_MCP_TOKEN_FILE ?? PROPERTY_FILTER_TOKEN_FILE;
  const timeout = Number(env.PROPERTY_FILTER_MCP_TIMEOUT_MS ?? "30000");
  if (url !== PROPERTY_FILTER_MCP_URL || tokenFile !== PROPERTY_FILTER_TOKEN_FILE
      || !Number.isSafeInteger(timeout) || timeout < 100 || timeout > 60000) {
    throw new Error("Invalid property-filter gateway configuration");
  }
  return { url, tokenFile, timeout };
}

export async function readPropertyFilterToken(filename: string): Promise<string> {
  const info = await lstat(filename);
  if (!info.isFile() || (info.mode & 0o777) !== 0o600 || info.uid !== process.getuid?.()) {
    throw new Error("Invalid property-filter gateway credential file");
  }
  const token = (await readFile(filename, "utf8")).replace(/\r?\n$/, "");
  if (!/^[\x21-\x7e]{32,4096}$/.test(token)) throw new Error("Invalid property-filter gateway credential");
  return token;
}

type Configuration = ReturnType<typeof propertyFilterConfiguration>;
type Dependencies = { readToken?: (filename: string) => Promise<string>; fetch?: typeof fetch };

/** A separate upstream client per request avoids sharing MCP session identity. */
export class PropertyFilterProxy {
  constructor(
    private readonly configuration: () => Configuration = propertyFilterConfiguration,
    private readonly dependencies: Dependencies = {},
  ) {}

  private async withClient<T>(operation: (client: Client, signal: AbortSignal, timeout: number) => Promise<T>): Promise<T> {
    const config = this.configuration();
    const token = await (this.dependencies.readToken ?? readPropertyFilterToken)(config.tokenFile);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), config.timeout);
    const client = new Client({ name: "nexpoly-capability-filter-bridge", version: "1.0.0" });
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
              stream.error(new Error("Property-filter response exceeds the gateway limit"));
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

  async listTools(): Promise<Tool[]> {
    try {
      return await this.withClient(async (client, signal, timeout) => {
        const tools: Tool[] = [];
        let cursor: string | undefined;
        const cursors = new Set<string>();
        do {
          const page = await client.listTools(cursor ? { cursor } : {}, { timeout, signal });
          for (const tool of page.tools) if (isPropertyFilterTool(tool.name)) tools.push(tool);
          cursor = page.nextCursor;
          if (cursor) {
            if (cursors.has(cursor) || cursors.size >= 10) throw new Error("Invalid upstream tool pagination");
            cursors.add(cursor);
          }
        } while (cursor);
        if (tools.length !== PROPERTY_FILTER_TOOLS.length || new Set(tools.map(tool => tool.name)).size !== tools.length) {
          throw new Error("Required property-filter tools are unavailable");
        }
        return PROPERTY_FILTER_TOOLS.map(name => tools.find(tool => tool.name === name)!);
      });
    } catch {
      // SDK/network diagnostics may contain URLs, headers or server text.
      throw new Error(FAILURE);
    }
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<CallToolResult> {
    if (!isPropertyFilterTool(name)) return { isError: true, content: [{ type: "text", text: "Unknown property-filter tool" }] };
    try {
      return await this.withClient((client, signal, timeout) => client.callTool(
        { name, arguments: args }, CallToolResultSchema, { timeout, signal },
      ));
    } catch {
      return { isError: true, content: [{ type: "text", text: FAILURE }] };
    }
  }
}

export const propertyFilterProxy = new PropertyFilterProxy();
