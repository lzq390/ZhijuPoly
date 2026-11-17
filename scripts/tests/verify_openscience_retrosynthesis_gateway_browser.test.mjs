import assert from "node:assert/strict"
import test from "node:test"
import { acceptanceReady, inspectMessages, inspectWorkflowMessages, options, readbackUrls, structuredOutput } from "../verify_openscience_retrosynthesis_gateway_browser.mjs"

const marker = "retrosynthesis-ui-aabbcc001122"
const session = "http://127.0.0.1:19111/chat/ses_probe-a"
const successfulTool = (extra = {}) => ({ type: "tool", tool: "capability_gateway_get_retrosynthesis_job",
  state: { status: "completed", input: { job_id: "job-a" }, output: 'Original summary\n{"ok":true,"job_id":"job-a","status":"completed"}' }, ...extra })
function messages() {
  return [{ info: { role: "user", id: "user-a" }, parts: [{ type: "text", text: `Generate once. ${marker}` }] },
    { info: { role: "assistant", id: "assistant-a", parentID: "user-a", time: { completed: 123 } },
      parts: [successfulTool(), { type: "text", text: `Actual generated answer. ${marker}` }] }]
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
  const draft = { ok: true, draft_id: "rtd_" + "a".repeat(32), conversation_id: "ses_probe-a" }
  const source = messages()
  source[1].parts = [{ type: "tool", tool: "capability_gateway_prepare_monomer_retrosynthesis",
    state: { status: "completed", input: {}, output: JSON.stringify(draft) } }, { type: "text", text: "请填写参数。" }]
  const receipt = { info: { role: "user", id: "receipt-a" }, parts: [{ type: "text", synthetic: true,
    text: "用户已确认。", metadata: { nexpoly_retrosynthesis_receipt: { draft_id: draft.draft_id, job_id: "job-a", revision: 3 } } }] }
  source.push(receipt, { info: { role: "assistant", parentID: "receipt-a", time: { completed: 456 } },
    parts: [successfulTool(), { type: "text", text: "生成已完成。" }] })
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
