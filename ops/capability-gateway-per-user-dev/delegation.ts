// Transport for the reviewed gateway's six native tools. One instance per MCP call.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { redact } from "./log";

const socket = process.env.OPENSCIENCE_M3_AUTH_SOCKET ?? "/nexpoly/auth.sock";
const opaque = (value: unknown): value is string =>
  typeof value === "string" && /^[A-Za-z0-9_-]{32,256}$/.test(value);
const sha = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");

export function serviceKey(variable: string): string {
  const filename = process.env[variable];
  if (!filename?.startsWith("/")) throw Error("Development service identity file is required");
  const value = readFileSync(filename, "utf8").trim();
  if (!opaque(value)) throw Error("Invalid development service identity");
  return value;
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return "{" + Object.keys(object).sort().map(key => JSON.stringify(key) + ":" + canonical(object[key])).join(",") + "}";
  }
  return JSON.stringify(value);
}

type Grant = { access_token: string; operation_id: string; valid_until: number };

export class ToolDelegation {
  protected readonly frontendOrigin: string;
  private readonly operation: string | null;
  private readonly argumentsHash: string;

  constructor(request: Request, private readonly tool: string, args: unknown) {
    this.operation = request.headers.get("X-OpenScience-Operation");
    this.argumentsHash = sha(canonical(args));
    const parent = process.env.OPENSCIENCE_M3_PARENT_ORIGIN ?? "https://app.m2.test:18760";
    if (new URL(parent).origin !== parent || !/^https:\/\/[a-z0-9.-]+\.test(?::\d+)?$/.test(parent))
      throw Error("Expected an independent HTTPS development parent");
    this.frontendOrigin = parent;
  }

  redactForLog(value: string | Error): string {
    return redact(value instanceof Error ? value.message : value, [this.operation ?? undefined]);
  }

  protected upstreamFailure(message: string, _response: Response): Error {
    return new Error(message);
  }

  protected async authenticatedRequest(_op: string, path: string, init: RequestInit, _jobId?: string) {
    if (!opaque(this.operation)) throw Error("This tool requires an authorized operation");
    if (!socket.startsWith("/")) throw Error("Expected a private NexPoly Unix socket");
    if (!path.startsWith("/api/v1/") || (init.body !== undefined && typeof init.body !== "string"))
      throw Error("Unsupported native request");
    const method = init.method ?? "GET";
    const url = new URL(path, "http://localhost");
    const credential = "Bearer " + serviceKey("OPENSCIENCE_M3_GATEWAY_KEY_FILE");
    const exchange = await fetch("http://localhost/internal/openscience/delegations/exchange", {
      unix: socket, method: "POST", redirect: "error",
      headers: { Authorization: credential, "Content-Type": "application/json" },
      body: JSON.stringify({
        operation_token: this.operation, tool: this.tool, arguments_sha256: this.argumentsHash,
        method, path: url.pathname, query: url.search.slice(1), request_sha256: sha(init.body ?? ""),
      }),
      signal: AbortSignal.timeout(4000),
    });
    if (!exchange.ok) {
      await exchange.body?.cancel();
      throw Error(`Tool authorization refused (HTTP ${exchange.status})`);
    }
    const grant: Grant = await exchange.json();
    const remainingMs = Math.floor(grant.valid_until * 1000 - Date.now());
    if (!opaque(grant.access_token) || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(grant.operation_id) ||
        !Number.isFinite(remainingMs) || remainingMs <= 0)
      throw Error("Invalid or expired tool authorization");
    const headers = new Headers({ Authorization: credential, "X-OpenScience-Delegation": grant.access_token });
    if (method === "POST") {
      headers.set("Content-Type", "application/json");
      headers.set("Idempotency-Key", "openscience-" + grant.operation_id);
    }
    // Never replay an ambiguous write. Native admission validates the bounded grant
    // and owns idempotency; a confirmed retry of the same operation retains its key.
    const response = await fetch(url, {
      unix: socket, method, body: init.body, headers, redirect: "error",
      signal: AbortSignal.timeout(Math.min(remainingMs, 30_000)),
    });
    if (response.ok) {
      const text = await response.text();
      if (Date.now() >= grant.valid_until * 1000) throw Error("Tool authorization expired");
      return { response, text };
    }
    await response.body?.cancel();
    return { response, text: "Native request refused (HTTP " + response.status + ")" };
  }
}
