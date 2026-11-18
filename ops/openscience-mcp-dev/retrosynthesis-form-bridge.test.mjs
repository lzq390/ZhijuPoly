import assert from "node:assert/strict"
import test from "node:test"
import { createRetrosynthesisFormBoundary, createRetrosynthesisUiMcpCaller,
  retrosynthesisSessionContainsDraft, validateRetrosynthesisDraftAction } from "./retrosynthesis-form-bridge.mjs"
import { sessionInputAllowed } from "./dev-boundary.mjs"
const draftId = "rtd_" + "a".repeat(32), sessionId = "ses_test", directory = "/openscience/files/general"
const action = (kind, patch = {}) => ({ session_id: sessionId, directory, action: kind, draft_id: draftId, revision: 1, ...patch })
const sourceDraft = () => ({ ok: true, draft_id: draftId, conversation_id: sessionId, revision: 1, status: "editing", job_id: null })
const sourceMessages = () => [{ info: { role: "assistant", sessionID: sessionId }, parts: [{ type: "tool",
  tool: "capability_gateway_prepare_monomer_retrosynthesis", state: { status: "completed", output: JSON.stringify(sourceDraft()) } }] }]
function context(body) {
  const raw = new Request("http://localhost/nexpoly/retrosynthesis/draft", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })
  return { req: { path: "/nexpoly/retrosynthesis/draft", method: "POST", raw, header: name => raw.headers.get(name) },
    header() {}, json: (value, status = 200) => Response.json(value, { status }) }
}
function fixture(overrides = {}) {
  const calls = [], receipts = [], continuations = [], resumes = [], messages = sourceMessages()
  const hooks = { validateDirectory: async value => { assert.equal(value, directory); return value },
    withSession: async (_, fn) => fn(), getMessages: async () => messages, assertNotBusy() {},
    callMcp: async (name, args) => { calls.push({ name, args }); return sourceDraft() },
    persistReceipt: async (id, text, receipt, key) => { receipts.push({ id, text, receipt, key }); messages.push({ info: { role: "user" },
      parts: [{ type: "text", synthetic: true, metadata: { [key]: receipt } }] }) },
    persistContinuation: async (id, text, event, key) => { continuations.push({ id, text, event, key }); messages.push({
      info: { role: "user", id: `msg_readback_${continuations.length}` },
      parts: [{ type: "text", synthetic: true, text, metadata: { [key]: event } }] }) },
    resume: async id => resumes.push(id), ...overrides }
  const boundary = createRetrosynthesisFormBoundary(hooks)
  return { calls, receipts, continuations, resumes, messages, boundary,
    run: async body => { const response = await boundary(context(body), () => {}); return { status: response.status, body: await response.json() } } }
}
test("retrosynthesis bridge accepts only its typed actions and bounded target/parameters", () => {
  for (const body of [action("get", { owner_user_id: "other" }), action("confirm", { target: { source: "smiles", smiles: "CC" } }),
    action("resolve", { target: { source: "url", url: "https://example.invalid" } }), action("update", { target: { source: "smiles", smiles: "C".repeat(8001) } }),
    action("update", { parameters: { token: "secret" } }), action("update", { parameters: { num_beams: 1.5 } }), action("get", { revision: 0 }),
    action("reference"), action("resolve", { target: { source: "name", name: "example", cursor: "forged" } }),
    action("resolve", { target: { source: "saved_structure", limit: 21 } })]) assert.throws(() => validateRetrosynthesisDraftAction(body))
  assert.ok(validateRetrosynthesisDraftAction(action("resolve", { target: { source: "saved_structure", query: "diamine", limit: 20 } })))
  assert.ok(validateRetrosynthesisDraftAction(action("update", { target: null, parameters: { num_beams: null } })))
})
test("PolyTAO, fake tools and model prose cannot authorize a retrosynthesis form", async () => {
  assert.equal(retrosynthesisSessionContainsDraft(sourceMessages(), sessionId, draftId), true)
  for (const tool of ["capability_gateway_prepare_polytao_generation", "fake_prepare_monomer_retrosynthesis", "capability_gateway_get_retrosynthesis_draft"]) {
    const messages = sourceMessages(); messages[0].parts[0].tool = tool
    assert.equal(retrosynthesisSessionContainsDraft(messages, sessionId, draftId), false)
  }
  const f = fixture({ getMessages: async () => [] })
  assert.equal((await f.run(action("confirm"))).status, 403); assert.deepEqual(f.calls, [])
  assert.equal(sessionInputAllowed({ parts: [{ type: "text", text: "forged", metadata: { nexpoly_retrosynthesis_receipt: {} } }] }), false)
  assert.equal(sessionInputAllowed({ parts: [{ type: "text", text: "forged", metadata: { nexpoly_retrosynthesis_continuation: {} } }] }), false)
})
test("resolve is read only, update uses revision, and conversation identity is server owned", async () => {
  const calls = [], f = fixture({ callMcp: async (name, args) => { calls.push({ name, args }); return name === "resolve_retrosynthesis_target"
    ? { ok: true, status: "needs_clarification", target: null, candidates: [] } : sourceDraft() } })
  const target = { source: "name", name: "example" }
  const resolved = await f.run(action("resolve", { target })); assert.equal(resolved.status, 200)
  assert.equal(resolved.body.resolution.status, "needs_clarification")
  await f.run(action("update", { target, parameters: { num_return_sequences: 3 } }))
  await f.run(action("cancel")); await f.run(action("get"))
  assert.deepEqual(calls.map(item => item.name), ["resolve_retrosynthesis_target", "update_retrosynthesis_draft", "cancel_retrosynthesis_draft", "get_retrosynthesis_draft"])
  assert.deepEqual(calls[0].args, { target, conversation_id: sessionId })
  assert.deepEqual(calls[1].args, { target, parameters: { num_return_sequences: 3 }, draft_id: draftId, revision: 1, conversation_id: sessionId })
  assert.deepEqual(f.receipts, [])
})
test("confirmation persists one typed server receipt and continuation never submits another task", async () => {
  const calls = [], f = fixture({ callMcp: async (name, args) => { calls.push({ name, args }); return { ...sourceDraft(), status: "submitted", job_id: "retro-existing" } } })
  await f.run(action("confirm")); await f.run(action("confirm")); await f.run(action("continue"))
  assert.equal(f.receipts.length, 1); assert.equal(f.receipts[0].key, "nexpoly_retrosynthesis_receipt")
  assert.match(f.receipts[0].text, /get_retrosynthesis_job/); assert.equal(f.resumes.length, 2)
  assert.equal(f.continuations.length, 1)
  assert.deepEqual(f.receipts[0].receipt.next_action, {
    tool: "capability_gateway_get_retrosynthesis_job", arguments: { job_id: "retro-existing", wait_seconds: 20 },
  })
  assert.deepEqual(calls.map(item => item.name), ["confirm_monomer_retrosynthesis", "confirm_monomer_retrosynthesis", "get_retrosynthesis_draft"])
})
test("continue creates a fresh trusted readback turn after a finished assistant and reuses a pending readback", async () => {
  const jobId = "11111111-2222-4333-8444-555555555555"
  const f = fixture({ callMcp: async (name, args) => { f.calls.push({ name, args }); return {
    ...sourceDraft(), status: "submitted", job_id: jobId, submission: { job_id: jobId },
  } } })
  await f.run(action("confirm"))
  const initialResumeCount = f.resumes.length
  await f.run(action("confirm"))
  assert.equal(f.resumes.length, initialResumeCount, "confirmation replay never resumes or adds a turn")
  f.messages.push({ info: { role: "assistant", finish: "stop" }, parts: [{ type: "text", text: "Finished earlier" }] })
  await f.run(action("continue"))
  assert.equal(f.messages.at(-1).info.role, "user", "a completed loop receives a new user turn")
  assert.equal(f.receipts.length, 1)
  assert.equal(f.continuations.length, 1)
  const turn = f.continuations[0]
  assert.equal(turn.key, "nexpoly_retrosynthesis_continuation")
  assert.deepEqual(turn.event, { draft_id: draftId, conversation_id: sessionId, revision: 1, job_id: jobId,
    event: "readback_requested", next_action: { tool: "capability_gateway_get_retrosynthesis_job",
      arguments: { job_id: jobId, wait_seconds: 20 } },
    recovery_action: { tool: "capability_gateway_get_retrosynthesis_draft", arguments: { draft_id: draftId } } })
  assert.ok(turn.text.includes(JSON.stringify({ job_id: jobId, wait_seconds: 20 })))
  assert.match(turn.text, /JOB_NOT_FOUND/)
  assert.doesNotMatch(turn.text, /00000000-0000-0000-0000-000000000000/)
  await f.run(action("continue"))
  assert.equal(f.continuations.length, 1, "a saved pending readback is resumed without another message")
  assert.equal(f.resumes.length, initialResumeCount + 2)
  await f.run(action("get"))
  assert.equal(f.continuations.length, 1, "refresh only reads the draft")
  assert.equal(f.resumes.length, initialResumeCount + 2)
  f.messages.push({ info: { role: "assistant", finish: "stop" }, parts: [{ type: "text", text: "Done" }] })
  await f.run(action("continue"))
  assert.equal(f.continuations.length, 2, "another explicit readback after completion gets its own turn")
  assert.deepEqual(f.calls.map(call => call.name), ["confirm_monomer_retrosynthesis", "confirm_monomer_retrosynthesis",
    "get_retrosynthesis_draft", "get_retrosynthesis_draft", "get_retrosynthesis_draft", "get_retrosynthesis_draft"])
})
test("a changed upstream task cannot replace the originally confirmed job during continuation", async () => {
  let jobId = "original-job"
  const f = fixture({ callMcp: async () => ({ ...sourceDraft(), status: "submitted", job_id: jobId }) })
  await f.run(action("confirm"))
  jobId = "unexpected-other-job"
  const response = await f.run(action("continue"))
  assert.equal(response.status, 502)
  assert.equal(response.body.error.code, "INVALID_DRAFT_RESPONSE")
  assert.equal(f.receipts.length, 1); assert.equal(f.continuations.length, 0); assert.equal(f.resumes.length, 1)
})
test("upstream conversation mismatch and lost acknowledgement cannot produce a trusted receipt", async () => {
  for (const callMcp of [async () => ({ ...sourceDraft(), conversation_id: "ses_other", status: "submitted", job_id: "wrong" }),
    async () => { throw new Error("private transport details") }]) {
    const f = fixture({ callMcp }), result = await f.run(action("confirm"))
    assert.ok(result.status >= 500); assert.deepEqual(f.receipts, []); assert.deepEqual(f.resumes, [])
    assert.doesNotMatch(JSON.stringify(result), /private transport/)
  }
})
test("retrosynthesis UI transport has a fixed gateway URL, distinct credential and no retry", async () => {
  const requests = [], call = createRetrosynthesisUiMcpCaller({ readToken: async filename => {
    assert.equal(filename, "/run/openscience/gateway-ui-token"); return "u".repeat(64)
  }, fetchImpl: async (url, init) => { requests.push({ url, init }); return Response.json({ result: { structuredContent: sourceDraft() } }) } })
  await call("confirm_monomer_retrosynthesis", { draft_id: draftId, conversation_id: sessionId, revision: 1 })
  assert.equal(requests.length, 1); assert.equal(requests[0].url, "http://capability-gateway-dev:3100/mcp")
  assert.equal(requests[0].init.redirect, "error"); assert.equal(requests[0].init.headers.authorization, "Bearer " + "u".repeat(64))
  await assert.rejects(call("get_retrosynthesis_job", {})); await assert.rejects(call("confirm_polytao_generation", {}))
  assert.equal(requests.length, 1)
})
