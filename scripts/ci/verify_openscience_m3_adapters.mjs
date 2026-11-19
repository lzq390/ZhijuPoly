// Full acceptance through the private dev-m1 browser, November OpenScience and native tools.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { developmentBrowser } from "./openscience_dev_browser.mjs";

const { values } = parseArgs({ options: { state: { type: "string" }, output: { type: "string" } } });
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
assert.equal(stack.model_manifest_sha256, "ad19a4f1cb954b3ee6999b7157c798fd887ecd3fd7ae12e40ac20a97637575e2");
const workspaceBindings = {};
for (const [user, volume] of Object.entries(stack.user_volumes)) {
  const row = registry.workspaces["dev-m1/" + user];
  assert.ok(row.initialized && row.uid > 0);
  assert.equal(volume, "nexpoly-m1-" + registry.deployment.slice(0, 12) + "-" + row.id + "-data");
  workspaceBindings[user] = { workspace_id: row.id, uid: row.uid, volume };
}
assert.deepEqual(Object.keys(workspaceBindings).sort(), ["test-user-a", "test-user-b"]);
assert.notEqual(workspaceBindings["test-user-a"].uid, workspaceBindings["test-user-b"].uid);
assert.notEqual(workspaceBindings["test-user-a"].volume, workspaceBindings["test-user-b"].volume);
const { browser, device, app, login, logout, workbench } = await developmentBrowser(state, stack);
const directory = "/openscience/files/general";
const profiles = [];
const report = {
  environment: "dev-m1",
  workspace_image: registry.image,
  gateway_image: stack.gateway_image,
  adapter_images: stack.adapter_images,
  model_manifest_sha256: stack.model_manifest_sha256,
  workspace_bindings: workspaceBindings,
  start: new Date().toISOString(),
  browser: browser.version(),
  status: "failed",
  adapters: {},
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
      const response = await fetch(
        url,
        body === undefined
          ? {}
          : {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            },
      );
      const text = await response.text();
      return { status: response.status, value: text && JSON.parse(text) };
    },
    { path, body, directory },
  );
}
async function native(user, path) {
  return user.p.evaluate(async (path) => {
    const session = await (await fetch("/api/v1/auth/session")).json();
    const response = await fetch(path, { headers: { "X-Session-Context": session.session_id } });
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
async function turn(user, conversation, tool, args, errorCode) {
  const path = "/session/" + conversation + "/message";
  const before = await api(user, path);
  assert.equal(before.status, 200);
  const seen = new Set(before.value.flatMap((m) => m.parts.map((p) => p.id)));
  const prompt = await api(user, path, {
    model: { providerID: "m0fixture", modelID: "m0" },
    parts: [{ type: "text", text: "M3-TEST-CALL " + JSON.stringify({ tool, arguments: args }) }],
  });
  assert.equal(prompt.status, 200);
  const after = (await api(user, path)).value;
  const invocation = after
    .flatMap((m) => m.parts)
    .filter((p) => p.type === "tool" && p.tool === "capability_gateway_" + tool && !seen.has(p.id));
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
  const path = "/session/" + draft.conversation_id + "/message";
  const prior = (await api(user, path)).value;
  const oldParts = new Set(prior.flatMap((m) => m.parts.map((p) => p.id)));
  const tool = kind === "polytao" ? "confirm_polytao_generation" : "confirm_monomer_retrosynthesis";
  const request = { tool, arguments: { draft_id: draft.draft_id, revision: draft.revision } };
  const answer = await api(user, path, {
    model: { providerID: "m0fixture", modelID: "m0" },
    parts: [{ type: "text", text: "M3-TEST-CALL " + JSON.stringify(request) }],
  });
  assert.equal(answer.status, 200);
  const parts = (await api(user, path)).value.flatMap((m) => m.parts).filter((p) => !oldParts.has(p.id));
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
async function confirm(user, kind, draft) {
  const route = "/openscience-api/nexpoly/" + kind + "/draft";
  const card = user.f.locator("[data-" + kind + '-draft="' + draft.draft_id + '"]');
  const confirmation = card.getByRole("button", { name: adapters[kind].button, exact: true });
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
  const responsePromise = user.p.waitForResponse(
    (response) => {
      const req = response.request();
      if (req.method() !== "POST" || new URL(response.url()).pathname !== route) return false;
      const body = req.postDataJSON();
      return body?.action === "confirm" && body.draft_id === draft.draft_id;
    },
    { timeout: 45000 },
  );
  responsePromise.catch(() => {}); // An absent card may fail before the request starts.
  try {
    await confirmation.click({ timeout: 12000 });
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
    throw new Error(kind + " " + user.tag + " confirmation card unavailable: " + JSON.stringify(summary), {
      cause: error,
    });
  }
  const response = await responsePromise,
    request = response.request().postDataJSON();
  errorResponse(
    { status: response.status(), value: await response.json() },
    503,
    "SUBMISSION_OUTCOME_UNKNOWN",
  );
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
  const after = (await api(user, "/session/" + draft.conversation_id + "/message")).value;
  const receipts = after
    .filter((m) => m.info.role === "user")
    .flatMap((m) => m.parts)
    .filter((p) => p.type === "text" && p.synthetic)
    .map((p) => p.metadata?.["nexpoly_" + kind + "_receipt"])
    .filter((r) => r?.draft_id === draft.draft_id);
  assert.equal(receipts.length, 1, "Expected one persisted receipt after two confirmations");
  assert.equal(receipts[0].job_id, recovered.job_id);
  return {
    job: recovered.job_id,
    lost_reply_http: response.status(),
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
async function testAdapter(kind, a, b, a2) {
  const evidence = (report.adapters[kind] = {});
  // Prepare the extra draft on A's second login. A's quota must apply across devices.
  console.log(kind, "opening the two private conversations");
  const title = "M3 " + kind + " " + report.start;
  const [as, bs] = await Promise.all([openConversation(a, title), openConversation(b, title)]);
  const [initialA, initialB] = await Promise.all([prepare(a, kind, as), prepare(b, kind, bs)]);
  const extra = await prepare(a2, kind, as, true);
  console.log(kind, "three editable drafts prepared");
  const editedA = await editOwnedDraft(kind, a, b, initialA, initialB);
  const editedB = await editOwnedDraft(kind, b, a, initialB, editedA.draft);
  const ad = editedA.draft,
    bd = editedB.draft;
  evidence.owner_edits = { a: editedA.evidence, b: editedB.evidence };
  evidence.private_drafts = {
    a: await checkPrivateDraft(kind, a, b, ad, bd),
    b: await checkPrivateDraft(kind, b, a, bd, ad),
  };
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
  const aReceipt = await confirm(a, kind, ad);
  // Check the blocked slot immediately, then let B use its own slot in that same window.
  admissionWindow();
  const limited = await form(a2, kind, extra, "confirm");
  errorResponse(limited, 429, "RATE_LIMITED");
  const waiting = await form(a2, kind, extra, "get");
  assert.equal(waiting.value.draft.job_id, null);
  console.log(kind, "A original task recovered; A second login rate-limited");
  const bReceipt = await confirm(b, kind, bd);
  admissionWindow();
  const stillLimited = await form(a2, kind, extra, "confirm");
  errorResponse(stillLimited, 429, "RATE_LIMITED");
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
  evidence.limits = {
    a_second_login: limited.status,
    a_extra_job: waiting.value.draft.job_id,
    b_accepted: true,
    a_still_limited_after_b: stillLimited.status,
    elapsed_ms: Date.now() - admissionStart,
  };
  evidence.confirmations = {
    a: aReceipt,
    b: bReceipt,
    wrong_login_after_accept: replayFromOtherLogin.status,
  };
  evidence.jobs = { a: await checkJob(a, b, kind), b: await checkJob(b, a, kind) };
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
  for (const kind of Object.keys(adapters)) await testAdapter(kind, a, b, a2);
  const users = {
    a: { id: a.user_id, session_ids: [a.session_id, a2.session_id], adapters: a.adapters },
    b: { id: b.user_id, session_ids: [b.session_id], adapters: b.adapters },
  };
  for (const kind of Object.keys(adapters)) {
    const injected = JSON.parse(await readFile(stack.adapter_retry_evidence[kind], "utf8"));
    const pairs = [];
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
    report.adapters[kind].verified_lost_native_receipts = pairs;
  }
  report.ledger = ledger({ baseline, users });
  report.status = "passed";
} catch (error) {
  console.error(String(error.stack ?? error).replace(/np_page=[^ &"]+/g, "np_page=[redacted]"));
  process.exitCode = 1;
} finally {
  for (const { p, name } of profiles) await logout(p, name).catch(() => {});
  await browser.close();
  report.end = new Date().toISOString();
  await writeFile(output, JSON.stringify(report, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  console.log("M3 adapter verification", report.status);
}
