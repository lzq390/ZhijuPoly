import { request as httpRequest } from "node:http";
import type { IncomingHttpHeaders, ServerResponse } from "node:http";
import type { Plugin } from "vite";

const MCP_PATH = /^\/mcp\/?(?:\?.*)?$/;
const PROXY_DEADLINE_MS = 35_000;
const HOP_HEADERS = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade"
]);

function endError(response: ServerResponse, status: number, code: string) {
  if (response.writableEnded || response.destroyed) return;
  if (response.headersSent) {
    response.destroy();
    return;
  }
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify({ error: code }));
}

function forwardHeaders(headers: IncomingHttpHeaders): IncomingHttpHeaders {
  const connectionHeaders = new Set((headers.connection ?? "").split(",").map((name) => name.trim().toLowerCase()));
  return Object.fromEntries(Object.entries(headers).filter(([name]) => !HOP_HEADERS.has(name) && !connectionHeaders.has(name)));
}

/** An opt-in dev-server route, deliberately absent from built/preview assets.
 * No browser-visible environment variable, injected credential or request log.
 */
export function developmentMcpProxy(targetValue?: string, allowedHostsValue?: string): Plugin | undefined {
  const configuredTarget = targetValue?.trim();
  if (!configuredTarget) return undefined;
  let target: URL;
  try {
    target = new URL(configuredTarget);
  } catch {
    throw new Error("DEV_MCP_PROXY_TARGET must be an explicit HTTP origin");
  }
  if (target.protocol !== "http:" || target.username || target.password ||
      target.pathname !== "/" || target.search || target.hash) {
    throw new Error("DEV_MCP_PROXY_TARGET must be an HTTP origin without credentials, path, query or fragment");
  }
  const hostValues = (allowedHostsValue ?? "localhost:9001,127.0.0.1:9001").split(",").map((host) => host.trim().toLowerCase());
  if (hostValues.some((host) => !host || /[\s/@?#*]/.test(host) || !/^(?:[a-z0-9.-]+|\[[a-f0-9:]+\]):[0-9]+$/.test(host))) {
    throw new Error("DEV_MCP_PROXY_ALLOWED_HOSTS must contain explicit host:port entries");
  }
  const allowedHosts = new Set(hostValues);

  return {
    name: "development-readonly-mcp-proxy",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use((incoming, response, next) => {
        const path = incoming.url ?? "";
        if (!MCP_PATH.test(path)) return next();
        // Check before rewriting Host; Vite itself allows arbitrary IP hosts.
        // Reject duplicate Host values as well as unconfigured entry points.
        const hostCount = incoming.rawHeaders.filter((_, index) => index % 2 === 0 && incoming.rawHeaders[index].toLowerCase() === "host").length;
        if (hostCount !== 1 || !allowedHosts.has((incoming.headers.host ?? "").toLowerCase())) {
          endError(response, 421, "invalid_host");
          return;
        }
        const connectionHeaders = (incoming.headers.connection ?? "").split(",").map((name) => name.trim().toLowerCase());
        // Stripping a caller-designated hop header must never remove an Origin
        // or credential before the MCP service evaluates its security policy.
        if (connectionHeaders.some((name) => ["host", "origin", "authorization", "last-event-id"].includes(name) || name.startsWith("mcp-"))) {
          endError(response, 400, "invalid_connection_header");
          return;
        }
        const headers = forwardHeaders(incoming.headers);
        headers.host = target.host;
        // Normalize only the optional terminal slash; query strings still reach
        // the MCP server, whose request guard rejects them.
        const upstreamPath = path.replace(/^\/mcp\/(?=\?|$)/, "/mcp");
        const upstream = httpRequest(target, { method: incoming.method, path: upstreamPath, headers, agent: false });
        let timer: ReturnType<typeof setTimeout>;
        let upstreamResponse: import("node:http").IncomingMessage | undefined;
        const cleanup = () => clearTimeout(timer);
        const abort = () => {
          cleanup();
          incoming.unpipe(upstream);
          upstreamResponse?.destroy();
          upstream.destroy();
        };
        timer = setTimeout(() => {
          endError(response, 504, "mcp_upstream_timeout");
          abort();
        }, PROXY_DEADLINE_MS);
        timer.unref();
        upstream.on("error", () => {
          // Never print URLs, upstream addresses, headers or the original error.
          endError(response, 502, "mcp_upstream_unavailable");
          abort();
        });
        upstream.on("response", (result) => {
          upstreamResponse = result;
          if (response.writableEnded || response.destroyed) {
            result.destroy();
            return;
          }
          response.writeHead(result.statusCode ?? 502, forwardHeaders(result.headers));
          result.on("error", () => {
            endError(response, 502, "mcp_upstream_unavailable");
            abort();
          });
          result.on("end", cleanup);
          result.pipe(response);
        });
        incoming.on("aborted", abort);
        incoming.on("error", abort);
        response.on("close", abort);
        // Preserve body streaming and backpressure. Authentication, 32 KiB body
        // limits and the 30 second tool deadline remain owned by the MCP server.
        incoming.pipe(upstream);
      });
    }
  };
}
