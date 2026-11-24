// Real browser rehearsal on the two retained dev-m1 quota clones.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { developmentBrowser, retainedBindings } from "./openscience_dev_browser.mjs";
import { holdTicket, losses, monitor, submitExchange } from "./openscience_browser_revocation.mjs";

const { values } = parseArgs({
  options: { state: { type: "string" }, work: { type: "string" }, phase: { type: "string" } },
});
const state = resolve(values.state),
  work = resolve(values.work),
  phase = values.phase;
assert.ok(["candidate", "rollback"].includes(phase));
const read = async (file) => JSON.parse(await readFile(file, "utf8"));
const stack = await read(resolve(state, "running.json"));
const registry = await read(resolve(stack.registry, "registry.json"));
const prior = await read(
  new URL("../../docs/verification/openscience-m5-review-round3-20261124.json", import.meta.url),
);
const A = "test-user-a",
  B = "test-user-b";
assert.equal(stack.m6_operator_id, "10000000-0000-4000-8000-000000000001");
assert.ok(
  stack.quota_files &&
    stack.m4 &&
    !stack.retry_evidence &&
    !Object.values(stack.adapter_retry_evidence ?? {}).some(Boolean),
);
assert.equal(registry.image, prior.images[phase]);
const pins = retainedBindings(stack, registry);
const report = {
  status: "failed",
  phase,
  environment: "dev-m1",
  image: registry.image,
  started: new Date().toISOString(),
  stages: {},
};
const gateTool = fileURLToPath(new URL("./verify_openscience_m6_operator.py", import.meta.url));
const sha = (s) => createHash("sha256").update(s).digest("hex");
const project = "/openscience/files/projects/same-project";
const routes = {
  notebook: "/openscience-api/file/content?" + new URLSearchParams({ directory: project, path: "notebook.txt" }),
  model_usage: "/openscience-api/m4/model/usage",
  private_knowledge: "/knowledge-api/kbs",
};
function gate(action) {
  return JSON.parse(
    execFileSync("python3", [gateTool, "gate", "--state", state, "--deployment", stack.deployment, action], {
      encoding: "utf8",
      timeout: 30000,
    }),
  );
}
async function ticketStatus(page) {
  return page.evaluate(async () => {
    const session = await (await fetch("/api/v1/auth/session")).json();
    if (!session.session_id || !session.capabilities?.["openscience.use"])
      throw Error("Expected a logged-in member");
    const result = await fetch("/api/v1/openscience/workspace-ticket", {
      method: "POST",
      headers: { "X-Session-Context": session.session_id, "X-CSRF-Token": session.csrf_token },
    });
    return result.status;
  });
}
async function deniedMember(page) {
  assert.equal(await ticketStatus(page), 403);
  const alert = page.getByRole("alert").filter({ hasText: "当前账号未获准使用此工作区。" });
  await alert.getByRole("button", { name: "重试", exact: true }).waitFor({ timeout: 30000 });
  assert.ok(!page.frames().some((f) => f.url().startsWith(stack.agent + "/") && !f.url().includes("/_nexpoly/")));
  const sidebar = await page.locator(".np-sidebar-desktop").innerText();
  assert.ok(!sidebar.includes("M1 synthetic ") && !sidebar.includes("same-project"));
}
async function contents(frame) {
  const observed = await frame.evaluate(
    async ({ project, routes, A, B }) => {
      async function get(url) {
        const r = await fetch(url);
        if (!r.ok) throw Error("Private read failed with " + r.status);
        return r.json();
      }
      const note = await get(routes.notebook),
        ids = {};
      for (const [kind, directory] of [
        ["general", "/openscience/files/general"],
        ["project", project],
      ]) {
        const q = "?" + new URLSearchParams({ directory });
        const list = await get("/openscience-api/session" + q);
        if (list.some((s) => s.title === "M1 synthetic " + B + " " + kind)) throw Error("Foreign session listed");
        const session = list.find((s) => s.title === "M1 synthetic " + A + " " + kind);
        if (!session) throw Error("Missing original " + kind + " conversation");
        const history = await get("/openscience-api/session/" + session.id + "/message" + q);
        const parts = history.flatMap((m) => m.parts);
        if (!parts.some((p) => p.text === "M1 synthetic " + A + " " + kind + " prompt"))
          throw Error("Missing owner prompt");
        if (kind === "project" && !parts.some((p) => p.type === "file" && p.filename === "notebook.txt"))
          throw Error("Missing project attachment");
        ids[kind] = session.id;
      }
      return { text: note.content, ids };
    },
    { project, routes, A, B },
  );
  const hash = sha(observed.text);
  assert.equal(hash, prior.rollback.files_panel[A].notebook_sha256);
  return {
    notebook_sha256: hash,
    session_refs: Object.fromEntries(Object.entries(observed.ids).map(([k, id]) => [k, sha(id)])),
    retained_project_attachment: true,
    foreign_sessions: 0,
    uid: pins[A].uid,
  };
}
async function privateStatuses(page, key) {
  return page.evaluate(
    async ({ key, routes }) => {
      const entries = await Promise.all(
        Object.entries(routes).map(async ([name, path]) => {
          const r = await fetch(path, { headers: { "X-Nexpoly-Page": key }, cache: "no-store" });
          await r.body?.cancel();
          return [name, r.status];
        }),
      );
      return Object.fromEntries(entries);
    },
    { key, routes },
  );
}
const { browser, device, app, login, workbench, logout } = await developmentBrowser(state, stack);
const profiles = [];
const forbiddenOrigins = new Set();
async function member(name) {
  const ctx = await device();
  ctx.on("request", (req) => {
    const u = new URL(req.url());
    if (["https:", "http:"].includes(u.protocol) && ![stack.parent, stack.agent].includes(u.origin)) {
      const destination = req.resourceType() + " " + u.origin;
      if (!forbiddenOrigins.has(destination)) console.log("Unexpected browser destination:", destination);
      forbiddenOrigins.add(destination);
    }
  });
  const page = await app(ctx);
  const policy = await page.evaluate(async () =>
    (await fetch("/", { method: "HEAD" })).headers.get("Content-Security-Policy"),
  );
  assert.ok(
    policy?.includes("style-src 'self' 'unsafe-inline'") && policy.includes("frame-src 'self' " + stack.agent),
  );
  const identity = await login(page, name);
  profiles.push({ page, name });
  return { ctx, page, identity };
}
try {
  assert.equal(gate("status").enabled, phase === "candidate");
  const b = await member(B);
  await deniedMember(b.page);
  console.log(phase, "ordinary member denied before exchange");
  report.stages.ordinary_member = { platform_login: 200, new_ticket: 403, no_private_sidebar: true };
  const a = await member(A);
  assert.equal(a.identity.user_id, stack.m6_operator_id);
  if (phase === "rollback") {
    await deniedMember(a.page);
    assert.equal(gate("open").enabled, true);
    await a.page.reload({ waitUntil: "domcontentloaded" });
  }
  const f = await workbench(a.page);
  const data = await contents(f);
  console.log(phase, "original operator chats, attachment and notebook verified");
  report.stages.operator = data;
  if (phase === "candidate") {
    await a.page
      .locator(".np-sidebar-desktop")
      .getByRole("button", { name: "M1 synthetic " + A + " general", exact: true })
      .waitFor();
    const key = await f.evaluate(() => history.state.npPage);
    const stream = await monitor(stack.agent, a.ctx, key);
    const before = await privateStatuses(stream.probe, key);
    assert.ok(Object.values(before).every((s) => s === 200));
    const held = await holdTicket(a.page);
    const beforeUnusedTicket = performance.now();
    const unused = await holdTicket(a.page);
    const start = performance.now();
    assert.equal(gate("close").enabled, false);
    const closed = await losses(start, stream, f);
    console.log("Candidate gate closed; original tab, sidebar and live stream retired", JSON.stringify(closed));
    const after = await privateStatuses(stream.probe, key);
    assert.ok(Object.values(after).every((s) => [401, 403].includes(s)));
    assert.equal(await ticketStatus(a.page), 403);
    assert.equal(await ticketStatus(b.page), 403);
    assert.equal(await submitExchange(stack.agent, a.page, held.fields), "nexpoly:openscience:error");
    assert.equal(gate("open").enabled, true);
    assert.equal(await submitExchange(stack.agent, a.page, unused.fields), "nexpoly:openscience:error");
    const ticketAgeUpperBoundMs = Math.ceil(performance.now() - beforeUnusedTicket);
    assert.ok(ticketAgeUpperBoundMs < unused.ttl * 1000, "Outstanding ticket must be refused before its TTL");
    assert.ok([401, 403].includes(await stream.status()));
    assert.equal(await ticketStatus(b.page), 403);
    await a.page.reload({ waitUntil: "domcontentloaded" });
    assert.deepEqual(await contents(await workbench(a.page)), data);
    report.stages.kill_switch = {
      ...closed,
      before,
      after,
      closed_ticket_http: { a: 403, b: 403 },
      old_ticket_during_close: "denied",
      unused_ticket_after_reopen: "denied_before_expiry",
      unused_ticket_age_upper_bound_ms: ticketAgeUpperBoundMs,
      old_context_after_reopen: "denied",
      fresh_operator_data_matches: true,
    };
    // Keep the durable switch closed through teardown and the next image's bootstrap.
    assert.equal(gate("close").enabled, false);
  } else {
    const candidate = await read(resolve(work, "candidate.json"));
    assert.equal(candidate.status, "passed");
    assert.deepEqual(data, candidate.stages.operator);
    assert.equal(await ticketStatus(b.page), 403);
    report.stages.rollback = {
      operator_ticket_before_reopen: 403,
      ordinary_member_after_reopen: 403,
      private_data_matches: true,
    };
  }
  assert.equal(forbiddenOrigins.size, 0, "A browser tried leaving the two private candidate origins");
  report.shared_or_external_requests = 0;
  report.status = "passed";
} catch (error) {
  console.error(phase, String(error).replace(/np_page=[^ &"]+/g, "np_page=[redacted]"));
  process.exitCode = 1;
} finally {
  for (const { page, name } of profiles.reverse()) await logout(page, name).catch(() => {});
  await browser.close();
  report.finished = new Date().toISOString();
  await writeFile(resolve(work, phase + ".json"), JSON.stringify(report, null, 2) + "\n", {
    flag: "wx",
    mode: 0o600,
  });
  console.log("M6 operator browser", phase, report.status);
}
