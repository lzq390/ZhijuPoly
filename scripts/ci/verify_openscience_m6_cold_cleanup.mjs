// Browser half of the cold quota-volume drill. The driver controls and measures the test pool.
import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {access, readFile, writeFile} from "node:fs/promises";
import {resolve} from "node:path";
import {setTimeout as sleep} from "node:timers/promises";
import {developmentBrowser, retainedBindings} from "./openscience_dev_browser.mjs";

const [state, work] = process.argv.slice(2);
const json = async path => JSON.parse(await readFile(path));
const stack = await json(resolve(state,"running.json"));
assert.equal(stack.environment, "dev-m1");
assert.equal(stack.quota_files, true);
retainedBindings(stack, await json(resolve(stack.registry,"registry.json")));
const {project, tag} = await json(resolve(work,"fixture.json"));
const baseline = await json(new URL("../../docs/verification/openscience-m6-ac22-20261124.json", import.meta.url));
assert.equal(project, baseline.project);
assert.match(tag, /^[a-f0-9]{12}$/);
const directory = "/openscience/files/projects/" + project;
const users = ["test-user-a", "test-user-b"];
const shared = "release-" + tag + ".txt", exclusive = "only-b-" + tag + ".txt", retained = "keep-" + tag + ".txt";
const marker = (user, file) => user + " " + file + " " + tag;
const sha = data => createHash("sha256").update(data).digest("hex");
const suite = await developmentBrowser(state,stack);
const report = {status:"failed", project, before:{}, after:{}, checks:{}};
const signal = name => writeFile(resolve(work,name), name, {flag:"wx",mode:0o600});
async function wait(name) {
  for (let i=0; i<400; i++) {
    try { await access(resolve(work,name)); return; } catch { await sleep(500); }
  }
  throw Error("Timed out waiting for " + name);
}
async function signIn(user) {
  const ctx = await suite.device(), page = await suite.app(ctx);
  const identity = await suite.login(page,user);
  assert.equal(identity.user_id, "10000000-0000-4000-8000-00000000000" + (users.indexOf(user)+1));
  return {user,ctx,page};
}
async function native(frame,path,query={},method="GET",body) {
  const r = await frame.evaluate(async ({path,query,method,body}) => {
    const response = await fetch("/openscience-api" + path + "?" + new URLSearchParams(query), {
      method, ...(body && {headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}),
    });
    return {status:response.status, data:await response.json()};
  }, {path,query,method,body});
  assert.equal(r.status, 200, "Native workspace " + path);
  return r.data;
}
const read = async (f,path) => (await native(f,"/file/content",{directory,path})).content;
const write = (f,path,content) => native(f,"/file/content",{directory},"PUT",{path,content});
async function history(frame, user) {
  const previous = baseline.project_data["dev-m1-" + user];
  const sessions = await native(frame,"/session",{directory});
  const rows = sessions.filter(row => row.title === project + " chat dev-m1-" + user);
  assert.equal(rows.length, 1);
  const id = rows[0].id;
  assert.equal(sha(id), previous.project_session_sha256);
  const messages = await native(frame,"/session/" + id + "/message",{directory});
  const parts = messages.flatMap(m => m.parts);
  const notebook = await read(frame,"notebook.txt");
  assert.equal(sha(notebook), previous.notebook_sha256);
  assert.ok(parts.some(p => p.text === notebook) && parts.some(p => p.text === "M0-SYNTHETIC-OK")
    && parts.some(p => p.type === "file" && p.filename === "notebook.txt"));
  const originalFiles = {};
  const files = await native(frame,"/file",{directory,path:""});
  for (const item of files.filter(row => row.type === "file" && !row.name.includes(tag))) {
    originalFiles[item.name] = sha(await read(frame,item.name));
  }
  assert.equal(originalFiles["notebook.txt"],previous.notebook_sha256);
  return {session_sha256:sha(id), messages_sha256:sha(JSON.stringify(parts)), notebook_sha256:sha(notebook),
    messages:messages.length, attachments:parts.filter(p => p.type === "file").length, original_files:originalFiles};
}
async function openCleanup(actor) {
  const button = actor.page.getByRole("button",{name:"清理我的项目文件",exact:true});
  await button.click({timeout:120000});
  const node = await actor.page.locator('iframe[title="我的项目文件"]').elementHandle();
  const frame = await node.contentFrame();
  await frame.getByRole("button",{name:project,exact:true}).click({timeout:45000});
  await frame.getByRole("heading",{name:project,exact:true}).waitFor();
  return frame;
}
async function preview(frame,file,text) {
  const response = frame.page().waitForResponse(r => r.url() === stack.agent + "/api/workspace-cleanup/read");
  await frame.getByRole("button",{name:file,exact:true}).click();
  const r = await response;
  assert.equal(r.status(),200);
  const result = await r.json();
  await frame.getByText(project+"/"+file,{exact:true}).waitFor();
  const visible = await frame.locator("#content").textContent();
  assert.ok(visible.startsWith(text));
  assert.equal(visible,Buffer.from(result.content,"base64").toString("utf8"));
  return result;
}
async function cold(frame,action,body) {
  return frame.evaluate(async ({action,body}) => {
    const r = await fetch("/api/workspace-cleanup/" + action, {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)});
    return {status:r.status,data:await r.json()};
  }, {action,body});
}
async function directoryView(frame) {
  await frame.getByRole("button",{name:"返回目录",exact:true}).click();
  await frame.getByRole("heading",{name:project,exact:true}).waitFor();
}
async function editorWrite(frame, name, content) {
  const bar = frame.locator('input[placeholder="/absolute/path"]:visible');
  if (!await bar.isVisible()) await frame.getByText(/^(Files|文件)$/).filter({visible:true}).first().click();
  await bar.fill(directory);
  await bar.press("Enter");
  const files = bar.locator("../../..");
  const row = files.getByRole("button").and(files.getByTitle(directory + "/" + name,{exact:true}));
  await row.click({timeout:30000});
  const toolbar = frame.getByRole("button",{name:"删除文件",exact:true}).locator("..").filter({has:frame.getByText(name,{exact:true})});
  const pane = toolbar.locator("..");
  await pane.locator(".atlas-file-content:visible").waitFor();
  const edit = toolbar.locator('button[title="edit source"]');
  if (await edit.isVisible()) await edit.click();
  await pane.locator("textarea.atlas-scroll:visible").fill(content);
  const [response] = await Promise.all([
    frame.page().waitForResponse(r => r.url().startsWith(stack.agent + "/openscience-api/file/content")
      && r.request().method() === "PUT" && r.request().postDataJSON()?.path?.endsWith(name)),
    toolbar.getByRole("button",{name:"save",exact:true}).click(),
  ]);
  assert.equal(response.status(),200);
  assert.equal(await read(frame,name),content);
  return response.status();
}
try {
  for (const user of users) {
    const actor = await signIn(user), frame = await suite.workbench(actor.page);
    report.before[user] = await history(frame,user);
    for (const file of user === users[0] ? [retained] : [retained, shared, exclusive]) {
      await write(frame,file,marker(user,file));
      assert.equal(await read(frame,file),marker(user,file));
    }
    await suite.logout(actor.page,user);
    await actor.ctx.close();
  }
  console.log("Original conversations and files verified for both owners");
  await signal("prepared");
  await wait("low");
  const a = await signIn(users[0]), b = await signIn(users[1]);
  const af = await openCleanup(a), bf = await openCleanup(b);
  console.log("Both cold project directories opened from the authenticated web page");
  assert.equal(await af.getByRole("button",{name:exclusive,exact:true}).count(),0);
  const deniedApi = await af.evaluate(async () => (await fetch("/openscience-api/session")).status);
  assert.equal(deniedApi,403);
  const bOwn = await preview(bf,exclusive,marker(b.user,exclusive));
  await directoryView(bf);
  const bSame = await preview(bf,shared,marker(b.user,shared));
  const absent = {status:404,data:{error:"cleanup_not_found"}};
  const foreignRead = await cold(af,"read",{path:project+"/"+exclusive,offset:0});
  assert.deepEqual(foreignRead,absent);
  const foreignDelete = await cold(af,"delete",{path:project+"/"+exclusive,version:bOwn.version});
  assert.deepEqual(foreignDelete,absent);
  const otherVersion = await cold(af,"delete",{path:project+"/"+shared,version:bSame.version});
  assert.deepEqual(otherVersion,{status:409,data:{error:"cleanup_changed"}});
  await preview(af,retained,marker(a.user,retained));
  await directoryView(af);
  const aLarge = await preview(af,shared,"A " + tag + " release");
  assert.equal(aLarge.size,96*1024*1024);
  const reverseVersion = await cold(bf,"delete",{path:project+"/"+shared,version:aLarge.version});
  assert.deepEqual(reverseVersion,{status:409,data:{error:"cleanup_changed"}});
  const page2 = af.page().waitForResponse(r => r.url() === stack.agent + "/api/workspace-cleanup/read");
  await af.getByRole("button",{name:"下一段",exact:true}).click();
  const readPage2 = await (await page2).json();
  assert.equal(readPage2.version,aLarge.version);
  await af.getByRole("button",{name:"上一段",exact:true}).waitFor();
  report.checks = {foreign_listed:false, foreign_read:foreignRead.status, foreign_delete:foreignDelete.status,
    same_path_foreign_version:otherVersion.status, reverse_foreign_version:reverseVersion.status,
    native_api_from_cleanup:deniedApi, a_size:aLarge.size,
    a_second_page_byte_offset:aLarge.next,b_unique_sha256:sha(marker(b.user,exclusive)),b_same_name_sha256:sha(marker(b.user,shared))};
  await af.getByRole("button",{name:"删除此文件",exact:true}).click();
  await af.getByRole("button",{name:"取消",exact:true}).click();
  await signal("checked");
  console.log("Foreign reads, deletes and same-name versions denied; both cold views checked");
  await wait("delete");
  await af.getByRole("button",{name:"删除此文件",exact:true}).click();
  const dialog = af.getByRole("dialog",{name:"确认删除文件"});
  assert.ok((await dialog.innerText()).includes(shared));
  const [removed] = await Promise.all([af.page().waitForResponse(r=>r.url() === stack.agent + "/api/workspace-cleanup/delete"),
    dialog.getByRole("button",{name:"确认删除",exact:true}).click()]);
  assert.equal(removed.status(),200);
  assert.deepEqual(await removed.json(),{deleted:true});
  await af.getByText(/文件已删除/).waitFor();
  report.checks.confirmed_delete_http = removed.status();
  await signal("deleted");
  console.log("A confirmed the native low-water file deletion");
  await wait("restart");
  const closed = await cold(af,"list",{path:"",after:""});
  assert.deepEqual(closed,{status:409,data:{error:"cleanup_unavailable"}});
  report.checks.cleanup_after_recovery = closed.status;
  for (const actor of [a,b]) {
    await actor.page.getByRole("button",{name:"重试",exact:true}).click();
    const frame = await suite.workbench(actor.page);
    report.after[actor.user] = await history(frame,actor.user);
    assert.deepEqual(report.after[actor.user],report.before[actor.user]);
    assert.equal(await read(frame,retained),marker(actor.user,retained));
    const files = await native(frame,"/file",{directory,path:""});
    assert.equal(files.some(r=>r.name===shared),actor===b);
    if (actor === a) {
      const postFile = "resumed-" + tag + ".txt", content = marker(a.user,postFile);
      await write(frame,postFile,content);
      assert.equal(await read(frame,postFile),content);
      // Open the real native conversation before selecting its Files pane.
      const sidebar = actor.page.locator(".np-sidebar-desktop");
      await sidebar.getByRole("button",{name:"M1 synthetic " + actor.user + " general",exact:true}).click();
      await frame.getByText("M1 synthetic " + actor.user + " general prompt",{exact:true}).first().waitFor();
      const toggle = sidebar.getByRole("button",{name:/^(展开|收起)项目$/});
      if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
      await sidebar.locator('button[data-project-directory="'+directory+'"]').click();
      await frame.getByText(project + " chat dev-m1-" + actor.user,{exact:true}).first().click();
      await frame.getByText(project + " marker dev-m1-" + actor.user,{exact:true}).first().waitFor();
      report.checks.native_editor_save = await editorWrite(frame,postFile,content + " saved");
      report.checks.new_file_sha256 = sha(content + " saved");
    } else {
      assert.equal(await read(frame,exclusive),marker(b.user,exclusive));
      assert.equal(await read(frame,shared),marker(b.user,shared));
      report.checks.b_both_files_unchanged = true;
    }
  }
  for (const actor of [a,b]) await suite.logout(actor.page,actor.user);
  report.status = "passed";
  console.log("Browser cleanup, cross-owner denials, native restart and write confirmed");
} catch (error) {
  report.error = String(error.message).slice(0,650);
  console.error(report.error);
  process.exitCode = 1;
} finally {
  await suite.browser.close();
  await writeFile(resolve(work,"browser.json"),JSON.stringify(report,null,2),{flag:"wx",mode:0o600});
}
