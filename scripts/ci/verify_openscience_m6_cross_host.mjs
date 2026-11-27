// Run on the source to seed two file messages, then on the recovery host to compare the restored workspaces.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { open, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import { developmentBrowser, enrolledMembers, retainedBindings } from "./openscience_dev_browser.mjs";

const { values } = parseArgs({ options: {
  state: { type: "string" }, output: { type: "string" }, run: { type: "string" }, "verify-from": { type: "string" },
} });
const digest = v => createHash("sha256").update(v).digest("hex");
function transcriptDigest(messages) {
  return digest(JSON.stringify(messages, (_key, value) => {
    if (value === null || typeof value !== "object" || Array.isArray(value)) return value;
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]]));
  }));
}
const json = async path => JSON.parse(await readFile(path, "utf8"));
const state = resolve(values.state);
const stack = await json(resolve(state, "running.json"));
assert.equal(stack.environment, "deploy-m6");
assert.equal(stack.quota_files, true);
const reg = await json(resolve(stack.registry, "registry.json"));
const bindings = retainedBindings(stack, reg);
const old = await json(new URL("../../docs/verification/openscience-deploy-m6-model-knowledge-20261125.json", import.meta.url));
const seed = values["verify-from"] && await json(resolve(values["verify-from"]));
const host = digest((await readFile("/etc/machine-id", "utf8")).trim());
const project = old.same_name_projects.project;
const directory = "/openscience/files/projects/" + project;
const general = "/openscience/files/general";
const mode = seed ? "verify" : "seed";
if (values.run) assert.match(values.run, /^[0-9a-f-]{36}$/);
if (seed) {
  assert.equal(seed.mode, "seed");
  assert.equal(seed.status, "passed");
  assert.equal(seed.project, project);
  assert.match(seed.run, /^[0-9a-f-]{36}$/);
  assert.equal(seed.workspace_image, old.workspace_image);
  assert.notEqual(host, seed.host_sha256);
  assert.notEqual(stack.deployment, seed.site_deployment);
  assert.notEqual(reg.deployment, seed.workspace_deployment);
} else {
  assert.equal(stack.deployment, old.site_deployment);
  assert.deepEqual(bindings, old.workspace_bindings);
}
const out = await open(resolve(values.output), "wx", 0o600);
const report = { mode, environment: stack.environment, start: new Date().toISOString(), status: "failed",
  host_sha256: host, site_deployment: stack.deployment, workspace_deployment: reg.deployment,
  project, workspace_image: reg.image, run: seed?.run || values.run || randomUUID(), owners: {}, checks: {} };
const suite = await developmentBrowser(state, stack);
const actors = [];
report.browser = suite.browser.version();

async function api(actor, path, { query = { directory }, method = "GET", body, status = 200 } = {}) {
  const res = await actor.frame.evaluate(async ({ path, query, method, body }) => {
    const url = new URL("/openscience-api" + path, location.origin);
    url.search = new URLSearchParams(query);
    const response = await fetch(url, { method, redirect: "error", signal: AbortSignal.timeout(180000),
      headers: body && { "Content-Type": "application/json" }, body: body && JSON.stringify(body) });
    const value = await response.json().catch(() => null);
    return { status: response.status, value };
  }, { path, query, method, body });
  assert.equal(res.status, status, method + " " + path + " returned " + res.status);
  return res.value;
}

async function login(tag) {
  const user = reg.candidate_owners[tag].user_id;
  const member = enrolledMembers(stack).find(m => m.user_id === user);
  assert.ok(member);
  const ctx = await suite.device();
  const page = await suite.app(ctx);
  const id = await suite.login(page, member.username, tag);
  assert.equal(id.user_id, user);
  const actor = { tag, page, ctx, username: member.username };
  actors.push(actor);
  actor.frame = await suite.workbench(page);
  const binding = bindings[user];
  const fs = reg.quota_storage["deploy-m6/" + user].filesystem_uuid;
  assert.equal(fs, reg.native_claim.filesystem_uuids[tag]);
  const docker = (...args) => execFileSync("docker", args, { encoding: "utf8", timeout: 15000 }).trim();
  const container = docker("ps", "-q", "--filter", "volume=" + binding.volume, "--filter",
    "label=org.nexpoly.m1.deployment=" + reg.deployment, "--filter", "label=org.nexpoly.m1.role=backend");
  assert.match(container, /^[0-9a-f]{12,64}$/);
  assert.equal(docker("inspect", "--format", "{{.Image}}", container), old.workspace_image);
  assert.equal(reg.image, old.workspace_image);
  const mounts = JSON.parse(docker("inspect", "--format", "{{json .Mounts}}", container));
  assert.equal(mounts.find(row => row.Destination === "/openscience")?.Name, binding.volume);
  const prior = old.workspace_bindings[user];
  assert.equal(binding.uid, prior.uid);
  assert.equal(binding.workspace_id, prior.workspace_id);
  if (seed) {
    assert.notEqual(seed.owners[tag].volume, binding.volume);
    assert.notEqual(seed.owners[tag].filesystem_uuid, fs);
    assert.equal(seed.owners[tag].user_id, user);
  }
  actor.owner = report.owners[tag] = { user_id: user, ...binding, filesystem_uuid: fs, mounted_backend: container,
    ...(seed ? { session: seed.owners[tag].session } : {}) };
  actor.filename = "restore-" + report.run + "-" + tag + ".txt";
  actor.prompt = "Acknowledge the attached recovery check for " + report.run + " " + tag + ". Reply in one short sentence.";
  actor.content = "OpenScience restore sample " + report.run + " owned by " + tag + "\n";
  console.log(mode, tag, "authenticated and mounted");
}

async function attach(actor) {
  const listings = await api(actor, "/file", { query: { directory, path: "" } });
  if (listings.some(item => item.name === actor.filename)) {
    const prior = await api(actor, "/file/content", { query: { directory, path: actor.filename } });
    assert.equal(prior.content, actor.content, "A different file already uses the run name");
  } else {
    await api(actor, "/file/content", { method: "PUT", body: { path: actor.filename, content: actor.content } });
  }
  const title = "Recovery " + report.run + " " + actor.tag;
  const prior = (await api(actor, "/session")).filter(s => s.title === title);
  assert.ok(prior.length <= 1, "Multiple attachment sessions for this run");
  const conversation = prior[0] || await api(actor, "/session", { method: "POST", body: { title } });
  assert.match(conversation.id, /^ses_[a-zA-Z0-9_-]+$/);
  actor.owner.session = conversation.id;
  if (prior.length) {
    const history = await api(actor, "/session/" + conversation.id + "/message");
    const turns = history.filter(m => m.info.role === "user");
    if (turns.length) {
      assert.equal(turns.length, 1);
      assert.ok(turns[0].parts.some(p => p.type === "text" && p.text === actor.prompt));
      return;
    }
  }
  const reply = await api(actor, "/session/" + conversation.id + "/message", { method: "POST", body: {
    model: stack.m4 ? { providerID: "m4local", modelID: "qwen2.5-7b" } : { providerID: "m0fixture", modelID: "m0" },
    parts: [{ type: "text", text: actor.prompt }, { type: "file", mime: "text/plain", filename: actor.filename,
      url: "file://" + directory + "/" + actor.filename }],
  } });
  assert.ok(reply?.info && !reply.info.error, "The attachment turn did not complete");
  console.log(mode, actor.tag, "sent attachment");
}

async function check(actor, other) {
  const expected = old.same_name_projects.owners[actor.tag];
  const folders = await api(actor, "/api/resolve-folder/list", { query: {} });
  assert.equal(folders.entries.filter(row => row.name === project).length, 1);
  const list = await api(actor, "/session");
  const original = list.filter(s => s.title === project + "-" + actor.tag);
  assert.equal(original.length, 1);
  assert.equal(digest(original[0].id), expected.session_sha256);
  const originalMessages = await api(actor, "/session/" + original[0].id + "/message");
  assert.ok(Array.isArray(originalMessages));
  const notebook = await api(actor, "/file/content", { query: { directory, path: old.same_name_projects.file } });
  assert.equal(digest(notebook.content), expected.file_sha256);
  const oldModel = old.users[actor.tag].model;
  const past = await api(actor, "/session/" + oldModel.workspace_session + "/message", { query: { directory: general } });
  const reply = past.filter(m => m.info.role === "assistant" && m.info.parentID === oldModel.turn_id)
    .flatMap(m => m.parts.filter(p => p.type === "text" && !p.synthetic).map(p => p.text)).join("").trim();
  assert.equal(digest(reply), oldModel.answer_sha256);
  assert.ok(list.some(s => s.id === actor.owner.session));
  assert.ok(!list.some(s => s.id === other.owner.session || digest(s.id) === old.same_name_projects.owners[other.tag].session_sha256));
  const history = await api(actor, "/session/" + actor.owner.session + "/message");
  const sent = history.filter(m => m.info.role === "user" && m.parts.some(p => p.type === "text" && p.text === actor.prompt));
  assert.equal(sent.length, 1);
  const parts = sent[0].parts.filter(p => p.type === "file");
  assert.equal(parts.length, 1);
  assert.equal(parts[0].filename, actor.filename);
  assert.equal(parts[0].mime, "text/plain");
  assert.equal(parts[0].url, "file://" + directory + "/" + actor.filename);
  assert.ok(history.some(m => m.info.role === "assistant" && !m.info.error && m.info.parentID === sent[0].info.id
    && m.parts.some(p => p.type === "text" && !p.synthetic && p.text.trim().length > 0)));
  const files = await api(actor, "/file", { query: { directory, path: "" } });
  assert.ok(files.some(f => f.name === actor.filename) && !files.some(f => f.name === other.filename));
  const file = await api(actor, "/file/content", { query: { directory, path: actor.filename } });
  assert.equal(file.content, actor.content);
  const missing = await api(actor, "/file/content", { query: { directory, path: other.filename } });
  assert.deepEqual(missing, { type: "text", content: "" });
  await api(actor, "/session/" + other.owner.session, { status: 404 });
  await api(actor, "/session/" + old.users[other.tag].model.workspace_session, { query: { directory: general }, status: 404 });
  const output = { original_session_sha256: digest(original[0].id),
    original_session_messages: originalMessages.length, original_session_transcript_sha256: transcriptDigest(originalMessages),
    original_notebook_sha256: digest(notebook.content),
    original_model_messages: past.length, original_model_transcript_sha256: transcriptDigest(past),
    original_reply_sha256: digest(reply), attachment_filename: actor.filename, attachment_sha256: digest(file.content),
    file_part_id: parts[0].id, attachment_message_id: sent[0].info.id, messages: history.length,
    transcript_sha256: transcriptDigest(history), other_new_and_original_session_http: 404,
    other_attachment_absent_from_list_and_read: true };
  assert.match(output.file_part_id, /^prt_[a-zA-Z0-9_-]+$/);
  assert.match(output.attachment_message_id, /^msg_[a-zA-Z0-9_-]+$/);
  if (seed) assert.deepEqual(output, seed.checks[actor.tag]);
  report.checks[actor.tag] = output;
}

try {
  for (const tag of ["a", "b"]) await login(tag);
  if (!seed) for (const actor of actors) await attach(actor);
  for (const actor of actors) await check(actor, actors.find(a => a !== actor));
  assert.notEqual(report.checks.a.attachment_sha256, report.checks.b.attachment_sha256);
  for (const actor of actors) await suite.logout(actor.page, actor.username);
  report.status = "passed";
} finally {
  await suite.browser.close();
  report.end = new Date().toISOString();
  try { await out.writeFile(JSON.stringify(report, null, 2) + "\n"); } finally { await out.close(); }
  console.log(mode, report.status);
}
