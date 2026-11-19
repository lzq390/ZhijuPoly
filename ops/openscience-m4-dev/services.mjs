// Only the private router has the service credentials and original page handoff.
import http from "node:http";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { pipeline } from "node:stream/promises";

const uuid = "[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}";
const kb = "/kbs/" + uuid;
const doc = kb + "/documents/" + uuid;
const webRead = new RegExp(
  "^(?:/kbs|" + kb + "(?:/documents)?|" + doc + "(?:/download|/chunks/" + uuid + ")?|/mcp)$",
);
const webWrite = new RegExp("^(?:/kbs|" + kb + "/documents|/search|/mcp)$");
const deny = (status, msg) => Object.assign(Error(msg), { status });
const digest = (value) => createHash("sha256").update(value).digest("hex");

export async function services(config, validate) {
  const secrets = {};
  for (const audience of ["knowledge", "model"]) {
    secrets[audience] = (await readFile(config[audience + "_key_file"], "utf8")).trim();
  }
  async function request(service, path, method, page, generation, data, type, signal, modelHeaders = {}) {
    const req = http.request({
      socketPath: config.authority_socket,
      path: "/internal/openscience/m4/" + service + path,
      method,
      agent: false,
      signal,
      headers: {
        authorization: "Bearer " + secrets[service],
        "x-m4-handoff": page.grant,
        "x-m4-generation": generation,
        "content-length": data?.length || 0,
        "content-type": type || "application/json",
        ...modelHeaders,
      },
    });
    req.on("error", () => {});
    const response = once(req, "response");
    req.end(data);
    return (await response)[0];
  }
  async function relay(req, res, service, path, method, page, generation, data, type, modelHeaders) {
    const controller = new AbortController();
    const close = () => controller.abort();
    page.active.add(controller);
    res.once("close", close);
    let timer;
    try {
      await validate(page);
      timer = setInterval(async () => {
        try {
          await validate(page);
          if (digest((await readFile(page.route.tokenFile, "utf8")).trim()) !== generation) close();
        } catch {
          close();
        }
      }, 3000).unref();
      const source = await request(
        service,
        path,
        method,
        page,
        generation,
        data,
        type,
        controller.signal,
        modelHeaders,
      );
      await validate(page);
      if (source.statusCode >= 300 && source.statusCode < 400) {
        source.destroy();
        throw deny(502, "service_redirect_denied");
      }
      const headers = {
        "content-type": source.headers["content-type"] || "application/octet-stream",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "x-accel-buffering": "no",
      };
      for (const key of ["content-disposition", "retry-after"])
        if (source.headers[key]) headers[key] = source.headers[key];
      res.writeHead(source.statusCode, headers);
      res.flushHeaders();
      await pipeline(source, res, { signal: controller.signal });
    } finally {
      clearInterval(timer);
      controller.abort();
      page.active.delete(controller);
      res.removeListener("close", close);
    }
  }
  return {
    async browser(req, res, url, page, readBody, origin) {
      const usage = url.pathname === "/openscience-api/m4/model/usage";
      const path = usage ? "/usage" : url.pathname.slice("/knowledge-api".length);
      if (
        !(usage
          ? req.method === "GET"
          : (req.method === "POST" && webWrite.test(path)) || (req.method === "GET" && webRead.test(path)))
      )
        throw deny(403, "m4_route_disabled");
      if (
        req.method === "POST" &&
        (req.headers.origin !== origin || req.headers["x-nexpoly-page"] !== page.key)
      )
        throw deny(403, "write_origin_denied");
      url.searchParams.delete("np_page");
      if (url.search) throw deny(400, "unexpected_service_query");
      const token = (await readFile(page.route.tokenFile, "utf8")).trim();
      const data = req.method === "POST" ? await readBody(req, 280 * 1024) : undefined;
      return relay(
        req,
        res,
        usage ? "model" : "knowledge",
        path,
        req.method,
        page,
        digest(token),
        data,
        req.headers["content-type"],
      );
    },
    async model(req, res, turn, readBody) {
      if (!["/v1/chat/completions", "/v1/responses"].includes(req.url) || req.method !== "POST")
        throw deny(403, "model_route_disabled");
      const session = req.headers["x-m4-session"],
        message = req.headers["x-m4-turn"],
        id = req.headers["x-m4-request"];
      if (
        turn.form ||
        turn.session !== session ||
        (turn.message && turn.message !== message) ||
        !/^msg_[\w-]{1,120}$/.test(message || "") ||
        !new RegExp("^" + uuid + "$").test(id || "")
      )
        throw deny(403, "model_turn_mismatch");
      turn.message = message;
      return relay(
        req,
        res,
        "model",
        req.url,
        "POST",
        turn.page,
        turn.credential,
        await readBody(req, 2 * 1024 * 1024),
        "application/json",
        { "x-m4-session": session, "x-m4-turn": message, "x-m4-request": id },
      );
    },
    async knowledge(t, method, params) {
      const obj = Buffer.from(JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }));
      const source = await request(
        "knowledge",
        "/mcp",
        "POST",
        t.page,
        t.credential,
        obj,
        "application/json",
        AbortSignal.timeout(15000),
      );
      if (source.statusCode !== 200) {
        source.destroy();
        throw deny(403, "knowledge_access_denied");
      }
      const parts = [];
      for await (const part of source) parts.push(part);
      const reply = JSON.parse(Buffer.concat(parts));
      if (reply.error || !reply.result) throw deny(403, "knowledge_operation_denied");
      return reply.result;
    },
  };
}
