// Exercise lifecycle events unique to long-running model streams, without invoking a model.
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { services } from "./services.mjs";

test("the model stream closes when its workspace generation changes", { timeout: 12000 }, async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "np-m4-stream-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const config = {
    model_key_file: join(dir, "model"),
    knowledge_key_file: join(dir, "kb"),
    authority_socket: join(dir, "test.sock"),
  };
  const tokenFile = join(dir, "generation");
  await Promise.all([
    writeFile(config.model_key_file, "model-secret"),
    writeFile(config.knowledge_key_file, "knowledge-secret"),
    writeFile(tokenFile, "first"),
  ]);
  const generation = createHash("sha256").update("first").digest("hex");
  const turn = {
    session: "ses_current",
    credential: generation,
    until: Date.now() + 60000,
    page: { grant: "synthetic-login", active: new Set(), route: { tokenFile } },
  };
  let requestHeaders;
  const upstream = http.createServer((req, res) => {
    requestHeaders = req.headers;
    req.resume();
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write("data: first\n\n");
  });
  const service = await services(config, async () => {});
  const server = http.createServer((req, res) => {
    service.model(req, res, turn, async () => Buffer.from("{}")).catch(() => res.destroy());
  });
  for (const [s, addr] of [
    [upstream, [config.authority_socket]],
    [server, [0, "127.0.0.1"]],
  ]) {
    s.listen(...addr);
    await once(s, "listening");
    t.after(() => {
      s.closeAllConnections();
      return new Promise((done) => s.close(done));
    });
  }
  const requestId = randomUUID();
  const req = http.request({
    host: "127.0.0.1",
    port: server.address().port,
    path: "/v1/chat/completions",
    method: "POST",
    headers: {
      "x-m4-session": turn.session, "x-m4-turn": "msg_current", "x-m4-request": requestId,
      "x-m4-turn-expires-ms": String(turn.until + 10000),
    },
  });
  const connected = once(req, "response");
  req.end();
  const [stream] = await connected;
  const closed = once(stream, "aborted");
  const [first] = await once(stream, "data");
  assert.equal(first.toString(), "data: first\n\n");
  assert.equal(requestHeaders.authorization, "Bearer model-secret");
  assert.equal(requestHeaders["x-m4-handoff"], turn.page.grant);
  assert.equal(requestHeaders["x-m4-request"], requestId);
  assert.equal(requestHeaders["x-m4-turn-expires-ms"], String(turn.until));
  const start = Date.now();
  await writeFile(tokenFile, "restarted");
  await closed;
  assert.ok(Date.now() - start < 10000);
  assert.equal(turn.page.active.size, 0);
});
