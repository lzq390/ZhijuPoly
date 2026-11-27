// Full acceptance through the private dev-m1 browser, November OpenScience and native tools.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { developmentBrowser, retainedBindings } from "./openscience_dev_browser.mjs";

import { verifyAdapterSuites } from "./openscience_m3_adapter_suites.mjs";

const { values } = parseArgs({ options: {
  state: { type: "string" }, output: { type: "string" }, adapter: { type: "string" },
} });
if (values.adapter !== undefined && !["polytao", "retrosynthesis"].includes(values.adapter))
  throw Error("--adapter must be polytao or retrosynthesis");
const state = resolve(values.state);
const output = resolve(values.output);
const stack = JSON.parse(await readFile(resolve(state, "running.json"), "utf8"));
const registry = JSON.parse(await readFile(resolve(stack.registry, "registry.json"), "utf8"));
const imageId = /^sha256:[0-9a-f]{64}$/;
for (const image of [
  stack.gateway_image,
  registry.image,
  stack.adapter_images.polytao,
  stack.adapter_images.retrosynthesis,
]) {
  assert.match(image, imageId);
}
const manifest = stack.adapter_manifest_sha256 ?? stack.model_manifest_sha256;
assert.equal(manifest, "ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2");
const workspaceBindings = retainedBindings(stack, registry);
const { browser, device, app, login, logout, workbench } = await developmentBrowser(state, stack);
const directory = "/openscience/files/general";
const profiles = [];
const report = {
  environment: "dev-m1",
  workspace_image: registry.image,
  gateway_image: stack.gateway_image,
  adapter_images: stack.adapter_images,
  model_manifest_sha256: manifest,
  workspace_bindings: workspaceBindings,
  start: new Date().toISOString(),
  browser: browser.version(),
  status: "failed",
  adapters: {},
  adapter_scope: values.adapter ?? "both",
  turn_timings: [],
  human_confirmation_timings: [],
};
const adapters = {
  polytao: {
    prepare: "prepare_polytao_generation",
    draft: "get_polytao_draft",
    job: "get_polytao_job",
    path: "/api/v1/conditional-generation/polytao/jobs/",
    button: "确认并生成",
  },
  retrosynthesis: {
    prepare: "prepare_monomer_retrosynthesis",
    draft: "get_retrosynthesis_draft",
    job: "get_retrosynthesis_job",
    path: "/api/v1/monomer-retrosynthesis/jobs/",
    button: "确认并预测",
  },
};
const sha = (value) => createHash("sha256").update(JSON.stringify(value)).digest("hex");
function ledger(input = {}) {
  return JSON.parse(
    execFileSync("python3", [fileURLToPath(new URL("openscience_m3_adapter_ledger.py", import.meta.url))], {
      input: JSON.stringify({
        pg: stack.pg,
        deployment: stack.deployment,
        state_dir: stack.state_dir,
        ...input,
      }),
      encoding: "utf8",
    }),
  );
}
async function client(tag) {
  const c = await device(),
    p = await app(c),
    name = "test-user-" + tag;
  const identity = await login(p, name);
  profiles.push({ p, name });
  return { ...identity, tag, p, f: await workbench(p), adapters: {} };
}
async function api(user, path, body) {
  return user.f.evaluate(
    async ({ path, body, directory }) => {
      const url = new URL("/openscience-api" + path, location.origin);
      url.searchParams.set("directory", directory);
      const signal = AbortSignal.timeout(150000);
      const response = await fetch(url, body === undefined ? {signal} : {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body), signal,
      }).catch(error => {
        throw new Error((body === undefined ? "GET " : "POST ") + path + " failed: " +
          (signal.aborted ? "150 s deadline" : error.name));
      });
      const text = await response.text();
      return { status: response.status, value: text && JSON.parse(text) };
    },
    { path, body, directory },
  );
}
async function native(user, path) {
  return user.p.evaluate(async (path) => {
    const signal = AbortSignal.timeout(45000);
    const session = await (await fetch("/api/v1/auth/session", {signal})).json();
    const response = await fetch(path, { headers: { "X-Session-Context": session.session_id }, signal });
    return { status: response.status, value: await response.json() };
  }, path);
}
async function openConversation(user, title) {
  const r = await api(user, "/session", { title });
  assert.equal(r.status, 200);
  const id = r.value.id;
  const select = user.p.getByRole("button", { name: title, exact: true });
  await select.click({ timeout: 30000 });
  await user.f.waitForFunction((id) => location.pathname === "/chat/" + id, id, { timeout: 20000 });
  return id;
}
async function fixtureTurn(user, conversation, request) {
  const path = "/session/" + conversation + "/message";
  const idleDeadline = Date.now() + 120000;
  while (true) {
    const current = await api(user, "/session/status");
    assert.equal(current.status, 200);
    const state = current.value[conversation];
    if (!state || state.type === "idle") break;
    if (Date.now() > idleDeadline) throw Error(user.tag + " " + request.tool +
      " not submitted: preceding turn is " + state.type + " in " + conversation);
    await sleep(1200);
  }
  const before = await api(user, path);
  assert.equal(before.status, 200);
  // November accepts a proposed native message ID if it sorts after the session's last.
  const last = before.value.map(m => m.info.id).sort().at(-1);
  const clockPrefix = (BigInt(Date.now()) << 12n) & 0xffffffffffffn;
  const minimum = last ? BigInt("0x" + last.slice(4, 16)) + 1n : 0n;
  const prefix = clockPrefix > minimum ? clockPrefix : minimum;
  const messageID = "msg_" + prefix.toString(16).padStart(12, "0") + randomBytes(7).toString("hex");
  const started = Date.now();
  const timing = { user: user.tag, tool: request.tool, conversation, message_id: messageID };
  report.turn_timings.push(timing);
  const text = "M3-TEST-CALL " + JSON.stringify(request);
  const submitted = await api(user, "/session/" + conversation + "/prompt_async", {
    model: { providerID: "m0fixture", modelID: "m0" },
    messageID, parts: [{ type: "text", text }],
  });
  timing.accepted_ms = Date.now() - started;
  assert.equal(submitted.status, 204);
  console.log(user.tag, request.tool, "accepted", messageID, timing.accepted_ms, "ms");
  // On the shared dev NFS volume a single user message's text appeared 128s after its metadata.
  const end = started + 300000;
  let observed = {};
  while (Date.now() < end) {
    const history = await api(user, path);
    assert.equal(history.status, 200);
    const elapsed = Date.now() - started;
    const message = history.value.find(m => m.info.id === messageID && m.info.role === "user");
    if (message) timing.first_metadata_read_ms ??= elapsed;
    const matching = history.value.filter(m => m.info.role === "user" && m.info.id === messageID &&
      m.parts.some(p => p.type === "text" && p.text === text));
    if (matching.length) timing.first_prompt_read_ms ??= elapsed;
    assert.ok(matching.length <= 1, "A single browser submission appeared twice");
    const replies = matching.length ? history.value.filter(m => m.info.role === "assistant" &&
      m.info.parentID === matching[0].info.id) : [];
    assert.ok(replies.every(m => !m.info.error), "Persisted adapter turn contains an assistant error");
    const parts = replies.flatMap(m => m.parts);
    if (parts.some(p => p.type === "tool" && ["completed", "error"].includes(p.state.status))) {
      timing.first_tool_result_read_ms ??= elapsed;
    }
    observed = {user_metadata:Boolean(message), user_message:matching.length, assistant_replies:replies.length,
      finish:replies.map(m => m.info.finish || null), completed:replies.map(m => Boolean(m.info.time.completed)), tool_states:parts.filter(p => p.type === "tool").map(p => p.state.status)};
    const terminal = replies.findLast(m => m.info.finish && m.info.finish !== "tool-calls");
    if (terminal && parts.filter(p => p.type === "tool").every(p => ["completed", "error"].includes(p.state.status))) {
      const current = await api(user, "/session/status");
      assert.equal(current.status, 200);
      const state = current.value[conversation]?.type ?? "idle";
      timing.last_native_session_state = state;
      const completedAt = terminal.info.time.completed;
      if (state === "idle" && Number.isFinite(completedAt) && completedAt > 0) {
        assert.equal(terminal.info.finish, "stop", "The deterministic adapter turn must finish normally");
        timing.finish = terminal.info.finish;
        timing.completion_timestamp_present = true;
        timing.completed_read_ms = Date.now() - started;
        console.log(user.tag, request.tool, JSON.stringify(timing));
        return parts;
      }
    }
    await sleep(elapsed > 20000 ? 3500 : 1200);
  }
  const current = await api(user, "/session/status");
  const state = current.value?.[conversation];
  throw Error(user.tag + " " + request.tool + " turn did not finish: " +
    JSON.stringify({...observed, session_state:state?.type || "idle", retry_attempt:state?.attempt}));
}
async function turn(user, conversation, tool, args, errorCode) {
  const parts = await fixtureTurn(user, conversation, {tool, arguments:args});
  const invocation = parts.filter(p => p.type === "tool" && p.tool === "capability_gateway_" + tool);
  assert.equal(invocation.length, 1, "Expected one persisted invocation of " + tool);
  const text = invocation[0].state.output || invocation[0].state.error || "";
  let result;
  for (const candidate of [text, text.split("\n").at(-1)]) {
    try {
      result = JSON.parse(candidate);
      break;
    } catch {
      /* A human summary may precede the final JSON. */
    }
  }
  if (errorCode) {
    assert.equal(
      result?.error?.code,
      errorCode,
      tool + " expected " + errorCode + ", received " + (result?.error?.code ?? text.slice(0, 130)),
    );
  } else {
    assert.equal(result?.ok, true, tool + " failed: " + (result?.error?.code ?? text.slice(0, 130)));
  }
  return result;
}
function formBody(draft, action, fields) {
  return {
    session_id: draft.conversation_id,
    directory,
    draft_id: draft.draft_id,
    revision: draft.revision,
    action,
    ...fields,
  };
}
async function form(user, kind, draft, action, fields) {
  const path = "/nexpoly/" + kind + "/draft";
  const body = formBody(draft, action, fields);
  const deadline = Date.now() + 4000;
  let reply;
  do {
    reply = await api(user, path, body);
    if (reply.status !== 409 || reply.value?.error?.code !== "DRAFT_BUSY") return reply;
    // The displayed form also refreshes. This explicit bridge response precedes dispatch.
    await sleep(150);
  } while (Date.now() < deadline);
  return reply;
}
function errorResponse(response, http, code) {
  assert.equal(
    response.status,
    http,
    "Unexpected HTTP status: " + JSON.stringify(response.value?.error ?? {}),
  );
  assert.equal(response.value?.error?.code, code);
}
async function prepare(user, kind, conversation, alt = false) {
  const molecule = user.tag === "a" ? (alt ? "CC" : "CCO") : "CCCO";
  const args =
    kind === "polytao"
      ? {
          reference_smiles: molecule,
          candidate_count: 1,
          temperature: 1.0,
          top_k: 50,
          top_p: 0.95,
          max_length: 128,
        }
      : {
          target: { source: "smiles", smiles: molecule },
          parameters: { num_return_sequences: 1, num_beams: 1, max_new_tokens: 32 },
        };
  const draft = await turn(user, conversation, adapters[kind].prepare, args);
  assert.equal(draft.conversation_id, conversation);
  assert.equal(draft.ready, true);
  assert.equal(draft.editable, true);
  assert.equal(draft.job_id, null);
  const saved = await form(user, kind, draft, "get");
  assert.equal(saved.status, 200, kind + " saved draft read: " + JSON.stringify(saved.value?.error));
  assert.equal(saved.value.draft.draft_id, draft.draft_id);
  assert.equal(saved.value.draft.revision, draft.revision);
  if (kind === "polytao") assert.equal(saved.value.draft.canonical_smiles, molecule);
  else assert.equal(saved.value.draft.resolved_target.canonical_smiles, molecule);
  return draft;
}
async function checkHumanOnly(user, kind, draft) {
  const tool = kind === "polytao" ? "confirm_polytao_generation" : "confirm_monomer_retrosynthesis";
  const request = { tool, arguments: { draft_id: draft.draft_id, revision: draft.revision } };
  const parts = await fixtureTurn(user, draft.conversation_id, request);
  assert.ok(parts.some((p) => p.type === "text"));
  assert.ok(!parts.some((p) => p.type === "tool"), "A model prompt attempted a human-only confirmation");
  const read = await form(user, kind, draft, "get");
  assert.equal(read.status, 200);
  assert.equal(read.value.draft.status, "editing");
  assert.equal(read.value.draft.job_id, null);
}
async function checkPrivateDraft(kind, user, other, mine, theirs) {
  const result = await form(user, kind, theirs, "get");
  assert.ok(
    [403, 404].includes(result.status),
    kind +
      " foreign draft returned HTTP " +
      result.status +
      " " +
      JSON.stringify(result.value?.error ?? result.value?.detail ?? {}),
  );
  assert.ok(["DRAFT_NOT_IN_SESSION", "DRAFT_NOT_FOUND"].includes(result.value.error?.code));
  const crossEdit = await form(user, kind, theirs, "update", {
    parameters: kind === "polytao" ? { temperature: 0.9 } : { max_new_tokens: 30 },
  });
  assert.equal(crossEdit.status, result.status);
  assert.equal(crossEdit.value.error?.code, result.value.error?.code);
  const crossConfirm = await form(user, kind, theirs, "confirm");
  assert.equal(crossConfirm.status, result.status);
  assert.equal(crossConfirm.value.error?.code, result.value.error?.code);
  await turn(
    user,
    mine.conversation_id,
    adapters[kind].draft,
    { draft_id: theirs.draft_id },
    "DRAFT_NOT_FOUND",
  );
  assert.equal((await form(other, kind, theirs, "get")).value.draft.status, "editing");
  return {
    foreign_form_status: result.status,
    foreign_edit_status: crossEdit.status,
    foreign_confirm_status: crossConfirm.status,
    foreign_tool: "DRAFT_NOT_FOUND",
  };
}
async function editOwnedDraft(kind, user, other, mine, theirs) {
  const field = kind === "polytao" ? "temperature" : "max_new_tokens";
  const setting = kind === "polytao" ? (user.tag === "a" ? 0.99 : 0.98) : user.tag === "a" ? 31 : 30;
  const updated = await form(user, kind, mine, "update", { parameters: { [field]: setting } });
  assert.equal(updated.status, 200);
  const draft = updated.value.draft;
  assert.equal(draft.draft_id, mine.draft_id);
  assert.equal(draft.revision, mine.revision + 1);
  assert.equal(draft.parameters[field], setting);
  assert.equal(draft.job_id, null);
  const invalid = await form(user, kind, mine, "confirm");
  errorResponse(invalid, 409, "DRAFT_REVISION_CONFLICT");
  const latest = await form(user, kind, draft, "get");
  assert.equal(latest.value.draft.status, "editing");
  assert.equal(latest.value.draft.job_id, null);
  const independent = await form(other, kind, theirs, "get");
  assert.equal(independent.value.draft.revision, theirs.revision);
  assert.deepEqual(independent.value.draft.parameters, theirs.parameters);
  return {
    draft,
    evidence: {
      from: mine.revision,
      to: draft.revision,
      field,
      value: setting,
      stale_confirmation: invalid.status,
      other_revision: theirs.revision,
      other_parameters_unchanged: true,
    },
  };
}
async function clickConfirm(user, kind, draft) {
  const route = "/openscience-api/nexpoly/" + kind + "/draft";
  const card = user.f.locator("[data-" + kind + '-draft="' + draft.draft_id + '"]');
  const confirmation = card.getByRole("button", { name: adapters[kind].button, exact: true });
  const trace = [];
  const started = Date.now();
  function actionOf(req) {
    if (req.method() !== "POST" || new URL(req.url()).pathname !== route) return;
    const data = req.postDataJSON();
    if (data?.draft_id === draft.draft_id) return data.action;
  }
  function mark(req, result) {
    const action = actionOf(req);
    if (action) trace.push({action, result, ms: Date.now() - started});
  }
  const sent = req => mark(req, "sent");
  const received = response => mark(response.request(), response.status());
  const failed = req => mark(req, "transport_failed");
  user.p.on("request", sent).on("response", received).on("requestfailed", failed);
  let response;
  try {
    await card.waitFor({ state: "visible", timeout: 20000 });
    // The real card disables actions after a failed refresh. Reload saved parameters
    // through the UI so the human confirmation uses the currently displayed revision.
    if (
      !(await confirmation.isEnabled()) ||
      Number(await card.getAttribute("data-draft-revision")) !== draft.revision
    ) {
      const reload = card.getByRole("button", { name: "重新载入已保存参数", exact: true });
      const [r] = await Promise.all([
        user.p.waitForResponse(
          (response) => {
            const req = response.request();
            if (req.method() !== "POST" || new URL(response.url()).pathname !== route) return false;
            const body = req.postDataJSON();
            return body.action === "get" && body.draft_id === draft.draft_id;
          },
          { timeout: 12000 },
        ),
        reload.click({ timeout: 12000 }),
      ]);
      const saved = await r.json();
      assert.equal(r.status(), 200, kind + " card refresh failed: " + JSON.stringify(saved.error));
      assert.equal(saved.draft.ready, true);
      assert.equal(saved.draft.status, "editing");
    }
    [response] = await Promise.all([
      user.p.waitForResponse(reply => actionOf(reply.request()) === "confirm", {timeout: 45000}),
      confirmation.click({ timeout: 12000 }),
    ]);
  } catch (error) {
    const summary = await card
      .evaluate((node) => {
        const notice = node.querySelector(".nexpoly-draft-notice")?.textContent || "";
        return {
          status: node.dataset.draftStatus,
          revision: node.dataset.draftRevision,
          dirty: node.dataset.draftDirty,
          busy: node.getAttribute("aria-busy"),
          reload_required: notice.includes("重新载入"),
          reading: notice.includes("正在恢复"),
          invalid_fields: node.querySelectorAll('[aria-invalid="true"]').length,
        };
      })
      .catch(() => ({ present: false }));
    throw new Error(kind + " " + user.tag + " confirmation failed: " + JSON.stringify({card: summary, requests:trace}), {cause:error});
  } finally {
    user.p.off("request", sent).off("response", received).off("requestfailed", failed);
    report.human_confirmation_timings.push({user:user.tag, adapter:kind, elapsed_ms:Date.now() - started, requests:trace});
  }
  const request = response.request().postDataJSON();
  errorResponse(
    { status: response.status(), value: await response.json() },
    503,
    "SUBMISSION_OUTCOME_UNKNOWN",
  );
  return {request, status: response.status()};
}

async function recoverConfirmation(user, kind, draft, clicked) {
  const {request} = clicked;
  const recovery = await api(user, "/nexpoly/" + kind + "/draft", request);
  assert.equal(
    recovery.status,
    200,
    "Original confirmation failed to recover: " + JSON.stringify(recovery.value.error),
  );
  const recovered = recovery.value.draft;
  assert.equal(recovered.status, "submitted");
  assert.ok(recovered.job_id);
  assert.equal(recovered.submission?.replayed, true, "Native recovery must return the original task");
  const repeated = await api(user, "/nexpoly/" + kind + "/draft", request);
  assert.equal(repeated.status, 200);
  assert.equal(repeated.value.draft.job_id, recovered.job_id);
  const evidence = { user: user.tag, adapter: kind, job_id: recovered.job_id,
    recovery_continuation: recovery.value.continuation?.status,
    repeated_continuation: repeated.value.continuation?.status, continued_by_user: false };
  (report.receipt_recovery ??= []).push(evidence);
  console.log(kind, user.tag, "receipt replay and repeat", JSON.stringify(evidence));
  async function readReceipts() {
    const history = await api(user, "/session/" + draft.conversation_id + "/message");
    assert.equal(history.status, 200);
    return history.value
      .filter(m => m.info.role === "user").flatMap(m => m.parts)
      .filter(p => p.type === "text" && p.synthetic)
      .map(p => p.metadata?.["nexpoly_" + kind + "_receipt"])
      .filter(r => r?.draft_id === draft.draft_id && r.conversation_id === draft.conversation_id);
  }
  let receipts = await readReceipts();
  const deadline = Date.now() + 300000;
  if (!receipts.length && evidence.repeated_continuation === "failed") {
    // The bridge explicitly reports a saved native job whose chat continuation
    // failed. Wait for the originating chat to be idle, then use its read-only UI
    // action. It may recover the receipt but may never submit another task.
    while (true) {
      const status = await api(user, "/session/status");
      assert.equal(status.status, 200);
      evidence.last_session_status = status.value[draft.conversation_id]?.type ?? "idle";
      if (evidence.last_session_status === "idle") break;
      if (Date.now() >= deadline) throw Error(kind + " " + user.tag + " remains busy before receipt readback");
      await sleep(3500);
    }
    const route = "/openscience-api/nexpoly/" + kind + "/draft";
    const card = user.f.locator("[data-" + kind + '-draft="' + draft.draft_id + '"]');
    const matchesAction = (res, action) => {
      const req = res.request();
      if (req.method() !== "POST" || new URL(res.url()).pathname !== route) return false;
      const body = req.postDataJSON();
      return body.action === action && body.draft_id === draft.draft_id;
    };
    const show = card.getByRole("button", {name:"继续查询结果", exact:true});
    if (!(await show.isVisible())) {
      const [reply] = await Promise.all([
        user.p.waitForResponse(r => matchesAction(r, "get"), {timeout:90000}),
        card.getByRole("button", {name:"重新载入已保存参数", exact:true}).click(),
      ]);
      assert.equal(reply.status(), 200);
    }
    const [continued] = await Promise.all([
      user.p.waitForResponse(r => matchesAction(r, "continue"), {timeout:90000}), show.click(),
    ]);
    assert.equal(continued.status(), 200);
    const answer = await continued.json();
    evidence.continued_by_user = true;
    evidence.continuation_status = answer.continuation?.status;
    assert.equal(answer.draft.job_id, recovered.job_id);
    assert.ok(["started", "existing"].includes(evidence.continuation_status),
      kind + " " + user.tag + " explicit readback did not restore the chat receipt: " + JSON.stringify(evidence));
  }
  while (!receipts.length && Date.now() < deadline) {
    await sleep(3500);
    receipts = await readReceipts();
  }
  evidence.receipts = receipts.length;
  assert.equal(receipts.length, 1, "Expected one persisted receipt after two confirmations");
  assert.equal(receipts[0].job_id, recovered.job_id);
  return {
    job: recovered.job_id,
    lost_reply_http: clicked.status,
    replayed: true,
    receipts: receipts.length,
    clicked_card: true,
  };
}
async function checkJob(user, other, kind) {
  const config = adapters[kind];
  const mine = user.adapters[kind],
    theirs = other.adapters[kind];
  await turn(user, mine.workspace_session, config.job, { job_id: theirs.job_id }, "JOB_NOT_FOUND");
  const foreign = await native(user, config.path + encodeURIComponent(theirs.job_id));
  assert.equal(foreign.status, 404);
  let result;
  const deadline = Date.now() + 120000;
  do {
    result = await turn(user, mine.workspace_session, config.job, { job_id: mine.job_id, wait_seconds: 5 });
    if (result.status === "completed") break;
    assert.ok(
      ["pending", "queued", "submitted", "running"].includes(result.status),
      "Unexpected job status " + result.status,
    );
    await sleep(400);
  } while (Date.now() < deadline);
  assert.equal(result.status, "completed");
  assert.equal(result.job_id, mine.job_id);
  const fetched = await native(user, config.path + encodeURIComponent(mine.job_id));
  assert.equal(fetched.status, 200);
  assert.equal(fetched.value.job_id, mine.job_id);
  const candidates =
    kind === "polytao"
      ? result.results.map((r) => r.generated_smiles)
      : result.result.candidates.map((r) => r.reactants_smiles);
  assert.ok(candidates.length > 0 && candidates.every((x) => typeof x === "string" && x.length));
  mine.result = result;
  let personal;
  do {
    personal = await native(user, "/api/v1/usage/activity?module=" + kind + "&limit=100");
    assert.equal(personal.status, 200);
    if (
      personal.value.items.some((item) => item.task_id.endsWith(mine.job_id) && item.status === "completed")
    )
      break;
    await sleep(400);
  } while (Date.now() < deadline);
  const tasks = personal.value.items;
  const own = tasks.filter((item) => item.task_id.endsWith(mine.job_id));
  assert.equal(own.length, 1);
  assert.equal(own[0].status, "completed");
  assert.equal(own[0].attempt_count, 1);
  assert.ok(!tasks.some((item) => item.task_id.endsWith(theirs.job_id)));
  return {
    native_owner: 200,
    native_other: foreign.status,
    foreign_tool: "JOB_NOT_FOUND",
    result_status: result.status,
    candidates: candidates.length,
    result_sha256: sha(candidates),
    own_usage_records: own.length,
    own_usage_attempts: own[0].attempt_count,
    foreign_usage_records: 0,
  };
}
async function completedPair(promises) {
  // Finish both in-flight requests before moving the same browser tabs to the next adapter.
  const results = await Promise.allSettled(promises);
  const failure = results.find(result => result.status === "rejected");
  if (failure) throw failure.reason;
  return results.map(result => result.value);
}
async function testAdapter(kind, a, b, a2, evidence) {
  // Prepare the extra draft on A's second login. A's quota must apply across devices.
  console.log(kind, "opening the two private conversations");
  const title = "M3 " + kind + " " + report.start;
  const [as, bs, secondLoginSession] = await completedPair([
    openConversation(a, title), openConversation(b, title), openConversation(a2, title + " second device"),
  ]);
  assert.notEqual(as, secondLoginSession);
  const [initialA, initialB] = await completedPair([prepare(a, kind, as), prepare(b, kind, bs)]);
  const extra = await prepare(a2, kind, secondLoginSession, true);
  console.log(kind, "three editable drafts prepared");
  const editedA = await editOwnedDraft(kind, a, b, initialA, initialB);
  evidence.owner_edits = { a: editedA.evidence };
  const editedB = await editOwnedDraft(kind, b, a, initialB, editedA.draft);
  evidence.owner_edits.b = editedB.evidence;
  const ad = editedA.draft,
    bd = editedB.draft;
  evidence.private_drafts = {};
  evidence.private_drafts.a = await checkPrivateDraft(kind, a, b, ad, bd);
  evidence.private_drafts.b = await checkPrivateDraft(kind, b, a, bd, ad);
  await checkHumanOnly(a, kind, ad);
  evidence.model_only_confirmation_created_job = false;
  const same = await form(a2, kind, ad, "get");
  assert.equal(same.status, 200);
  const wrongLogin = await form(a2, kind, ad, "confirm");
  errorResponse(wrongLogin, 403, "CONFIRMATION_LOGIN_MISMATCH");
  const before = await form(a, kind, ad, "get");
  assert.equal(before.value.draft.job_id, null);
  evidence.wrong_login_confirmation = wrongLogin.status;
  console.log(kind, "foreign drafts and non-originating confirmation refused");
  const admissionStart = Date.now();
  const admissionWindow = () =>
    assert.ok(Date.now() - admissionStart < 55000, "Rate-limit verification left its admission window");
  const first = await clickConfirm(a, kind, ad);
  // Check actual admission while A is limited. Receipt replay and conversation
  // inspection do not consume new slots and can follow both admissions.
  admissionWindow();
  const limited = await form(a2, kind, extra, "confirm");
  errorResponse(limited, 429, "RATE_LIMITED");
  console.log(kind, "A confirmation receipt lost; A second login rate-limited");
  const second = await clickConfirm(b, kind, bd);
  admissionWindow();
  const stillLimited = await form(a2, kind, extra, "confirm");
  errorResponse(stillLimited, 429, "RATE_LIMITED");
  const admissionElapsed = Date.now() - admissionStart;
  evidence.limits = {a_second_login: limited.status, b_accepted: true,
    a_still_limited_after_b: stillLimited.status, elapsed_ms: admissionElapsed};
  evidence.confirmations = {};
  const aReceipt = evidence.confirmations.a = await recoverConfirmation(a, kind, ad, first);
  const bReceipt = evidence.confirmations.b = await recoverConfirmation(b, kind, bd, second);
  const waiting = await form(a2, kind, extra, "get");
  assert.equal(waiting.value.draft.job_id, null);
  const replayFromOtherLogin = await form(a2, kind, ad, "confirm");
  errorResponse(replayFromOtherLogin, 403, "CONFIRMATION_LOGIN_MISMATCH");
  assert.notEqual(aReceipt.job, bReceipt.job);
  a.adapters[kind] = {
    workspace_session: as,
    draft_id: ad.draft_id,
    job_id: aReceipt.job,
    limited_draft_id: extra.draft_id,
  };
  b.adapters[kind] = { workspace_session: bs, draft_id: bd.draft_id, job_id: bReceipt.job };
  evidence.limits.a_extra_job = waiting.value.draft.job_id;
  evidence.confirmations.wrong_login_after_accept = replayFromOtherLogin.status;
  evidence.jobs = {};
  evidence.jobs.a = await checkJob(a, b, kind);
  evidence.jobs.b = await checkJob(b, a, kind);
  assert.notEqual(
    evidence.jobs.a.result_sha256,
    evidence.jobs.b.result_sha256,
    "Expected distinguishable owner results",
  );
  console.log(kind, "A/B drafts, confirmation, recovery, rate limit, results and usage verified");
}
try {
  const baseline = ledger();
  const [a, b] = await Promise.all([client("a"), client("b")]);
  const a2 = await client("a");
  assert.equal(a.user_id, a2.user_id);
  assert.notEqual(a.session_id, a2.session_id);
  assert.notEqual(a.user_id, b.user_id);
  const users = {
    a: { id: a.user_id, session_ids: [a.session_id, a2.session_id], adapters: a.adapters },
    b: { id: b.user_id, session_ids: [b.session_id], adapters: b.adapters },
  };
  const kinds = values.adapter ? [values.adapter] : Object.keys(adapters);
  await verifyAdapterSuites(report, kinds, async (kind, evidence) => {
    const since = values.adapter ? baseline : ledger();
    await testAdapter(kind, a, b, a2, evidence);
    const injected = JSON.parse(await readFile(stack.adapter_retry_evidence[kind], "utf8"));
    const pairs = evidence.verified_lost_native_receipts = [];
    for (const user of [a, b]) {
      const match = (item) => item.owner_id === user.user_id && item.job_id === user.adapters[kind].job_id;
      const dropped = injected.dropped.filter(match),
        recovered = injected.recovered.filter(match);
      assert.equal(dropped.length, 1);
      assert.ok(recovered.length >= 1);
      assert.match(dropped[0].key, /^[0-9a-f]{64}$/);
      assert.ok(recovered.every((item) => item.key === dropped[0].key));
      pairs.push({ user: user.tag, job_id: dropped[0].job_id, key_sha256: sha(dropped[0].key) });
    }
    evidence.ledger = ledger({ baseline: since, users, module: kind });
  }, () => values.adapter ? report.adapters[values.adapter].ledger : ledger({ baseline, users }));
  report.status = "passed";
} catch (error) {
  report.failure = String(error.message ?? error).replace(/np_page=[^ &"]+/g, "np_page=[redacted]");
  console.error(String(error.stack ?? error).replace(/np_page=[^ &"]+/g, "np_page=[redacted]"));
  process.exitCode = 1;
} finally {
  for (const { p, name } of profiles) await logout(p, name).catch(() => {});
  await browser.close();
  const slowest = (rows, field) => rows.filter(row => Number.isFinite(row[field]))
    .sort((left, right) => right[field] - left[field])[0] || null;
  report.slowest_prompt_read = slowest(report.turn_timings, "first_prompt_read_ms");
  report.slowest_completed_read = slowest(report.turn_timings, "completed_read_ms");
  report.slowest_human_confirmation = slowest(report.human_confirmation_timings, "elapsed_ms");
  report.end = new Date().toISOString();
  await writeFile(output, JSON.stringify(report, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  console.log("M3 adapter verification", report.status);
}
