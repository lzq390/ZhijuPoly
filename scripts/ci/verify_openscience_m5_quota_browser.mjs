// Actual NexPoly form login, project tree, November editor and kernel quota.
// The host coordinator changes only the isolated quota volume or the global test pool.
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { lstat, open, readFile, realpath, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { developmentBrowser } from "./openscience_dev_browser.mjs";

function option(name) {
  const i = process.argv.indexOf(name);
  if (i === -1 || !process.argv[i + 1]) throw Error("Missing " + name);
  return process.argv[i + 1];
}
const state = resolve(option("--state"));
const phase = option("--phase");
const stack = JSON.parse(await readFile(resolve(state, "running.json"), "utf8"));
assert.equal(stack.quota_files, true, "Only a quota-backed clone can run the browser drill");
assert.ok(["full", "readback", "watermark"].includes(phase));
const markers = resolve(option("--markers"));
const dir = await lstat(markers);
assert.equal(await realpath(markers), markers, "Marker directory and its parents must be real directories");
assert.ok(dir.isDirectory() && dir.uid === process.getuid() && !(dir.mode & 0o077) && markers !== await realpath(state),
  "Use a dedicated private acceptance directory for drill markers");
const ready = resolve(markers, "quota-browser-ready");
const full = resolve(markers, "quota-browser-full");
async function exists(path) {
  try {
    const info = await lstat(path);
    assert.ok(info.isFile() && info.nlink === 1 && info.uid === process.getuid(), "Invalid acceptance marker");
    return true;
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    return false;
  }
}
if (phase === "full" && ((await exists(ready)) || (await exists(full)))) throw Error("Stale quota drill markers");

const project = "/openscience/files/projects/same-project";
const notebook = "notebook.txt";
const release = phase === "watermark" ? "m5-watermark-release.txt" : "m5-release.txt";
// The dedicated PostgreSQL seed verifies these existing account IDs on each run.
const owners = {
  "test-user-a": "10000000-0000-4000-8000-000000000001",
  "test-user-b": "10000000-0000-4000-8000-000000000002",
};
const users = Object.keys(owners);
const original = user => "M1 synthetic " + user + " project file";
const updated = user => original(user) + "\nM5 web quota " + user + "\n";
const sha = text => createHash("sha256").update(text).digest("hex");
const report = {phase, status: "failed", started: new Date().toISOString(), stages: {}};
const output = await open(resolve(option("--output")), "wx", 0o600);
let browser, activePage, signingOut = false;
const signOut = [];
function stage(label) {
  report.step = label;
  console.error("[M5]", label);
}

// Match the response to the very action clicked, including its selected file.
function operation(frame, method, name) {
  return frame.page().waitForResponse(response => {
    const req = response.request(), url = new URL(response.url());
    if (req.method() !== method || url.origin !== stack.agent || !url.pathname.endsWith("/file/content")) return false;
    const path = method === "GET" ? url.searchParams.get("path") : req.postDataJSON()?.path;
    const dir = url.searchParams.get("directory") || req.headers()["x-openscience-directory"] || "";
    return (dir === project || decodeURIComponent(dir) === project) && (path === name || path === project + "/" + name);
  }, {timeout: 90000});
}
async function readVisible(frame, expected) {
  const [reply] = await Promise.all([
    operation(frame, "GET", notebook),
    frame.getByRole("button", {name: "删除文件", exact: true}).locator("..").locator('button[title="refresh"]').click(),
  ]);
  assert.equal(reply.status(), 200);
  assert.equal((await reply.json()).content, expected);
  const editor = frame.locator("textarea.atlas-scroll:visible");
  if (!(await editor.count())) await frame.locator('button[title="edit source"]:visible').click();
  await frame.waitForFunction(value => [...document.querySelectorAll("textarea.atlas-scroll")]
    .some(node => node.getClientRects().length && node.value === value), expected, {timeout: 15000});
}
async function projectFileList(frame) {
  const pathBar = frame.locator('input[placeholder="/absolute/path"]:visible');
  if (!(await pathBar.isVisible())) await frame.getByText(/^(Files|文件)$/).filter({visible: true}).first().click();
  await pathBar.waitFor();
  return pathBar.locator("../../..");
}
async function selectProjectFile(frame, panel, name) {
  await panel.getByText(name, {exact: true}).click({timeout: 90000});
  const toolbar = frame.getByRole("button", {name: "删除文件", exact: true}).locator("..");
  await toolbar.getByText(name, {exact: true}).waitFor();
}
async function openProjectFile(frame, name) {
  const panel = await projectFileList(frame);
  if (!(await panel.getByText(name, {exact: true}).isVisible()))
    await panel.locator('button[title="refresh"]').click();
  await selectProjectFile(frame, panel, name);
}
async function refreshProjectFileList(frame, panel) {
  const [response] = await Promise.all([
    frame.page().waitForResponse(r => {
      const url = new URL(r.url()), req = r.request();
      const dir = url.searchParams.get("directory") || req.headers()["x-openscience-directory"] || "";
      return req.method() === "GET" && url.origin === stack.agent && url.pathname === "/openscience-api/file"
        && (dir === project || decodeURIComponent(dir) === project);
    }, {timeout: 90000}),
    panel.locator('button[title="refresh"]').click(),
  ]);
  assert.equal(response.status(), 200);
}
async function save(frame, value) {
  const editor = frame.locator("textarea.atlas-scroll:visible");
  if (!(await editor.count())) await frame.locator('button[title="edit source"]:visible').click();
  await editor.fill(value);
  const [reply] = await Promise.all([
    operation(frame, "PUT", notebook),
    frame.getByRole("button", {name: "save", exact: true}).click(),
  ]);
  return {status: reply.status(), body: await reply.json()};
}

async function removeFile(frame, release) {
  await openProjectFile(frame, release);
  const button = frame.getByRole("button", {name: "删除文件", exact: true});
  await button.click();
  const dialog = frame.getByRole("dialog", {name: "删除文件"});
  assert.ok((await dialog.innerText()).includes(release));
  const [deleted] = await Promise.all([
    operation(frame, "DELETE", release), dialog.getByRole("button", {name: "确认删除"}).click(),
  ]);
  assert.equal(deleted.status(), 200);
  // The editor consumes the authenticated receipt before dismissing its
  // dialog. Verify the persisted effect from a new, visible file listing.
  stage("A checks the deletion receipt and refreshed project file list");
  await dialog.waitFor({state: "detached"});
  const files = await projectFileList(frame);
  await refreshProjectFileList(frame, files);
  await files.getByText(release, {exact: true}).waitFor({state: "detached"});
  await selectProjectFile(frame, files, notebook);
  return deleted;
}

async function verifyWatermark(signIn, openNotebook) {
  const previous = JSON.parse(await readFile(new URL("../../docs/verification/openscience-m5-workspace-quota-20261123.json", import.meta.url), "utf8"));
  assert.equal(previous.status, "passed");
  for (const user of users) assert.equal(sha(updated(user)), previous.browser.web_after_restore.stages[user].notebook_sha256);
  const readyFlag = "watermark-browser-ready", lowFlag = "watermark-low";
  const deniedFlag = "watermark-browser-denied", beforeDeleteFlag = "watermark-before-delete";
  const deletedFlag = "watermark-browser-deleted", recoveredFlag = "watermark-recovered";
  for (const name of [readyFlag, lowFlag, deniedFlag, beforeDeleteFlag, deletedFlag, recoveredFlag]) {
    if (await exists(resolve(markers, name))) throw Error("Stale watermark drill markers");
  }
  const signal = name => writeFile(resolve(markers, name), name + "\n", {flag:"wx", mode:0o600});
  async function waitFor(name) {
    const deadline = Date.now() + 10 * 60 * 1000;
    while (!(await exists(resolve(markers, name)))) {
      if (Date.now() >= deadline) throw Error("Timed out waiting for water level");
      await sleep(300);
    }
  }

  const [aUser,bUser] = users;
  const change = user => updated(user) + "M5 shared disk retry " + user + "\n";
  const a = await openNotebook(await signIn(aUser), aUser, updated(aUser));
  await signal(readyFlag);
  await waitFor(lowFlag);
  stage("A reads the original notebook and is refused a save at low water");
  await readVisible(a, updated(aUser));
  const denied = await save(a, change(aUser));
  assert.deepEqual(denied, {status:507, body:{error:"workspace_disk_low_watermark"}});
  await a.getByText("工作区共享磁盘空间不足，暂时无法写入。已有文件仍可读取或删除。").first().waitFor();
  await readVisible(a, updated(aUser));

  stage("A attempts to create a new file through the authenticated file API");
  const creation = await a.evaluate(async ({project, name}) => {
    const params = new URLSearchParams({directory:project});
    const file = await fetch("/openscience-api/file/content?" + params, {method:"PUT", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:name,content:"M5 shared disk test"})});
    const reason = (await file.json()).error;
    params.set("path","");
    const listing = await fetch("/openscience-api/file?" + params);
    return {status:file.status,error:reason,listing:listing.status,present:(await listing.json()).some(item => item.name === name)};
  }, {project, name:"m5-watermark-create-" + randomUUID() + ".txt"});
  assert.deepEqual(creation, {status:507,error:"workspace_disk_low_watermark",listing:200,present:false});
  report.stages.low_write = {file_save:denied.status,error:denied.body.error,read_before_and_after:200,
    notebook_sha256:sha(updated(aUser)),new_file:creation};

  const bPage = await signIn(bUser);
  stage("B's cold workspace launch is refused at low water");
  const alert = bPage.getByRole("alert").filter({hasText:"工作区容量已满，请稍后重试。"});
  await alert.waitFor({timeout:90000});
  const retry = alert.getByRole("button",{name:"重试",exact:true});
  await retry.waitFor();
  report.stages.low_start = {real_login:true,user_id:owners[bUser],capacity_refusal_visible:true,retry_available:true};

  await signal(deniedFlag);
  await waitFor(beforeDeleteFlag);
  activePage = a.page();
  stage("A retries the same write below the recovery threshold before deleting anything");
  const stillLow = await save(a, change(aUser));
  assert.deepEqual(stillLow, {status:507, body:{error:"workspace_disk_low_watermark"}});
  await readVisible(a, updated(aUser));
  stage("A deletes the release file and refreshes the project listing while writes are refused");
  const deleted = await removeFile(a, release);
  await readVisible(a, updated(aUser));
  report.stages.low_delete = {file:release,save_before_delete:stillLow.status,status:deleted.status(),list_status:200,absent:true,existing_read:200};
  await signal(deletedFlag);
  await waitFor(recoveredFlag);

  async function verifyAndRestore(frame, user) {
    stage("Save and read at recovered water level for " + user);
    const value = change(user);
    const written = await save(frame, value);
    assert.equal(written.status, 200);
    try {
      assert.equal(written.body.content, value);
      await readVisible(frame,value);
    } finally {
      const restored = await save(frame, updated(user));
      assert.equal(restored.status, 200);
      await readVisible(frame,updated(user));
    }
    return {save:written.status,read:200,written_sha256:sha(value),baseline_restored_sha256:sha(updated(user))};
  }
  report.stages.a_recovered = {same_save_retried:true,...await verifyAndRestore(a, aUser)};
  stage("B retries the cold workspace launch from the same logged-in page after recovery");
  activePage = bPage;
  await retry.click();
  const b = await openNotebook(bPage, bUser, updated(bUser));
  report.stages.b_recovered = {cold_start_retried:true,...await verifyAndRestore(b, bUser)};
}

try {
  const driver = await developmentBrowser(state, stack);
  browser = driver.browser;
  report.browser = browser.version();
  async function signIn(user) {
    stage("Web login for " + user);
    const p = await driver.app(await driver.device());
    activePage = p;
    p.setDefaultTimeout(90000);
    const observations = new Set();
    function observe(request, status) {
      if (signingOut) return;
      const {origin, pathname} = new URL(request.url());
      let path;
      if (origin === stack.parent && pathname === "/api/v1/auth/login") path = pathname;
      if (origin === stack.agent) {
        if (["/_nexpoly/auth/exchange", "/_nexpoly/status"].includes(pathname)
            || /^\/openscience-api\/(global\/(health|event|config)|event|path|project(\/current)?|session(\/status)?|permission|question|config(\/providers)?|provider(\/auth)?|file(\/(content|status))?|agent|command|mcp|vcs|skill|lsp|formatter)$/.test(pathname))
          path = pathname;
        else if (/^\/openscience-api\/session\/ses_[\w-]+(?:\/message)?$/.test(pathname)) path = "/openscience-api/session/:id" + (pathname.endsWith("/message") ? "/message" : "");
      }
      if (!path) return;
      const key = [request.method(), path, status].join(" ");
      if (!observations.has(key) || (path.endsWith("/file/content") && status !== "started")) {
        console.error("[M5]", user, key);
        observations.add(key);
      }
      if (status !== "started" && !report.error && activePage === p) report.last_http = {method: request.method(), path, status};
    }
    p.on("request", r => observe(r, "started"));
    p.on("requestfailed", r => observe(r, "failed"));
    p.on("response", r => observe(r.request(), r.status()));
    const {user_id} = await driver.login(p, user);
    signOut.push(() => driver.logout(p, user));
    assert.equal(user_id, owners[user], "Authenticated account differs from its retained dev-m1 identity");
    return p;
  }
  async function openNotebook(p, user, expected) {
    activePage = p;
    stage("Authenticated workspace for " + user);
    const f = await driver.workbench(p);
    const sidebar = p.locator(".np-sidebar-desktop");
    // The authenticated HTML shell precedes both the embedded application and
    // the real sidebar data. Open the retained general
    // session first, as in M2, before sending the project navigation action.
    stage("Wait for the live workspace and open the original general conversation for " + user);
    const generalSession = sidebar.getByRole("button", {name: "M1 synthetic " + user + " general", exact: true});
    await generalSession.click({timeout: 150000});
    await f.getByText("M1 synthetic " + user + " general prompt", {exact: true}).first().waitFor({timeout: 150000});
    stage("Open the project from the parent sidebar for " + user);
    const toggle = sidebar.getByRole("button", {name: /^(展开|收起)项目$/});
    await toggle.waitFor({state: "visible", timeout: 60000});
    if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
    const projectButton = sidebar.locator('button[data-project-directory="' + project + '"]');
    await projectButton.click({timeout: 60000});
    stage("Select the original project conversation for " + user);
    await f.getByText("M1 synthetic " + user + " project", {exact: true}).first().click({timeout: 150000});
    stage("Read the original project conversation for " + user);
    await f.getByText("M1 synthetic " + user + " project prompt", {exact: true}).first().waitFor({timeout: 150000});
    stage("Project opened, read notebook for " + user);
    await openProjectFile(f, notebook);
    await readVisible(f, expected);
    stage("Notebook verified for " + user);
    report.stages[user] = {real_login: true, user_id: owners[user], project_conversation_visible: true, notebook_sha256: sha(expected)};
    return f;
  }
  const people = [];
  if (phase === "watermark") {
    await verifyWatermark(signIn, openNotebook);
  } else {
    for (const user of users) {
      const page = await signIn(user);
      people.push({f: await openNotebook(page, user, phase === "full" ? original(user) : updated(user))});
    }
  }
  if (phase === "full") {
    const [{f: a}, {f: b}] = people;
    activePage = a.page();
    stage("Wait for A to reach the kernel quota");
    await writeFile(ready, "both browsers ready\n", {flag: "wx", mode: 0o600});
    const deadline = Date.now() + 10 * 60 * 1000;
    while (!(await exists(full))) {
      if (Date.now() >= deadline) throw Error("Timed out waiting for the clone's kernel quota fill");
      await sleep(250);
    }
    await readVisible(a, original(users[0]));
    stage("A attempts to save at hard quota");
    const refused = await save(a, updated(users[0]));
    assert.equal(refused.status, 507);
    assert.equal(refused.body.error, "workspace_quota_exceeded");
    await a.getByText("工作区磁盘配额已用完。删除文件后可重试。").first().waitFor();
    await readVisible(a, original(users[0]));
    report.stages.a_full = {save_status: 507, code: refused.body.error, original_read_after_failed_save: true};

    activePage = b.page();
    stage("B saves while A is full");
    const independent = await save(b, updated(users[1]));
    assert.equal(independent.status, 200);
    assert.equal(independent.body.content, updated(users[1]));
    await readVisible(b, updated(users[1]));
    activePage = a.page();
    const stillFull = await save(a, updated(users[0]));
    assert.equal(stillFull.status, 507);
    assert.equal(stillFull.body.error, "workspace_quota_exceeded");
    await readVisible(a, original(users[0]));
    report.stages.b_while_a_full = {save_status: 200, notebook_sha256: sha(updated(users[1])), a_still_full_status: 507};

    stage("A reclaims space and retries the save");
    const deleted = await removeFile(a, release);
    stage("A retries the same save after deleting the release file");
    const retried = await save(a, updated(users[0]));
    assert.equal(retried.status, 200);
    assert.equal(retried.body.content, updated(users[0]));
    await readVisible(a, updated(users[0]));
    report.stages.a_recovered = {removed_in_browser: release, delete_status: deleted.status(), refreshed_list_status: 200,
      absent_from_refreshed_list: true, same_save_retried: true, save_status: retried.status, notebook_sha256: sha(updated(users[0]))};
  }
  report.status = "passed";
  delete report.step;
  delete report.last_http;
} catch (error) {
  // Browser diagnostics can include ticket query strings or a complete DOM
  // snapshot. Keep the durable private report and stderr free of both.
  report.error = error.name;
  console.error("[M5]", report.step || "Startup", report.error);
  if (process.argv.includes("--inspect-on-failure") && activePage) {
    // Keep the failed UI available briefly. Emit only booleans about the known
    // fixture controls; never dump page text, request queries or ticket values.
    const deadline = Date.now() + 90000;
    let previous;
    while (Date.now() < deadline && !(await exists(resolve(markers, "inspection-complete")))) {
      const frame = activePage.frames().find(f => f.url().startsWith(stack.agent + "/") && !f.url().includes("/_nexpoly/"));
      const controls = frame && await frame.evaluate(({notebook, release}) => {
        const buttons = [...document.querySelectorAll("button")].filter(node => node.getClientRects().length);
        const body = document.body.innerText;
        return {
          notebook_named: body.includes(notebook), release_named: body.includes(release),
          files_panel: !!document.querySelector('input[placeholder="/absolute/path"]')?.getClientRects().length,
          source: buttons.some(b => b.title === "edit source"), refresh: buttons.some(b => b.title === "refresh"),
          editor: [...document.querySelectorAll("textarea.atlas-scroll")].some(e => e.getClientRects().length),
          remove: buttons.some(b => b.textContent.trim() === "删除文件"), save: buttons.some(b => b.textContent.trim() === "save"),
          confirm: !!document.querySelector("dialog[open]"), quota_error: body.includes("工作区磁盘配额已用完"),
        };
      }, {notebook, release}).catch(() => null);
      const summary = JSON.stringify(controls);
      if (summary !== previous) console.error("[M5] failed browser controls", summary);
      previous = summary;
      await sleep(3000);
    }
  }
  throw new Error(report.step || "Browser startup or report generation failed");
} finally {
  // Closing an older document also closes its event stream.
  signingOut = true;
  // Sign out only the two browser contexts opened by this invocation.
  await Promise.allSettled(signOut.map(leave => leave()));
  await browser?.close();
  report.finished = new Date().toISOString();
  await output.writeFile(JSON.stringify(report, null, 2) + "\n");
  await output.close();
}
