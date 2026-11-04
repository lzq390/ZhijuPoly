#!/usr/bin/env node
// Read-only acceptance of real development UI results from existing sessions.
// No prompts are submitted and no model-written JSON is used as display truth.
import assert from "node:assert/strict"
import { createRequire } from "node:module"
import { existsSync } from "node:fs"
import { mkdir, writeFile, chmod } from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const ORIGIN = "http://127.0.0.1:19111"
const PRIMARY = { sessionId: "ses_e60eb9693ffei3Y8AvJIX0I20T", userId: "msg_19f146975001OaPrwFeQE8C1Ne", count: 5026 }
const OTHER = { sessionId: "ses_e60fa0a7bffeWAkIwUeY6JjKyR", userId: "msg_19f05f58c0016CILrMooSS8ZM5", count: 18614 }
const PREFIX = "nexpoly_property_filter_"
const NAMES = ["list_filter_properties", "search_polymers", "get_property_histogram"]
const PANEL = "[data-nexpoly-results]"
const format = value => new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 6 }).format(value)
const compact = value => value.replace(/[,，\s]/g, "")

function config(argv) {
  const result = { outputDir: path.join(ROOT, ".runtime", "openscience-mcp-dev", "results-ui-evidence",
    new Date().toISOString().replace(/[:.]/g, "-")), checkConfig: false }
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--check-config") result.checkConfig = true
    else if (argv[index] === "--output-dir" && argv[index + 1]) result.outputDir = path.resolve(argv[++index])
    else throw new Error("Unknown or incomplete argument")
  }
  assert.ok(result.outputDir.startsWith(path.join(ROOT, ".runtime") + path.sep), "Evidence must remain in private .runtime")
  return result
}

function parseOutput(part) {
  assert.equal(typeof part.state?.output, "string", "Missing actual tool output")
  const raw = part.state.output.trim()
  for (const candidate of [raw, raw.slice(raw.lastIndexOf("\n") + 1).trim()]) {
    try {
      const value = JSON.parse(candidate)
      if (value?.ok === true && typeof value.dataset_revision === "string") return value
    } catch { /* The tool's summary is not the structured payload. */ }
  }
  throw new Error("The completed MCP call has no successful structured output")
}

function expected(messages, target) {
  assert.equal(messages.filter(message => message.info?.role === "user" && message.info.id === target.userId).length, 1)
  const tools = messages.filter(message => message.info?.role === "assistant" && message.info.parentID === target.userId)
    .flatMap(message => (message.parts ?? []).filter(part => part.type === "tool"))
  assert.ok(tools.every(part => NAMES.some(name => part.tool === PREFIX + name) && part.state?.status === "completed"))
  const calls = Object.fromEntries(NAMES.map(name => [name, [...tools].reverse().find(part => part.tool === PREFIX + name)]))
  assert.ok(NAMES.every(name => calls[name]), "The existing user turn is missing a required actual MCP call")
  const output = Object.fromEntries(NAMES.map(name => [name, parseOutput(calls[name])]))
  const revision = output.list_filter_properties.dataset_revision
  for (const name of ["search_polymers", "get_property_histogram"]) {
    assert.equal(calls[name].state.input.dataset_revision, revision)
    assert.equal(output[name].dataset_revision, revision)
  }
  const search = output.search_polymers
  const histogram = output.get_property_histogram
  assert.equal(search.matched_material_count, target.count)
  assert.equal(search.results.length, 2)
  assert.equal(histogram.histogram.total_count, 45160)
  return { ...target, revision, search, histogram, calls }
}

async function main(options) {
  assert.ok(Number(process.versions.node.split(".")[0]) >= 20, "Use Node 20 or newer")
  const packagePath = [path.join(ROOT, "ops/openscience-ui-overlay/node_modules/playwright"),
    "/data/lzq/gith/nexpoly-dev/ops/openscience-ui-overlay/node_modules/playwright"]
    .find(candidate => existsSync(path.join(candidate, "package.json")))
  assert.ok(packagePath, "The installed Playwright package is unavailable")
  const { chromium } = createRequire(import.meta.url)(packagePath)
  process.umask(0o077)
  await mkdir(options.outputDir, { recursive: true, mode: 0o700 })
  await chmod(options.outputDir, 0o700)
  const reportPath = path.join(options.outputDir, "report.json")
  const report = { ok: false, readOnly: true, dataSource: "persisted_completed_mcp_tool_outputs",
    startedAt: new Date().toISOString(), origin: ORIGIN, attemptedMutations: [], blockedOrigins: [],
    browserErrors: [], checks: [], screenshots: [] }
  const requestMetadata = new Map()
  let browser
  let page
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, locale: "zh-CN", serviceWorkers: "block" })
    await context.route("**/*", async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.origin !== ORIGIN || request.method() !== "GET") {
        if (url.origin !== ORIGIN && !report.blockedOrigins.includes(url.origin)) report.blockedOrigins.push(url.origin)
        if (request.method() !== "GET") report.attemptedMutations.push({ method: request.method(), path: url.pathname })
        await route.abort("blockedbyclient")
        return
      }
      const match = url.pathname.match(/\/session\/([^/]+)\/message$/)
      if (match) {
        const headers = await request.allHeaders()
        const directory = headers["x-openscience-directory"]
        if (directory) requestMetadata.set(match[1], { url: url.href, directory })
      }
      await route.continue()
    })
    page = await context.newPage()
    page.on("pageerror", error => report.browserErrors.push({ name: error.name }))
    async function readJson(url, directory) {
      assert.equal(new URL(url).origin, ORIGIN)
      const response = await context.request.get(url, { headers: { "x-openscience-directory": directory }, timeout: 10000, maxRedirects: 0 })
      assert.equal(response.status(), 200, "Existing-session verification did not return HTTP 200")
      return response.json()
    }
    async function loadExpected(target) {
      const deadline = Date.now() + 15000
      while (!requestMetadata.has(target.sessionId) && Date.now() < deadline) await page.waitForTimeout(100)
      const metadata = requestMetadata.get(target.sessionId)
      assert.ok(metadata, "No actual UI request supplied the selected session directory")
      return expected(await readJson(metadata.url, metadata.directory), target)
    }
    async function screenshot(name, element) {
      const filename = path.join(options.outputDir, name + ".png")
      if (element) await element.screenshot({ path: filename })
      else await page.screenshot({ path: filename, fullPage: false })
      await chmod(filename, 0o600)
      report.screenshots.push(filename)
    }
    async function verifyPanel(data, label, mobile = false) {
      const panel = page.locator(`${PANEL}[data-turn-id="${data.userId}"]`)
      await panel.waitFor({ state: "visible", timeout: 30000 })
      assert.equal(await page.locator(PANEL).count(), 1, "Another session's result panel remains mounted")
      assert.equal(await panel.getAttribute("data-dataset-revision"), data.revision)
      assert.equal(await panel.evaluate(element => element.closest("[data-message-id]")?.getAttribute("data-message-id")), data.userId)
      const host = panel.locator("xpath=ancestor::*[@data-nexpoly-results-host][1]")
      assert.equal(await host.getAttribute("data-session-id"), data.sessionId)
      assert.equal(await host.getAttribute("data-turn-id"), data.userId)
      const response = page.locator(`[data-message-id="${data.userId}"] [data-slot="session-turn-response"]`)
      const toggle = host.locator("[data-nexpoly-original-toggle]")
      if (data.sessionId === PRIMARY.sessionId) {
        await response.waitFor({ state: "hidden", timeout: 5000 })
        assert.equal(await toggle.getAttribute("aria-expanded"), "false")
        await toggle.click()
        await response.waitFor({ state: "visible", timeout: 5000 })
        assert.equal(await toggle.getAttribute("aria-expanded"), "true")
        await toggle.click()
        await response.waitFor({ state: "hidden", timeout: 5000 })
      } else {
        assert.equal(await toggle.count(), 0, "A normal prose answer was unnecessarily hidden")
        assert.equal(await response.isVisible(), true)
      }
      const summary = await panel.locator("[data-result-summary]").innerText()
      assert.ok(compact(summary).includes(String(data.search.matched_material_count)))
      assert.ok(summary.includes("不同条件可能由不同样本或实验满足"))
      const cards = panel.locator("[data-material-card]")
      assert.equal(await cards.count(), data.search.results.length)
      const cardChecks = []
      for (const [index, material] of data.search.results.entries()) {
        const card = cards.nth(index)
        assert.equal(await card.locator("[data-smiles]").textContent(), material.canonical_smiles || material.smiles)
        const materialRef = await card.getAttribute("data-material-ref")
        if (materialRef !== null) assert.equal(materialRef, material.material_ref)
        const evidenceNodes = card.locator("[data-filter-evidence]")
        assert.equal(await evidenceNodes.count(), material.evidence.length)
        for (const [evidenceIndex, evidence] of material.evidence.entries()) {
          const evidenceNode = evidenceNodes.nth(evidenceIndex)
          const rows = evidenceNode.locator("[data-measurement]")
          assert.equal(await rows.count(), evidence.measurements.length)
          const note = evidenceNode.locator("[data-evidence-truncation]")
          assert.equal(await note.getAttribute("data-evidence-truncation"), String(evidence.truncated))
          assert.ok((await note.innerText()).includes(evidence.truncated ? "证据已截断" : "证据已全部展示"))
          for (const [measurementIndex, measurement] of evidence.measurements.entries()) {
            const row = rows.nth(measurementIndex)
            const text = await row.innerText()
            assert.ok(text.includes(measurement.property_value), "The original measurement value was lost")
            const origins = { observed: "实测值", median: "中位数值", imputed: "填补值" }
            assert.ok(text.includes(origins[measurement.value_origin] ?? measurement.value_origin))
            if (measurement.label_source === "exp") assert.ok(text.includes("实验数据"))
            const recordRef = await row.getAttribute("data-record-ref")
            if (recordRef !== null) assert.equal(recordRef, measurement.record_ref)
          }
        }
        cardChecks.push({ materialRef: material.material_ref, completeSmiles: true,
          measurementCount: material.evidence.reduce((sum, item) => sum + item.measurements.length, 0),
          truncated: material.evidence.map(item => item.truncated) })
      }
      const figure = panel.locator("[data-property-histogram]")
      assert.equal(await figure.count(), 1)
      const figureText = await figure.innerText()
      assert.ok(figureText.includes("全库测量分布") && figureText.includes("未应用本次材料筛选条件"))
      assert.ok(compact(figureText).includes(String(data.histogram.histogram.total_count)))
      const bars = figure.locator("svg[data-histogram-chart] rect[data-histogram-bin]")
      const geometry = await bars.evaluateAll(elements => elements.map(element => ({
        index: Number(element.getAttribute("data-bin-index")), count: Number(element.getAttribute("data-count")),
        height: element.getBBox().height, title: element.querySelector("title")?.textContent,
      })))
      const hist = data.histogram.histogram
      assert.equal(geometry.length, hist.counts.length)
      const tallest = Math.max(...geometry.map(bar => bar.height))
      const largest = Math.max(...hist.counts)
      for (const [index, bar] of geometry.entries()) {
        assert.equal(bar.index, index)
        assert.equal(bar.count, hist.counts[index])
        assert.ok(Math.abs(bar.height / tallest - bar.count / largest) < 0.002, "SVG bar heights do not represent the actual counts")
        assert.ok(bar.title.includes(format(bar.count) + " 条测量"))
      }
      const tooltip = figure.locator("[data-histogram-tooltip]")
      if (!mobile) {
        await bars.nth(0).hover()
        assert.equal(await tooltip.innerText(), geometry[0].title)
        await bars.nth(0).focus()
        await page.keyboard.press("Tab")
        assert.equal(await tooltip.innerText(), geometry[1].title)
        assert.equal(await bars.nth(1).evaluate(element => element === document.activeElement), true)
      }
      const provenance = panel.locator("details[data-result-provenance]")
      if (!(await provenance.evaluate(element => element.open))) await provenance.locator("summary").click()
      const provenanceText = await provenance.innerText()
      assert.ok(provenanceText.includes(data.revision))
      for (const material of data.search.results) for (const evidence of material.evidence) {
        for (const record of evidence.measurements) assert.ok(provenanceText.includes(record.record_ref))
      }
      await provenance.locator("summary").click()
      if (mobile) {
        const bounds = await panel.evaluate(element => {
          const rect = element.getBoundingClientRect()
          return { left: rect.left, right: rect.right, viewport: innerWidth,
            smiles: [...element.querySelectorAll("[data-smiles]")].map(code => ({ width: code.clientWidth, scrollWidth: code.scrollWidth })) }
        })
        assert.ok(bounds.left >= -1 && bounds.right <= bounds.viewport + 1, "The result panel overflows the phone viewport")
        assert.ok(bounds.smiles.every(code => code.scrollWidth <= code.width + 1), "Complete SMILES do not wrap on mobile")
      }
      await panel.scrollIntoViewIfNeeded()
      await screenshot(label)
      if (label === "desktop") {
        await figure.evaluate(element => element.scrollIntoView({ block: "center" }))
        await screenshot("histogram", figure)
        await cards.first().evaluate(element => element.scrollIntoView({ block: "center" }))
        await screenshot("material-card", cards.first())
      }
      report.checks.push({ label, sessionId: data.sessionId, userMessageId: data.userId,
        datasetRevision: data.revision, matchedMaterialCount: data.search.matched_material_count,
        cards: cardChecks, histogramTotal: hist.total_count, histogramCounts: geometry.map(bar => bar.count),
        truthfulSvgHeights: true, tooltipHoverAndKeyboard: !mobile, fullProvenance: true,
        originalAnswer: data.sessionId === PRIMARY.sessionId ? "json_hidden_by_default_and_toggle_verified" : "prose_visible", mobile })
    }
    await page.goto(`${ORIGIN}/chat/${PRIMARY.sessionId}`, { waitUntil: "domcontentloaded", timeout: 30000 })
    const primary = await loadExpected(PRIMARY)
    await verifyPanel(primary, "desktop")
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 })
    await verifyPanel(await loadExpected(PRIMARY), "refreshed")
    const metadata = requestMetadata.get(PRIMARY.sessionId)
    const otherInfo = await readJson(`${ORIGIN}/openscience-api/session/${OTHER.sessionId}`, metadata.directory)
    const title = page.getByText(otherInfo.title, { exact: true })
    assert.equal(await title.count(), 1, "The existing history entry is ambiguous")
    await title.click()
    await page.waitForFunction(id => location.pathname.endsWith("/" + id), OTHER.sessionId, { timeout: 15000 })
    await verifyPanel(await loadExpected(OTHER), "other-session")
    assert.equal(await page.locator(`${PANEL}[data-turn-id="${PRIMARY.userId}"]`).count(), 0)
    await page.goBack({ waitUntil: "domcontentloaded", timeout: 30000 })
    await page.waitForFunction(id => location.pathname.endsWith("/" + id), PRIMARY.sessionId, { timeout: 15000 })
    await verifyPanel(primary, "returned-session")
    await page.setViewportSize({ width: 390, height: 844 })
    await verifyPanel(primary, "mobile", true)
    assert.equal(report.attemptedMutations.length, 0, "The UI attempted a mutation during read-only verification")
    assert.equal(report.browserErrors.length, 0, "The UI produced a browser exception")
    report.ok = true
  } catch (error) {
    report.failure = { name: error.name, message: error.message }
    if (page) {
      const filename = path.join(options.outputDir, "failure.png")
      try { await page.screenshot({ path: filename, timeout: 10000 }); await chmod(filename, 0o600); report.screenshots.push(filename) } catch {}
    }
  } finally {
    report.finishedAt = new Date().toISOString()
    await writeFile(reportPath, JSON.stringify(report, null, 2) + "\n", { mode: 0o600 })
    await chmod(reportPath, 0o600)
    if (browser) await browser.close()
  }
  console.log(JSON.stringify({ ok: report.ok, report: reportPath, checks: report.checks.map(check => check.label), screenshots: report.screenshots }))
  if (!report.ok) process.exitCode = 1
}

try {
  const options = config(process.argv.slice(2))
  if (options.checkConfig) console.log(JSON.stringify({ ok: true, origin: ORIGIN, readOnly: true, sessions: [PRIMARY.sessionId, OTHER.sessionId] }))
  else await main(options)
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: error.message }))
  process.exitCode = 1
}
