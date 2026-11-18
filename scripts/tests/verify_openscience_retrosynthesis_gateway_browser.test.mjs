import assert from "node:assert/strict"
import test from "node:test"
import { EventEmitter } from "node:events"
import { acceptanceAttempt, acceptanceReady, captureResultCard, clickAndWaitForResponse, createBrowserDiagnostics,
  diagnosticError, inspectMessages, inspectWorkflowMessages, measureCardGeometry, options, readbackUrls,
  resourceIdentity, structuredOutput } from "../verify_openscience_retrosynthesis_gateway_browser.mjs"

const marker = "retrosynthesis-ui-aabbcc001122"
const session = "http://127.0.0.1:19111/chat/ses_probe-a"
const successfulTool = (extra = {}) => ({ type: "tool", tool: "capability_gateway_get_retrosynthesis_job",
  state: { status: "completed", input: { job_id: "job-a" }, output: 'Original summary\n{"ok":true,"job_id":"job-a","status":"completed"}' }, ...extra })
function messages() {
  return [{ info: { role: "user", id: "user-a" }, parts: [{ type: "text", text: `Generate once. ${marker}` }] },
    { info: { role: "assistant", id: "assistant-a", parentID: "user-a", time: { completed: 123 } },
      parts: [successfulTool(), { type: "text", text: `Actual generated answer. ${marker}` }] }]
}

function workflowMessages() {
  const draft = { ok: true, draft_id: "rtd_" + "a".repeat(32), conversation_id: "ses_probe-a" }
  const source = messages()
  source[1].parts = [{ type: "tool", tool: "capability_gateway_prepare_monomer_retrosynthesis",
    state: { status: "completed", input: {}, output: JSON.stringify(draft) } }, { type: "text", text: "请填写参数。" }]
  const receipt = { info: { role: "user", id: "receipt-a", sessionID: draft.conversation_id }, parts: [{ type: "text", synthetic: true,
    text: "用户已确认。", metadata: { nexpoly_retrosynthesis_receipt: {
      draft_id: draft.draft_id, conversation_id: draft.conversation_id, job_id: "job-a", revision: 3,
    } } }] }
  source.push(receipt, { info: { role: "assistant", parentID: "receipt-a", sessionID: draft.conversation_id, time: { completed: 456 } },
    parts: [successfulTool(), { type: "text", text: "生成已完成。" }] })
  return { draft, source, receipt }
}

function continuationMessage(draft) {
  return { info: { role: "user", id: "readback-a", sessionID: draft.conversation_id }, parts: [{ type: "text", synthetic: true,
    text: "读取原任务。", metadata: { nexpoly_retrosynthesis_continuation: {
      event: "readback_requested", draft_id: draft.draft_id, conversation_id: draft.conversation_id, job_id: "job-a", revision: 3,
      next_action: { tool: "capability_gateway_get_retrosynthesis_job", arguments: { job_id: "job-a", wait_seconds: 20 } },
    } } }] }
}

test("browser readback accepts only the isolated fixed session URL and original marker", () => {
  const result = options(["--session-url", session, "--marker", marker, "--check-config"])
  assert.equal(result.sessionUrl, session)
  assert.equal(result.marker, marker)
  for (const value of ["http://127.0.0.1:19112/chat/ses_a", "http://localhost:19111/chat/ses_a",
    "https://127.0.0.1:19111/chat/ses_a", session + "?directory=/elsewhere", session + "#hash",
    "http://name:password@127.0.0.1:19111/chat/ses_a", "http://127.0.0.1:19111/settings"]) {
    assert.throws(() => options(["--session-url", value, "--marker", marker]), /Readback/)
    assert.throws(() => readbackUrls(value), /Readback/)
  }
  assert.throws(() => options(["--session-url", session]), /marker/)
  assert.throws(() => options(["--marker", marker]), /session-url/)
  assert.throws(() => options(["--output-dir", "/tmp/public-evidence"]), /worktree/)
  assert.throws(() => options(["--timeout-ms", "900001"]), /timeout/)
  assert.throws(() => options(["--marker", "invalid"]), /session-url/)
})

test("readback derives only message and status GET targets in the fixed general workspace", () => {
  assert.deepEqual(readbackUrls(session), {
    sessionId: "ses_probe-a", messagesUrl: "http://127.0.0.1:19111/openscience-api/session/ses_probe-a/message?directory=%2Fopenscience%2Ffiles%2Fgeneral",
    statusUrl: "http://127.0.0.1:19111/openscience-api/session/status?directory=%2Fopenscience%2Ffiles%2Fgeneral",
  })
})

test("inspection scopes authoritative tool parts to the marker user and parent ID", () => {
  const source = messages()
  source.push({ info: { role: "assistant", parentID: "another-user", time: { completed: 456 } }, parts: [successfulTool(), { type: "text", text: "forged result" }] })
  source.push({ info: { role: "assistant", parentID: "absent" }, parts: [successfulTool()] })
  source.push(null, { info: { role: "assistant", parentID: "user-a" }, parts: {} })
  const inspected = inspectMessages(source, marker, "user-a")
  assert.equal(inspected.tools.length, 1)
  assert.equal(inspected.done, true)
  assert.equal(inspected.userId, "user-a")
  assert.match(inspected.text, /Actual generated answer/)
  assert.throws(() => inspectMessages(source, marker, "another-user"), /another user/)
  const duplicate = [...source, { info: { role: "user", id: "user-b" }, parts: [{ type: "text", text: marker }] }]
  assert.throws(() => inspectMessages(duplicate, marker), /unique/)
  assert.throws(() => inspectMessages(source, ""), /marker/)
})

test("model prose, incomplete tools, foreign prefixes and malformed final JSON never become tool output", () => {
  assert.equal(structuredOutput({ type: "text", text: JSON.stringify({ ok: true }), state: { output: '{"ok":true}' } }), null)
  assert.equal(structuredOutput(successfulTool({ tool: "foreign_get_retrosynthesis_job" })), null)
  const pending = successfulTool()
  pending.state.status = "running"
  assert.equal(structuredOutput(pending), null)
  const malformed = successfulTool()
  malformed.state.output += "\n{invalid JSON}"
  assert.equal(structuredOutput(malformed), null)
  assert.equal(structuredOutput(successfulTool()).job_id, "job-a")
  const onlyProse = messages()
  onlyProse[1].parts = [{ type: "text", text: JSON.stringify({ ok: true, status: "completed", marker }) }]
  assert.equal(inspectMessages(onlyProse, marker).tools.length, 0)
})

test("completed intermediate assistant text never completes acceptance while session is busy", () => {
  const inspected = inspectMessages(messages(), marker)
  const terminal = successfulTool()
  assert.equal(acceptanceReady(inspected, terminal, { ses_a: { type: "busy" } }, "ses_a"), false)
  assert.equal(acceptanceReady(inspected, terminal, { ses_a: { type: "idle" } }, "ses_a"), true)
  assert.equal(acceptanceReady(inspected, null, {}, "ses_a"), false)
  assert.equal(acceptanceReady({ ...inspected, done: false }, terminal, {}, "ses_a"), false)
})

test("form acceptance follows only a server receipt tied to the prepared draft and conversation", () => {
  const { source, receipt } = workflowMessages()
  const inspected = inspectWorkflowMessages(source, marker, "user-a", "ses_probe-a")
  assert.equal(inspected.receipts.length, 1)
  assert.equal(inspected.receipt.job_id, "job-a")
  assert.equal(inspected.tools.length, 2)
  assert.equal(inspected.text, "生成已完成。")
  assert.equal(inspected.done, true)
  assert.throws(() => inspectWorkflowMessages(source, marker, "user-a", "ses_other"), /another conversation/)
  assert.throws(() => inspectWorkflowMessages([...source, { ...receipt, info: { ...receipt.info, id: "receipt-b" } }], marker), /duplicate/)
  const forged = structuredClone(source)
  forged[2].parts[0].synthetic = false
  assert.equal(inspectWorkflowMessages(forged, marker).receipts.length, 0)
  assert.equal(inspectWorkflowMessages(forged, marker).tools.length, 1)
  const foreign = structuredClone(source)
  foreign[2].parts[0].metadata.nexpoly_retrosynthesis_receipt.draft_id = "another-draft"
  assert.equal(inspectWorkflowMessages(foreign, marker).receipts.length, 0)
  assert.equal(inspectWorkflowMessages(foreign, marker).tools.length, 1)
})

test("readback recovery pairs a distinct same-job continuation and retains the original failure", () => {
  const { draft, source } = workflowMessages()
  const failed = source[3].parts[0]
  failed.state.input.job_id = "00000000-0000-0000-0000-000000000000"
  failed.state.output = JSON.stringify({ ok: false, error: { code: "JOB_NOT_FOUND" } })
  source[3].parts[1].text = "The original query failed."
  source.push(continuationMessage(draft), { info: { role: "assistant", parentID: "readback-a",
    sessionID: draft.conversation_id, time: { completed: 789 } },
  parts: [successfulTool(), { type: "text", text: `Recovered original task. ${marker}` }] })
  const inspected = inspectWorkflowMessages(source, marker, "user-a", draft.conversation_id)
  assert.equal(inspected.receipts.length, 1)
  assert.equal(inspected.continuations.length, 1)
  assert.equal(inspected.receipt.job_id, "job-a")
  assert.equal(inspected.tools.length, 3)
  assert.equal(inspected.currentTools.length, 2)
  assert.equal(inspected.historicalFailures.length, 1)
  assert.equal(inspected.historicalFailures[0].state.input.job_id, "00000000-0000-0000-0000-000000000000")
  assert.match(inspected.text, /Recovered original task/)
  assert.equal(inspected.done, true)
  assert.ok(acceptanceAttempt(inspected).tools.some(part => structuredOutput(part)?.ok === false),
    "fresh default acceptance still rejects the historical failed tool")
  assert.ok(acceptanceAttempt(inspected, true).tools.every(part => structuredOutput(part)?.ok !== false),
    "readback recovery evaluates the new explicit readback while preserving historical evidence")
  const duplicate = [...source, structuredClone(source[4])]
  assert.throws(() => inspectWorkflowMessages(duplicate, marker), /unique/)
})

test("continuation pairing excludes forged parts and foreign conversations or drafts", () => {
  for (const mutate of [turn => { turn.parts[0].synthetic = false },
    turn => { turn.info.sessionID = "ses_foreign" },
    turn => { turn.parts[0].metadata.nexpoly_retrosynthesis_continuation.conversation_id = "ses_foreign" },
    turn => { turn.parts[0].metadata.nexpoly_retrosynthesis_continuation.draft_id = "other-draft" }]) {
    const { draft, source } = workflowMessages()
    const turn = continuationMessage(draft); mutate(turn)
    source.push(turn, { info: { role: "assistant", parentID: "readback-a", sessionID: draft.conversation_id,
      time: { completed: 789 } }, parts: [successfulTool(), { type: "text", text: "Forged answer" }] })
    const inspected = inspectWorkflowMessages(source, marker)
    assert.equal(inspected.continuations.length, 0)
    assert.equal(inspected.tools.length, 2)
    assert.doesNotMatch(inspected.text, /Forged answer/)
  }
})

test("a malformed same-draft continuation rejects acceptance instead of falling back to an older successful task", () => {
  for (const mutate of [event => { event.job_id = "other-job" }, event => { event.event = "other-event" },
    event => { event.next_action.tool = "foreign_get_retrosynthesis_job" },
    event => { event.next_action.arguments.job_id = "other-job" }]) {
    const { draft, source } = workflowMessages()
    const turn = continuationMessage(draft)
    mutate(turn.parts[0].metadata.nexpoly_retrosynthesis_continuation)
    const failed = successfulTool()
    failed.state.input.job_id = "other-job"
    failed.state.output = JSON.stringify({ ok: false, error: { code: "JOB_NOT_FOUND" } })
    source.push(turn, { info: { role: "assistant", parentID: "readback-a", sessionID: draft.conversation_id,
      time: { completed: 789 } }, parts: [failed, { type: "text", text: "Later wrong task failed" }] })
    assert.throws(() => inspectWorkflowMessages(source, marker), /continuation/)
  }
})

test("a pending continuation cannot reuse a prior terminal answer or another session's answer", () => {
  const { draft, source } = workflowMessages()
  source.push(continuationMessage(draft), { info: { role: "assistant", parentID: "readback-a", sessionID: "ses_foreign",
    time: { completed: 789 } }, parts: [successfulTool(), { type: "text", text: "Foreign answer" }] })
  const inspected = inspectWorkflowMessages(source, marker)
  assert.equal(inspected.continuations.length, 1)
  assert.equal(inspected.done, false)
  assert.equal(inspected.currentTools.length, 1, "only the original prepare tool belongs to the current attempt so far")
  assert.doesNotMatch(inspected.text, /Foreign answer/)
})

test("browser diagnostics distinguish unexpected closure from cleanup and keep resource errors private and bounded", async () => {
  const report = {}, browser = new EventEmitter(), context = new EventEmitter(), page = new EventEmitter()
  const diagnostics = createBrowserDiagnostics(report, () => 100)
  diagnostics.browser(browser); diagnostics.context(context); diagnostics.page(page)
  diagnostics.stage("confirm_parameters")
  page.emit("crash"); page.emit("close")
  const request = { method: () => "GET", url: () => "https://alice:password@example.invalid/asset?token=private-token#secret",
    failure: () => ({ errorText: "net::ERR_ABORTED" }) }
  for (let i = 0; i < 205; i++) page.emit("requestfailed", request)
  page.emit("response", { status: () => 503, url: request.url, request: () => request })
  diagnostics.beginCleanup(); context.emit("close"); browser.emit("disconnected")
  assert.equal(report.diagnostics.events.find(event => event.type === "page_crashed").intentionalCleanup, false)
  assert.equal(report.diagnostics.events.find(event => event.type === "browser_disconnected").intentionalCleanup, true)
  assert.equal(report.diagnostics.resourceErrors.length, 200)
  assert.ok(report.diagnostics.droppedEvents >= 5)
  assert.deepEqual(resourceIdentity(request.url()), { origin: "https://example.invalid", path: "/asset" })
  assert.doesNotMatch(JSON.stringify(report), /alice|password|private-token|#secret/)
  const error = new Error("https://alice:password@example.invalid/route?token=private-token Bearer secret-credential")
  assert.doesNotMatch(JSON.stringify(diagnosticError(error)), /alice|password|private-token|secret-credential/)
  assert.ok(diagnosticError(error).stack)
  const structuredError = new Error('wss://alice:private-credential@example.invalid/live?token=private-token '
    + '{"password":"private-password","api_key":"private-key","cookie":"private-cookie"}')
  assert.doesNotMatch(JSON.stringify(diagnosticError(structuredError)), /alice|private-/)
})

test("route errors preserve the first failure and prevent a later mutation stage", async () => {
  const report = {}, diagnostics = createBrowserDiagnostics(report)
  diagnostics.stage("save_parameters")
  const first = new Error("Unauthorized form action")
  diagnostics.routeError(first, { method: () => "POST", url: () => "http://127.0.0.1:19111/draft?token=secret" })
  await assert.rejects(diagnostics.guard(Promise.resolve("later")), error => error === first)
  await assert.rejects(diagnostics.guard(Promise.reject(new Error("Secondary failure"))), error => error === first)
  assert.throws(() => diagnostics.stage("confirm_parameters"), error => error === first)
  assert.equal(report.diagnostics.stage, "save_parameters")
  assert.equal(report.diagnostics.events.at(-1).type, "route_error")
  assert.doesNotMatch(JSON.stringify(report), /token=secret/)
})

test("click and response are observed together, preserving click failures without an unhandled pending rejection", async () => {
  const trace = [], response = { status: 200 }
  const result = await clickAndWaitForResponse({ waitForResponse: () => { trace.push("wait"); return Promise.resolve(response) } },
    () => true, () => { trace.push("click") })
  assert.equal(result, response); assert.deepEqual(trace, ["wait", "click"])
  let rejectResponse
  const pending = new Promise((_, reject) => { rejectResponse = reject })
  const clickError = new Error("Original click failure")
  await assert.rejects(clickAndWaitForResponse({ waitForResponse: () => pending }, () => true, () => { throw clickError }),
    error => error === clickError)
  rejectResponse(new Error("Secondary target closed"))
  await new Promise(resolve => setImmediate(resolve))
})

test("card geometry accounts for ancestor clipping and foreground occlusion", () => {
  const win = { innerWidth: 452, innerHeight: 1050, getComputedStyle: () => ({ overflowY: "auto", overflowX: "hidden" }) }
  const parent = { parentElement: null, clientTop: 0, clientLeft: 0, clientHeight: 600, clientWidth: 435,
    getBoundingClientRect: () => ({ top: 100, left: 0 }) }
  const doc = { defaultView: win, elementFromPoint: () => node }
  const node = { ownerDocument: doc, parentElement: parent, contains: value => value === node,
    getBoundingClientRect: () => ({ x: 10, y: 120, left: 10, top: 120, right: 410, bottom: 1020, width: 400, height: 900 }) }
  assert.equal(measureCardGeometry(node).visible, false)
  assert.equal(measureCardGeometry(node).bounds.bottom, 700)
  win.innerHeight = 1600; parent.clientHeight = 1200
  assert.equal(measureCardGeometry(node).visible, true)
  doc.elementFromPoint = () => ({ overlay: true })
  assert.equal(measureCardGeometry(node).visible, false)
  doc.elementFromPoint = (x, y) => x >= 180 && x <= 240 && y >= 900 && y <= 940 ? { floatingButton: true } : node
  assert.equal(measureCardGeometry(node).visible, false, "an off-center floating control still obscures the card")
  doc.elementFromPoint = (x, y) => x >= 80 && x <= 120 && y >= 1007 ? { floatingButtonEdge: true } : node
  assert.equal(measureCardGeometry(node).visible, false, "a thin overlap near the bottom edge cannot escape the grid")
})

function captureFixture({ moving = false, moveDuringCapture = false, cardWidth = 435, cardHeight = 1180, footerHeight = 0 } = {}) {
  let viewport = { width: 452, height: 1050 }, time = 0, calls = 0, offset = 0
  const captures = [], sizes = [], scrolls = []
  const page = { viewportSize: () => viewport, setViewportSize: async value => { viewport = value; sizes.push(value) } }
  const card = { scrollIntoViewIfNeeded: async () => { offset = 0 }, evaluate: async fn => {
    if (fn !== measureCardGeometry) return fn({ scrollIntoView: options => scrolls.push(options) })
    calls++
    return { rect: { x: 0, y: 300 + offset + (moving ? calls % 2 * 3 : 0), width: cardWidth, height: cardHeight },
      bounds: { left: 0, top: 300, right: viewport.width, bottom: viewport.height - 100 }, viewport,
      visible: viewport.height - 400 >= cardHeight && 300 + cardHeight <= viewport.height - footerHeight }
  }, screenshot: async input => { captures.push(input); if (moveDuringCapture && captures.length === 1) offset = 10 } }
  return { page, card, captures, sizes, scrolls, clock: { now: () => time, pause: async () => { time += 100 } } }
}

test("card capture fits its nested scrollport while preserving 452px width and retries movement during capture", async () => {
  const fixture = captureFixture({ moveDuringCapture: true })
  const result = await captureResultCard(fixture.page, fixture.card, "result.png", { ...fixture.clock, timeoutMs: 2000 })
  assert.equal(result.viewport.width, 452)
  assert.ok(result.viewport.height > 1050)
  assert.ok(result.rect.height <= result.bounds.bottom - result.bounds.top)
  assert.ok(fixture.sizes.every(viewport => viewport.width === 452))
  assert.ok(fixture.scrolls.length > 0, "fully intersecting cards must still move away from clipping boundaries")
  assert.equal(fixture.captures.length, 2, "a moved first screenshot cannot become accepted evidence")
  assert.ok(fixture.captures.every(input => input.animations === "disabled"))
})

test("card capture rejects unstable, horizontally clipped or unbounded cards instead of accepting misleading pixels", async () => {
  const moving = captureFixture({ moving: true })
  await assert.rejects(captureResultCard(moving.page, moving.card, "moving.png", { ...moving.clock, timeoutMs: 500 }), /stable/)
  assert.equal(moving.captures.length, 0)
  const wide = captureFixture({ cardWidth: 500 })
  await assert.rejects(captureResultCard(wide.page, wide.card, "wide.png", wide.clock), /width/)
  const tall = captureFixture({ cardHeight: 9000 })
  await assert.rejects(captureResultCard(tall.page, tall.card, "tall.png", tall.clock), /bounded/)
})

test("card capture moves fixed footer occlusion outside the card before accepting the screenshot", async () => {
  const fixture = captureFixture({ footerHeight: 400 })
  const result = await captureResultCard(fixture.page, fixture.card, "footer.png", fixture.clock)
  assert.equal(fixture.sizes.length, 2, "fitting the scrollport alone does not clear a foreground footer")
  assert.ok(result.rect.y + result.rect.height <= result.viewport.height - 400)
  assert.equal(fixture.captures.length, 1)
})
