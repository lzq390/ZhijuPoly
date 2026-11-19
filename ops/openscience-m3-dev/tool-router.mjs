// The private instance socket and the original authenticated page are both required.
import http from "node:http";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { chmod, readFile } from "node:fs/promises";
import { once } from "node:events";

const names = new Set([
  "predict_polymer_properties",
  "polymerize_monomers",
  "dft_job_submit",
  "dft_job_status",
  "dft_job_list",
  "dft_job_result",
]);
const sha = (value) => createHash("sha256").update(value).digest("hex");
const ordered = (value) =>
  Array.isArray(value)
    ? value.map(ordered)
    : value && typeof value === "object"
      ? Object.fromEntries(
          Object.keys(value)
            .sort()
            .map((k) => [k, ordered(value[k])]),
        )
      : value;
const deny = (status, message) => Object.assign(Error(message), { status });
async function bytes(stream, limit) {
  const parts = [];
  let count = 0;
  for await (const b of stream) {
    if ((count += b.length) > limit) throw deny(413, "payload_limit");
    parts.push(b);
  }
  return Buffer.concat(parts);
}
async function unix(socketPath, method, path, headers, payload) {
  const req = http.request({
    socketPath,
    method,
    path,
    headers,
    agent: false,
    signal: AbortSignal.timeout(48000),
  });
  const response = once(req, "response");
  req.end(payload);
  const [r] = await response,
    raw = await bytes(r, 2 * 1024 * 1024);
  if (r.statusCode !== 200)
    throw deny(r.statusCode >= 400 && r.statusCode < 500 ? r.statusCode : 503, "tool_upstream_refused");
  return JSON.parse(raw);
}
export async function toolRouter(config, auth, validate) {
  const ingress = (await readFile(config.ingress_file, "utf8")).trim();
  const turns = new Map(),
    approvals = new Map(),
    servers = [];
  const gc = () => {
    for (const [k, t] of turns)
      if (t.until <= Date.now()) {
        turns.delete(k);
        for (const op of t.calls.values()) approvals.delete(op.id);
      }
  };
  async function mcp(method, params, credential) {
    const msg = await unix(
      config.gateway_socket,
      "POST",
      "/mcp",
      {
        authorization: "Bearer " + ingress,
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        ...(credential ? { "x-openscience-operation": credential } : {}),
      },
      JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    );
    if (!msg.result || msg.error) throw deny(502, "tool_protocol_failed");
    return msg.result;
  }
  function begin(page, session, credential) {
    gc();
    const handle = randomBytes(32).toString("base64url");
    turns.set(handle, {
      page,
      session,
      credential: sha(credential),
      until: Date.now() + 15 * 60_000,
      calls: new Map(),
    });
    return handle;
  }
  async function original(t) {
    if (!t || t.until <= Date.now()) throw deny(401, "turn_expired");
    await validate(t.page);
    const current = (await readFile(t.page.route.tokenFile, "utf8")).trim();
    if (sha(current) !== t.credential) throw deny(401, "workspace_generation_changed");
    if (t.until <= Date.now()) throw deny(401, "turn_expired");
  }
  async function handle(user, req) {
    gc();
    if (req.method !== "POST") throw deny(405, "method");
    const h = req.headers.authorization,
      t = typeof h === "string" && h.startsWith("Bearer ") && turns.get(h.slice(7));
    if (!t || t.page.owner !== user) throw deny(401, "workspace_turn_required");
    const data = JSON.parse(await bytes(req, 16384));
    await original(t);
    if (
      data.session !== t.session ||
      !/^msg_[a-zA-Z0-9_-]{1,120}$/.test(data.turn) ||
      (t.message && t.message !== data.turn)
    )
      throw deny(403, "turn_mismatch");
    t.message = data.turn;
    if (req.url === "/catalog") {
      const result = await mcp("tools/list", {});
      if (
        result.tools?.length !== names.size ||
        new Set(result.tools.map((x) => x.name)).size !== names.size ||
        result.tools.some((x) => !names.has(x.name))
      )
        throw deny(502, "unexpected_tool_catalog");
      return result;
    }
    if (
      !["/prepare", "/call"].includes(req.url) ||
      !names.has(data.tool) ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(data.call) ||
      !data.arguments ||
      Array.isArray(data.arguments) ||
      typeof data.arguments !== "object"
    )
      throw deny(400, "invalid_tool_call");
    const hash = sha(JSON.stringify(ordered(data.arguments)));
    let op = t.calls.get(data.call);
    if (op && (op.hash !== hash || op.tool !== data.tool)) throw deny(409, "tool_call_changed");
    if (!op) {
      op = { id: randomUUID(), tool: data.tool, hash, turn: t, call: data.call, confirmed: false };
      t.calls.set(data.call, op);
      if (data.tool === "dft_job_submit") approvals.set(op.id, op);
    }
    if (req.url === "/prepare") return { operation_id: op.id };
    if (data.tool === "dft_job_submit" && !op.confirmed) throw deny(403, "human_confirmation_required");
    const grant = await auth("delegations", {
      agent_session: t.page.grant,
      operation_id: op.id,
      tool: data.tool,
      arguments_sha256: hash,
      workspace_session: t.session,
      turn_id: data.turn,
      call_id: data.call,
      confirmed: op.confirmed,
      workspace_generation: t.credential,
      turn_expires_ms: t.until,
      ...(["dft_job_status", "dft_job_result"].includes(data.tool) ? { job_id: data.arguments.job_id } : {}),
    });
    const result = await mcp(
      "tools/call",
      { name: data.tool, arguments: data.arguments },
      grant.operation_token,
    );
    await original(t);
    if (Date.now() >= grant.valid_until * 1000) throw deny(401, "tool_lease_expired");
    return result;
  }
  // The native permission record comes from the scoped instance, including the saved
  // operation ID and actual (server generated) session and tool call association.
  async function confirm(page, path, query, payload) {
    const match = /^\/permission\/(per_[a-zA-Z0-9_-]+)\/reply$/.exec(path);
    if (!match) throw deny(403, "unsupported_confirmation");
    const data = JSON.parse(payload);
    if (!["once", "reject"].includes(data.reply)) throw deny(403, "single_confirmation_required");
    const token = (await readFile(page.route.tokenFile, "utf8")).trim();
    const permissions = await unix(
      page.route.socket,
      "GET",
      "/permission" + query,
      { authorization: "Bearer " + token },
      null,
    );
    const record = permissions.find((x) => x.id === match[1]);
    const op = record && approvals.get(record.metadata?.operation_id);
    if (
      !op ||
      record.sessionID !== op.turn.session ||
      record.tool?.callID !== op.call ||
      op.turn.page.login !== page.login ||
      op.turn.page.owner !== page.owner ||
      sha(JSON.stringify(ordered(record.metadata?.arguments))) !== op.hash
    )
      throw deny(403, "confirmation_owner_mismatch");
    await original(op.turn);
    op.confirmed = data.reply === "once";
  }
  try {
    for (const [user, socket] of Object.entries(config.peer_sockets)) {
      const server = http.createServer((req, res) =>
        handle(user, req)
          .then((result) => {
            res.writeHead(200, { "content-type": "application/json" });
            res.end(JSON.stringify(result));
          })
          .catch((err) => {
            res.writeHead(err.status || 503);
            res.end("Tool access unavailable");
          }),
      );
      servers.push(server);
      server.listen(socket);
      await once(server, "listening");
      await chmod(socket, 0o666);
    }
  } catch (e) {
    for (const s of servers) s.close();
    throw e;
  }
  return {
    begin,
    confirm,
    close() {
      for (const s of servers) {
        s.close();
        s.closeAllConnections();
      }
    },
  };
}
