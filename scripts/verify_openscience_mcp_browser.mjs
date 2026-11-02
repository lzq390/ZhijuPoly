#!/usr/bin/env node
// Real UI acceptance probe for the isolated OpenScience development instance.
// Requires Node >= 20 and the already installed Playwright package. It never
// submits a prompt through the API and never supplies synthetic tool results.

import assert from "node:assert/strict"
import { createHash, randomBytes } from "node:crypto"
import { existsSync } from "node:fs"
import { chmod, mkdir, writeFile } from "node:fs/promises"
import { createRequire } from "node:module"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const TOOL_NAMES = ["list_filter_properties", "search_polymers", "get_property_histogram"]
const TOOL_PREFIX = "nexpoly_property_filter_"
const INPUT = '[data-component="prompt-input"][role="textbox"][contenteditable="true"]'
const RESPONSE = '[data-slot="session-turn-markdown"]'

function options(argv) {
  const result = {
    baseUrl: "http://127.0.0.1:19111/",
    outputDir: path.join(ROOT, ".runtime", "openscience-mcp-dev", "browser-evidence",
      new Date().toISOString().replace(/[:.]/g, "-")),
    timeoutMs: 240000,
    agent: "property-filter",
    playwrightPackage: undefined,
    checkConfig: false,
  }
  const names = { "--base-url": "baseUrl", "--output-dir": "outputDir", "--timeout-ms": "timeoutMs",
    "--agent": "agent", "--playwright-package": "playwrightPackage" }
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--check-config") {
      result.checkConfig = true
      continue
    }
    const name = names[argv[index]]
    if (!name || !argv[index + 1]) throw new Error("Unknown or incomplete argument")
    result[name] = argv[++index]
  }
  const url = new URL(result.baseUrl)
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname)
      || url.port !== "19111" || url.pathname !== "/" || url.search || url.hash || url.username || url.password) {
    throw new Error("Only the isolated loopback OpenScience UI on port 19111 is permitted")
  }
  result.baseUrl = url.origin + "/"
  result.timeoutMs = Number(result.timeoutMs)
  if (!Number.isInteger(result.timeoutMs) || result.timeoutMs < 10000 || result.timeoutMs > 360000) {
    throw new Error("timeout-ms must be between 10000 and 360000")
  }
  if (result.agent !== "property-filter") throw new Error("Only the isolated property-filter agent is permitted")
  result.outputDir = path.resolve(result.outputDir)
  if (!result.outputDir.startsWith(path.join(ROOT, ".runtime") + path.sep)) {
    throw new Error("Browser evidence must remain in this worktree's private .runtime directory")
  }
  return result
}

function playwright(packagePath) {
  if (Number(process.versions.node.split(".")[0]) < 20) throw new Error("Use Node 20 or newer")
  const candidates = packagePath ? [path.resolve(packagePath)] : [
    path.join(ROOT, "ops", "openscience-ui-overlay", "node_modules", "playwright"),
    "/data/lzq/gith/nexpoly-dev/ops/openscience-ui-overlay/node_modules/playwright",
  ]
  const selected = candidates.find(candidate => existsSync(path.join(candidate, "package.json")))
  if (!selected) throw new Error("The preinstalled Playwright package was not found")
  return createRequire(import.meta.url)(selected)
}

function digest(value) {
  return createHash("sha256").update(value).digest("hex")
}

function logicalTool(name) {
  return TOOL_NAMES.find(tool => name === TOOL_PREFIX + tool)
}

function inspectMessages(messages, scope) {
  assert.ok(Array.isArray(messages), "Session message endpoint did not return a message array")
  const users = messages.filter(message => message.info?.role === "user" && (message.parts ?? []).some(
    part => part.type === "text" && part.text?.includes(scope.marker)))
  assert.ok(users.length <= 1, "The acceptance marker belongs to more than one user message")
  const user = users[0]
  if (user) assert.equal(typeof user.info.id, "string", "The acceptance user message has no stable message ID")
  if (user && scope.userMessageId) {
    assert.equal(user.info.id, scope.userMessageId, "The marker does not belong to the UI-submitted message")
  }
  const assistant = user ? messages.filter(message => message.info?.role === "assistant"
    && message.info?.parentID === user.info.id) : []
  const tools = assistant.flatMap(message => (message.parts ?? []).filter(part => part.type === "tool"))
  const finalMessage = [...assistant].reverse().find(message => (message.parts ?? []).some(
    part => part.type === "text" && part.text?.trim()))
  const finalText = (finalMessage?.parts ?? []).filter(part => part.type === "text").map(part => part.text).join("\n")
  return { tools, finalText, finalMessage,
    modelErrors: assistant.map(message => message.info?.error).filter(Boolean),
    userMessageId: user?.info.id, messageCount: assistant.length + (user ? 1 : 0) }
}

function toolSummary(part) {
  const output = typeof part.state?.output === "string" ? part.state.output : JSON.stringify(part.state?.output ?? null)
  const structured = structuredOutput(part)
  const input = part.state?.input ?? null
  const started = part.state?.time?.start
  const ended = part.state?.time?.end
  return { name: part.tool, logicalName: logicalTool(part.tool), callId: part.callID, assistantMessageId: part.messageID,
    status: part.state?.status, ok: structured?.ok, errorCode: structured?.error?.code,
    inputDatasetRevision: typeof input?.dataset_revision === "string" ? input.dataset_revision : null,
    outputDatasetRevision: structured?.dataset_revision,
    durationMs: Number.isFinite(started) && Number.isFinite(ended) ? ended - started : undefined,
    inputBytes: Buffer.byteLength(JSON.stringify(input)),
    outputBytes: Buffer.byteLength(output), outputSha256: digest(output) }
}

function structuredOutput(part) {
  const output = part?.state?.output
  if (typeof output !== "string") return undefined
  // The development adapter appends the actual MCP structuredContent as one
  // JSON text block; its human-readable summary may precede that block.
  for (const candidate of [output.trim(), ...output.split("\n").reverse().map(line => line.trim())]) {
    if (!candidate.startsWith("{")) continue
    try {
      const parsed = JSON.parse(candidate)
      if (parsed && typeof parsed === "object" && typeof parsed.ok === "boolean") return parsed
    } catch { /* Other lines are the original tool's descriptive text. */ }
  }
  return undefined
}

async function privateJson(filename, value) {
  await writeFile(filename, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 })
  await chmod(filename, 0o600)
}

async function main(config) {
  const { chromium } = playwright(config.playwrightPackage)
  process.umask(0o077)
  await mkdir(config.outputDir, { recursive: true, mode: 0o700 })
  await chmod(config.outputDir, 0o700)
  const reportPath = path.join(config.outputDir, "report.json")
  const marker = "nexpoly-mcp-ui-" + randomBytes(6).toString("hex")
  const prompt = [
    `这是独立开发环境的真实只读工具验收，标记 ${marker}。请实际调用已连接的 NexPoly 数据库筛选 MCP，禁止编造工具结果。`,
    "请依次完成三步：",
    "1. 调用 list_filter_properties，选择目录中一个有数值范围的属性（优先 Tg），记住真实 option_key、单位和 dataset_revision。",
    "2. 用该目录返回的 min_value 作为下界，调用 search_polymers，page=1、page_size=2、measurement_limit_per_filter=1，必须传入目录返回的 dataset_revision。",
    "3. 用相同 option_key 和 dataset_revision 调用 get_property_histogram。",
    "仅允许这三个 MCP 工具；不要使用 shell、文件、网页或其他工具。请用中文展示实际属性/单位、匹配材料数、最多两个结果、测量来源/截断情况和直方图样本数，并原样完整展示三个结果共用的 dataset_revision；说明跨性能 AND 不保证同一实验条件。",
    `最后单独一行写验收标记：${marker}。`,
  ].join("\n")
  const report = { ok: false, baseUrl: config.baseUrl, startedAt: new Date().toISOString(), marker,
    evidence: { promptSubmittedThroughUi: false, allThreeToolsCompleted: false, responseRendered: false, refreshPreservedResponse: false },
    toolCalls: [], browserErrors: [], blockedOrigins: [], promptRequests: 0 }
  let browser
  let page
  let context
  let promptRequest
  let unsafePrompt
  let inspected
  try {
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] })
    context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, locale: "en-US", serviceWorkers: "block" })
    const origin = new URL(config.baseUrl).origin
    await context.route("**/*", async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (["http:", "https:", "ws:", "wss:"].includes(url.protocol) && url.origin !== origin) {
        if (!report.blockedOrigins.includes(url.origin)) report.blockedOrigins.push(url.origin)
        await route.abort("blockedbyclient")
        return
      }
      if (request.method() === "POST" && /\/session\/[^/]+\/(message|prompt_async)$/.test(url.pathname)) {
        let body
        try { body = request.postDataJSON() } catch { body = null }
        if (body?.agent !== config.agent || !JSON.stringify(body?.parts ?? []).includes(marker)) {
          unsafePrompt = "The UI attempted a prompt outside the dedicated agent or acceptance marker"
          await route.abort("blockedbyclient")
          return
        }
        report.promptRequests += 1
        if (report.promptRequests > 1) {
          unsafePrompt = "The UI attempted more than one acceptance prompt"
          await route.abort("blockedbyclient")
          return
        }
        promptRequest = { url: url.href, body }
      }
      await route.continue()
    })
    page = await context.newPage()
    page.on("pageerror", error => report.browserErrors.push({ type: "pageerror", name: error.name }))
    await page.goto(config.baseUrl, { waitUntil: "domcontentloaded", timeout: 30000 })
    const input = page.locator(INPUT)
    await input.waitFor({ state: "visible", timeout: 45000 })
    await input.fill(prompt)
    const submit = input.locator("xpath=ancestor::form").locator('button[type="submit"]')
    await submit.click({ timeout: 15000 })
    const deadline = Date.now() + config.timeoutMs
    while (!promptRequest && !unsafePrompt && Date.now() < deadline) await page.waitForTimeout(250)
    assert.equal(unsafePrompt, undefined, unsafePrompt)
    assert.ok(promptRequest, "The UI did not submit an acceptance prompt")
    report.evidence.promptSubmittedThroughUi = true
    const sentUrl = new URL(promptRequest.url)
    const matchedPath = sentUrl.pathname.match(/^(.*\/session\/)([^/]+)\/(message|prompt_async)$/)
    assert.ok(matchedPath, "Could not identify the UI-created session")
    report.sessionId = decodeURIComponent(matchedPath[2])
    report.agent = promptRequest.body.agent
    report.model = promptRequest.body.model
    const messageScope = { marker, userMessageId: promptRequest.body.messageID }
    const messagesUrl = new URL(sentUrl)
    messagesUrl.pathname = matchedPath[1] + matchedPath[2] + "/message"
    const statusUrl = new URL(sentUrl)
    statusUrl.pathname = matchedPath[1] + "status"
    async function readJson(url) {
      assert.equal(url.origin, origin, "Read-only verification left the isolated UI origin")
      const response = await context.request.get(url.href, { timeout: 10000, maxRedirects: 0 })
      assert.equal(response.status(), 200, "Read-only session verification did not return HTTP 200")
      return response.json()
    }
    let completed = false
    let idleIncomplete = 0
    while (Date.now() < deadline) {
      inspected = inspectMessages(await readJson(messagesUrl), messageScope)
      report.toolCalls = inspected.tools.map(toolSummary)
      const unexpected = inspected.tools.filter(part => !logicalTool(part.tool))
      assert.equal(unexpected.length, 0, "The model used a tool outside the three-tool acceptance scope")
      const failed = inspected.tools.filter(part => part.state?.status === "error")
      assert.equal(failed.length, 0, "A real MCP tool call failed; inspect the private development session")
      const completedTools = new Set(inspected.tools.filter(part => part.state?.status === "completed"
        && structuredOutput(part)?.ok === true).map(part => logicalTool(part.tool)))
      const status = await readJson(statusUrl)
      const idle = !status[report.sessionId] || status[report.sessionId].type === "idle"
      const done = inspected.finalMessage?.info?.time?.completed
      if (TOOL_NAMES.every(tool => completedTools.has(tool)) && inspected.finalText.includes(marker) && idle && done) {
        completed = true
        break
      }
      report.modelErrors = inspected.modelErrors.map(error => ({ name: error.name,
        statusCode: Number.isInteger(error.data?.statusCode) ? error.data.statusCode : undefined,
        isRetryable: typeof error.data?.isRetryable === "boolean" ? error.data.isRetryable : undefined }))
      assert.equal(inspected.modelErrors.length, 0, "The model returned an error; inspect the private development session")
      idleIncomplete = idle && done && inspected.finalText.trim() ? idleIncomplete + 1 : 0
      assert.ok(idleIncomplete < 3,
        "The model finished without all three successful MCP calls and the acceptance marker")
      await page.waitForTimeout(1000)
    }
    assert.ok(completed, "Timed out waiting for all three real tool calls and the completed model response")
    report.evidence.allThreeToolsCompleted = true
    report.userMessageId = inspected.userMessageId
    report.messageCount = inspected.messageCount
    report.assistantResponse = { bytes: Buffer.byteLength(inspected.finalText), sha256: digest(inspected.finalText) }
    const actualCalls = Object.fromEntries(TOOL_NAMES.map(tool => [tool,
      [...inspected.tools].reverse().find(part => logicalTool(part.tool) === tool && structuredOutput(part)?.ok === true)]))
    const actualOutputs = Object.fromEntries(TOOL_NAMES.map(tool => [tool, structuredOutput(actualCalls[tool])]))
    const revision = actualOutputs.list_filter_properties.dataset_revision
    assert.equal(typeof revision, "string", "Catalog response did not contain a real dataset revision")
    assert.equal(actualOutputs.search_polymers.dataset_revision, revision, "Search used a different dataset revision")
    assert.equal(actualOutputs.get_property_histogram.dataset_revision, revision, "Histogram used a different dataset revision")
    assert.ok(actualOutputs.search_polymers.results.length <= 2, "Search exceeded the requested two-material sample")
    assert.equal(actualOutputs.search_polymers.measurement_limit_per_filter, 1, "Search did not apply the evidence limit")
    assert.ok(inspected.finalText.includes(revision), "The visible answer omitted the complete dataset revision")
    for (const tool of ["search_polymers", "get_property_histogram"]) {
      const call = actualCalls[tool]
      assert.equal(call.state?.input?.dataset_revision, revision, "A dependent tool did not receive the catalog revision")
    }
    const searchInput = actualCalls.search_polymers.state.input
    const histogramInput = actualCalls.get_property_histogram.state.input
    assert.equal(searchInput.filters?.length, 1, "The requested search must use exactly one catalog option")
    const condition = searchInput.filters[0]
    assert.equal(condition.option_key, histogramInput.option_key, "Search and histogram used different options")
    assert.equal(actualOutputs.get_property_histogram.option_key, condition.option_key, "Histogram returned a different option")
    const selectedOption = actualOutputs.list_filter_properties.options.find(option => option.option_key === condition.option_key)
    assert.ok(selectedOption, "The searched option was not returned by this prompt's catalog call")
    assert.ok(Number.isFinite(selectedOption.min_value), "The selected catalog option lacks a numeric lower bound")
    assert.equal(condition.min_value, selectedOption.min_value, "Search did not use the catalog's exact lower bound")
    assert.ok(condition.max_value == null, "Search added an unrequested upper bound")
    assert.equal(searchInput.q ?? "", "", "Search added an unrequested keyword constraint")
    assert.equal(searchInput.page ?? 1, 1, "Search did not request the first page")
    assert.equal(searchInput.page_size ?? 10, 2, "Search did not request two materials")
    assert.equal(searchInput.measurement_limit_per_filter ?? 1, 1, "Search input did not limit evidence to one measurement")
    assert.equal(actualOutputs.search_polymers.page, 1, "Search returned a different page")
    assert.equal(actualOutputs.search_polymers.page_size, 2, "Search returned a different page size")
    report.verifiedData = { datasetRevision: revision,
      optionKey: condition.option_key, label: selectedOption.label, unit: selectedOption.unit,
      minValue: condition.min_value, page: 1, pageSize: 2,
      matchedMaterialCount: actualOutputs.search_polymers.matched_material_count,
      returnedMaterialCount: actualOutputs.search_polymers.results.length,
      histogramCount: actualOutputs.get_property_histogram.histogram.total_count }
    await page.waitForFunction(({ selector, marker, revision }) => [...document.querySelectorAll(selector)].some(
      element => element.textContent?.includes(marker) && element.textContent?.includes(revision)),
      { selector: RESPONSE, marker, revision }, { timeout: 20000 })
    report.evidence.responseRendered = true
    await page.locator(RESPONSE).last().scrollIntoViewIfNeeded()
    const screenshot = path.join(config.outputDir, "conversation.png")
    await page.screenshot({ path: screenshot, fullPage: false })
    await chmod(screenshot, 0o600)
    report.screenshot = screenshot
    const responseScreenshot = path.join(config.outputDir, "response.png")
    await page.locator(RESPONSE).last().screenshot({ path: responseScreenshot })
    await chmod(responseScreenshot, 0o600)
    report.responseScreenshot = responseScreenshot
    report.sessionUrl = page.url()
    assert.equal(new URL(report.sessionUrl).origin, origin)
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 })
    await page.waitForFunction(({ selector, marker }) => [...document.querySelectorAll(selector)].some(
      element => element.textContent?.includes(marker)), { selector: RESPONSE, marker }, { timeout: 30000 })
    const reloaded = inspectMessages(await readJson(messagesUrl), messageScope)
    assert.equal(digest(reloaded.finalText), report.assistantResponse.sha256, "Saved response changed after refresh")
    report.evidence.refreshPreservedResponse = true
    report.ok = true
  } catch (error) {
    report.failure = { name: error.name, message: error.message }
    if (page) {
      if (new URL(page.url()).origin === new URL(config.baseUrl).origin) report.sessionUrl = page.url()
      const screenshot = path.join(config.outputDir, "failure.png")
      try {
        await page.screenshot({ path: screenshot, fullPage: false, timeout: 10000 })
        await chmod(screenshot, 0o600)
        report.screenshot = screenshot
      } catch { /* The private report remains useful if Chromium already exited. */ }
    }
  } finally {
    report.finishedAt = new Date().toISOString()
    await privateJson(reportPath, report)
    if (context) await context.close()
    if (browser) await browser.close()
  }
  console.log(JSON.stringify({ ok: report.ok, report: reportPath, screenshot: report.screenshot,
    sessionId: report.sessionId, completedTools: report.toolCalls.filter(call => call.status === "completed" && call.ok === true).map(call => call.logicalName) }))
  if (!report.ok) process.exitCode = 1
}

try {
  const config = options(process.argv.slice(2))
  if (config.checkConfig) console.log(JSON.stringify({ ok: true, baseUrl: config.baseUrl, agent: config.agent }))
  else await main(config)
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: error.message }))
  process.exitCode = 1
}
