// Run within an isolated deploy-m6 capacity site after the supervisor admits the selected workspaces.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { open, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { developmentBrowser, enrolledMembers, retainedBindings } from "./openscience_dev_browser.mjs";

const { values } = parseArgs({ options: {
  state: { type: "string" }, output: { type: "string" }, only: { type: "string" }, "browser-cold": { type: "boolean" },
} });
const state = resolve(values.state);
const stack = JSON.parse(await readFile(resolve(state, "running.json")));
assert.equal(stack.environment, "deploy-m6");
assert.equal(stack.parent, "https://app.capacity-m6.test:18960");
assert.equal(stack.agent, "https://agent.capacity-m6.test:18961");
assert.equal(stack.quota_files, true);
assert.ok(stack.gateway_image && !stack.m4, "Expected the deployment gateway and controlled model");
if (values.only) assert.ok(["a", "b"].includes(values.only));
const registry = JSON.parse(await readFile(resolve(stack.registry, "registry.json")));
const members = enrolledMembers(stack);
const bindings = retainedBindings(stack, registry);
const owners = values.only ? [values.only] : ["a", "b"];
const cold = Boolean(values["browser-cold"]);
for (const owner of owners) {
  const volume = bindings[registry.candidate_owners[owner].user_id].volume;
  const holders = execFileSync("docker", ["ps", "-aq", "--filter", "volume=" + volume], { encoding: "utf8" }).trim();
  assert.equal(Boolean(holders), !cold, "Unexpected initial state of the capacity workspace");
}
const suite = await developmentBrowser(state, stack);
const clients = [];
const report = { start: new Date().toISOString(), status: "failed", deployment: stack.deployment,
  registry_deployment: registry.deployment, gateway_image: stack.gateway_image,
  browser: suite.browser.version(), run: randomUUID(), calls: [], handoffs: [], initial_workspace: cold ? "cold" : "running" };
const output = await open(resolve(values.output), "wx", 0o600);

function ledger(extra = {}) {
  return JSON.parse(execFileSync("python3", [fileURLToPath(new URL("openscience_m3_ledger.py", import.meta.url))], {
    input: JSON.stringify({ pg: stack.pg, deployment: stack.deployment, environment: stack.environment, members, ...extra }),
    encoding: "utf8", timeout: 45000,
  }));
}

async function api(frame, path, body) {
  const result = await frame.evaluate(async ({ path, body }) => {
    const url = new URL("/openscience-api" + path, location.origin);
    url.searchParams.set("directory", "/openscience/files/general");
    const response = await fetch(url, { method: body ? "POST" : "GET", redirect: "error",
      headers: body && { "Content-Type": "application/json" }, body: body && JSON.stringify(body),
      signal: AbortSignal.timeout(120000) });
    return { status: response.status, value: await response.json() };
  }, { path, body });
  assert.equal(result.status, 200, path);
  return result.value;
}

async function login(tag) {
  const member = members.find(m => m.user_id === registry.candidate_owners[tag].user_id);
  assert.ok(member);
  const ctx = await suite.device(), page = await suite.app(ctx);
  let ticketAt;
  page.on("request", request => {
    if (request.method() === "POST" && request.url() === stack.parent + "/api/v1/openscience/workspace-ticket") {
      ticketAt ??= performance.now();
    }
  });
  const start = performance.now();
  const login = await suite.login(page, member.username, tag);
  assert.equal(login.user_id, member.user_id);
  clients.push({ page, name: member.username });
  const frame = await suite.workbench(page);
  const rendered = performance.now();
  assert.ok(ticketAt >= start && ticketAt <= rendered, "Missing the original workspace ticket handoff");
  assert.equal((await api(frame, "/global/health")).healthy, true);
  const health = performance.now();
  report.handoffs.push({ owner: tag, login_to_workspace_rendered_ms: Math.round(rendered - start),
    ticket_to_rendered_ms: Math.round(rendered - ticketAt), ticket_to_verified_health_ms: Math.round(health - ticketAt) });
  const { id } = await api(frame, "/session", { title: "Capacity " + report.run + " " + tag });
  assert.match(id, /^ses_[a-zA-Z0-9_-]+$/);
  return { tag, login, frame, id };
}

async function predict({ tag, login, frame, id }) {
  const tool = "predict_polymer_properties";
  const args = { smiles: tag === "a" ? "*CC*" : "*CCC*", properties: ["Glass transition temperature"] };
  const started = new Date().toISOString(), path = "/session/" + id + "/message";
  const reply = await api(frame, path, {
    model: { providerID: "m0fixture", modelID: "m0" },
    parts: [{ type: "text", text: "M3-TEST-CALL " + JSON.stringify({ tool, arguments: args }) }],
  });
  assert.ok(reply.info && !reply.info.error, "Expected the logged-in tool turn to complete");
  const finished = new Date().toISOString();
  const parts = (await api(frame, path)).flatMap(m => m.parts).filter(p => p.type === "tool");
  assert.equal(parts.length, 1);
  const [part] = parts;
  assert.equal(part.tool, "capability_gateway_" + tool);
  assert.equal(part.state.status, "completed");
  const value = JSON.parse(part.state.output).predictions["Glass transition temperature"];
  assert.ok(Number.isFinite(value));
  return { owner: tag, user_id: login.user_id, login_id: login.session_id, workspace_session: id,
    workspace_id: bindings[login.user_id].workspace_id, started, finished, call_id: part.callID, predicted_tg_c: value };
}

try {
  const baseline = ledger();
  const authenticated = await Promise.all(owners.map(login));
  const calls = await Promise.all(authenticated.map(predict));
  if (calls.length === 2) {
    const [a, b] = calls;
    assert.notEqual(a.predicted_tg_c, b.predicted_tg_c);
    assert.ok(a.started < b.finished && b.started < a.finished, "Expected overlapping tool turns");
  }
  const rows = ledger({ baseline, capacity_calls: calls });
  assert.equal(rows.length, calls.length);
  for (const call of calls) {
    const op = rows.find(r => r.call_id === call.call_id && r.workspace_session === call.workspace_session);
    assert.ok(op?.login_owned && op.workspace_bound);
    assert.equal(op.owner_id, call.user_id);
    assert.equal(op.workspace_user_id, call.user_id);
    assert.equal(op.login_id, call.login_id);
    assert.equal(op.tool, "predict_polymer_properties");
    assert.ok(op.turn_id && op.operation_id);
    assert.equal(op.linked_quota_rows, 1);
    assert.equal(op.receipts.length, 1);
    const [r] = op.receipts;
    assert.equal(r.status, 200);
    assert.equal(r.native_status, 200);
    assert.ok(r.native_received && r.has_result && r.browser_result_matches && r.qualified);
    assert.equal(r.module, "predict");
    assert.equal(r.native_operation, r.usage_operation_id);
    assert.equal(r.native_outcome, "response_finished");
    assert.equal(r.state, "succeeded");
    assert.equal(r.settlements, 1);
    assert.equal(r.completed_sync_spans, 1);
    assert.equal(r.key_hash, createHash("sha256").update("openscience-" + op.operation_id).digest("hex"));
    const { login_id, ...observed } = call;
    report.calls.push({ ...observed, operation_id: op.operation_id, native_http: r.status,
      usage_operation_id: r.usage_operation_id, successful_settlements: r.settlements,
      completed_native_tasks: r.completed_sync_spans, stored_result_matches_browser: r.browser_result_matches });
  }
  report.unrelated_owner_delta = 0;
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  throw error;
} finally {
  try {
    if (report.status === "passed") await Promise.all(clients.map(({ page, name }) => suite.logout(page, name)));
  } catch (error) {
    report.status = "failed";
    throw error;
  } finally {
    await suite.browser.close();
    report.end = new Date().toISOString();
    await output.writeFile(JSON.stringify(report, null, 2) + "\n");
    await output.close();
  }
}
