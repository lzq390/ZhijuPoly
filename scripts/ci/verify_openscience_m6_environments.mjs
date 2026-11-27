// AC-22 uses one Chromium profile per member, with that member logged in to both dedicated sites.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import { developmentBrowser, retainedBindings } from "./openscience_dev_browser.mjs";
import { holdTicket, losses, monitor, submitExchange, within } from "./openscience_browser_revocation.mjs";

const { values } = parseArgs({ options: { work: { type: "string" }, fixture: { type: "string" } } });
const states = ["nexpoly-openscience-m2-dev", "nexpoly-m6-ac22/site"].map((s) =>
  resolve(homedir(), ".local/state", s),
);
const json = async (p) => JSON.parse(await readFile(p, "utf8"));
const stacks = await Promise.all(states.map((s) => json(resolve(s, "running.json"))));
assert.deepEqual(
  stacks.map((s) => s.environment),
  ["dev-m1", "dev-m6"],
);
const previous = values.fixture ? await json(resolve(values.fixture)) : null;
const project = previous ? previous.project : "ac22-" + randomUUID().slice(0, 12);
assert.match(project, /^ac22-[a-f0-9]{8}-[a-f0-9]{3}$/);
const general = "/openscience/files/general",
  parentDirectory = "/openscience/files/projects";
const directory = parentDirectory + "/" + project;
const sha = (data) => createHash("sha256").update(data).digest("hex");
const readOnlyFile = "notebook.txt";
const gateTool = fileURLToPath(new URL("./verify_openscience_m6_environments.py", import.meta.url));
const suite = await developmentBrowser(states[0], stacks[0], [{ state: states[1], stack: stacks[1] }]);
const actors = [],
  profiles = [],
  destinations = new Set(),
  sites = stacks.map((stack, i) => ({ ...stack, controls: suite.sites[i] }));
const report = {
  status: "failed",
  project,
  fixture: previous ? "reopened" : "created",
  started: new Date().toISOString(),
  owners: {},
  tickets: [],
  decisions: [],
};

function gate(site, action) {
  return JSON.parse(
    execFileSync("python3", [gateTool, "gate", site.environment, "--deployment", site.deployment, action], {
      encoding: "utf8",
      timeout: 30000,
    }),
  );
}
async function api(actor, path, { method = "GET", body, query } = {}) {
  return actor.frame.evaluate(
    async ({ path, method, body, query }) => {
      const url = new URL("/openscience-api" + path, location.origin);
      url.search = new URLSearchParams(query);
      const res = await fetch(url, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      const text = await res.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {}
      return { status: res.status, data };
    },
    { path, method, body, query },
  );
}
async function ok(actor, path, options) {
  const r = await api(actor, path, options);
  assert.ok(
    [200, 201, 204].includes(r.status),
    actor.name + " " + path.split("/").slice(0, 2).join("/") + " returned " + r.status,
  );
  return r.data;
}
async function file(actor, name, content) {
  if (content !== undefined)
    await ok(actor, "/file/content", { method: "PUT", query: { directory }, body: { path: name, content } });
  return (await ok(actor, "/file/content", { query: { directory, path: name } })).content;
}
async function current(actor) {
  actor.frame = await actor.site.controls.workbench(actor.page);
  actor.key = await actor.frame.evaluate(() => history.state.npPage);
  assert.ok(actor.key);
}
async function createSession(actor, dir, title) {
  const session = await ok(actor, "/session", { method: "POST", query: { directory: dir }, body: { title } });
  assert.match(session.id, /^ses_[a-zA-Z0-9_-]+$/);
  return session.id;
}
async function seed(actor) {
  const created = await ok(actor, "/api/resolve-folder/create", {
    method: "POST",
    body: { parent: parentDirectory, name: project },
  });
  assert.equal(created.ok, true);
  assert.equal(created.absolute, directory);
  assert.equal(await file(actor, readOnlyFile, actor.marker), actor.marker);
  assert.equal(await file(actor, actor.privateFile, actor.marker), actor.marker);
  actor.session = await createSession(actor, directory, actor.title);
  const reply = await ok(actor, "/session/" + actor.session + "/message", {
    method: "POST",
    query: { directory },
    body: {
      model: { providerID: "m0fixture", modelID: "m0" },
      parts: [
        { type: "text", text: actor.marker },
        {
          type: "file",
          mime: "text/plain",
          filename: readOnlyFile,
          url: "file://" + directory + "/" + readOnlyFile,
        },
      ],
    },
  });
  assert.ok(
    reply.parts.some((p) => p.text === "M0-SYNTHETIC-OK"),
    actor.name + " fixture reply missing",
  );
  await createSession(actor, general, actor.sidebar);
}
async function rowsVisible(actor) {
  return actor.page.locator(".np-sidebar-desktop").innerText();
}
async function data(actor) {
  const folders = await ok(actor, "/api/resolve-folder/list");
  assert.equal(folders.entries.filter((e) => e.name === project).length, 1);
  const sessions = await ok(actor, "/session", { query: { directory } });
  assert.deepEqual(
    sessions.filter((s) => s.title.startsWith(project)).map((s) => s.id),
    [actor.session],
  );
  const history = await ok(actor, "/session/" + actor.session + "/message", { query: { directory } });
  const parts = history.flatMap((m) => m.parts);
  assert.ok(
    parts.some((p) => p.text === actor.marker) &&
      parts.some((p) => p.text === "M0-SYNTHETIC-OK") &&
      parts.some((p) => p.type === "file" && p.filename === readOnlyFile),
  );
  for (const other of actors.filter((r) => r !== actor))
    assert.ok(!JSON.stringify(history).includes(other.marker));
  assert.equal(await file(actor, readOnlyFile), actor.marker);
  const files = await ok(actor, "/file", { query: { directory, path: "" } });
  assert.ok(files.some((f) => f.name === actor.privateFile));
  const missing = await api(actor, "/file/content", { query: { directory, path: "absent-" + project + ".txt" } });
  assert.equal(missing.status, 200);
  assert.deepEqual(missing.data, { type: "text", content: "" });
  const deny = [];
  for (const other of actors.filter((r) => r !== actor)) {
    assert.ok(!files.some((f) => f.name === other.privateFile));
    const byName = await api(actor, "/file/content", { query: { directory, path: other.privateFile } });
    assert.deepEqual(byName, missing, "Another workspace's private file was readable");
    const foreign = await api(actor, "/session/" + other.session, { query: { directory } });
    assert.equal(foreign.status, 404, "Foreign session detail visible from " + actor.name);
    deny.push(foreign.status);
  }
  return {
    notebook_sha256: sha(actor.marker),
    project_session_sha256: sha(actor.session),
    foreign_detail_http: deny,
    foreign_marker_files_listed: 0,
    foreign_file_reads_matching_missing_file: deny.length,
    own_prompt_reply_attachment: true,
  };
}
async function ticketStatus(actor) {
  return actor.page.evaluate(async () => {
    const session = await (await fetch("/api/v1/auth/session")).json();
    if (!session.session_id || !session.capabilities?.["openscience.use"])
      throw Error("Expected a logged-in member");
    const r = await fetch("/api/v1/openscience/workspace-ticket", {
      method: "POST",
      headers: { "X-Session-Context": session.session_id, "X-CSRF-Token": session.csrf_token },
    });
    return r.status;
  });
}
async function tickets(source, target) {
  const started = performance.now();
  const t = await holdTicket(source.page);
  // The target's own authenticated parent submits the ticket at its normal exchange endpoint.
  // The same still-unused ticket must then be accepted by its issuing site.
  const foreign = await submitExchange(target.site.agent, target.page, t.fields, true);
  assert.deepEqual(foreign, { type: "nexpoly:openscience:error", reason: "session_expired" });
  assert.equal(await submitExchange(source.site.agent, source.page, t.fields), "nexpoly:openscience:ready");
  const age = Math.ceil(performance.now() - started);
  assert.ok(age < t.ttl * 1000);
  report.tickets.push({
    user: source.user,
    from: source.site.environment,
    to: target.site.environment,
    receiving_authority: "denied",
    issuing_site: "accepted_same_ticket",
    age_upper_bound_ms: age,
  });
}
async function control(site, kind) {
  const affected = actors.filter((a) => a.site === site && (kind === "close" || a.user === "test-user-a"));
  const survivors = actors.filter((a) => !affected.includes(a));
  const labels = actors.map((a) => a.name + "-event-" + randomUUID());
  const watches = new Map();
  try {
    await Promise.all(
      actors.map(async (a) => {
        await a.page
          .locator(".np-sidebar-desktop")
          .getByRole("button", { name: a.sidebar, exact: true })
          .waitFor({ timeout: 40000 });
        const watch = await monitor(a.site.agent, a.ctx, a.key, labels);
        watch.ended = false;
        watch.end.then(() => {
          watch.ended = true;
        });
        watches.set(a, watch);
        assert.equal(await watch.status(), 200);
      }),
    );
    const start = performance.now();
    const switched = gate(site, kind);
    if (kind === "close") assert.equal(switched.enabled, false);
    else assert.deepEqual(switched, { enabled: true, members: { "test-user-a": false, "test-user-b": true } });
    const retired = {},
      continuous = {};
    await Promise.all([
      ...affected.map(async (a) => {
        retired[a.name] = await losses(start, watches.get(a), a.frame, [401, 403], [a.sidebar]);
        assert.equal(await ticketStatus(a), 403);
      }),
      ...survivors.map(async (a) => {
        const watch = watches.get(a),
          i = actors.indexOf(a);
        assert.equal(await file(a, readOnlyFile), a.marker);
        assert.equal(await file(a, "continuity.txt", labels[i]), labels[i]);
        const event = await createSession(a, general, labels[i]);
        await within(
          () => watch.probe.evaluate((i) => window.seenMarkers[i], i),
          "Original survivor SSE delivered its event",
        );
        assert.equal(watch.ended, false);
        assert.equal(await watch.status(), 200);
        assert.equal(await a.frame.evaluate(() => history.state.npPage), a.key);
        assert.equal(await ticketStatus(a), 200);
        await ok(a, "/session/" + event, { method: "DELETE", query: { directory: general } });
        continuous[a.name] = {
          original_page: 200,
          new_ticket: 200,
          original_stream_delivered: true,
          private_file_read_write: true,
        };
      }),
    ]);
    for (const a of survivors) {
      const hits = await watches.get(a).probe.evaluate(() => window.seenMarkers);
      assert.deepEqual(
        hits,
        actors.map((x) => x === a),
      );
      assert.equal(watches.get(a).ended, false);
    }
    const restored = gate(site, kind === "close" ? "open" : "restore-a");
    assert.ok(restored.enabled && Object.values(restored.members).every(Boolean));
    for (const a of affected) {
      assert.ok([401, 403].includes(await watches.get(a).status()));
      await a.page.reload({ waitUntil: "domcontentloaded" });
      await current(a);
      assert.equal(await file(a, readOnlyFile), a.marker);
    }
    report.decisions.push({
      environment: site.environment,
      action: kind,
      revoked: retired,
      unaffected: continuous,
      old_context_denied_after_restore: true,
      fresh_handoff_recovered_data: true,
    });
    console.log(site.environment, kind, "revoked", affected.length, "continued", survivors.length);
  } finally {
    await Promise.all(Array.from(watches.values(), (w) => w.probe.close()));
  }
}
try {
  for (const [i, user] of ["test-user-a", "test-user-b"].entries()) {
    const ctx = await suite.device();
    profiles.push(ctx);
    ctx.on("request", (r) => {
      const u = new URL(r.url());
      if (["http:", "https:"].includes(u.protocol)) destinations.add(u.origin);
    });
    for (const site of sites) {
      const page = await site.controls.app(ctx),
        identity = await site.controls.login(page, user);
      assert.equal(identity.user_id, "10000000-0000-4000-8000-00000000000" + (i + 1));
      const name = site.environment + "-" + user;
      actors.push({
        site,
        user,
        name,
        ctx,
        page,
        identity,
        marker: project + " marker " + name,
        privateFile: "only-" + name + ".txt",
        title: project + " chat " + name,
        sidebar: project + " general " + name,
      });
    }
  }
  await Promise.all(actors.map(current));
  assert.equal(new Set(actors.map((a) => a.identity.session_id)).size, 4);
  const parents = sites.map((site) => site.parent);
  for (const ctx of profiles) {
    const logins = (await ctx.cookies(parents)).filter((cookie) => cookie.name === "__Host-nexpoly_m2_session");
    assert.deepEqual(logins.map((c) => c.domain).sort(), parents.map((p) => new URL(p).hostname).sort());
    assert.ok(logins.every((c) => c.httpOnly && c.secure && c.path === "/"));
    assert.equal(new Set(logins.map((c) => c.value)).size, 2);
  }
  report.separate_host_only_logins = true;
  for (const site of sites) retainedBindings(site, await json(resolve(site.registry, "registry.json")));
  console.log("Four logged-in candidate workspaces ready");
  if (previous) {
    assert.equal(Object.keys(previous.owners).length, 4);
    for (const a of actors) {
      const sessions = await ok(a, "/session", { query: { directory } });
      const matches = sessions.filter((s) => s.title === a.title);
      assert.equal(matches.length, 1);
      a.session = matches[0].id;
      assert.equal(sha(a.session), previous.owners[a.name].project_session_sha256);
      assert.equal(sha(await file(a, readOnlyFile)), previous.owners[a.name].notebook_sha256);
    }
  } else {
    await Promise.all(actors.map(seed));
  }
  for (const a of actors) {
    // Refresh the real November workbench and its parent sidebar from persisted data.
    await a.page.reload({ waitUntil: "domcontentloaded" });
    await current(a);
    await a.page
      .locator(".np-sidebar-desktop")
      .getByRole("button", { name: a.sidebar, exact: true })
      .waitFor({ timeout: 40000 });
    report.owners[a.name] = await data(a);
    const text = await rowsVisible(a);
    assert.ok(actors.filter((r) => r !== a).every((r) => !text.includes(r.sidebar)));
  }
  console.log("Four same-name projects, files, chats, replies and attachments verified");
  for (const a of actors)
    await tickets(
      a,
      actors.find((b) => b.site !== a.site && b.user === a.user),
    );
  for (const kind of ["remove-a", "close"]) for (const site of sites) await control(site, kind);
  for (const a of actors) assert.deepEqual(await data(a), report.owners[a.name]);
  for (const a of actors) await a.site.controls.logout(a.page, a.user);
  assert.deepEqual([...destinations].sort(), sites.flatMap((s) => [s.parent, s.agent]).sort());
  report.http_origins = [...destinations].sort();
  report.status = "passed";
} catch (e) {
  // No credential, raw response or URL query belongs in the acceptance report.
  console.error("AC-22:", String(e).replace(/np_page=[^ &"]+/g, "np_page=[redacted]"));
  process.exitCode = 1;
} finally {
  if (report.status !== "passed")
    await Promise.allSettled(actors.map((a) => a.site.controls.logout(a.page, a.user)));
  await suite.browser.close();
  report.finished = new Date().toISOString();
  await writeFile(resolve(values.work, "browser.json"), JSON.stringify(report, null, 2) + "\n", {
    flag: "wx",
    mode: 0o600,
  });
}
