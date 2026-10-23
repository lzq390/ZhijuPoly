import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Connect, Plugin } from "vite";

/** Handoff only: all requests remain on the current local frontend server. */
export const handoffContentSecurityPolicy = [
  "default-src 'self'",
  // Current native Ketcher SDK compiles runtime helpers from strings.
  "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob:",
  "style-src 'self' 'unsafe-inline'",
  "connect-src 'self' blob: data: ws://127.0.0.1:* ws://localhost:* ws://[::1]:*",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "worker-src 'self' blob:",
  "frame-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

const mediaTypes: Record<string, string> = {
  csv: "text/csv; charset=utf-8",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  zip: "application/zip",
  json: "application/json; charset=utf-8",
  xyz: "chemical/x-xyz",
  sdf: "chemical/x-mdl-sdfile",
  cube: "text/plain; charset=utf-8",
  txt: "text/plain; charset=utf-8",
};

function jsonResponse(response: ServerResponse, status: number, data: unknown) {
  response.statusCode = status;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.end(JSON.stringify(data));
}

/** Exact public templates plus synthetic artifact downloads; no filesystem path input. */
function downloadRoute(path: string): { asset: string; name: string } | undefined {
  const template = path.match(/^\/api\/v1\/monomer-polymerization\/batch\/templates\/([ab])\.(csv|xlsx)$/);
  if (template) return { asset: `${template[1]}.${template[2]}`, name: `monomers_${template[1]}.${template[2]}` };
  const batch = path.match(/^\/api\/v1\/monomer-polymerization\/batch\/jobs\/[^/]+\/artifacts\/(results\.(?:xlsx|zip)|input_errors\.csv|candidates\.csv)$/);
  if (batch) return { asset: batch[1] === "candidates.csv" ? "result.csv" : batch[1], name: batch[1] };
  if (/^\/api\/v1\/monomer-dft\/jobs\/[^/]+\/bundle$/.test(path)) {
    return { asset: "dft-bundle.zip", name: "monomer-dft-demo.zip" };
  }
  const artifact = path.match(/^\/api\/v1\/monomer-(?:dft|md)\/jobs\/[^/]+\/artifacts\/([^/]+)$/);
  if (artifact) {
    const name = artifact[1];
    if (name === "scientific_result") return { asset: "scientific_result.json", name: "scientific_result.json" };
    const suffix = name.split(".").at(-1)?.toLowerCase();
    if (suffix && ["json", "xyz", "sdf", "cube", "txt", "csv"].includes(suffix)) {
      return { asset: `result.${suffix}`, name };
    }
  }
  return undefined;
}

/** Does not provide auth/business APIs: those are intercepted before app preload. */
export type HandoffFixtureFactory = (options: {
  apiBaseUrl?: string;
  pageUrl?: string;
  nativeFetch?: typeof fetch;
}) => typeof fetch;

export function handoffPlugin(options: { httpFixtures?: boolean; fixtureFactory?: HandoffFixtureFactory } = {}): Plugin {
  if (options.httpFixtures && !options.fixtureFactory) {
    throw new Error("Handoff HTTP fixtures require an explicitly supplied scenario provider");
  }
  let publicDir = "";
  const fixtureFetchers = new Map<string, typeof fetch>();
  function fixtureFetch(request: IncomingMessage, response: ServerResponse) {
    const existingId = /(?:^|;\s*)nexpoly-handoff-http=([a-f0-9-]+)/.exec(request.headers.cookie ?? "")?.[1];
    const sessionId = existingId ?? randomUUID();
    if (!existingId) response.setHeader("Set-Cookie", `nexpoly-handoff-http=${sessionId}; Path=/; HttpOnly; SameSite=Strict`);
    let handler = fixtureFetchers.get(sessionId);
    if (!handler) {
      const pagePath = (request.headers.accept ?? "").includes("text/html") ? request.url ?? "/" : "/";
      const pageUrl = new URL(pagePath, "http://handoff.local").href;
      handler = options.fixtureFactory!({
        pageUrl,
        nativeFetch: async input => {
          const url = new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url, pageUrl);
          const name = /^\/handoff\/downloads\/([a-zA-Z0-9_.-]+)$/.exec(url.pathname)?.[1];
          if (url.origin !== "http://handoff.local" || !name) return new Response("No fixture asset", { status: 501 });
          try {
            return new Response(new Uint8Array(readFileSync(resolve(publicDir, "handoff/downloads", name))), { headers: { "Content-Type": mediaTypes[name.split(".").at(-1)!] ?? "application/octet-stream" } });
          } catch { return new Response("Missing fixture asset", { status: 404 }); }
        },
      });
      fixtureFetchers.set(sessionId, handler);
    }
    return handler;
  }
  async function serveFixture(request: IncomingMessage, response: ServerResponse) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    const incompleteClose = () => { if (!request.complete) abort(); };
    request.once("aborted", abort);
    request.once("close", incompleteClose);
    response.once("close", abort);
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const handler = fixtureFetch(request, response);
      const method = request.method ?? "GET";
      const headers = new Headers();
      for (const [key, values] of Object.entries(request.headers)) {
        if (Array.isArray(values)) values.forEach(value => headers.append(key, value));
        else if (values !== undefined) headers.set(key, values);
      }
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      const bytes = Buffer.concat(chunks);
      const type = headers.get("content-type") ?? "";
      let body: BodyInit | undefined;
      if (!["GET", "HEAD"].includes(method) && bytes.length) {
        body = type.startsWith("multipart/form-data")
          ? await new Response(new Uint8Array(bytes), { headers: { "Content-Type": type } }).formData()
          : bytes.toString("utf8");
      }
      const fixture = await handler(new URL(request.url ?? "/", "http://handoff.local"), { method, headers, body, signal: controller.signal });
      response.statusCode = fixture.status;
      fixture.headers.forEach((value, key) => response.setHeader(key, value));
      response.flushHeaders();
      if (method === "HEAD" || !fixture.body) { response.end(); return; }
      reader = fixture.body.getReader();
      while (!controller.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!response.write(value)) await once(response, "drain", { signal: controller.signal });
      }
      if (!response.destroyed) response.end();
    } catch (error) {
      if (!controller.signal.aborted && !response.destroyed) {
        if (!response.headersSent) jsonResponse(response, 500, { code: "HANDOFF_HTTP_FIXTURE_ERROR", detail: error instanceof Error ? error.message : String(error) });
        else response.destroy(error instanceof Error ? error : undefined);
      }
    } finally {
      controller.abort();
      await reader?.cancel().catch(() => undefined);
      reader?.releaseLock();
      request.removeListener("aborted", abort);
      request.removeListener("close", incompleteClose);
      response.removeListener("close", abort);
    }
  }
  function middleware(request: IncomingMessage, response: ServerResponse, next: Connect.NextFunction) {
    response.setHeader("Content-Security-Policy", handoffContentSecurityPolicy + "; frame-ancestors 'self'");
    response.setHeader("Referrer-Policy", "no-referrer");
    let path: string;
    try {
      path = new URL(request.url ?? "/", "http://handoff.local").pathname;
    } catch {
      jsonResponse(response, 400, { detail: "Invalid request URL" });
      return;
    }
    if (path === "/health") {
      jsonResponse(response, 200, { status: "ok", mode: "frontend-handoff", synthetic: true });
      return;
    }
    if (!(path === "/api" || path.startsWith("/api/"))) {
      if (options.httpFixtures && (request.headers.accept ?? "").includes("text/html")) fixtureFetch(request, response);
      next();
      return;
    }
    response.setHeader("Cache-Control", "no-store");
    const download = downloadRoute(path);
    if (download && ["GET", "HEAD"].includes(request.method ?? "GET")) {
      try {
        const content = readFileSync(resolve(publicDir, "handoff", "downloads", download.asset));
        response.setHeader("Content-Type", mediaTypes[download.asset.split(".").at(-1)!] ?? "application/octet-stream");
        response.setHeader("Content-Disposition", `attachment; filename="${download.name.replace(/[^a-zA-Z0-9_.-]/g, "_")}"`);
        response.setHeader("Content-Length", content.length);
        response.statusCode = 200;
        response.end(request.method === "HEAD" ? undefined : content);
      } catch {
        jsonResponse(response, 500, { detail: "Missing bundled synthetic download", asset: download.asset });
      }
      return;
    }
    if (options.httpFixtures) {
      void serveFixture(request, response);
      return;
    }
    jsonResponse(response, 501, {
      code: "HANDOFF_API_NOT_IMPLEMENTED",
      detail: `Handoff has no backend. No HTTP mock for ${request.method ?? "GET"} ${path}`,
      synthetic: true,
    });
  }
  return {
    name: "nexpoly-frontend-handoff",
    configResolved(config) { publicDir = config.publicDir; },
    configureServer(server) { server.middlewares.use(middleware); },
    configurePreviewServer(server) { server.middlewares.use(middleware); },
    transformIndexHtml() {
      return [{ tag: "meta", attrs: { "http-equiv": "Content-Security-Policy", content: handoffContentSecurityPolicy }, injectTo: "head-prepend" }];
    },
  };
}
