// Developer acceptance only. Repeat a successful MCP write after deliberately dropping its first reply.
// Distinct accounts and the native usage/operation IDs are checked separately against PostgreSQL.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { chmodSync, renameSync, statSync, writeFileSync } from "node:fs";
import http from "node:http";
import { isAbsolute } from "node:path";
import { parseArgs } from "node:util";

const { values } = parseArgs({
  options: {
    listen: { type: "string" },
    upstream: { type: "string" },
    evidence: { type: "string" },
  },
});
const { listen, upstream, evidence } = values;
if (
  ![listen, upstream, evidence].every((p) => typeof p === "string" && isAbsolute(p)) ||
  new Set([listen, upstream, evidence]).size !== 3 ||
  !statSync(upstream).isSocket()
) {
  throw Error("Expected two distinct local Unix sockets and a new evidence path");
}
const counts = {
  job_ids: [],
  retries: 0,
  sync_retries: { predict_polymer_properties: 0, polymerize_monomers: 0 },
};
const state = () => JSON.stringify(counts, null, 2) + "\n";
writeFileSync(evidence, state(), { mode: 0o600, flag: "wx" });
function checkpoint() {
  const temp = evidence + "." + randomUUID();
  writeFileSync(temp, state(), { mode: 0o600, flag: "wx" });
  renameSync(temp, evidence);
}

async function read(stream, limit) {
  let total = 0;
  const parts = [];
  for await (const chunk of stream) {
    total += chunk.length;
    if (total > limit) throw Error("Payload limit exceeded");
    parts.push(chunk);
  }
  return Buffer.concat(parts, total);
}

function forward(headers, body) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        socketPath: upstream,
        path: "/mcp",
        method: "POST",
        headers,
        agent: false,
        signal: AbortSignal.timeout(45000),
      },
      async (response) => {
        try {
          const bytes = await read(response, 2 * 1024 * 1024);
          resolve({ status: response.statusCode, type: response.headers["content-type"], bytes });
        } catch (e) {
          reject(e);
        }
      },
    );
    request.on("error", reject);
    request.end(body);
  });
}

function decode(response, id) {
  if (response.status !== 200 || !response.type?.includes("application/json")) return;
  const rpc = JSON.parse(response.bytes);
  if (rpc.id !== id || rpc.jsonrpc !== "2.0" || rpc.error || !rpc.result || rpc.result.isError) return;
  const content = rpc.result.content;
  if (!Array.isArray(content) || content.length !== 1 || content[0].type !== "text") {
    throw Error("Unexpected MCP write receipt");
  }
  return JSON.parse(content[0].text);
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method !== "POST" || req.url !== "/mcp") {
      res.writeHead(404).end();
      return;
    }
    const bytes = await read(req, 65536);
    const rpc = JSON.parse(bytes);
    const headers = {};
    for (const k of ["authorization", "x-openscience-operation", "content-type", "accept"]) {
      if (typeof req.headers[k] === "string") headers[k] = req.headers[k];
    }
    headers["content-length"] = String(bytes.length);
    let reply = await forward(headers, bytes);
    const tool = rpc.method === "tools/call" ? rpc.params?.name : null;
    if (tool === "dft_job_submit" || Object.hasOwn(counts.sync_retries, tool)) {
      const original = decode(reply, rpc.id);
      if (original !== undefined) {
        assert.match(headers["x-openscience-operation"] ?? "", /^[A-Za-z0-9_-]{43}$/);
        const retry = await forward(headers, bytes); // Same caller's tool credential and exact original body.
        assert.equal(retry.status, reply.status);
        const repeated = decode(retry, rpc.id);
        assert.ok(repeated && typeof repeated === "object");
        if (tool === "dft_job_submit") {
          assert.match(original.job_id, /^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$/i);
          assert.match(original.request_sha256, /^[a-f\d]{64}$/);
          assert.equal(repeated.job_id, original.job_id);
          assert.equal(repeated.request_sha256, original.request_sha256);
          assert.equal(repeated.created_at, original.created_at);
          assert.ok(!counts.job_ids.includes(original.job_id), "Duplicate acceptance job");
          counts.job_ids.push(original.job_id);
          counts.retries++;
        } else {
          assert.deepEqual(repeated, original);
          counts.sync_retries[tool]++;
        }
        checkpoint();
        reply = retry;
      }
    }
    res.writeHead(reply.status, {
      "Content-Type": reply.type ?? "application/json",
      "Cache-Control": "no-store",
    });
    res.end(reply.bytes);
  } catch {
    // Errors and comparison diagnostics can contain MCP arguments or credentials.
    counts.failed = true;
    checkpoint();
    res.writeHead(502, { "Cache-Control": "no-store" }).end("M3 retry verification failed");
  }
});
server.listen(listen, () => chmodSync(listen, 0o600));
