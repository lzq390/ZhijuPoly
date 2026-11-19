// Browser acceptance against the dedicated development stack and retained M1 workspaces.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { developmentBrowser } from "./openscience_dev_browser.mjs";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { checkVisibleWorkspace } from "./openscience_m2_visible_ui.mjs";

function option(key) {
  const at = process.argv.indexOf(key);
  if (at < 0 || !process.argv[at + 1]) throw Error("Missing " + key);
  return resolve(process.argv[at + 1]);
}
const state = option("--state"),
  output = option("--output");
const stack = JSON.parse(await readFile(resolve(state, "running.json"), "utf8"));
const { parent, agent } = stack;
const { browser, device, app, login, logout, workbench, authorizedContexts } = await developmentBrowser(state, stack);
const A = "test-user-a",
  B = "test-user-b";
const project = "/openscience/files/projects/same-project",
  general = "/openscience/files/general";
const report = {
  start: new Date().toISOString(),
  status: "failed",
  browser: browser.version(),
  authority: "real NexPoly AuthService + dedicated PostgreSQL",
  origins: { parent, agent },
  environment: "dev-m1",
  retained_volumes: stack.user_volumes,
  stages: {},
};
let restorePilot = false,
  pausedAuthority = false;
function ownedContainer(id) {
  const label = execFileSync(
    "docker",
    ["inspect", "--format", '{{index .Config.Labels "org.nexpoly.m2.deployment"}}', id],
    { encoding: "utf8" },
  ).trim();
  assert.equal(label, stack.deployment);
  return id;
}
function pauseAuthority(paused) {
  execFileSync("docker", [paused ? "pause" : "unpause", ownedContainer(stack.api)]);
}
function setPilot(enabled) {
  ownedContainer(stack.pg);
  const sql =
    "UPDATE openscience_m2.workspace_bindings SET enabled=" +
    (enabled ? "true" : "false") +
    " WHERE environment='dev-m1' AND workspace_user_id='test-user-a' AND enabled=" +
    (enabled ? "false" : "true") +
    " RETURNING workspace_user_id";
  const script =
    'export PGPASSWORD="$(cat /admin-pw)" PGHOST=/m2pg PGUSER=m2_admin; exec psql -XqAt -v ON_ERROR_STOP=1 -d nexpoly_m2_dev -c "$1"';
  const changed = execFileSync("docker", ["exec", stack.pg, "sh", "-ec", script, "m2", sql], {
    encoding: "utf8",
  }).trim();
  assert.equal(changed, A);
}
async function ownership(f, owner, foreign) {
  const result = await f.evaluate(
    async ({ owner, foreign, project, general }) => {
      async function get(path, params = {}) {
        const url = new URL("/openscience-api" + path, location.origin);
        url.search = new URLSearchParams(params);
        const r = await fetch(url);
        if (r.status !== 200)
          throw Error("Private read failed: " + path.split("/").slice(0, 2).join("/") + " " + r.status);
        return r.json();
      }
      const folders = await get("/api/resolve-folder/list");
      const file = await get("/file/content", { directory: project, path: "notebook.txt" });
      const ids = {};
      let ownSessions = 0,
        foreignMarkers = 0,
        attachment = false;
      for (const [kind, directory] of [
        ["general", general],
        ["project", project],
      ]) {
        const list = await get("/session", { directory });
        foreignMarkers += list.filter((s) => s.title.includes(foreign)).length;
        const found = list.find((s) => s.title === "M1 synthetic " + owner + " " + kind);
        if (!found) throw Error("Owner session missing: " + kind);
        ids[kind] = found.id;
        ownSessions++;
        const messages = await get("/session/" + found.id + "/message", { directory });
        foreignMarkers += Number(JSON.stringify(messages).includes(foreign));
        if (
          !messages.some((m) =>
            m.parts.some((p) => p.text === "M1 synthetic " + owner + " " + kind + " prompt"),
          )
        )
          throw Error("Owner prompt missing");
        if (kind === "project")
          attachment = messages.some((m) =>
            m.parts.some((p) => p.type === "file" && p.filename === "notebook.txt"),
          );
      }
      return {
        same_project: folders.entries.some((p) => p.name === "same-project"),
        own_sessions: ownSessions,
        attachment,
        own_file: file.content === "M1 synthetic " + owner + " project file",
        foreign_markers: foreignMarkers,
        ids,
        pageContext: history.state.npPage,
      };
    },
    { owner, foreign, project, general },
  );
  const { ids, pageContext, ...observed } = result;
  assert.deepEqual(observed, {
    same_project: true,
    own_sessions: 2,
    attachment: true,
    own_file: true,
    foreign_markers: 0,
  });
  return { observed, ids, pageContext };
}
async function monitor(c, pageContext, markers = []) {
  // This browser document stays loaded after the UI disappears. EOF therefore measures
  // server-side stream termination independently of the tab unloading its EventSource.
  const probe = await c.newPage();
  await probe.goto(agent + "/_nexpoly/expired");
  let ended;
  const end = new Promise((resolve) => {
    ended = resolve;
  });
  await probe.exposeFunction("recordStreamEnd", () => ended(performance.now()));
  await probe.evaluate(
    async ({ key, markers }) => {
      const r = await fetch("/openscience-api/global/event", {
        headers: { "X-Nexpoly-Page": key },
        cache: "no-store",
      });
      if (r.status !== 200 || !r.headers.get("Content-Type")?.includes("text/event-stream"))
        throw Error("Authorized SSE did not open");
      const reader = r.body.getReader();
      if ((await reader.read()).done) throw Error("SSE ended before revocation");
      const decoder = new TextDecoder();
      window.seenMarkers = markers.map(() => false);
      void (async () => {
        let tail = "";
        try {
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            tail += decoder.decode(value, { stream: true });
            markers.forEach((m, i) => {
              if (tail.includes(m)) window.seenMarkers[i] = true;
            });
            tail = tail.slice(-160);
          }
        } catch {
        } finally {
          await window.recordStreamEnd();
        }
      })();
    },
    { key: pageContext, markers },
  );
  const status = () =>
    probe.evaluate(
      async (key) =>
        (await fetch("/_nexpoly/status", { headers: { "X-Nexpoly-Page": key }, cache: "no-store" })).status,
      pageContext,
    );
  const reconnect = () =>
    probe.evaluate(async (key) => {
      const r = await fetch("/openscience-api/global/event", {
        headers: { "X-Nexpoly-Page": key },
        signal: AbortSignal.timeout(5000),
      });
      await r.body?.cancel();
      return r.status;
    }, pageContext);
  return { probe, end, status, reconnect };
}
async function within(fn, description) {
  const deadline = performance.now() + 30000;
  while (performance.now() < deadline) {
    if (await fn()) return performance.now();
    await sleep(100);
  }
  throw Error(description + " exceeded 30 seconds");
}
async function losses(start, m, old, denial = [401, 403]) {
  const elapsed = (t) => Math.ceil(t - start);
  const [stream, denied, tab, sidebar] = await Promise.all([
    Promise.race([
      m.end,
      sleep(30000, undefined, { ref: false }).then(() => {
        throw Error("SSE remained open");
      }),
    ]),
    within(async () => denial.includes(await m.status()), "Old context"),
    within(async () => old.isDetached() || old.url().includes("/_nexpoly/expired"), "Old tab"),
    within(async () => {
      const text = await old.page().locator(".np-sidebar-desktop").innerText();
      return !text.includes("M1 synthetic ") && !text.includes("same-project");
    }, "Private sidebar"),
  ]);
  const result = {
    server_closed_sse_ms: elapsed(stream),
    private_page_denied_ms: elapsed(denied),
    old_document_invalid_ms: elapsed(tab),
    private_sidebar_cleared_ms: elapsed(sidebar),
  };
  assert.ok(Object.values(result).every((v) => v >= 0 && v <= 30000));
  result.stream_reconnect_status = await m.reconnect();
  assert.ok(denial.includes(result.stream_reconnect_status));
  return result;
}
async function eventMarkers(fA, fB, watchA, watchB, markers) {
  const created = [];
  try {
    for (const [i, frame] of [fA, fB].entries()) {
      const id = await frame.evaluate(
        async ({ title, general }) => {
          const r = await fetch("/openscience-api/session?" + new URLSearchParams({ directory: general }), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ title }),
          });
          if (!r.ok) throw Error("Could not create temporary event fixture: " + r.status);
          return (await r.json()).id;
        },
        { title: markers[i], general },
      );
      assert.match(id, /^ses_[a-zA-Z0-9_-]+$/);
      created.push({ frame, id });
    }
    for (const [i, watch] of [watchA, watchB].entries()) {
      await within(async () => watch.probe.evaluate((i) => window.seenMarkers[i], i), "Own live event");
    }
    for (const [i, watch] of [watchA, watchB].entries()) {
      assert.equal(await watch.probe.evaluate((i) => window.seenMarkers[1 - i], i), false);
    }
  } finally {
    for (const { frame, id } of created) {
      const status = await frame.evaluate(
        async ({ id, general }) =>
          (
            await fetch(
              "/openscience-api/session/" + id + "?" + new URLSearchParams({ directory: general }),
              { method: "DELETE" },
            )
          ).status,
        { id, general },
      );
      assert.equal(status, 200);
    }
  }
}
async function submitExchange(page, fields) {
  // A second real form submission uses the browser's original Origin and TLS policy.
  return page.evaluate(
    ({ action, fields }) =>
      new Promise((resolve) => {
        const target = document.createElement("iframe");
        target.name = "m2-replay";
        target.hidden = true;
        document.body.append(target);
        const timeout = setTimeout(() => {
          window.removeEventListener("message", receive);
          target.remove();
          resolve("timed_out");
        }, 10000);
        function receive(e) {
          if (e.origin !== new URL(action).origin || e.source !== target.contentWindow) return;
          clearTimeout(timeout);
          window.removeEventListener("message", receive);
          target.remove();
          resolve(e.data?.type);
        }
        window.addEventListener("message", receive);
        const form = document.createElement("form");
        form.method = "POST";
        form.target = target.name;
        form.action = action;
        for (const [name, value] of new URLSearchParams(fields)) {
          const input = document.createElement("input");
          input.type = "hidden";
          input.name = name;
          input.value = value;
          form.append(input);
        }
        document.body.append(form);
        form.submit();
        form.remove();
      }),
    { action: agent + "/_nexpoly/auth/exchange", fields },
  );
}
async function holdTicket(page) {
  return page.evaluate(async () => {
    const session = await (await fetch("/api/v1/auth/session")).json();
    const r = await fetch("/api/v1/openscience/workspace-ticket", {
      method: "POST",
      headers: { "X-Session-Context": session.session_id, "X-CSRF-Token": session.csrf_token },
    });
    if (!r.ok) throw Error("Expected a valid, unused workspace ticket");
    const data = await r.json();
    return {
      ttl: data.expires_in,
      fields: new URLSearchParams({
        ticket: data.ticket,
        handoff_id: data.handoff_id,
        browser_id: localStorage.getItem("nexpoly.auth.browser-id"),
      }).toString(),
    };
  });
}
async function probeForeign(f, id) {
  return f.evaluate(
    async ({ id, project }) =>
      (await fetch("/openscience-api/session/" + id + "?" + new URLSearchParams({ directory: project })))
        .status,
    { id, project },
  );
}
try {
  const device1 = await device(),
    device2 = await device();
  const a1 = await app(device1);
  let captured;
  a1.on("request", (r) => {
    if (!captured && r.url() === agent + "/_nexpoly/auth/exchange") captured = r.postData();
  });
  await login(a1, A);
  const f1 = await workbench(a1),
    r1 = await ownership(f1, A, B);
  const cookies = await device1.cookies();
  assert.ok(
    cookies.some(
      (c) => c.domain === "app.m2.test" && c.name === "__Host-nexpoly_m2_session" && c.secure && c.httpOnly,
    ),
  );
  assert.ok(
    cookies.some(
      (c) => c.domain === "agent.m2.test" && c.name.startsWith("__Host-np_os_m2_") && c.secure && c.httpOnly,
    ),
  );
  assert.ok(!cookies.some((c) => c.domain === ".m2.test"));
  assert.ok(captured);
  const replay = await submitExchange(a1, captured);
  captured = null;
  assert.equal(replay, "nexpoly:openscience:error");
  report.stages.initial = {
    real_ui: true,
    host_scoped_httponly_cookies: true,
    ticket_replay_refused: true,
    owner: r1.observed,
    visible: await checkVisibleWorkspace(a1, f1, A, B),
  };
  console.log("A rendered the retained conversations, project and attachment.");
  const a1old = await app(device1),
    old = await workbench(a1old),
    oldData = await ownership(old, A, B);
  const a2 = await app(device2);
  await login(a2, A);
  const f2 = await workbench(a2),
    r2 = await ownership(f2, A, B);
  assert.deepEqual(r1.ids, r2.ids);
  const m1 = await monitor(device1, oldData.pageContext);
  const start = performance.now();
  await logout(a1, A);
  const logoutLoss = await losses(start, m1, old);
  assert.deepEqual((await ownership(f2, A, B)).ids, r1.ids);
  report.stages.device_logout = { ...logoutLoss, other_device_still_reads: true };
  await login(a1, B);
  const fb = await workbench(a1),
    rb = await ownership(fb, B, A);
  const held = await holdTicket(a1),
    heldSince = performance.now();
  assert.equal(held.ttl, 60);
  assert.notEqual(r1.ids.project, rb.ids.project);
  const foreignStatus = [await probeForeign(f2, rb.ids.project), await probeForeign(fb, r1.ids.project)];
  assert.deepEqual(foreignStatus, [404, 404]);
  assert.ok([401, 403].includes(await m1.status()));
  report.stages.account_switch = {
    owner: rb.observed,
    foreign_session_status: foreignStatus,
    old_page_still_denied: true,
    visible: await checkVisibleWorkspace(a1, fb, B, A),
  };
  console.log("B recovered the B workspace; A on device two retained the A workspace.");
  // Refresh the embedded SPA itself after project navigation, then use its
  // message bridge again before refreshing the entire parent page.
  await Promise.all([
    fb.waitForNavigation({ waitUntil: "domcontentloaded" }),
    fb.evaluate(() => location.reload()),
  ]);
  const reloadedFrame = await workbench(a1);
  const embedded = await ownership(reloadedFrame, B, A);
  assert.deepEqual(embedded.ids, rb.ids);
  await checkVisibleWorkspace(a1, reloadedFrame, B, A);
  report.stages.account_switch.embedded_refresh_restored_bridge = true;
  await a1.reload({ waitUntil: "domcontentloaded" });
  const refreshedB = await workbench(a1),
    afterRefresh = await ownership(refreshedB, B, A);
  assert.deepEqual(afterRefresh.ids, rb.ids);
  report.stages.account_switch.refresh_restored_own = true;
  const oldTab = await app(device2),
    fOld = await workbench(oldTab),
    old2 = await ownership(fOld, A, B);
  const markers = [A, B].map((u) => "M2 event " + u + " " + randomUUID());
  const m2 = await monitor(device2, old2.pageContext, markers);
  const mB = await monitor(device1, afterRefresh.pageContext, markers);
  await eventMarkers(fOld, refreshedB, m2, mB, markers);
  report.stages.event_visibility = {
    a_received_own: true,
    b_received_own: true,
    foreign_markers: 0,
    temporary_sessions_removed: 2,
  };
  await oldTab.getByRole("button", { name: "M1 synthetic " + A + " general", exact: true }).waitFor();
  const revokeStart = performance.now();
  setPilot(false);
  restorePilot = true;
  const revoked = await losses(revokeStart, m2, fOld);
  assert.deepEqual((await ownership(await workbench(a1), B, A)).ids, rb.ids);
  setPilot(true);
  restorePilot = false;
  assert.ok([401, 403].includes(await m2.status()));
  report.stages.pilot_revocation = {
    ...revoked,
    parent_snapshot_cleared: true,
    other_user_still_reads: true,
    old_context_denied_after_reenable: true,
  };
  const outageStart = performance.now();
  pauseAuthority(true);
  pausedAuthority = true;
  const outage = await losses(outageStart, mB, refreshedB, [401, 403, 503]);
  pauseAuthority(false);
  pausedAuthority = false;
  report.stages.authority_unavailable = outage;
  await sleep(Math.max(0, 61000 - (performance.now() - heldSince)));
  assert.equal(await submitExchange(a1, held.fields), "nexpoly:openscience:error");
  const fresh = await holdTicket(a1);
  assert.equal(await submitExchange(a1, fresh.fields), "nexpoly:openscience:ready");
  report.stages.ticket_expiration = {
    ttl_seconds: 60,
    unused_expired_ticket_refused: true,
    same_login_fresh_ticket_accepted: true,
  };
  report.status = "passed";
} catch (error) {
  console.error(String(error).replace(/np_page=[^ &"]+/g, "np_page=[redacted]"));
  process.exitCode = 1;
} finally {
  if (pausedAuthority) pauseAuthority(false);
  if (restorePilot) setPilot(true);
  for (const p of authorizedContexts.reverse()) {
    const menu = p.locator(".np-sidebar-desktop").getByRole("button", { name: /^账号菜单：/ });
    if (await menu.count().catch(() => 0))
      await menu
        .click({ timeout: 1500 })
        .then(() => p.getByRole("menuitem", { name: "退出登录", exact: true }).click({ timeout: 1500 }))
        .catch(() => {});
  }
  await browser.close();
  report.end = new Date().toISOString();
  await writeFile(output, JSON.stringify(report, null, 2) + "\n", { flag: "wx", mode: 0o600 });
  console.log("M2 browser verification", report.status);
}
