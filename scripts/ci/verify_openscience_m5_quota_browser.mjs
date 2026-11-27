// Actual NexPoly form login, project tree, November editor and kernel quota.
// The host coordinator changes only the isolated quota volume or the global test pool.
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { lstat, open, readFile, realpath, writeFile } from "node:fs/promises";
import { posix, resolve } from "node:path";
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
assert.ok(["full", "readback", "watermark", "rollback-full"].includes(phase));
const rollback = phase === "rollback-full";
const runId = rollback ? option("--run-id") : "";
if (rollback) assert.match(runId, /^[a-f0-9]{32}$/);
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
if ((phase === "full" || rollback) && ((await exists(ready)) || (await exists(full)))) throw Error("Stale quota drill markers");

const project = "/openscience/files/projects/same-project";
const notebook = "notebook.txt";
const release = rollback ? "m5-rb-" + runId + "-release.txt" : phase === "watermark" ? "m5-watermark-release.txt" : "m5-release.txt";
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

// The native SDK can use the project itself or an enclosing workspace
// directory plus a relative path. Match the resolved on-volume target.
function requestedPath(req) {
  const url = new URL(req.url());
  const directory = url.searchParams.get("directory") || req.headers()["x-openscience-directory"] || "";
  let base;
  try { base = decodeURIComponent(directory); } catch { return; }
  if (!posix.isAbsolute(base)) return;
  const path = req.method() === "GET" ? url.searchParams.get("path") : req.postDataJSON()?.path;
  return posix.resolve(base, typeof path === "string" ? path : ".");
}

function operation(frame, method, name) {
  return frame.page().waitForResponse(response => {
    const req = response.request(), url = new URL(response.url());
    return req.method() === method && url.origin === stack.agent && url.pathname === "/openscience-api/file/content"
      && requestedPath(req) === project + "/" + name;
  }, {timeout: 90000});
}
function fileToolbar(frame, filename) {
  // The November UI retains an editor for every open file and hides inactive tabs.
  return frame.getByRole("button", {name: "删除文件", exact: true}).locator("..")
    .filter({has: frame.getByText(filename, {exact: true})});
}

async function editableFile(frame, filename) {
  const toolbar = fileToolbar(frame, filename);
  const document = toolbar.locator("..");
  // Refresh temporarily replaces the content pane with a loading indicator.
  // The toolbar and its edit-mode flag remain mounted during that interval.
  await document.locator(".atlas-file-content:visible").waitFor();
  const edit = toolbar.locator('button[title="edit source"]');
  if (await edit.isVisible()) await edit.click();
  const field = document.locator("textarea.atlas-scroll:visible");
  await field.waitFor();
  return field;
}

async function readVisible(frame, expected, filename = notebook) {
  const [reply] = await Promise.all([
    operation(frame, "GET", filename),
    fileToolbar(frame, filename).locator('button[title="refresh"]').click(),
  ]);
  assert.equal(reply.status(), 200);
  assert.equal((await reply.json()).content, expected);
  await editableFile(frame, filename);
  await frame.waitForFunction(value => [...document.querySelectorAll("textarea.atlas-scroll")]
    .some(node => node.getClientRects().length && node.value === value), expected, {polling: 100, timeout: 15000});
}
async function projectFileList(frame) {
  const pathBar = frame.locator('input[placeholder="/absolute/path"]:visible');
  if (!(await pathBar.isVisible())) await frame.getByText(/^(Files|文件)$/).filter({visible: true}).first().click();
  await pathBar.waitFor();
  return pathBar.locator("../../..");
}
function fileRow(panel, name) {
  // November list and grid views both expose the real absolute path as the row's title.
  return panel.getByRole("button").and(panel.getByTitle(project + "/" + name, {exact: true}));
}
async function selectProjectFile(frame, panel, name) {
  await fileRow(panel, name).click({timeout: 15000});
  await fileToolbar(frame, name).waitFor();
}
async function openProjectFile(frame, name) {
  const panel = await projectFileList(frame);
  if (!(await fileRow(panel, name).isVisible())) {
    await panel.locator('button[title="refresh"]').click();
  }
  await selectProjectFile(frame, panel, name);
}
async function refreshProjectFileList(frame, panel) {
  // Follow the request *started by this refresh*, not a late response to
  // a previous directory, tab activation or automatic load.
  const [request] = await Promise.all([
    frame.page().waitForRequest(req => {
      const url = new URL(req.url());
      return req.method() === "GET" && url.origin === stack.agent && url.pathname === "/openscience-api/file"
        && requestedPath(req) === project;
    }, {timeout: 20000}),
    panel.locator('button[title="refresh"]').click(),
  ]);
  const response = await request.response();
  assert.equal(response?.status(), 200, "The explicitly selected project directory must be readable");
  return response;
}
async function save(frame, value, filename = notebook) {
  const editor = await editableFile(frame, filename);
  await editor.fill(value);
  const [reply] = await Promise.all([
    operation(frame, "PUT", filename),
    fileToolbar(frame, filename).getByRole("button", {name: "save", exact: true}).click(),
  ]);
  return {status: reply.status(), body: await reply.json()};
}

async function removeFile(frame, release, nextFile = notebook) {
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
  stage("Check the deletion receipt and refreshed project file list");
  await dialog.waitFor({state: "detached"});
  const files = await projectFileList(frame);
  await refreshProjectFileList(frame, files);
  await files.getByText(release, {exact: true}).waitFor({state: "detached"});
  await selectProjectFile(frame, files, nextFile);
  return deleted;
}

// Rollback reads the persisted bytes through the same authenticated endpoints
// as the editor. The interactive editor is exercised by the full and watermark phases.
async function rollbackFile(frame, method, name, content) {
  return frame.evaluate(async ({project, method, name, content}) => {
    const query = new URLSearchParams({directory: project});
    const headers = {"x-openscience-directory": project};
    const options = {method, headers};
    if (method === "GET") query.set("path", name);
    else {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify({path: name, content});
    }
    const res = await fetch("/openscience-api/file/content?" + query, options);
    return {status: res.status, body: await res.json()};
  }, {project, method, name, content});
}

async function rollbackRead(frame, expected, filename = notebook) {
  const r = await rollbackFile(frame, "GET", filename);
  report.last_file_check = {status: r.status, expected_sha256: sha(expected),
    received_sha256: typeof r.body.content === "string" ? sha(r.body.content) : null};
  assert.equal(r.status, 200);
  assert.equal(r.body.content, expected, "Rollback file must match its owner's retained bytes");
  delete report.last_file_check;
}

async function verifyRollbackPanels(people) {
  const name = "m5-rb-" + runId + "-panel.txt";
  const content = user => "Private Files panel " + runId + " " + user + "\n";
  const observed = Object.fromEntries(users.map(user => [user, {}]));
  report.stages.files_panels = observed;
  async function refresh(index, present, step) {
    const frame = people[index].f;
    await frame.page().bringToFront();
    const panel = await projectFileList(frame);
    const pathBar = panel.locator('input[placeholder="/absolute/path"]');
    if (await pathBar.inputValue() !== project) {
      await pathBar.fill(project);
      await pathBar.press("Enter");
    }
    const response = await refreshProjectFileList(frame, panel);
    const rows = await response.json();
    const hasFile = target => rows.some(row => row.name === target && row.absolute === project + "/" + target);
    assert.ok(hasFile(notebook), "Refresh lost the retained notebook");
    assert.ok(hasFile("m5-rb-" + runId + "-edit.txt"), "Refresh lost the ext4 file created while this owner was offline");
    assert.equal(hasFile(name), present, "The listing must reflect this owner's last write or deletion");
    await fileRow(panel, name).waitFor({state: present ? "visible" : "detached"});
    observed[users[index]][step] = response.status();
  }
  async function clickAndRead(index, target, value, step) {
    const frame = people[index].f;
    await selectProjectFile(frame, await projectFileList(frame), target);
    await readVisible(frame, value, target);
    observed[users[index]][step] = sha(value);
  }
  for (const [i, {f}] of people.entries()) {
    stage("Refresh the cold project Files panel for " + users[i]);
    activePage = f.page();
    await refresh(i, false, "cold_listing");
    await clickAndRead(i, notebook, updated(users[i]), "notebook_sha256");
    stage("Create and open the private same-name file for " + users[i]);
    const result = await rollbackFile(f, "PUT", name, content(users[i]));
    assert.equal(result.status, 200);
    await refresh(i, true, "created_listing");
    await clickAndRead(i, name, content(users[i]), "created_sha256");
  }
  for (const [i, {f}] of people.entries()) {
    stage("Refresh after deletion and verify the other owner's file for " + users[i]);
    activePage = f.page();
    await activePage.bringToFront();
    if (i === 1) {
      await refresh(i, true, "listing_after_other_owner_delete");
      await clickAndRead(i, name, content(users[i]), "sha256_after_other_owner_delete");
    }
    const deleted = await removeFile(f, name, notebook);
    assert.deepEqual(await deleted.json(), {deleted: true});
    observed[users[i]].delete_button_status = deleted.status();
    await refresh(i, false, "deleted_listing");
  }
}

async function verifyRetainedTurns(people) {
  const candidate = resolve(option("--candidate-reports"));
  const reports = await Promise.all(["m3", "m4"].map(async stage =>
    JSON.parse(await readFile(resolve(candidate, stage + ".json"), "utf8"))));
  for (const record of reports) assert.equal(record.status, "passed");
  const sessions = tag => [reports[0].users[tag].workspace_session, reports[1].users[tag].model.workspace_session];
  const evidence = {};
  for (const [index, tag, peer] of [[0, "a", "b"], [1, "b", "a"]]) {
    const model = reports[1].users[tag].model;
    const own = sessions(tag), foreign = sessions(peer);
    for (const value of [...own, ...foreign]) assert.match(value, /^ses_[a-zA-Z0-9_-]+$/);
    const observed = await people[index].f.evaluate(async ({own, foreign, turn}) => {
      const get = async path => {
        const query = new URLSearchParams({directory:"/openscience/files/general"});
        const response = await fetch("/openscience-api/session" + path + "?" + query);
        if (response.status !== 200) throw Error("Private rollback history returned " + response.status);
        return response.json();
      };
      const ids = new Set((await get("")).map(row => row.id));
      const tool = await get("/" + own[0] + "/message");
      const history = await get("/" + own[1] + "/message");
      const answer = history.filter(m => m.info.role === "assistant" && m.info.parentID === turn)
        .flatMap(m => m.parts.filter(p => p.type === "text" && !p.synthetic).map(p => p.text)).join("").trim();
      return {own:own.filter(id => ids.has(id)).length, foreign:foreign.filter(id => ids.has(id)).length,
        tool:tool.some(m => m.parts.some(p => p.type === "tool")), answer};
    }, {own, foreign, turn:model.turn_id});
    assert.equal(observed.own, 2);
    assert.equal(observed.foreign, 0);
    assert.equal(observed.tool, true);
    assert.equal(sha(observed.answer), model.answer_sha256);
    evidence[tag] = {own_sessions:2, foreign_sessions:0, completed_tool_history:true, model_answer_sha256:model.answer_sha256};
  }
  return evidence;
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
    const start = Date.now();
    const written = await save(frame, value);
    const completed = Date.now();
    assert.equal(written.status, 200);
    try {
      assert.equal(written.body.content, value);
      await readVisible(frame,value);
    } finally {
      const restored = await save(frame, updated(user));
      assert.equal(restored.status, 200);
      await readVisible(frame,updated(user));
    }
    return {save:written.status,read:200,save_ms:completed-start,save_completed_ms:completed,
      written_sha256:sha(value),baseline_restored_sha256:sha(updated(user))};
  }
  stage("B retries the cold workspace launch from the same logged-in page after recovery");
  activePage = bPage;
  const exchange = r => new URL(r.url()).pathname === "/_nexpoly/auth/exchange";
  const exchangeDone = bPage.waitForResponse(exchange).then(() => Date.now());
  await Promise.all([bPage.waitForRequest(exchange), retry.click()]);
  const [saved, exchangeCompleted] = await Promise.all([verifyAndRestore(a, aUser), exchangeDone]);
  report.stages.a_recovered = {same_save_retried:true,...saved,
    saved_before_b_exchange_completed:saved.save_completed_ms <= exchangeCompleted};
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
    if (rollback) await rollbackRead(f, expected);
    else {
      await openProjectFile(f, notebook);
      await readVisible(f, expected);
    }
    stage("Notebook verified for " + user);
    report.stages[user] = {real_login: true, user_id: owners[user], project_conversation_visible: true, notebook_sha256: sha(expected)};
    return f;
  }
  const people = [];
  if (phase === "watermark") {
    await verifyWatermark(signIn, openNotebook);
  } else if (rollback) {
    const started = Date.now();
    const pages = await Promise.all(users.map(async user => {
      const page = await signIn(user);
      const f = await driver.workbench(page);
      return {user, page, f, readyMs: Date.now() - started};
    }));
    report.stages.concurrent_login_to_frame_ms = Object.fromEntries(pages.map(p => [p.user, p.readyMs]));
    stage("Verify retained candidate tool and model conversations in both authenticated workspaces");
    report.stages.retained_candidate_turns = await verifyRetainedTurns(pages);
    for (const {user, page} of pages) {
      people.push({f: await openNotebook(page, user, updated(user))});
    }
    await verifyRollbackPanels(people);
  } else {
    for (const user of users) {
      const page = await signIn(user);
      people.push({f: await openNotebook(page, user, phase === "full" ? original(user) : updated(user))});
    }
  }
  if (phase === "full" || rollback) {
    const [{f: a}, {f: b}] = people;
    const filename = rollback ? "m5-rb-" + runId + "-edit.txt" : notebook;
    const initial = rollback ? user => "M5 rollback " + runId + " " + user + "\n" : original;
    const next = rollback ? user => initial(user) + "saved\n" : updated;
    const readFileContent = rollback ? rollbackRead : readVisible;
    const saveFile = rollback ? (frame, value, name) => rollbackFile(frame, "PUT", name, value) : save;
    if (rollback) {
      report.stages.file_interface = "real browser login, authenticated native file API";
      for (let i = 0; i < users.length; i++) {
        stage("Verify the seeded rollback file for " + users[i]);
        await readFileContent(people[i].f, initial(users[i]), filename);
      }
    }
    activePage = a.page();
    stage("Wait for A to reach the kernel quota");
    await writeFile(ready, "both browsers ready\n", {flag: "wx", mode: 0o600});
    const deadline = Date.now() + 10 * 60 * 1000;
    while (!(await exists(full))) {
      if (Date.now() >= deadline) throw Error("Timed out waiting for the clone's kernel quota fill");
      await sleep(250);
    }
    await readFileContent(a, initial(users[0]), filename);
    stage("A attempts to save at hard quota");
    const refused = await saveFile(a, next(users[0]), filename);
    assert.equal(refused.status, 507);
    assert.equal(refused.body.error, "workspace_quota_exceeded");
    if (!rollback) await a.getByText("工作区磁盘配额已用完。删除文件后可重试。").first().waitFor();
    await readFileContent(a, initial(users[0]), filename);
    report.stages.a_full = {save_status: 507, code: refused.body.error, original_read_after_failed_save: true};

    activePage = b.page();
    stage("B saves while A is full");
    const independent = await saveFile(b, next(users[1]), filename);
    assert.equal(independent.status, 200);
    assert.equal(independent.body.content, next(users[1]));
    await readFileContent(b, next(users[1]), filename);
    activePage = a.page();
    const stillFull = await saveFile(a, next(users[0]), filename);
    assert.equal(stillFull.status, 507);
    assert.equal(stillFull.body.error, "workspace_quota_exceeded");
    await readFileContent(a, initial(users[0]), filename);
    report.stages.b_while_a_full = {save_status: 200, file: filename, notebook_sha256: sha(next(users[1])), a_still_full_status: 507};

    stage("A reclaims space and retries the save");
    let deletedStatus, absence;
    if (rollback) {
      const deleted = await rollbackFile(a, "DELETE", release);
      assert.deepEqual(deleted, {status: 200, body: {deleted: true}});
      deletedStatus = deleted.status;
      const repeated = await rollbackFile(a, "DELETE", release);
      assert.deepEqual(repeated, {status: 404, body: {error: "file_missing"}});
      absence = {repeat_delete_status: repeated.status};
    } else {
      deletedStatus = (await removeFile(a, release, filename)).status();
      absence = {refreshed_list_status: 200, absent_from_refreshed_list: true};
    }
    stage("A retries the same save after deleting the release file");
    const retried = await saveFile(a, next(users[0]), filename);
    assert.equal(retried.status, 200);
    assert.equal(retried.body.content, next(users[0]));
    await readFileContent(a, next(users[0]), filename);
    report.stages.a_recovered = {removed_in_browser: release, delete_status: deletedStatus, ...absence, same_save_retried: true, save_status: retried.status, file: filename, notebook_sha256: sha(next(users[0]))};
    if (rollback) {
      for (let i = 0; i < users.length; i++) {
        await readFileContent(people[i].f, updated(users[i]));
      }
      report.stages.retained_notebooks = Object.fromEntries(users.map(user => [user, sha(updated(user))]));
      assert.deepEqual(await verifyRetainedTurns(people), report.stages.retained_candidate_turns,
        "The same private candidate conversations must remain readable after quota recovery");
    }
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
