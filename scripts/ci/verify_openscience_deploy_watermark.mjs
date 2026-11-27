// Browser half of the threshold-only deploy-m6 disk drill. The operator controls the threshold and A's recycle.
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { lstat, readFile, readdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { developmentBrowser, enrolledMembers, retainedBindings } from "./openscience_dev_browser.mjs";

const [state, work] = process.argv.slice(2);
const json = async path => JSON.parse(await readFile(path, "utf8"));
const stack = await json(resolve(state, "running.json"));
assert.equal(stack.environment, "deploy-m6");
assert.equal(stack.quota_files, true);
const registry = await json(resolve(stack.registry, "registry.json"));
retainedBindings(stack, registry);
assert.equal(registry.format, 5);
assert.equal(registry.native_claim.phase, "src");
const members = enrolledMembers(stack);
const privateDir = await lstat(work);
assert.ok(privateDir.isDirectory() && privateDir.uid === process.getuid() && (privateDir.mode & 0o077) === 0);
assert.deepEqual(await readdir(work), [], "Use a fresh private coordination directory");
const parent = "/openscience/files/projects";
const project = "watermark-" + randomBytes(6).toString("hex");
const directory = parent + "/" + project;
const keep = "keep.txt", release = "release.txt", fresh = "after.txt";
const sha = value => createHash("sha256").update(value).digest("hex");
const content = (role, file) => [project, role, file].join(" ");
const suite = await developmentBrowser(state, stack);
const report = { environment: stack.environment, browser: suite.browser.version(), project,
  started_at: new Date().toISOString(), status: "failed", before: {}, low: {}, recovered: {} };

async function exists(name) {
  try { await lstat(resolve(work, name)); return true; }
  catch (error) { if (error.code === "ENOENT") return false; throw error; }
}
async function signal(name) {
  await writeFile(resolve(work, name), new Date().toISOString() + "\n", { flag: "wx", mode: 0o600 });
  console.log(name);
}
async function wait(name) {
  const deadline = Date.now() + 15 * 60_000;
  while (Date.now() < deadline) {
    if (!suite.browser.isConnected() || await exists("abort")) throw Error("Coordination interrupted");
    if (await exists(name)) return;
    await sleep(500);
  }
  throw Error("Timed out waiting for " + name);
}
async function login(role) {
  const member = members.find(m => m.user_id === registry.candidate_owners[role].user_id);
  assert.ok(member);
  const ctx = await suite.device();
  ctx.setDefaultTimeout(60000);
  const page = await suite.app(ctx);
  const identity = await suite.login(page, member.username, role);
  assert.equal(identity.user_id, member.user_id);
  return { role, member, ctx, page };
}
async function native(frame, path, { query = { directory }, method = "GET", body, status = 200 } = {}) {
  const result = await frame.evaluate(async ({ path, query, method, body }) => {
    const reply = await fetch("/openscience-api" + path + "?" + new URLSearchParams(query), {
      method, signal: AbortSignal.timeout(60000),
      ...(body && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    return { status: reply.status, data: await reply.json().catch(() => null) };
  }, { path, query, method, body });
  assert.equal(result.status, status, method + " " + path + " expected " + status + " got " + result.status);
  return result.data;
}
async function readOwn(frame, role, file) {
  const data = await native(frame, "/file/content", { query: { directory, path: file } });
  assert.equal(data.content, content(role, file));
  return sha(data.content);
}
const list = async frame => (await native(frame, "/file", { query: { directory, path: "" } })).map(row => row.name);
const save = (frame, role, file, status = 200) => native(frame, "/file/content", { method: "PUT",
  body: { path: file, content: content(role, file) }, status });
async function onlySession(frame, actor) {
  const sessions = await native(frame, "/session");
  assert.deepEqual(sessions.map(s => s.id), [actor.session]);
  return sha(actor.session);
}
async function seed(role) {
  const actor = await login(role);
  const frame = await suite.workbench(actor.page);
  const folder = await native(frame, "/api/resolve-folder/create", { query: {}, method: "POST",
    body: { parent, name: project }, status: 201 });
  assert.equal(folder.absolute, directory);
  for (const file of [keep, release]) {
    await save(frame, role, file);
    await readOwn(frame, role, file);
  }
  const session = await native(frame, "/session", { method: "POST", body: { title: project + "-" + role } });
  assert.match(session.id, /^ses_[A-Za-z0-9_-]+$/);
  Object.assign(actor, { frame, session: session.id });
  report.before[role] = { session_sha256: await onlySession(frame, actor), keep_sha256: await readOwn(frame, role, keep),
    release_sha256: await readOwn(frame, role, release) };
  return actor;
}
async function coldRead(frame, file) {
  const [response] = await Promise.all([
    frame.page().waitForResponse(r => r.url() === stack.agent + "/api/workspace-cleanup/read"),
    frame.getByRole("button", { name: file, exact: true }).click(),
  ]);
  assert.equal(response.status(), 200);
  const data = await response.json();
  assert.equal(Buffer.from(data.content, "base64").toString(), content("a", file));
  await frame.getByText(project + "/" + file, { exact: true }).waitFor();
  assert.equal(await frame.locator("#content").textContent(), content("a", file));
  return sha(content("a", file));
}
async function coldChecks(actor) {
  const limited = actor.page.getByRole("alert").filter({ hasText: "工作区容量已满" });
  await limited.getByRole("button", { name: "清理我的项目文件" }).click({ timeout: 120000 });
  const frame = actor.page.frameLocator('iframe[title="我的项目文件"]');
  await frame.getByRole("button", { name: project, exact: true }).click({ timeout: 60000 });
  const cleanup = await (await actor.page.locator('iframe[title="我的项目文件"]').elementHandle()).contentFrame();
  const denied = await native(cleanup, "/session", { status: 403 });
  assert.equal(denied.error, "route_disabled");
  const keepHash = await coldRead(cleanup, keep);
  await cleanup.getByRole("button", { name: "返回目录", exact: true }).click();
  const releaseHash = await coldRead(cleanup, release);
  await cleanup.getByRole("button", { name: "删除此文件", exact: true }).click();
  const dialog = cleanup.getByRole("dialog", { name: "确认删除文件" });
  assert.ok((await dialog.innerText()).includes(release));
  const [response] = await Promise.all([
    actor.page.waitForResponse(r => r.url() === stack.agent + "/api/workspace-cleanup/delete"),
    dialog.getByRole("button", { name: "确认删除", exact: true }).click(),
  ]);
  assert.equal(response.status(), 200);
  assert.deepEqual(await response.json(), { deleted: true });
  await cleanup.getByText(/文件已删除/).waitFor();
  return { cold_handoff: "cleanup_only", native_sessions_http: 403,
    keep_sha256: keepHash, release_sha256: releaseHash, confirmed_delete_http: 200 };
}
async function warmChecks(actor) {
  const { frame, role } = actor;
  const denial = await save(frame, role, fresh, 507);
  assert.deepEqual(denial, { error: "workspace_disk_low_watermark" });
  assert.ok(!(await list(frame)).includes(fresh));
  const keepHash = await readOwn(frame, role, keep);
  const releaseHash = await readOwn(frame, role, release);
  const deleted = await native(frame, "/file/content", { method: "DELETE", body: { path: release } });
  assert.equal(deleted.deleted, true);
  const files = await list(frame);
  assert.ok(files.includes(keep) && !files.includes(release) && !files.includes(fresh));
  return { create_http: 507, error: denial.error, rejected_file_absent: true,
    keep_sha256: keepHash, release_sha256: releaseHash, delete_http: 200, deleted_file_absent: true };
}
try {
  report.phase = "seed-a";
  const seededA = await seed("a");
  await suite.logout(seededA.page, seededA.member.username);
  await seededA.ctx.close();
  report.phase = "seed-b";
  const b = await seed("b");
  assert.notEqual(seededA.session, b.session);
  report.phase = "await-low";
  await signal("prepared");
  await wait("low"); // A must already be recycled; B keeps its original browser session and live instance.
  report.phase = "low-cold-a";
  const a = await login("a");
  a.session = seededA.session;
  report.low.a = await coldChecks(a);
  report.phase = "low-warm-b";
  report.low.b = await warmChecks(b);
  for (const role of ["a", "b"]) {
    assert.equal(report.low[role].keep_sha256, report.before[role].keep_sha256);
    assert.equal(report.low[role].release_sha256, report.before[role].release_sha256);
  }
  report.phase = "await-normal";
  await signal("low-verified");
  await wait("normal");
  report.phase = "recover-a";
  await a.page.getByRole("button", { name: "重试", exact: true }).click();
  a.frame = await suite.workbench(a.page);
  for (const actor of [a, b]) {
    const { frame, role } = actor;
    report.phase = "recover-" + role;
    const files = await list(frame);
    assert.ok(files.includes(keep) && !files.includes(release) && !files.includes(fresh));
    const keepHash = await readOwn(frame, role, keep);
    const sessionHash = await onlySession(frame, actor);
    assert.equal(keepHash, report.before[role].keep_sha256);
    assert.equal(sessionHash, report.before[role].session_sha256);
    await save(frame, role, fresh);
    report.recovered[role] = { session_sha256: sessionHash, keep_sha256: keepHash, deleted_file_absent: true,
      new_write_http: 200, new_file_sha256: await readOwn(frame, role, fresh) };
  }
  assert.notEqual(report.recovered.a.new_file_sha256, report.recovered.b.new_file_sha256);
  report.phase = "logout";
  for (const actor of [a, b]) await suite.logout(actor.page, actor.member.username);
  report.status = "passed";
  report.phase = "completed";
} catch (error) {
  // Browser errors can include page grants and session IDs. Preserve only the step and code location.
  report.error = error.name;
  const where = error.stack?.match(/verify_openscience_deploy_watermark\.mjs:\d+:\d+/)?.[0];
  const http = /^(GET|PUT|POST|DELETE) \/[a-z/-]+ expected \d+ got \d+$/.test(error.message);
  console.error(report.phase, report.error, where || "", http ? error.message : "");
  process.exitCode = 1;
} finally {
  report.finished_at = new Date().toISOString();
  await writeFile(resolve(work, "browser.json"), JSON.stringify(report, null, 2) + "\n", { flag: "wx", mode: 0o600 });
  await suite.browser.close();
  await signal(report.status === "passed" ? "normal-verified" : "failed");
}
