import assert from "node:assert/strict";
import test from "node:test";
import { Readable, Writable } from "node:stream";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { handoffPlugin, handoffContentSecurityPolicy } from "../overlay/build/handoff.ts";
import { createHandoffAuthFixture, guestHandoffSession, memberHandoffSession } from "../overlay/src/mocks/handoffAuthFixture.ts";
class Reply extends Writable {
  constructor() { super(); this.headers = {}; this.statusCode = 200; this.headersSent = false; this.chunks = []; this.on("error", error => { this.failure = error; }); }
  _write(chunk, _encoding, next) { this.chunks.push(Buffer.from(chunk)); next(); }
  setHeader(name, value) { this.headers[name.toLowerCase()] = value; }
  flushHeaders() { this.headersSent = true; }
  text() { return Buffer.concat(this.chunks).toString("utf8"); }
}
function server(options = {}, publicDir = "/unit-no-assets") {
  const plugin = handoffPlugin(options); let middleware;
  plugin.configResolved({ publicDir }); plugin.configureServer({ middlewares: { use(value) { middleware = value; } } });
  return (url, method = "GET", headers = {}, body = "") => {
    const request = Readable.from(body ? [Buffer.from(body)] : []);
    Object.assign(request, { url, method, headers, complete: true });
    const response = new Reply(); let next = false;
    const done = new Promise(resolve => { response.once("finish", resolve); response.once("close", resolve); });
    middleware(request, response, () => { next = true; });
    return { request, response, done, next };
  };
}
test("handoff CSP preserves local-only connection boundaries", () => {
  assert.match(handoffContentSecurityPolicy, /connect-src 'self'/); assert.match(handoffContentSecurityPolicy, /object-src 'none'/);
  assert.doesNotMatch(handoffContentSecurityPolicy, /connect-src[^;]*https:/);
});
test("HTTP scenario mode requires an explicit provider", () => assert.throws(() => handoffPlugin({ httpFixtures: true }), /explicitly supplied/));
test("health does not invoke a business provider", async () => {
  let calls = 0; const call = server({ fixtureFactory: () => { calls++; throw new Error("must not run"); } })("/health");
  await call.done; assert.equal(call.response.statusCode, 200); assert.equal(JSON.parse(call.response.text()).synthetic, true); assert.equal(calls, 0);
});
test("ordinary static paths call the next middleware", () => {
  const call = server()("/unit-page"); assert.equal(call.next, true); assert.match(call.response.headers["content-security-policy"], /frame-ancestors 'self'/);
});
test("unimplemented business APIs fail closed", async () => {
  const call = server()("/api/v1/unit-unknown", "POST"); await call.done;
  assert.equal(call.response.statusCode, 501); assert.equal(JSON.parse(call.response.text()).code, "HANDOFF_API_NOT_IMPLEMENTED");
});
test("injected provider receives the original method, body, headers and abort signal", async () => {
  let received; const send = server({ httpFixtures: true, fixtureFactory: () => async (input, init) => {
    received = { input, init }; return new Response("unit-echo", { status: 202, headers: { "X-Unit": "yes" } });
  } });
  const call = send("/api/v1/unit-echo", "POST", { "content-type": "application/json", "x-test": "unit" }, '{"unitOnly":true}'); await call.done;
  assert.equal(received.init.method, "POST"); assert.equal(received.init.body, '{"unitOnly":true}');
  assert.equal(received.init.headers.get("x-test"), "unit"); assert.ok(received.init.signal instanceof AbortSignal);
  assert.equal(call.response.statusCode, 202); assert.equal(call.response.text(), "unit-echo"); assert.equal(call.response.headers["x-unit"], "yes");
});
test("HEAD responses suppress provider response bodies", async () => {
  const call = server({ httpFixtures: true, fixtureFactory: () => async () => new Response("unit body") })("/api/v1/unit-head", "HEAD");
  await call.done; assert.equal(call.response.text(), "");
});
test("the same synthetic HTTP cookie retains one provider session", async () => {
  let created = 0; const send = server({ httpFixtures: true, fixtureFactory: () => { created++; return async () => new Response("unit"); } });
  const first = send("/api/v1/unit"); await first.done; const cookie = first.response.headers["set-cookie"].split(";")[0];
  const second = send("/api/v1/unit", "GET", { cookie }); await second.done; assert.equal(created, 1);
  const third = send("/api/v1/unit"); await third.done; assert.equal(created, 2);
});
test("downloads use only a temporary unit asset and preserve GET/HEAD headers", async () => {
  const temp = await mkdtemp(join(tmpdir(), "handoff-download-unit-")); await mkdir(join(temp, "handoff/downloads"), { recursive: true });
  await writeFile(join(temp, "handoff/downloads/result.txt"), "UNIT-ONLY-DOWNLOAD"); const send = server({}, temp);
  const get = send("/api/v1/monomer-dft/jobs/unit/artifacts/result.txt"); await get.done; assert.equal(get.response.text(), "UNIT-ONLY-DOWNLOAD");
  assert.equal(get.response.headers["content-length"], 18); const head = send("/api/v1/monomer-dft/jobs/unit/artifacts/result.txt", "HEAD"); await head.done;
  assert.equal(head.response.text(), ""); assert.equal(head.response.headers["content-length"], 18);
});
test("the injected static reader rejects external and traversal-shaped URLs", async () => {
  let native; const send = server({ httpFixtures: true, fixtureFactory: options => { native = options.nativeFetch; return async () => new Response("unit"); } });
  const call = send("/api/v1/unit"); await call.done;
  assert.equal((await native("https://unit.invalid/handoff/downloads/result.txt")).status, 501);
  assert.equal((await native("http://handoff.local/handoff/downloads/..%2Foutside.txt")).status, 501);
});
test("client abort reaches the provider and releases request listeners", async () => {
  let notify; const started = new Promise(resolve => { notify = resolve; }); let signal;
  const call = server({ httpFixtures: true, fixtureFactory: () => async (_input, init) => {
    signal = init.signal; notify(); return await new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("unit", "AbortError")), { once: true }));
  } })("/api/v1/unit-loading");
  await started; call.request.emit("aborted"); call.response.destroy(); await call.done; await new Promise(resolve => setImmediate(resolve));
  assert.equal(signal.aborted, true); assert.equal(call.request.listenerCount("aborted"), 0);
});
const url = name => `http://handoff.local/api/v1/auth/${name}`;
test("synthetic member and guest identities retain capability boundaries", () => {
  assert.equal(guestHandoffSession().authenticated, false); const member = memberHandoffSession();
  assert.equal(member.account_type, "member"); assert.equal(member.capabilities["openscience.use"], true); assert.equal(member.capabilities.dev_gpu_control, false);
});
test("auth-only fixture does not consume external or business requests", async () => {
  const auth = createHandoffAuthFixture(); assert.equal(await auth("https://unit.invalid/api/v1/auth/session"), undefined);
  assert.equal(await auth("http://handoff.local/api/v1/unit-business"), undefined);
});
test("synthetic guest session has no stored identity", async () => {
  const response = await createHandoffAuthFixture({ guest: true })(url("session")); const session = await response.json();
  assert.equal(session.user, null); assert.equal(session.csrf_token, null); assert.deepEqual(session.capabilities, {});
});
test("empty demonstration login is rejected", async () => {
  const auth = createHandoffAuthFixture({ guest: true }); const response = await auth(url("login"), { method: "POST", body: "{}" });
  assert.equal(response.status, 400); assert.equal((await (await auth(url("session"))).json()).authenticated, false);
});
test("demonstration login never returns the entered secret", async () => {
  const auth = createHandoffAuthFixture({ guest: true }); const response = await auth(new Request(url("login"), { method: "POST", body: JSON.stringify({ username: " unit-user ", password: "UNIT-ONLY-INPUT" }) }));
  assert.equal(response.status, 200); const text = await response.text(); assert.equal(text.includes("UNIT-ONLY-INPUT"), false); assert.equal(JSON.parse(text).user.username, "unit-user");
});
test("guest demonstration password change remains denied", async () => assert.equal((await createHandoffAuthFixture({ guest: true })(url("password"), { method: "POST", body: "{}" })).status, 401));
test("invalid demonstration password input retains the member session", async () => {
  const auth = createHandoffAuthFixture(); assert.equal((await auth(url("password"), { method: "POST", body: JSON.stringify({ current_password: "UNIT-ONLY", new_password: "short" }) })).status, 400);
  assert.equal((await (await auth(url("session"))).json()).authenticated, true);
});
test("valid demonstration password input retires the session", async () => {
  const auth = createHandoffAuthFixture(); assert.equal((await auth(url("password"), { method: "POST", body: JSON.stringify({ current_password: "UNIT-ONLY", new_password: "UNIT-ONLY-NEW-INPUT" }) })).status, 204);
  assert.equal((await (await auth(url("session"))).json()).authenticated, false);
});
test("demonstration logout retires only the in-memory session", async () => {
  const auth = createHandoffAuthFixture(); assert.equal((await auth(url("logout"), { method: "POST" })).status, 204);
  assert.equal((await (await auth(url("session"))).json()).authenticated, false);
});
test("unknown demonstration auth route is explicit", async () => assert.equal((await createHandoffAuthFixture()(url("unit-unknown"))).status, 501));

test("non-object demonstration JSON is rejected without retiring or elevating a session", async () => {
  for (const value of [null, true, [], "unit-value"]) {
    const auth = createHandoffAuthFixture({ guest: true });
    assert.equal((await auth(url("login"), { method: "POST", body: JSON.stringify(value) })).status, 400);
    assert.equal((await (await auth(url("session"))).json()).authenticated, false);
  }
});
