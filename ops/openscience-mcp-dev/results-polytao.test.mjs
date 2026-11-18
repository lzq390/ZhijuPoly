import assert from "node:assert/strict"
import { createServer } from "node:http"
import { existsSync, readFileSync } from "node:fs"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"
import path from "node:path"
import test from "node:test"
import { extractPolytaoTurns } from "./results-view.mjs"

const DIR = path.dirname(fileURLToPath(import.meta.url))
const candidate = (rank = 1) => ({ rank, generated_smiles: `*${"C".repeat(rank + 1)}*`, valid_smiles: true, sa_score: rank === 1 ? 2.1 : null, warnings: [] })
function snapshot(extra = {}) {
  return { ok: true, request_id: "request-a", job_id: "job-a", status: "completed",
    progress_stage: "completed", progress_percent: 100, created_at: "2026-11-16T01:00:00Z",
    updated_at: "2026-11-16T01:01:00Z", started_at: "2026-11-16T01:00:01Z", finished_at: "2026-11-16T01:01:00Z",
    snapshot_at: "2026-11-16T01:02:00Z", requested_count: 5, returned_count: 3, attempts: 1,
    filter_counter: { duplicate: 2 }, warnings: [], error_code: null,
    results: [candidate()], offset: 0, limit: 1, next_offset: 1, ...extra }
}
function tool(output = snapshot(), input = { job_id: output.job_id, offset: output.offset, limit: output.limit }, name = "get_polytao_job", prefix = "capability_gateway_") {
  return { type: "tool", tool: prefix + name, callID: name + output.snapshot_at,
    state: { status: "completed", input, output: "Authoritative summary\n" + JSON.stringify(output) } }
}
function messages(parts, turnId = "user-a") {
  return [{ info: { role: "user", id: turnId }, parts: [] },
    { info: { role: "assistant", id: "assistant-" + turnId, parentID: turnId }, parts }]
}

test("PolyTAO cards use only completed exact tool JSON and validate query correlation", () => {
  const genuine = tool()
  const forged = { ...genuine, tool: "fake_" + genuine.tool }
  const prose = { type: "text", text: JSON.stringify(snapshot()) }
  assert.deepEqual(extractPolytaoTurns(messages([forged, prose])), [])
  assert.deepEqual(extractPolytaoTurns(messages([{ ...genuine, state: { ...genuine.state, status: "running" } }])), [])
  assert.deepEqual(extractPolytaoTurns(messages([tool(snapshot(), { job_id: "different", limit: 1 })])), [])
  assert.deepEqual(extractPolytaoTurns(messages([tool(snapshot(), { job_id: "job-a", limit: 2 })])), [])
  const model = extractPolytaoTurns(messages([genuine, prose]))[0]
  assert.equal(model.jobId, "job-a")
  assert.equal(model.snapshot.returned_count, 3)
  assert.equal(model.results.length, 1)
})

test("PolyTAO page merge preserves task counts, chronological snapshots, turn and job isolation", () => {
  const olderPage = snapshot({ offset: 1, results: [candidate(2)], next_offset: 2, snapshot_at: "2026-11-16T01:01:30Z" })
  const latestPage = snapshot({ offset: 2, results: [candidate(3)], next_offset: null, snapshot_at: "2026-11-16T01:03:00Z" })
  const first = messages([tool(), tool(latestPage), tool(olderPage)])
  const second = messages([tool(snapshot({ job_id: "job-b" }))], "user-b")
  const third = messages([tool(snapshot())], "user-c")
  const orphan = { info: { role: "assistant", parentID: "absent" }, parts: [tool()] }
  const session = [...first, ...second, ...third, orphan]
  const before = JSON.stringify(session)
  const models = extractPolytaoTurns(session)
  assert.equal(models.length, 3)
  assert.deepEqual(models[0].results.map(item => item.rank), [1, 2, 3])
  assert.equal(models[0].snapshot.snapshot_at, latestPage.snapshot_at)
  assert.equal(models[0].snapshot.returned_count, 3)
  assert.deepEqual(models[2].results.map(item => item.rank), [1])
  assert.equal(JSON.stringify(session), before)
  assert.deepEqual(extractPolytaoTurns(JSON.parse(before)), models, "refresh can reconstruct saved cards")
})

test("PolyTAO ignores late running status and detects conflicting immutable candidates", () => {
  const pending = snapshot({ status: "running", returned_count: 0, results: [], next_offset: null,
    finished_at: null, snapshot_at: "2026-11-16T01:00:30Z" })
  const conflict = snapshot({ results: [{ ...candidate(), generated_smiles: "*NN*" }] })
  const model = extractPolytaoTurns(messages([tool(), tool(pending), tool(conflict)]))[0]
  assert.equal(model.status, "completed")
  assert.equal(model.results[0].generated_smiles, candidate().generated_smiles)
  assert.equal(model.issues.length, 1)
})

test("PolyTAO accepts submitted, zero-result, failure snapshots and rejects malformed candidate pages", () => {
  const submit = { ok: true, job_id: "job-a", status: "pending", replayed: false, effective_parameters: { candidate_count: 3 } }
  const model = extractPolytaoTurns(messages([tool(submit, { idempotency_key: "stable-key", descriptors: {} }, "submit_polytao_generation", "nexpoly_polytao_")]))[0]
  assert.equal(model.requestedCount, 3)
  assert.equal(model.snapshot, null)
  for (const status of ["completed", "failed", "running"]) {
    const empty = snapshot({ status, returned_count: 0, results: [], next_offset: null })
    assert.equal(extractPolytaoTurns(messages([tool(empty)]))[0].results.length, 0)
  }
  for (const mutate of [
    value => { value.returned_count = "3" }, value => { value.results[0].rank = 2 },
    value => { value.results[0].sa_score = "2.1" }, value => { value.results[0].warnings = "warning" },
    value => { value.next_offset = 2 }, value => { value.filter_counter.duplicate = -1 },
    value => { value.snapshot_at = "yesterday" }, value => { value.requested_count = 0 },
  ]) {
    const invalid = snapshot()
    mutate(invalid)
    assert.deepEqual(extractPolytaoTurns(messages([tool(invalid)])), [])
  }
})

const playwrightPaths = [path.join(DIR, "..", "openscience-ui-overlay", "node_modules", "playwright"),
  "/data/lzq/gith/nexpoly-dev/ops/openscience-ui-overlay/node_modules/playwright"]
const playwrightPath = playwrightPaths.find(value => existsSync(path.join(value, "package.json")))

test("PolyTAO browser cards keep task summaries readable at narrow widths and safely render authoritative results", {
  skip: !playwrightPath && "The existing Playwright package is not installed",
}, async () => {
  const { chromium } = createRequire(import.meta.url)(playwrightPath)
  const server = createServer((request, response) => {
    if (["/results-view.mjs", "/results-view.css"].includes(request.url)) {
      response.setHeader("content-type", request.url.endsWith("mjs") ? "text/javascript" : "text/css")
      response.end(readFileSync(path.join(DIR, request.url.slice(1))))
    } else {
      response.setHeader("content-type", "text/html")
      response.end('<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/results-view.css"><main style="padding:12px"></main></html>')
    }
  })
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve))
  let browser
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] })
    const page = await browser.newPage({ viewport: { width: 320, height: 900 } })
    await page.goto(`http://127.0.0.1:${server.address().port}`)
    const long = "*" + "CC(=O)N".repeat(200) + "*"
    const unsafe = '<img src=x onerror="window.injected=true">'
    const jobId = "polytao.60cce249c59943be.6cfe8e0cfe584699aa2d5515c6868744"
    const session = messages([tool(snapshot({ job_id: jobId, requested_count: 5, returned_count: 2, results: [
      { ...candidate(), generated_smiles: long, warnings: [unsafe] }, candidate(2)], limit: 2, next_offset: null }))])
    await page.evaluate(async messages => {
      Object.defineProperty(navigator, "clipboard", { value: { writeText: async value => { window.copied = value } } })
      const { extractPolytaoTurns, renderPolytaoResult } = await import("/results-view.mjs")
      document.querySelector("main").append(renderPolytaoResult(document, extractPolytaoTurns(messages)[0]))
    }, session)
    const card = page.locator(`[data-nexpoly-polytao-job="${jobId}"]`)
    assert.equal(await card.locator("[data-polytao-candidate]").count(), 2)
    assert.match(await card.innerText(), /2 \/ 5/)
    assert.match(await card.innerText(), /查询时间/)
    assert.doesNotMatch(await card.innerText(), /快照|completed/)
    assert.equal(await card.locator("time").getAttribute("datetime"), "2026-11-16T01:02:00Z")
    assert.equal(await card.locator("details").getAttribute("open"), null)
    assert.ok(!(await card.innerText()).includes(jobId), "Long task identifiers stay in collapsed task details")
    assert.match(await card.innerText(), /部分候选/)
    assert.match(await card.innerText(), /SA score：未提供/)
    assert.equal(await card.locator("img,script,a").count(), 0)
    assert.equal(await card.locator("[data-smiles]").first().textContent(), long)
    await card.getByRole("button", { name: "复制候选 1 的 SMILES" }).click()
    assert.equal(await page.evaluate(() => window.copied), long)
    assert.match(await card.getByRole("status").first().textContent(), /已复制/)
    for (const { width, container } of [{ width: 320 }, { width: 452 }, { width: 768 },
      { width: 1440 }, { width: 1440, container: 280 }]) {
      await page.setViewportSize({ width, height: 900 })
      await page.locator("main").evaluate((node, container) => { node.style.width = container ? `${container}px` : "" }, container)
      const layout = await card.evaluate(node => {
        const box = node.getBoundingClientRect()
        const summaries = [...node.querySelectorAll(".nexpoly-polytao-metric dt, .nexpoly-polytao-metric dd")]
        return { width: node.clientWidth, scroll: node.scrollWidth, right: box.right,
          summaries: summaries.map(item => ({ text: item.textContent, height: item.getBoundingClientRect().height,
            width: item.getBoundingClientRect().width, fontSize: parseFloat(getComputedStyle(item).fontSize) })) }
      })
      assert.ok(layout.scroll <= layout.width + 1, `No result overflow at ${width}px in ${container ?? "full-width"} container`)
      assert.ok(layout.right <= width)
      for (const metric of layout.summaries) {
        assert.ok(metric.width >= 80, `${metric.text} must have a readable column at ${width}px`)
        assert.ok(metric.height <= metric.fontSize * 3.3, `${metric.text} must not wrap vertically at ${width}px`)
      }
    }
    await card.locator("details > summary").click()
    assert.ok((await card.innerText()).includes(jobId), "The complete task ID remains available on demand")
    assert.match(await card.innerText(), /执行阶段\s+已完成/)
    const replay = messages([tool({ ok: true, job_id: "replayed-job", status: "completed", replayed: true,
      effective_parameters: { candidate_count: 3 } }, { idempotency_key: "stable-key", descriptors: {} }, "submit_polytao_generation", "nexpoly_polytao_")])
    await page.evaluate(async messages => {
      const { extractPolytaoTurns, renderPolytaoResult } = await import("/results-view.mjs")
      document.querySelector("main").append(renderPolytaoResult(document, extractPolytaoTurns(messages)[0]))
    }, replay)
    const replayCard = page.locator('[data-nexpoly-polytao-job="replayed-job"]')
    assert.match(await replayCard.innerText(), /候选结果尚未读取/)
    assert.match(await replayCard.innerText(), /待确认 \/ 3/)
    assert.equal(await replayCard.locator("time").count(), 0, "Submission acceptance does not invent a query time")
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false)
  } finally {
    await browser?.close()
    await new Promise(resolve => server.close(resolve))
  }
})

test("gateway and legacy PolyTAO history use exact tool aliases without classifying unrelated gateway calls", () => {
  assert.equal(extractPolytaoTurns(messages([tool(snapshot(), undefined, "get_polytao_job", "nexpoly_polytao_")])).length, 1)
  for (const name of ["search_polymers", "polymerize_monomers", "prepare_polytao_generation_extra", "submit_polytao_generation"]) {
    assert.deepEqual(extractPolytaoTurns(messages([tool(snapshot(), undefined, name)])), [])
  }
})
