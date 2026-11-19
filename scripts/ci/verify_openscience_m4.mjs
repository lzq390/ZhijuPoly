// Exercise the isolated November workspaces through two real NexPoly web logins.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { developmentBrowser } from "./openscience_dev_browser.mjs";

const { values } = parseArgs({ options: { state: { type: "string" }, output: { type: "string" } } });
const state = resolve(values.state);
const output = resolve(values.output);
const stack = JSON.parse(await readFile(resolve(state, "running.json"), "utf8"));
assert.equal(stack.m4, true, "Start the dedicated M4 development stack");
assert.match(stack.model_image, /^sha256:[0-9a-f]{64}$/);
assert.match(stack.model_manifest_sha256, /^[0-9a-f]{64}$/);
const registry = JSON.parse(await readFile(resolve(stack.registry, "registry.json"), "utf8"));
const bindings = Object.fromEntries(
  ["a", "b"].map((tag) => {
    const user = "test-user-" + tag;
    const { id, uid, initialized } = registry.workspaces["dev-m1/" + user];
    const volume = stack.user_volumes[user];
    assert.ok(initialized && uid > 0);
    assert.equal(volume, "nexpoly-m1-" + registry.deployment.slice(0, 12) + "-" + id + "-data");
    return [user, { workspace_id: id, uid, volume }];
  }),
);
assert.notEqual(bindings["test-user-a"].uid, bindings["test-user-b"].uid);
assert.notEqual(bindings["test-user-a"].volume, bindings["test-user-b"].volume);
const { browser, device, app, login, logout, workbench } = await developmentBrowser(state, stack);
const report = {
  environment: "dev-m1",
  workspace_image: registry.image,
  model_image: stack.model_image,
  model_manifest_sha256: stack.model_manifest_sha256,
  workspace_bindings: bindings,
  browser: browser.version(),
  start: new Date().toISOString(),
  status: "failed",
  users: {},
};
const profiles = [];
const run = randomUUID().replaceAll("-", "");
const directory = "/openscience/files/general";
const common = "sample" + run;
const filename = "notebook-" + run + ".txt";
const digest = (value) => createHash("sha256").update(value).digest("hex");

function ledger(input = {}) {
  return JSON.parse(
    execFileSync("python3", [fileURLToPath(new URL("openscience_m4_ledger.py", import.meta.url))], {
      input: JSON.stringify({ pg: stack.pg, deployment: stack.deployment, state_dir: state, ...input }),
      encoding: "utf8",
    }),
  );
}
async function client(tag) {
  const context = await device();
  const page = await app(context);
  const name = "test-user-" + tag;
  const identity = await login(page, name);
  profiles.push({ page, name });
  const frame = await workbench(page);
  const marker = "lab" + randomUUID().replaceAll("-", "");
  const content = ["Private notebook.", "Experiment " + common + ".", "Specimen " + marker + ".", ""].join(
    "\n",
  );
  return { ...identity, tag, page, frame, marker, content };
}
async function request(user, path, { method = "GET", json } = {}) {
  return user.frame.evaluate(
    async ({ path, method, json }) => {
      const url = new URL(path, location.origin);
      if (url.origin !== location.origin) throw Error("Expected a private development route");
      let body, headers;
      if (json !== undefined) {
        body = JSON.stringify(json);
        headers = { "Content-Type": "application/json" };
      }
      const response = await fetch(url, { method, body, headers, redirect: "error" });
      const text = await response.text();
      return {
        status: response.status,
        value: text && (response.headers.get("content-type")?.includes("json") ? JSON.parse(text) : text),
      };
    },
    { path, method, json },
  );
}

async function workspace(user, path, json) {
  return request(
    user,
    "/openscience-api" + path + "?directory=" + encodeURIComponent(directory),
    json === undefined ? {} : { method: "POST", json },
  );
}
async function conversation(user) {
  const response = await workspace(user, "/session", { title: "M4 private notebook " + run });
  assert.equal(response.status, 200);
  assert.match(response.value.id, /^ses_[A-Za-z0-9_-]+$/);
  const id = response.value.id;
  await user.page
    .getByRole("button", { name: "M4 private notebook " + run, exact: true })
    .click({ timeout: 30000 });
  await user.frame.waitForFunction((id) => location.pathname === "/chat/" + id, id, { timeout: 20000 });
  return id;
}

async function upload(user) {
  const name = "M4 notebook " + run;
  await user.frame.getByRole("tab", { name: "知识库", exact: true }).click();
  const panel = user.frame.getByRole("region", { name: "知识库管理" });
  await panel.getByPlaceholder("输入知识库名称（1-64 字符）").fill(name);
  const observe = (path) =>
    user.page.waitForResponse(
      (r) =>
        new URL(r.url()).origin === stack.agent &&
        new URL(r.url()).pathname === path &&
        r.request().method() === "POST",
    );
  const [created] = await Promise.all([
    observe("/knowledge-api/kbs"),
    panel.getByRole("button", { name: "创建知识库", exact: true }).click(),
  ]);
  assert.ok([200, 201].includes(created.status()), "Create private collection: " + created.status());
  const kb = await created.json();
  assert.equal(kb.name, name);
  assert.equal(kb.owner_id, user.user_id);
  user.kb = kb.kb_id;
  await panel.getByRole("heading", { name, exact: true }).waitFor();
  const path = "/knowledge-api/kbs/" + encodeURIComponent(user.kb) + "/documents";
  const [uploaded] = await Promise.all([
    observe(path),
    panel.locator("input[type=file]").setInputFiles({
      name: filename,
      mimeType: "text/plain",
      buffer: Buffer.from(user.content, "utf8"),
    }),
  ]);
  assert.ok([200, 201].includes(uploaded.status()), "Upload private document: " + uploaded.status());
  const document = await uploaded.json();
  assert.equal(document.filename, filename);
  assert.equal(document.kb_id, user.kb);
  user.doc = document.doc_id;
  await panel.getByText(filename, { exact: true }).waitFor();
  assert.ok(user.kb && user.doc);
}

let rpcId = 0;
async function rpc(user, method, params) {
  const id = ++rpcId;
  const response = await request(user, "/knowledge-api/mcp", {
    method: "POST",
    json: { jsonrpc: "2.0", id, method, params },
  });
  assert.equal(response.status, 200);
  assert.equal(response.value.id, id);
  assert.equal(response.value.jsonrpc, "2.0");
  assert.equal(response.value.error, undefined, "MCP method failed: " + method);
  return response.value.result;
}
async function knowledgeTool(user, name, args, error) {
  const result = await rpc(user, "tools/call", { name, arguments: args });
  assert.equal(Boolean(result.isError), Boolean(error), "Unexpected MCP result for " + name);
  assert.equal(result.content.length, 1);
  assert.equal(result.content[0].type, "text");
  const value = JSON.parse(result.content[0].text);
  if (error) assert.equal(value.error.code, error);
  return value;
}
async function search(user, args) {
  return request(user, "/knowledge-api/search", { method: "POST", json: args });
}
function ownHit(results, user, other) {
  assert.equal(results.length, 1, "Expected one result for the run-specific search term");
  const [hit] = results;
  assert.equal(hit.ref, "E1");
  assert.equal(hit.kb_id, user.kb);
  assert.equal(hit.document_id, user.doc);
  assert.equal(hit.document_name, filename);
  assert.ok(hit.text.includes(user.marker));
  assert.ok(!JSON.stringify(results).includes(other.marker));
  assert.ok(hit.chunk_id);
  assert.equal(
    hit.citation_url,
    "/knowledge-api/kbs/" + user.kb + "/documents/" + user.doc + "/chunks/" + hit.chunk_id,
  );
  return hit;
}
function checkCitation(value, hit, user, other) {
  for (const key of ["chunk_id", "kb_id", "document_id", "document_name", "citation_url"]) {
    assert.equal(value[key], hit[key], "Citation " + key);
  }
  assert.ok(value.text.includes(user.marker));
  assert.equal(value.text, hit.text);
  assert.ok(!JSON.stringify(value).includes(other.marker));
}
async function verifySearchPanel(user, other, hit) {
  const panel = user.frame.getByRole("region", { name: "私有文档全文检索" });
  const input = panel.getByRole("searchbox", { name: "检索我的私有文档" });
  const submit = panel.getByRole("button", { name: "检索正文" });
  const results = panel.getByRole("list", { name: "全文检索结果" });
  async function find(term) {
    await input.fill(term);
    const [response] = await Promise.all([
      user.page.waitForResponse(
        (r) =>
          new URL(r.url()).origin === stack.agent &&
          new URL(r.url()).pathname === "/knowledge-api/search" &&
          r.request().method() === "POST",
      ),
      submit.click(),
    ]);
    assert.equal(response.status(), 200);
  }
  await find(common);
  const items = results.getByRole("listitem");
  const source = items.getByRole("link", { name: "打开原文引用" });
  await source.waitFor();
  assert.equal(await items.count(), 1);
  const text = await items.innerText();
  assert.ok(text.includes(filename) && text.includes(user.marker) && !text.includes(other.marker));
  const link = new URL(await source.getAttribute("href"), stack.agent);
  assert.equal(link.origin, stack.agent);
  assert.equal(link.pathname, hit.citation_url);
  const [response] = await Promise.all([
    user.page.waitForResponse(
      (r) =>
        new URL(r.url()).origin === stack.agent &&
        new URL(r.url()).pathname === hit.citation_url &&
        r.request().method() === "GET",
    ),
    source.click(),
  ]);
  assert.equal(response.status(), 200);
  const original = items.getByRole("region", { name: "引用原文" });
  const quote = original.locator("blockquote");
  await quote.waitFor();
  assert.equal(await quote.textContent(), hit.text);
  assert.ok(!(await original.innerText()).includes(other.marker));
  await find(other.marker);
  await panel.getByText("未找到匹配的私有文档。", { exact: true }).waitFor();
  assert.equal(await items.count(), 0);
  return {
    common_term_visible_hits: 1,
    source_opened: true,
    source_text_matches: true,
    other_term_visible_hits: 0,
  };
}
async function privateSearch(user, other) {
  const collections = await request(user, "/knowledge-api/kbs");
  assert.equal(collections.status, 200);
  assert.ok(collections.value.some((item) => item.kb_id === user.kb));
  assert.ok(!collections.value.some((item) => item.kb_id === other.kb));
  assert.ok(
    collections.value.every((item) => item.owner_id === user.user_id && item.visibility === "private"),
  );
  const base = "/knowledge-api/kbs/" + user.kb + "/documents";
  const documents = await request(user, base);
  assert.equal(documents.status, 200);
  assert.deepEqual(
    documents.value.map((item) => item.doc_id),
    [user.doc],
  );
  const record = await request(user, base + "/" + user.doc);
  assert.equal(record.status, 200);
  assert.equal(record.value.filename, filename);
  const bytes = await request(user, base + "/" + user.doc + "/download");
  assert.equal(bytes.status, 200);
  assert.equal(bytes.value, user.content);
  const initialized = await rpc(user, "initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "dev-m1-browser", version: "1" },
  });
  assert.equal(initialized.protocolVersion, "2025-06-18");
  const notified = await request(user, "/knowledge-api/mcp", {
    method: "POST",
    json: {
      jsonrpc: "2.0",
      method: "notifications/initialized",
    },
  });
  assert.equal(notified.status, 202);
  const tools = await rpc(user, "tools/list", {});
  const names = tools.tools.map((tool) => tool.name);
  assert.deepEqual(names.sort(), ["kb_read_citation", "kb_search"]);
  const args = { query: common, top_k: 5 };
  const found = await search(user, args);
  assert.equal(found.status, 200);
  const hit = ownHit(found.value.results, user, other);
  const toolPage = await knowledgeTool(user, "kb_search", args);
  assert.ok(found.value.kb_ids.includes(user.kb) && !found.value.kb_ids.includes(other.kb));
  assert.deepEqual(toolPage.kb_ids, found.value.kb_ids);
  const toolHit = ownHit(toolPage.results, user, other);
  assert.deepEqual(toolHit, hit);
  const scoped = await search(user, { ...args, kb_ids: [user.kb] });
  assert.equal(scoped.status, 200);
  assert.equal(ownHit(scoped.value.results, user, other).chunk_id, hit.chunk_id);
  const ownCitation = await request(user, hit.citation_url);
  assert.equal(ownCitation.status, 200);
  checkCitation(ownCitation.value, hit, user, other);
  const citationArgs = { kb_id: user.kb, document_id: user.doc, chunk_id: hit.chunk_id };
  checkCitation(await knowledgeTool(user, "kb_read_citation", citationArgs), hit, user, other);
  const foreignOnly = { query: other.marker, top_k: 5 };
  const empty = await search(user, foreignOnly);
  assert.equal(empty.status, 200);
  assert.deepEqual(empty.value.results, []);
  assert.deepEqual((await knowledgeTool(user, "kb_search", foreignOnly)).results, []);
  const mixed = { ...args, kb_ids: [user.kb, other.kb] };
  const forbidden = await search(user, mixed);
  assert.equal(forbidden.status, 403);
  await knowledgeTool(user, "kb_search", mixed, "forbidden_kb");
  const foreignStatuses = {};
  for (const [operation, path] of Object.entries({
    list_documents: base,
    document: base + "/" + user.doc,
    download: base + "/" + user.doc + "/download",
    citation: hit.citation_url,
  })) {
    const denied = await request(other, path);
    assert.equal(denied.status, 404, "Foreign " + operation);
    assert.ok(!JSON.stringify(denied.value).includes(user.marker));
    foreignStatuses[operation] = denied.status;
  }
  await knowledgeTool(other, "kb_read_citation", citationArgs, "not_found");
  return {
    visible_search_panel: await verifySearchPanel(user, other, hit),
    kb_id: user.kb,
    document_id: user.doc,
    chunk_id: hit.chunk_id,
    same_name: filename,
    upload_sha256: digest(user.content),
    citation_text_sha256: digest(hit.text),
    own_download_matches: true,
    private_collection_list: true,
    search_web_hits: 1,
    search_mcp_hits: 1,
    foreign_marker_web_hits: 0,
    foreign_marker_mcp_hits: 0,
    web_mcp_citation_matches: true,
    mixed_collections_web: forbidden.status,
    mixed_collections_mcp: "forbidden_kb",
    other_login_http: foreignStatuses,
    other_login_mcp_citation: "not_found",
  };
}

async function infer(user) {
  const provider = user.tag === "a" ? "m4local" : "m4local-responses";
  const prompt =
    user.tag === "a"
      ? "What colour is snow? Give a short answer."
      : "What colour is typical of healthy grass growing in sunlight? Give a short answer in English.";
  const path = "/session/" + user.conversation + "/message";
  const response = await workspace(user, path, {
    model: { providerID: provider, modelID: "qwen2.5-7b" },
    parts: [{ type: "text", text: prompt }],
  });
  assert.equal(response.status, 200);
  const history = await workspace(user, path);
  assert.equal(history.status, 200);
  const sent = history.value.filter(
    (m) => m.info.role === "user" && m.parts.some((p) => p.type === "text" && p.text === prompt),
  );
  assert.equal(sent.length, 1, "Expected the caller's persisted question");
  const assistants = history.value.filter(
    (m) => m.info.role === "assistant" && m.info.parentID === sent[0].info.id,
  );
  assert.ok(assistants.length > 0, "Expected the local provider's persisted reply");
  assert.ok(
    assistants.every((m) => !m.info.error),
    "Local model returned an assistant error: " +
      JSON.stringify(assistants.map((m) => m.info.error)).slice(0, 500),
  );
  assert.ok(assistants.every((m) => m.info.providerID === provider && m.info.modelID === "qwen2.5-7b"));
  const answer = assistants
    .flatMap((m) => m.parts.filter((p) => p.type === "text" && !p.synthetic).map((p) => p.text))
    .join("")
    .trim();
  assert.ok(answer.length > 0, "Local CPU inference returned no answer");
  user.turn = sent[0].info.id;
  return {
    model: "qwen2.5-7b",
    provider,
    protocol: user.tag === "a" ? "chat.completions" : "responses",
    workspace_session: user.conversation,
    turn_id: user.turn,
    answer_sha256: digest(answer),
    answer_characters: [...answer].length,
  };
}
async function usage(user, other) {
  const result = await request(user, "/openscience-api/m4/model/usage");
  assert.equal(result.status, 200);
  const { records } = result.value;
  assert.ok(records.every((row) => row.owner_id === user.user_id));
  assert.ok(!records.some((row) => row.workspace_session === other.conversation));
  const own = records.filter(
    (row) => row.workspace_session === user.conversation && row.turn_id === user.turn,
  );
  assert.ok(
    own.some((row) => row.protocol === report.users[user.tag].model.protocol),
    "The selected model protocol has no usage for the persisted question",
  );
  assert.ok(
    own.every(
      (row) =>
        row.login_id === user.session_id &&
        row.state === "actual" &&
        row.model === "qwen2.5-7b" &&
        Number.isInteger(row.input_tokens) &&
        row.input_tokens > 0 &&
        Number.isInteger(row.output_tokens) &&
        row.output_tokens > 0,
    ),
  );
  return own.map(({ request_id, protocol, input_tokens, output_tokens, state: settlement }) => ({
    request_id,
    protocol,
    input_tokens,
    output_tokens,
    settlement,
  }));
}
try {
  const baseline = ledger();
  const a = await client("a"),
    b = await client("b");
  assert.notEqual(a.user_id, b.user_id);
  assert.notEqual(a.session_id, b.session_id);
  for (const u of [a, b]) {
    u.conversation = await conversation(u);
    await upload(u);
    console.log(u.tag, "uploaded private notebook in the knowledge panel");
  }
  assert.notEqual(a.kb, b.kb);
  assert.notEqual(a.doc, b.doc);
  const users = {};
  for (const [u, other] of [
    [a, b],
    [b, a],
  ]) {
    const knowledge = await privateSearch(u, other);
    const model = await infer(u);
    report.users[u.tag] = { knowledge, model };
    console.log(u.tag, "verified web and MCP search/citations; real model reply persisted");
    users[u.tag] = {
      id: u.user_id,
      login_id: u.session_id,
      content_sha256: digest(u.content),
      ...knowledge,
      ...model,
    };
  }
  for (const [u, other] of [
    [a, b],
    [b, a],
  ]) {
    const requests = await usage(u, other);
    users[u.tag].requests = requests;
    report.users[u.tag].model.requests = requests;
    report.users[u.tag].model.foreign_usage = 0;
  }
  report.ledger = ledger({ baseline, filename, users });
  report.status = "passed";
} catch (error) {
  console.error(String(error.stack ?? error).replace(/np_page=[^ &"]+/g, "np_page=[redacted]"));
  process.exitCode = 1;
} finally {
  for (const { page, name } of profiles) await logout(page, name).catch(() => {});
  await browser.close();
  report.end = new Date().toISOString();
  await writeFile(output, JSON.stringify(report, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  console.log("M4 model and knowledge verification", report.status);
}
