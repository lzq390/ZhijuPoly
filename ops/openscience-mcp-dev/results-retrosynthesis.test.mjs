import assert from "node:assert/strict"
import { createServer } from "node:http"
import { existsSync, readFileSync } from "node:fs"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"
import path from "node:path"
import test from "node:test"
import { extractRetrosynthesisDrafts, extractRetrosynthesisTurns, validRetrosynthesisDraft } from "./results-view.mjs"
import { retrosynthesisDraftBridgeBody } from "./results-controller.mjs"
const DIR = path.dirname(fileURLToPath(import.meta.url)), ID = "rtd_" + "a".repeat(32)
const parameters = { target_role: "auto", num_return_sequences: 5, num_beams: 5, max_new_tokens: 128 }
function resolved(smiles = "CC", patch = {}) { return { canonical_smiles: smiles, input_smiles: smiles, inferred_target_role: "other",
  preview_svg: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><text x="20" y="40">C-C</text></svg>',
  source: { type: "smiles", label: "示例单体", record_id: null }, ...patch } }
function draft(patch = {}) { return { ok: true, request_id: "request", draft_id: ID, conversation_id: "ses_current", revision: 1,
  status: "editing", ready: false, editable: true, target: null, resolved_target: null, target_candidates: [], next_cursor: null,
  parameters: { ...parameters }, explicit_parameters: [], parameter_fields: { target_role: { type: "string", enum: ["auto", "diamine", "dianhydride", "other"], default: "auto" },
    num_return_sequences: { type: "integer", minimum: 1, maximum: 10, default: 5 }, num_beams: { type: "integer", minimum: 1, maximum: 20, default: 5 },
    max_new_tokens: { type: "integer", minimum: 16, maximum: 256, default: 128 } },
  missing_fields: ["target"], errors: [{ field: "target", message: "请选择明确目标" }], job_id: null, submission: null, submission_error: null,
  created_at: "2026-11-17T00:00:00Z", updated_at: "2026-11-17T00:00:00Z", ...patch } }
function job(patch = {}) { return { ok: true, request_id: "request", job_id: "retro-job", status: "completed", stage: "completed",
  created_at: "2026-11-17T00:00:00Z", updated_at: "2026-11-17T00:01:00Z", started_at: "2026-11-17T00:00:01Z", finished_at: "2026-11-17T00:01:00Z",
  snapshot_at: "2026-11-17T00:02:00Z", error_code: null, warnings: [], result: { input_smiles: "CC", canonical_smiles: "CC", target_role: "auto", inferred_target_role: "other",
    total: 1, model_id: "ReactionT5", query_time_ms: 123, candidates: [{ rank: 1, reactants_smiles: "C.C", canonical_reactants_smiles: "C.C",
      valid_smiles: true, all_reactants_smaller_than_target: true, reaction_hint: "前体提示", reactants: [
        { input_smiles: "C", canonical_smiles: "C", valid_smiles: true, heavy_atom_count: 1 }, { input_smiles: "C", canonical_smiles: "C", valid_smiles: true, heavy_atom_count: 1 }] }] }, ...patch } }
function messages(name, output, input = {}) { return [{ info: { role: "user", id: "user-one" }, parts: [] },
  { info: { role: "assistant", parentID: "user-one", sessionID: "ses_current" }, parts: [{ type: "tool", tool: "capability_gateway_" + name,
    state: { status: "completed", input, output: "authoritative\n" + JSON.stringify(output) } }] }] }
test("retrosynthesis cards require exact saved tools, correlated IDs and valid structured data", () => {
  assert.equal(validRetrosynthesisDraft(draft()), true)
  assert.equal(extractRetrosynthesisDrafts(messages("prepare_monomer_retrosynthesis", draft())).length, 1)
  assert.equal(extractRetrosynthesisDrafts(messages("prepare_polytao_generation", draft())).length, 0)
  assert.equal(extractRetrosynthesisDrafts(messages("prepare_monomer_retrosynthesis", draft(), { conversation_id: "ses_wrong" })).length, 0)
  assert.equal(extractRetrosynthesisDrafts(messages("get_retrosynthesis_draft", draft(), { draft_id: "rtd_wrong" })).length, 0)
  assert.equal(extractRetrosynthesisTurns(messages("get_retrosynthesis_job", job(), { job_id: "retro-job" })).length, 1)
  assert.equal(extractRetrosynthesisTurns(messages("get_retrosynthesis_job", job(), { job_id: "wrong" })).length, 0)
  for (const patch of [{ snapshot_at: "not-a-time" }, { status: "running" }, { result: { ...job().result, total: 2 } }]) {
    assert.equal(extractRetrosynthesisTurns(messages("get_retrosynthesis_job", job(patch), { job_id: "retro-job" })).length, 0)
  }
  const before = draft(), after = draft({ revision: 2, updated_at: "2026-11-17T00:02:00Z" })
  const history = [...messages("prepare_monomer_retrosynthesis", after), ...messages("get_retrosynthesis_draft", before, { draft_id: ID })]
  assert.equal(extractRetrosynthesisDrafts(history)[0].draft.revision, 2)
})
test("browser bridge bodies use only observed session, draft, target and parameters", () => {
  const body = retrosynthesisDraftBridgeBody("ses_current", "/openscience/files/general", "update", { draft_id: ID, revision: 1,
    target: { source: "smiles", smiles: "CC" }, parameters: { num_beams: 7 }, owner: "forged", token: "forged" })
  assert.deepEqual(Object.keys(body).sort(), ["session_id", "directory", "action", "draft_id", "revision", "target", "parameters"].sort())
  assert.throws(() => retrosynthesisDraftBridgeBody("ses_current", "/run/private", "confirm", { draft_id: ID, revision: 1 }))
  assert.throws(() => retrosynthesisDraftBridgeBody("ses_current", "/openscience/files/general", "submit", { draft_id: ID, revision: 1 }))
})
const playwrightPath = [path.join(DIR, "..", "openscience-ui-overlay", "node_modules", "playwright"),
  "/data/lzq/gith/nexpoly-dev/ops/openscience-ui-overlay/node_modules/playwright"].find(value => existsSync(path.join(value, "package.json")))
const browserOptions = { skip: !playwrightPath && "Playwright package is not installed" }
async function browserHarness(run) {
  const { chromium } = createRequire(import.meta.url)(playwrightPath)
  const server = createServer((request, response) => {
    const asset = new URL(request.url, "http://localhost").pathname
    if (["/results-view.mjs", "/results-controller.mjs", "/results-view.css"].includes(asset)) {
      response.setHeader("content-type", asset.endsWith("mjs") ? "text/javascript" : "text/css"); response.end(readFileSync(path.join(DIR, asset.slice(1))))
    } else { response.setHeader("content-type", "text/html"); response.end('<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/results-view.css"><main style="padding:12px"></main></html>') }
  })
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve))
  let browser
  try { browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] }); const page = await browser.newPage({ viewport: { width: 452, height: 1000 } }); await page.goto(`http://127.0.0.1:${server.address().port}`); await run(page) }
  finally { await browser?.close(); await new Promise(resolve => server.close(resolve)) }
}
async function idle(page) { await page.waitForFunction(() => document.querySelector("[data-retrosynthesis-draft]").getAttribute("aria-busy") === "false") }
async function mount(page, initial = draft()) {
  await page.evaluate(async ({ initial, resolvedTarget }) => {
    const { renderRetrosynthesisDraft } = await import("/results-view.mjs")
    window.savedDraft = structuredClone(initial); window.operations = []; window.failConfirm = false
    window.panel = renderRetrosynthesisDraft(document, { kind: "retrosynthesis-draft", turnId: "user-one", draft: initial }, {
      perform: async (action, input) => {
        window.operations.push({ action, input: structuredClone(input) }); await new Promise(resolve => setTimeout(resolve, 10))
        if (action === "get" || action === "continue") return { ok: true, draft: structuredClone(window.savedDraft) }
        if (action === "resolve") return { ok: true, resolution: { ok: true, status: input.target.source === "smiles" ? "resolved" : "needs_clarification",
          target: input.target.source === "smiles" ? resolvedTarget : null, candidates: input.target.source === "smiles" ? [] : [
            { ...resolvedTarget, source: { type: input.target.source, label: "候选单体", record_id: input.target.source === "saved_structure" ? "12345678-1234-1234-1234-123456789abc" : null } }], next_cursor: null } }
        if (input.revision !== window.savedDraft.revision) { const error = new Error("conflict"); error.code = "REVISION_CONFLICT"; throw error }
        const next = structuredClone(window.savedDraft)
        if (action === "update") {
          if (Object.hasOwn(input, "target")) { next.target = input.target; next.resolved_target = input.target ? { ...resolvedTarget,
            source: { type: input.target.source, label: "候选单体", record_id: input.target.record_id ?? null } } : null }
          const explicit = new Set(next.explicit_parameters)
          for (const [name, value] of Object.entries(input.parameters ?? {})) { next.parameters[name] = value; explicit.add(name) }
          if (!explicit.has("num_beams")) next.parameters.num_beams = Math.max(5, next.parameters.num_return_sequences)
          next.explicit_parameters = [...explicit]; next.revision++; next.ready = Boolean(next.resolved_target); next.errors = next.ready ? [] : next.errors
        } else if (action === "confirm") { next.status = "submitted"; next.editable = false; next.job_id = "retro-job" }
        else if (action === "cancel") { next.status = "cancelled"; next.editable = false }
        window.savedDraft = next
        if (action === "confirm" && window.failConfirm) throw new Error("lost acknowledgement")
        return { ok: true, draft: structuredClone(next), continuation: { status: "started" } }
      }, onChange() {},
    }); document.querySelector("main").append(window.panel)
  }, { initial, resolvedTarget: resolved() }); await idle(page)
}
test("SMILES parameter form adjusts default beams, saves without inference and confirms once", browserOptions, async () => browserHarness(async page => {
  await mount(page); assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get"])
  assert.equal(await page.getByRole("button", { name: "确认并预测", exact: true }).isDisabled(), true)
  await page.getByRole("textbox", { name: "目标结构或名称", exact: true }).fill("CC")
  await page.getByRole("textbox", { name: "候选数量", exact: true }).fill("8")
  assert.equal(await page.locator('[data-parameter-name="num_beams"]').inputValue(), "8")
  await page.getByRole("button", { name: "保存参数", exact: true }).click(); await idle(page)
  assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "update"])
  assert.deepEqual(await page.evaluate(() => window.operations[1].input.parameters), { num_return_sequences: 8 })
  await page.getByRole("button", { name: "确认并预测", exact: true }).dblclick(); await idle(page)
  assert.equal(await page.evaluate(() => window.operations.filter(item => item.action === "confirm").length), 1)
  assert.match(await page.locator("[data-retrosynthesis-draft]").innerText(), /retro-job/)
}))
test("name ambiguity and saved structure selection resolve before confirmation, preserve source on parameter edits", browserOptions, async () => browserHarness(async page => {
  await mount(page)
  await page.getByLabel("目标来源", { exact: true }).selectOption("saved_structure")
  await page.getByRole("button", { name: "解析 / 搜索结构", exact: true }).click(); await idle(page)
  assert.equal(await page.evaluate(() => window.operations.filter(item => item.action === "confirm").length), 0)
  await page.getByRole("button", { name: "选择 候选单体", exact: true }).click(); await idle(page)
  await page.getByRole("textbox", { name: "候选数量", exact: true }).fill("3")
  await page.getByRole("button", { name: "保存参数", exact: true }).click(); await idle(page)
  assert.deepEqual(await page.evaluate(() => window.operations.at(-1).input.target), { source: "saved_structure", record_id: "12345678-1234-1234-1234-123456789abc" })
  await page.locator(".nexpoly-draft-advanced > summary").click()
  await page.getByRole("textbox", { name: "搜索宽度（Beam）", exact: true }).fill("12")
  await page.getByRole("button", { name: "保存参数", exact: true }).click(); await idle(page)
  await page.getByRole("textbox", { name: "候选数量", exact: true }).fill("9")
  assert.equal(await page.getByRole("textbox", { name: "搜索宽度（Beam）", exact: true }).inputValue(), "12")
}))
test("lost confirmation acknowledgement recovers by reading original draft and never automatically resubmits", browserOptions, async () => browserHarness(async page => {
  await mount(page, draft({ ready: true, target: { source: "smiles", smiles: "CC" }, resolved_target: resolved(), missing_fields: [], errors: [] }))
  await page.evaluate(() => { window.failConfirm = true })
  await page.getByRole("button", { name: "确认并预测", exact: true }).click(); await idle(page)
  assert.match(await page.locator("[data-retrosynthesis-draft]").innerText(), /不会自动重复提交/)
  await page.getByRole("button", { name: "重新载入已保存参数", exact: true }).click(); await idle(page)
  await page.getByRole("button", { name: "继续查询结果", exact: true }).click(); await idle(page)
  assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "confirm", "get", "continue"])
}))
test("candidate result cards stay readable on mobile and retain invalid or unknown flags", browserOptions, async () => browserHarness(async page => {
  const snapshot = job(); snapshot.result.candidates[0].valid_smiles = false; snapshot.result.candidates[0].all_reactants_smaller_than_target = null
  snapshot.result.candidates[0].reaction_hint = '<script>window.injected=true</script>'
  await page.evaluate(async snapshot => { const { renderRetrosynthesisResult } = await import("/results-view.mjs")
    document.querySelector("main").append(renderRetrosynthesisResult(document, { kind: "retrosynthesis", turnId: "user-one", jobId: snapshot.job_id, snapshot })) }, snapshot)
  assert.match(await page.locator("[data-retrosynthesis-candidate]").innerText(), /结构解析：无效/)
  assert.match(await page.locator("[data-retrosynthesis-candidate]").innerText(), /未判断/)
  assert.equal(await page.evaluate(() => window.injected), undefined)
  for (const width of [1280, 360]) { await page.setViewportSize({ width, height: 1000 }); assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true) }
}))

test("conversation controller renders retro jobs and restores only the same-origin draft without submission", browserOptions, async () => browserHarness(async page => {
  const saved = draft(), session = [...messages("prepare_monomer_retrosynthesis", saved), ...messages("get_retrosynthesis_job", job(), { job_id: "retro-job", conversation_id: "ses_current" })]
  const requests = []
  await page.route("**/openscience-api/**", async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.pathname.endsWith("/message")) return route.fulfill({ json: session })
    assert.equal(url.pathname, "/openscience-api/nexpoly/retrosynthesis/draft")
    assert.equal(request.method(), "POST"); assert.equal(request.headers().authorization, undefined)
    const body = request.postDataJSON(); requests.push(body)
    assert.equal(body.session_id, "ses_current"); assert.equal(body.draft_id, ID); assert.equal(body.action, "get")
    return route.fulfill({ json: { ok: true, draft: saved } })
  })
  await page.evaluate(async () => {
    history.replaceState({}, "", "/chat/ses_current")
    const node = document.createElement("div"); node.dataset.messageId = "user-one"
    node.innerHTML = '<div data-component="session-turn"><div data-slot="session-turn-content"><div data-slot="session-turn-response">正常回答</div></div></div>'
    document.querySelector("main").append(node)
    await import("/results-controller.mjs")
    await fetch("/openscience-api/session/ses_current/message", { headers: { "x-openscience-directory": "/openscience/files/general" } })
  })
  await page.locator("[data-retrosynthesis-draft]").waitFor(); await idle(page)
  await page.locator("[data-retrosynthesis-results]").waitFor()
  assert.equal(await page.locator("[data-retrosynthesis-candidate]").count(), 1)
  assert.equal(requests.length, 1); assert.equal(requests[0].action, "get")
}))

test("ambiguous local names show candidates and select a concrete structure without model invocation", browserOptions, async () => browserHarness(async page => {
  await mount(page)
  await page.getByLabel("目标来源", { exact: true }).selectOption("name")
  await page.getByRole("textbox", { name: "目标结构或名称", exact: true }).fill("ambiguous name")
  await page.getByRole("button", { name: "解析 / 搜索结构", exact: true }).click(); await idle(page)
  assert.deepEqual(await page.evaluate(() => window.operations.at(-1).input.target), { source: "name", name: "ambiguous name" })
  await page.getByRole("button", { name: "选择 候选单体", exact: true }).click(); await idle(page)
  assert.deepEqual(await page.evaluate(() => window.savedDraft.target), { source: "smiles", smiles: "CC" })
  assert.equal(await page.evaluate(() => window.operations.some(item => item.action === "confirm")), false)
  assert.equal(await page.getByRole("button", { name: "确认并预测", exact: true }).isEnabled(), true)
}))

test("a restored uncertain submission retries only on explicit user action with its frozen draft revision", browserOptions, async () => browserHarness(async page => {
  await mount(page, draft({ ready: true, editable: false, status: "submission_unknown", submission_error: "SUBMISSION_NOT_RECEIVED",
    target: { source: "smiles", smiles: "CC" }, resolved_target: resolved(), missing_fields: [], errors: [] }))
  assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get"])
  assert.equal(await page.getByRole("textbox", { name: "目标结构或名称", exact: true }).isDisabled(), true)
  assert.match(await page.locator("[data-retrosynthesis-draft]").innerText(), /相同参数和幂等键/)
  await page.getByRole("button", { name: "重试原提交", exact: true }).click(); await idle(page)
  assert.deepEqual(await page.evaluate(() => window.operations.map(item => item.action)), ["get", "confirm"])
  assert.deepEqual(await page.evaluate(() => window.operations.at(-1).input), { draft_id: ID, revision: 1 })
}))
