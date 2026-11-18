#!/usr/bin/env node
// One real UI prompt and one explicit form confirmation, then read-only verification.
import assert from "node:assert/strict"
import { createHash, randomBytes } from "node:crypto"
import { existsSync } from "node:fs"
import { chmod, mkdir, writeFile } from "node:fs/promises"
import { createRequire } from "node:module"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const ORIGIN = "http://127.0.0.1:19111"
const PREFIX = "capability_gateway_"
const TOOLS = ["get_retrosynthesis_capabilities", "resolve_retrosynthesis_target", "prepare_monomer_retrosynthesis",
  "get_retrosynthesis_draft", "get_retrosynthesis_job"]
const INPUT = '[data-component="prompt-input"][role="textbox"][contenteditable="true"]'
const MARKER = /^retrosynthesis-ui-[a-f0-9]{12}$/

export function options(argv) {
  const config = { outputDir: path.join(ROOT, ".runtime/openscience-mcp-dev/retrosynthesis-browser-evidence",
    new Date().toISOString().replace(/[:.]/g, "-")), timeoutMs: 360000, checkConfig: false }
  const names = { "--output-dir": "outputDir", "--timeout-ms": "timeoutMs", "--playwright-package": "playwrightPackage",
    "--session-url": "sessionUrl", "--marker": "marker" }
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === "--check-config") { config.checkConfig = true; continue }
    const name = names[argv[index]]
    if (!name || !argv[index + 1]) throw new Error("Unknown or incomplete argument")
    config[name] = argv[++index]
  }
  config.timeoutMs = Number(config.timeoutMs)
  if (!Number.isInteger(config.timeoutMs) || config.timeoutMs < 10000 || config.timeoutMs > 900000) {
    throw new Error("timeout-ms must be between 10000 and 900000")
  }
  config.outputDir = path.resolve(config.outputDir)
  if (!config.outputDir.startsWith(path.join(ROOT, ".runtime") + path.sep)) throw new Error("Evidence must remain inside this worktree's .runtime")
  if (config.sessionUrl) {
    const url = new URL(config.sessionUrl)
    if (url.origin !== ORIGIN || !/^\/chat\/ses_[A-Za-z0-9_-]+\/?$/.test(url.pathname) || url.search || url.hash || url.username || url.password) {
      throw new Error("Readback requires a session on the isolated Retrosynthesis UI")
    }
    if (!MARKER.test(config.marker ?? "")) throw new Error("Readback requires the original acceptance marker")
  } else if (config.marker) throw new Error("marker is only accepted with session-url")
  return config
}

export function structuredOutput(part) {
  if (part?.type !== "tool" || part.state?.status !== "completed"
      || !TOOLS.some(tool => part.tool === PREFIX + tool) || typeof part.state?.output !== "string") return null
  const text = part.state.output.trim()
  for (const line of [text, text.slice(text.lastIndexOf("\n") + 1).trim()]) {
    try {
      const value = JSON.parse(line)
      if (value && typeof value === "object" && !Array.isArray(value) && typeof value.ok === "boolean") return value
    } catch { /* Only complete JSON tool output is authoritative. */ }
  }
  return null
}

export function inspectMessages(messages, marker, expectedUserId) {
  assert.ok(Array.isArray(messages), "Invalid session message response")
  assert.ok(MARKER.test(marker), "Invalid acceptance marker")
  const users = messages.filter(message => message?.info?.role === "user" && Array.isArray(message.parts) && message.parts.some(
    part => part?.type === "text" && typeof part.text === "string" && part.text.includes(marker)))
  assert.ok(users.length <= 1, "Acceptance marker is not unique")
  const user = users[0]
  if (user) {
    assert.ok(typeof user.info.id === "string" && user.info.id, "Acceptance user has no stable ID")
    if (expectedUserId) assert.equal(user.info.id, expectedUserId, "Acceptance marker belongs to another user message")
  }
  const answers = user ? messages.filter(message => message?.info?.role === "assistant" && message.info.parentID === user.info.id) : []
  const parts = message => Array.isArray(message?.parts) ? message.parts : []
  const tools = answers.flatMap(message => parts(message).filter(part => part?.type === "tool"))
  const last = [...answers].reverse().find(message => parts(message).some(part => part?.type === "text" && typeof part.text === "string" && part.text.trim()))
  return { userId: user?.info.id, tools, done: Boolean(last?.info.time?.completed),
    text: last?.parts?.filter(part => part.type === "text").map(part => part.text).join("\n") ?? "",
    modelErrors: answers.map(message => message.info.error).filter(Boolean) }
}

const failedTool = part => part.state?.status === "error" || structuredOutput(part)?.ok === false

/** Follow the original server receipt and explicit readbacks of that same task. */
export function inspectWorkflowMessages(messages, marker, expectedUserId, conversationId) {
  const origin = inspectMessages(messages, marker, expectedUserId)
  const prepared = origin.tools.filter(part => part.tool === PREFIX + "prepare_monomer_retrosynthesis"
    && part.state?.status === "completed" && structuredOutput(part)?.ok === true)
  const draft = prepared.length ? structuredOutput(prepared[0]) : null
  if (!draft) return { ...origin, draft: null, receipts: [], receipt: null, continuations: [],
    currentTools: origin.tools, currentModelErrors: origin.modelErrors, historicalFailures: [], historicalModelErrors: [] }
  assert.ok(prepared.every(part => structuredOutput(part).draft_id === draft.draft_id), "More than one draft was prepared")
  if (conversationId) assert.equal(draft.conversation_id, conversationId, "Draft belongs to another conversation")
  const validReceiptPart = part => {
    const receipt = part.type === "text" && part.synthetic === true && part.metadata?.nexpoly_retrosynthesis_receipt
    return receipt?.draft_id === draft.draft_id && receipt.conversation_id === draft.conversation_id
      && typeof receipt.job_id === "string" && receipt.job_id
  }
  const receipts = messages.filter(message => message?.info?.role === "user" && typeof message.info.id === "string"
    && message.info.sessionID === draft.conversation_id && Array.isArray(message.parts) && message.parts.some(validReceiptPart))
  assert.ok(receipts.length <= 1, "Confirmation appended duplicate confirmation receipts")
  const receipt = receipts[0]?.parts.find(validReceiptPart)?.metadata.nexpoly_retrosynthesis_receipt ?? null
  const receiptIndex = receipts.length ? messages.indexOf(receipts[0]) : -1
  const continuations = []
  if (receipt) for (const [index, message] of messages.entries()) {
    if (index <= receiptIndex || message?.info?.role !== "user" || typeof message.info.id !== "string"
        || message.info.sessionID !== draft.conversation_id || !Array.isArray(message.parts)) continue
    const events = message.parts.filter(part => part.type === "text" && part.synthetic === true)
      .map(part => part.metadata?.nexpoly_retrosynthesis_continuation)
      .filter(event => event?.draft_id === draft.draft_id && event.conversation_id === draft.conversation_id)
    if (!events.length) continue
    for (const event of events) {
      assert.equal(event.event, "readback_requested", "Invalid same-draft readback continuation event")
      assert.equal(event.job_id, receipt.job_id, "Readback continuation changed the originally confirmed job")
      assert.equal(event.next_action?.tool, PREFIX + "get_retrosynthesis_job", "Readback continuation changed its query tool")
      assert.equal(event.next_action.arguments?.job_id, receipt.job_id, "Readback continuation changed its query job")
    }
    continuations.push(message)
  }
  const turns = [...receipts, ...continuations]
  const ids = new Set(turns.map(message => message.info.id))
  assert.equal(ids.size, turns.length, "Readback user message IDs must be unique")
  const answers = messages.filter(message => message?.info?.role === "assistant"
    && message.info.sessionID === draft.conversation_id && ids.has(message.info.parentID))
  const parts = answers.flatMap(message => Array.isArray(message.parts) ? message.parts : [])
  const currentId = turns.at(-1)?.info.id
  const currentAnswers = answers.filter(message => message.info.parentID === currentId)
  const currentParts = currentAnswers.flatMap(message => Array.isArray(message.parts) ? message.parts : [])
  const historicalAnswers = answers.filter(message => message.info.parentID !== currentId)
  const historicalParts = historicalAnswers.flatMap(message => Array.isArray(message.parts) ? message.parts : [])
  const last = [...currentAnswers].reverse().find(message => Array.isArray(message.parts)
    && message.parts.some(part => part.type === "text" && part.text?.trim()))
  return { ...origin, draft, receipts, receipt, continuations, tools: [...origin.tools, ...parts.filter(part => part.type === "tool")],
    currentTools: [...origin.tools, ...currentParts.filter(part => part.type === "tool")],
    currentModelErrors: [...origin.modelErrors, ...currentAnswers.map(message => message.info.error).filter(Boolean)],
    historicalFailures: historicalParts.filter(part => part.type === "tool" && failedTool(part)),
    historicalModelErrors: historicalAnswers.map(message => message.info.error).filter(Boolean),
    done: receipt ? Boolean(last?.info.time?.completed) : origin.done,
    text: last ? last.parts.filter(part => part.type === "text").map(part => part.text).join("\n") : origin.text,
    modelErrors: [...origin.modelErrors, ...answers.map(message => message.info.error).filter(Boolean)] }
}

export function readbackUrls(sessionUrl) {
  const url = new URL(sessionUrl)
  assert.ok(url.origin === ORIGIN && /^\/chat\/ses_[A-Za-z0-9_-]+\/?$/.test(url.pathname)
    && !url.username && !url.password && !url.search && !url.hash, "Readback requires the isolated session URL")
  const sessionId = url.pathname.split("/").filter(Boolean).at(-1)
  const base = `${ORIGIN}/openscience-api/session/`
  const query = "?directory=%2Fopenscience%2Ffiles%2Fgeneral"
  return { sessionId, messagesUrl: base + sessionId + "/message" + query, statusUrl: base + "status" + query }
}

export function acceptanceReady(inspected, terminal, statuses, sessionId) {
  const idle = !statuses?.[sessionId] || statuses[sessionId].type === "idle"
  return idle && Boolean(inspected.done) && Boolean(terminal)
}

/** Fresh acceptance is strict; readback may recover only earlier readback turns. */
export function acceptanceAttempt(inspected, recovery = false) {
  return { tools: recovery ? inspected.currentTools : inspected.tools,
    modelErrors: recovery ? inspected.currentModelErrors : inspected.modelErrors }
}

export function resourceIdentity(raw) {
  try { const url = new URL(raw); return { origin: url.origin, path: url.pathname } }
  catch { return { origin: "invalid", path: "invalid" } }
}

export function diagnosticError(error) {
  const clean = value => String(value ?? "").replace(/(?:https?|wss?):\/\/[^\s<>"']+/g, raw => {
    const value = resourceIdentity(raw); return value.origin + value.path
  }).replace(/(bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[redacted]")
    .replace(/((?:"(?:authorization|cookie|api[_-]?key|password)"|'(?:authorization|cookie|api[_-]?key|password)')\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/gi, "$1\"[redacted]\"")
    .replace(/((?:authorization|cookie|api[_-]?key|password)\s*[:=]\s*)[^\r\n]+/gi, "$1[redacted]")
  return { name: clean(error?.name || "Error").slice(0, 100), message: clean(error?.message || error).slice(0, 2000),
    stack: clean(error?.stack).slice(0, 8000) }
}

export function createBrowserDiagnostics(report, now = Date.now) {
  const started = now(), data = report.diagnostics = { stage: "initializing", events: [], resourceErrors: [] }
  let cleanup = false, failure, rejectFailure
  const failed = new Promise((_, reject) => { rejectFailure = reject })
  failed.catch(() => {})
  const append = (list, event) => { if (list.length < 200) list.push({ stage: data.stage, elapsedMs: now() - started,
    intentionalCleanup: cleanup, ...event }); else data.droppedEvents = (data.droppedEvents ?? 0) + 1 }
  const event = (type, detail = {}) => append(data.events, { type, ...detail })
  return {
    stage(value) { if (failure) throw failure; data.stage = value; event("stage") },
    beginCleanup() { cleanup = true; event("cleanup_started") },
    guard(promise) {
      const operation = Promise.resolve(promise)
      if (failure) { operation.catch(() => {}); return Promise.reject(failure) }
      return Promise.race([operation, failed])
    },
    assertHealthy() { if (failure) throw failure },
    routeError(error, request) {
      event("route_error", { method: request.method(), ...resourceIdentity(request.url()), error: diagnosticError(error) })
      if (!cleanup && !failure) { failure = error; rejectFailure(error) }
    },
    browser(browser) { browser.on("disconnected", () => event("browser_disconnected")) },
    context(context) { context.on("close", () => event("context_closed")) },
    page(page) {
      page.on("close", () => event("page_closed")); page.on("crash", () => event("page_crashed"))
      page.on("pageerror", error => event("page_error", { error: diagnosticError(error) }))
      page.on("requestfailed", request => {
        const reason = request.failure()?.errorText ?? "request_failed"
        append(data.resourceErrors, { type: "request_failed", method: request.method(), ...resourceIdentity(request.url()),
          reason: /^net::ERR_[A-Z0-9_]+$/.test(reason) ? reason : "request_failed" })
      })
      page.on("response", response => { if (response.status() >= 400) append(data.resourceErrors, {
        type: "http_error", status: response.status(), method: response.request().method(), ...resourceIdentity(response.url()),
      }) })
    },
  }
}

/** Attach rejection handlers to both operations before the click can fail. */
export async function clickAndWaitForResponse(page, predicate, click) {
  const [response] = await Promise.all([page.waitForResponse(predicate, { timeout: 30000 }), Promise.resolve().then(click)])
  return response
}

export function measureCardGeometry(node) {
  const doc = node.ownerDocument, win = doc.defaultView, rect = node.getBoundingClientRect()
  const bounds = { left: 0, top: 0, right: win.innerWidth, bottom: win.innerHeight }
  for (let parent = node.parentElement; parent; parent = parent.parentElement) {
    const style = win.getComputedStyle(parent), box = parent.getBoundingClientRect()
    if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
      bounds.top = Math.max(bounds.top, box.top + parent.clientTop)
      bounds.bottom = Math.min(bounds.bottom, box.top + parent.clientTop + parent.clientHeight)
    }
    if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
      bounds.left = Math.max(bounds.left, box.left + parent.clientLeft)
      bounds.right = Math.min(bounds.right, box.left + parent.clientLeft + parent.clientWidth)
    }
  }
  const inside = rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1
    && rect.top >= bounds.top - 1 && rect.bottom <= bounds.bottom + 1
  const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2
  const points = [[x, rect.top + 2], [x, rect.bottom - 2], [rect.left + 2, y], [rect.right - 2, y], [x, y]]
  // Insets avoid rounded corners; the interior grid catches floating chat controls.
  const insetX = Math.min(16, rect.width / 2), insetY = Math.min(16, rect.height / 2)
  for (let px = rect.left + insetX; px <= rect.right - insetX; px += 32) {
    points.push([px, rect.top + 2], [px, rect.bottom - 2])
    for (let py = rect.top + insetY; py <= rect.bottom - insetY; py += 32) points.push([px, py])
  }
  for (let py = rect.top + insetY; py <= rect.bottom - insetY; py += 32) points.push([rect.left + 2, py], [rect.right - 2, py])
  const visible = inside && points
    .every(([px, py]) => { const hit = doc.elementFromPoint(px, py); return hit === node || node.contains(hit) })
  return { rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, bounds,
    viewport: { width: win.innerWidth, height: win.innerHeight }, visible }
}

const sameGeometry = (a, b) => a && b && ["x", "y", "width", "height"].every(key => Math.abs(a.rect[key] - b.rect[key]) <= 1)
  && ["left", "top", "right", "bottom"].every(key => Math.abs(a.bounds[key] - b.bounds[key]) <= 1)

/** A card must fit its nested scrollport before Chromium can capture its pixels. */
export async function captureResultCard(page, card, filename, { timeoutMs = 10000, now = Date.now,
  pause = () => page.waitForTimeout(100), onSample = () => {} } = {}) {
  const width = page.viewportSize().width, deadline = now() + timeoutMs
  let previous, stable = 0, attempts = 0
  while (now() < deadline) {
    await card.scrollIntoViewIfNeeded({ timeout: timeoutMs })
    await card.evaluate(node => node.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" }))
    const current = await card.evaluate(measureCardGeometry); onSample(current)
    assert.equal(current.viewport.width, width, "Card capture must preserve the requested layout width")
    assert.ok(current.rect.width > 0 && current.rect.height > 0, "Result card has no visible dimensions")
    assert.ok(current.rect.width <= current.bounds.right - current.bounds.left + 1, "Result card overflows its scrollport width")
    const missingHeight = current.rect.height - (current.bounds.bottom - current.bounds.top)
    if (missingHeight > 0) {
      const height = Math.ceil(current.viewport.height + missingHeight + 200)
      assert.ok(height <= 8192, "Result card cannot fit within the bounded screenshot viewport")
      await page.setViewportSize({ width, height }); previous = undefined; stable = 0; continue
    }
    stable = sameGeometry(previous, current) ? stable + 1 : 0
    previous = current
    if (stable >= 2) {
      if (!current.visible) {
        const height = current.viewport.height + 200
        assert.ok(height <= 8192, "Result card cannot fit within the bounded screenshot viewport")
        await page.setViewportSize({ width, height }); previous = undefined; stable = 0; continue
      }
      attempts++
      await card.screenshot({ path: filename, animations: "disabled", timeout: timeoutMs })
      const after = await card.evaluate(measureCardGeometry); onSample(after)
      if (after.visible && sameGeometry(current, after)) return { viewport: after.viewport, rect: after.rect, bounds: after.bounds, attempts }
      previous = undefined; stable = 0
    }
    await pause()
  }
  throw new Error("Result card did not remain fully visible and stable during capture")
}

const digest = text => createHash("sha256").update(text).digest("hex")
async function privateJson(filename, value) {
  await writeFile(filename, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 })
  await chmod(filename, 0o600)
}

async function main(config) {
  process.umask(0o077)
  const candidates = config.playwrightPackage ? [path.resolve(config.playwrightPackage)] : [
    path.join(ROOT, "ops/openscience-ui-overlay/node_modules/playwright"),
    "/data/lzq/gith/nexpoly-dev/ops/openscience-ui-overlay/node_modules/playwright",
  ]
  const selected = candidates.find(candidate => existsSync(path.join(candidate, "package.json")))
  if (!selected || Number(process.versions.node.split(".")[0]) < 20) throw new Error("Use Node >=20 and an installed Playwright package")
  const { chromium } = createRequire(import.meta.url)(selected)
  await mkdir(config.outputDir, { recursive: true, mode: 0o700 })
  await chmod(config.outputDir, 0o700)
  const marker = config.marker ?? "retrosynthesis-ui-" + randomBytes(6).toString("hex")
  const report = { ok: false, marker, mode: config.sessionUrl ? "readback" : "real_ui", startedAt: new Date().toISOString(), promptRequests: 0 }
  const diagnostics = createBrowserDiagnostics(report)
  let browser, context, page, promptRequest, unsafePrompt, confirmationBody
  try {
    diagnostics.stage("launch_browser")
    browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] })
    diagnostics.browser(browser)
    context = await browser.newContext({ viewport: { width: 1440, height: 1050 }, locale: "zh-CN", serviceWorkers: "block" })
    diagnostics.context(context)
    await context.route("**/*", async route => {
      const request = route.request()
      try {
        const url = new URL(request.url())
        if (["http:", "https:", "ws:", "wss:"].includes(url.protocol) && url.origin !== ORIGIN) return await route.abort("blockedbyclient")
        if (request.method() === "POST" && /\/session\/[^/]+\/(message|prompt_async)$/.test(url.pathname)) {
          let body
          try { body = request.postDataJSON() } catch { body = null }
          report.promptRequests++
          if (config.sessionUrl || report.promptRequests > 1 || body?.agent !== "research" || !JSON.stringify(body.parts ?? []).includes(marker)) {
            unsafePrompt = true
            return await route.abort("blockedbyclient")
          }
          promptRequest = { url, body }
        }
        if (request.method() === "POST" && url.pathname === "/openscience-api/nexpoly/retrosynthesis/draft") {
          const body = request.postDataJSON()
          if (config.sessionUrl && body.action !== "get") return await route.abort("blockedbyclient")
          if (body.action === "confirm") {
            assert.ok(report.confirmationAuthorized, "A form submitted before the explicit confirmation step")
            confirmationBody = body
            report.confirmRequests = (report.confirmRequests ?? 0) + 1
          }
        }
        return await route.continue()
      } catch (error) {
        diagnostics.routeError(error, request)
        try { await route.abort("failed") } catch { /* A closed target may already have disposed its route. */ }
      }
    })
    page = await context.newPage()
    diagnostics.page(page)
    diagnostics.stage("open_session")
    await page.goto(config.sessionUrl ?? ORIGIN + "/", { waitUntil: "domcontentloaded", timeout: 30000 })
    if (!config.sessionUrl) {
      diagnostics.stage("submit_initial_prompt")
      const prompt = ["我想做单体逆合成，但还没选定目标分子，也不知道需要填写哪些参数。",
        "请先查询逆合成服务能力，打开空白的目标分子与参数输入卡片。未知目标先留空，我会在卡片中输入后确认。",
        "确认后请只查询这次任务，展示真实前体候选、数量和结构有效性，不要另建任务。",
        `请在最终答复中保留验收标记 ${marker}。`].join("\n")
      const input = page.locator(INPUT)
      await input.waitFor({ state: "visible", timeout: 45000 })
      await input.fill(prompt)
      await input.locator("xpath=ancestor::form").locator('button[type="submit"]').click()
      const sentDeadline = Date.now() + 15000
      while (!promptRequest && !unsafePrompt && Date.now() < sentDeadline) await page.waitForTimeout(250)
      assert.ok(promptRequest && !unsafePrompt, "UI did not submit the one approved prompt")
      report.promptSubmittedThroughUi = true
      const matched = promptRequest.url.pathname.match(/^(.*\/session\/)(ses_[A-Za-z0-9_-]+)\/(message|prompt_async)$/)
      assert.ok(matched, "Unable to identify UI-created session")
      report.sessionId = matched[2]
      report.userMessageId = promptRequest.body.messageID
      report.agent = promptRequest.body.agent
    } else {
      report.sessionId = new URL(config.sessionUrl).pathname.split("/").filter(Boolean).at(-1)
    }
    report.sessionUrl = `${ORIGIN}/chat/${report.sessionId}`
    Object.assign(report, readbackUrls(report.sessionUrl))
    await privateJson(path.join(config.outputDir, "report.json"), report)
    async function readJson(url) {
      assert.equal(new URL(url).origin, ORIGIN, "Read-only verification left the isolated UI origin")
      const response = await context.request.get(url, { timeout: 10000, maxRedirects: 0 })
      assert.equal(response.status(), 200, "Session readback failed")
      return response.json()
    }
    const agents = await readJson(ORIGIN + "/openscience-api/agent?directory=%2Fopenscience%2Ffiles%2Fgeneral")
    assert.equal(agents[0]?.name, "research", "The normal Research conversation must be the default")
    assert.ok(!agents.some(agent => agent.name === "retrosynthesis-generation"), "Retrosynthesis must use the normal conversation mode")
    report.normalResearchEntry = true
    async function readMessages() {
      return inspectWorkflowMessages(await readJson(report.messagesUrl), marker, report.userMessageId, report.sessionId)
    }
    let inspected, terminal, accepted = false
    const deadline = Date.now() + config.timeoutMs
    if (!config.sessionUrl) {
      diagnostics.stage("wait_for_parameter_card")
      while (Date.now() < deadline) {
        diagnostics.assertHealthy()
        inspected = await readMessages()
        assert.equal(inspected.modelErrors.length, 0, "The initial parameter-card conversation failed")
        assert.ok(!inspected.tools.some(part => part.tool.endsWith("submit_retrosynthesis_generation") || part.tool.endsWith("get_retrosynthesis_job")),
          "Generation was attempted before confirmation")
        const statuses = await readJson(report.statusUrl)
        if (inspected.draft && inspected.done && (!statuses[report.sessionId] || statuses[report.sessionId].type === "idle")) break
        await page.waitForTimeout(1000)
      }
      assert.ok(inspected?.draft, "Natural language did not prepare a parameter card")
      const draftId = inspected.draft.draft_id
      report.draftId = draftId
      const form = page.locator(`[data-retrosynthesis-draft="${draftId}"]`)
      await form.waitFor({ state: "visible", timeout: 30000 })
      const confirm = form.getByRole("button", { name: "确认并预测", exact: true })
      assert.equal(await confirm.isEnabled(), false, "Missing target must block confirmation")
      assert.equal(inspected.draft.job_id, null)
      assert.ok(inspected.draft.missing_fields.includes("target"))
      report.noJobBeforeConfirmation = true
      await form.screenshot({ path: path.join(config.outputDir, "parameters-empty.png") })
      await form.getByRole("textbox", { name: "目标结构或名称", exact: true }).fill("Nc1ccc(N)cc1")
      await form.locator('[data-parameter-name="num_return_sequences"]').fill("3")
      diagnostics.stage("save_parameters")
      const saveResponse = await diagnostics.guard(clickAndWaitForResponse(page,
        response => response.url().endsWith("/nexpoly/retrosynthesis/draft")
          && response.request().method() === "POST" && response.request().postDataJSON()?.action === "update",
        () => form.getByRole("button", { name: "保存参数", exact: true }).click()))
      const saved = await saveResponse.json()
      assert.equal(saved.ok, true)
      assert.equal(saved.draft.ready, true)
      assert.equal(saved.draft.job_id, null)
      assert.ok(saved.draft.resolved_target.canonical_smiles)
      await privateJson(path.join(config.outputDir, "parameters-saved.json"), saved.draft)
      diagnostics.stage("restore_saved_parameters")
      await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 })
      const restored = page.locator(`[data-retrosynthesis-draft="${draftId}"]`)
      await restored.waitFor({ state: "visible", timeout: 30000 })
      await page.waitForFunction(({ draftId }) => document.querySelector(`[data-retrosynthesis-draft="${draftId}"]`)?.getAttribute("aria-busy") === "false",
        { draftId }, { timeout: 20000 })
      assert.equal(await restored.getByRole("textbox", { name: "目标结构或名称", exact: true }).inputValue(), "Nc1ccc(N)cc1")
      assert.equal(await restored.locator('[data-parameter-name="num_return_sequences"]').inputValue(), "3")
      report.refreshPreservedDraft = true
      await restored.screenshot({ path: path.join(config.outputDir, "parameters-ready.png") })
      report.confirmationAuthorized = true
      await privateJson(path.join(config.outputDir, "report.json"), report)
      diagnostics.stage("confirm_parameters")
      const confirmationResponse = await diagnostics.guard(clickAndWaitForResponse(page,
        response => response.url().endsWith("/nexpoly/retrosynthesis/draft")
          && response.request().method() === "POST" && response.request().postDataJSON()?.action === "confirm",
        () => page.locator(`[data-retrosynthesis-draft="${draftId}"]`).getByRole("button", { name: "确认并预测", exact: true })
          .evaluate(node => { node.click(); node.click() })))
      const confirmed = await confirmationResponse.json()
      assert.equal(confirmed.ok, true)
      assert.ok(confirmed.draft.job_id)
      report.confirmedJobId = confirmed.draft.job_id
      await privateJson(path.join(config.outputDir, "confirmation.json"), confirmed)
      await privateJson(path.join(config.outputDir, "report.json"), report)
      assert.equal(report.confirmRequests, 1, "Double clicking must send one confirmation")
      // Simulate a lost response being retried: the original revision must replay
      // the original job and must not append another continuation receipt.
      diagnostics.stage("verify_confirmation_replay")
      const replayResponse = await context.request.post(ORIGIN + "/openscience-api/nexpoly/retrosynthesis/draft", {
        data: confirmationBody, headers: { Origin: ORIGIN }, timeout: 30000, maxRedirects: 0,
      })
      const replay = await replayResponse.json()
      assert.equal(replay.ok, true)
      assert.equal(replay.draft.job_id, confirmed.draft.job_id)
      report.confirmationReplayPreservedJob = true
    }
    diagnostics.stage("verify_task_readback")
    let idleIncomplete = 0
    while (Date.now() < deadline) {
      diagnostics.assertHealthy()
      inspected = await readMessages()
      assert.ok(!unsafePrompt, "Unexpected second prompt was blocked")
      const { tools: currentTools, modelErrors } = acceptanceAttempt(inspected, Boolean(config.sessionUrl))
      if (config.sessionUrl) {
        report.historicalFailures = inspected.historicalFailures.map(part => ({ tool: part.tool, input: part.state?.input,
          error: structuredOutput(part)?.error ?? part.state?.error }))
        report.historicalModelErrors = inspected.historicalModelErrors
        report.readbackContinuationCount = inspected.continuations.length
      }
      assert.equal(modelErrors.length, 0, "Model request failed; inspect the private session")
      assert.ok(inspected.tools.every(part => TOOLS.some(tool => part.tool === PREFIX + tool)), "Unexpected tool in acceptance turn")
      const completed = currentTools.filter(part => part.state?.status === "completed")
      const failed = currentTools.filter(failedTool)
      assert.equal(failed.length, 0, "A real tool failed; do not retry generation automatically")
      terminal = [...completed].reverse().find(part => part.tool === PREFIX + "get_retrosynthesis_job" &&
        ["completed", "failed", "timed_out", "interrupted", "unknown"].includes(structuredOutput(part)?.status))
      const statuses = await readJson(report.statusUrl)
      const idle = !statuses[report.sessionId] || statuses[report.sessionId].type === "idle"
      if (acceptanceReady(inspected, terminal, statuses, report.sessionId)) { accepted = true; break }
      idleIncomplete = idle && inspected.done && inspected.text.trim() ? idleIncomplete + 1 : 0
      assert.ok(idleIncomplete < 3, "Agent finished before obtaining a terminal task and acceptance marker; resume this session read-only")
      await page.waitForTimeout(1000)
    }
    assert.ok(accepted, "Timed out; use session-url and marker for read-only follow-up")
    const outputs = inspected.tools.filter(part => part.state?.status === "completed").map(part => ({ name: part.tool, input: part.state.input, output: structuredOutput(part) }))
    assert.ok(inspected.draft, "A real parameter form is required")
    assert.equal(inspected.receipts.length, 1, "Confirmation must append one server receipt")
    assert.ok(outputs.some(call => call.name === PREFIX + "get_retrosynthesis_capabilities" && call.output?.ok === true))
    assert.ok(outputs.every(call => TOOLS.includes(call.name.slice(PREFIX.length))), "The model must use only approved read and prepare tools")
    report.continuedFromConfirmedDraft = true
    const output = structuredOutput(terminal)
    assert.equal(output.status, "completed", "Actual prediction did not complete successfully")
    assert.equal(output.job_id, inspected.receipt.job_id)
    assert.equal(terminal.state.input.job_id, inspected.receipt.job_id, "The model must request the originally confirmed task")
    assert.ok(output.result)
    assert.equal(output.result.total, output.result.candidates.length)
    assert.ok(output.result.total >= 0 && output.result.total <= 3)
    const smiles = output.result.candidates.map(item => item.canonical_reactants_smiles || item.reactants_smiles)
    const card = page.locator(`[data-retrosynthesis-results][data-job-id="${output.job_id}"]`).last()
    await card.waitFor({ state: "visible", timeout: 20000 })
    const shown = await card.locator("[data-retrosynthesis-candidate] > code.nexpoly-smiles").allTextContents()
    assert.deepEqual(shown, smiles, "Card precursor SMILES differ from authoritative output")
    assert.equal(await card.locator("[data-retrosynthesis-candidate]").count(), output.result.total)
    for (const candidate of output.result.candidates) {
      assert.equal(typeof candidate.valid_smiles, "boolean")
      const candidateCard = card.locator(`[data-retrosynthesis-candidate="${candidate.rank}"]`)
      assert.ok((await candidateCard.innerText()).includes(candidate.valid_smiles ? "结构解析：有效" : "结构解析：无效"))
    }
    report.job = { jobId: output.job_id, returnedCount: output.result.total,
      validCount: output.result.candidates.filter(candidate => candidate.valid_smiles).length }
    report.answer = { source: "model_generated_text", sha256: digest(inspected.text) }
    await privateJson(path.join(config.outputDir, "tool-results.json"), outputs)
    async function captureCard(locator, name) {
      diagnostics.stage("capture_" + name)
      const entry = { file: name, samples: [] }
      report.cardCaptures ??= []
      report.cardCaptures.push(entry)
      entry.geometry = await diagnostics.guard(captureResultCard(page, locator, path.join(config.outputDir, name), {
        onSample: sample => { entry.samples.push(sample); if (entry.samples.length > 8) entry.samples.shift() },
      }))
    }
    await captureCard(card, "results.png")
    await page.setViewportSize({ width: 1440, height: 1050 })
    diagnostics.stage("verify_result_reload")
    await page.reload({ waitUntil: "domcontentloaded", timeout: 30000 })
    await page.locator(`[data-retrosynthesis-results][data-job-id="${output.job_id}"]`).last().waitFor({ state: "visible", timeout: 30000 })
    const reloaded = await readMessages()
    assert.equal(digest(reloaded.text), report.answer.sha256)
    await page.screenshot({ path: path.join(config.outputDir, "conversation.png"), fullPage: false })
    report.refreshPreservedResults = true
    for (const width of [320, 452, 1440]) {
      diagnostics.stage("validate_layout_" + width)
      await page.setViewportSize({ width, height: 1050 })
      const reloadedCard = page.locator(`[data-retrosynthesis-results][data-job-id="${output.job_id}"]`).last()
      const metrics = await reloadedCard.locator(".nexpoly-polytao-metric dt, .nexpoly-polytao-metric dd").evaluateAll(nodes => nodes.map(node => ({
        width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height,
        fontSize: parseFloat(getComputedStyle(node).fontSize),
      })))
      assert.equal(metrics.length, 6)
      assert.ok(metrics.every(item => item.width >= 80 && item.height <= item.fontSize * 3.3), "The task summary wraps vertically")
      assert.ok(await reloadedCard.locator(".nexpoly-polytao-metric").last().innerText(), "The snapshot query time must remain visible")
      assert.equal(await reloadedCard.locator("details").getAttribute("open"), null)
      if (width === 452) await captureCard(reloadedCard, "results-narrow.png")
    }
    diagnostics.assertHealthy()
    report.readableTaskSummary = true
    report.ok = true
    diagnostics.stage("complete")
  } catch (error) {
    report.failure = { ...diagnosticError(error), stage: report.diagnostics.stage }
    report.failureScreenshot = { status: "unavailable", reason: !page ? "page_not_created" : "page_closed" }
    if (page && !page.isClosed()) {
      try {
        await page.screenshot({ path: path.join(config.outputDir, "failure.png"), fullPage: false, timeout: 5000 })
        report.failureScreenshot = { status: "captured", file: "failure.png" }
      } catch (captureError) { report.failureScreenshot = { status: "unavailable", reason: "capture_failed", error: diagnosticError(captureError) } }
    }
  } finally {
    diagnostics.beginCleanup()
    try { await context?.close() } catch { /* Preserve the first failure and lifecycle evidence. */ }
    try { await browser?.close() } catch { /* A crashed browser may already be disconnected. */ }
    report.finishedAt = new Date().toISOString()
    await privateJson(path.join(config.outputDir, "report.json"), report)
  }
  console.log(JSON.stringify({ ok: report.ok, report: path.join(config.outputDir, "report.json"), sessionUrl: report.sessionUrl, marker }))
  if (!report.ok) process.exitCode = 1
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const config = options(process.argv.slice(2))
    if (config.checkConfig) console.log(JSON.stringify({ ok: true, origin: ORIGIN, mode: config.sessionUrl ? "readback" : "real_ui" }))
    else await main(config)
  } catch (error) {
    console.error(JSON.stringify({ ok: false, error: error.message }))
    process.exitCode = 1
  }
}
