import assert from "node:assert/strict"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { createPolytaoFormBoundary, createUiMcpCaller, sessionContainsDraft, validateDraftAction } from "./polytao-form-bridge.mjs"

const draftId = "ptd_" + "a".repeat(32), sessionId = "ses_test", directory = "/openscience/files/general"
const action = (kind, patch = {}) => ({ session_id: sessionId, directory, action: kind, draft_id: draftId, revision: 1, ...patch })
const sourceDraft = () => ({ ok: true, draft_id: draftId, conversation_id: sessionId, revision: 1,
  status: "editing", job_id: null })
const sourceMessages = () => [{ info: { role: "assistant", sessionID: sessionId }, parts: [{ type: "tool",
  tool: "capability_gateway_prepare_polytao_generation", state: { status: "completed", output: "summary\n\n" + JSON.stringify(sourceDraft()) } }] }]
function context(body, { method = "POST", endpoint = "/nexpoly/polytao/draft", type = "application/json" } = {}) {
  const raw = new Request("http://localhost:4096" + endpoint, { method, ...(method === "POST"
    ? { headers: { "content-type": type }, body: typeof body === "string" ? body : JSON.stringify(body) } : {}) })
  return { req: { path: endpoint, method, raw, header: name => raw.headers.get(name) },
    header: () => {}, json: (value, status = 200) => new Response(JSON.stringify(value), { status,
      headers: { "content-type": "application/json" } }) }
}
function fixture(overrides = {}) {
  const calls = [], receipts = [], resumes = []
  let messages = sourceMessages()
  const hooks = { validateDirectory: async value => value,
    withSession: async (value, fn) => { assert.equal(value, directory); return fn() },
    getMessages: async () => messages, assertNotBusy: () => {},
    callMcp: async (name, args) => { calls.push({ name, args }); return sourceDraft() },
    persistReceipt: async (id, text, receipt) => { receipts.push({ id, text, receipt }); messages.push({
      info: { role: "user", sessionID: id }, parts: [{ type: "text", synthetic: true, text,
        metadata: { nexpoly_polytao_receipt: receipt } }] }) },
    resume: async id => { resumes.push(id) }, ...overrides }
  return { handle: createPolytaoFormBoundary(hooks), calls, receipts, resumes,
    run: async body => { const response = await createPolytaoFormBoundary(hooks)(context(body), () => {}); return { status: response.status, body: await response.json() } } }
}

test("draft action input rejects privilege fields, invalid revisions and action-specific extras", () => {
  for (const body of [action("get", { client_id: "other" }), action("update", { owner_client_id: "other" }),
    action("confirm", { descriptors: {} }), action("get", { revision: 0 }), action("get", { draft_id: "guess" }),
    action("reference", { reference_smiles: 123 }), action("update", { parameters: [] }), action("get", { action: "submit" })]) {
    assert.throws(() => validateDraftAction(body))
  }
  assert.equal(validateDraftAction(action("reference", { reference_smiles: null })).reference_smiles, null)
})

test("only an exact completed prepare tool in this conversation authorizes a draft", () => {
  assert.ok(sessionContainsDraft(sourceMessages(), sessionId, draftId))
  for (const change of [messages => { messages[0].info.role = "user" },
    messages => { messages[0].info.sessionID = "ses_other" }, messages => { messages[0].parts[0].tool = "other_prepare_polytao_generation" },
    messages => { messages[0].parts[0].state.status = "running" },
    messages => { messages[0].parts[0].state.output = JSON.stringify({ ...sourceDraft(), conversation_id: "ses_other" }) }]) {
    const messages = sourceMessages(); change(messages)
    assert.ok(!sessionContainsDraft(messages, sessionId, draftId))
  }
})

test("UI actions map to exact MCP tools with server-owned conversation identity", async () => {
  const f = fixture()
  for (const kind of ["get", "update", "reference", "cancel"]) {
    const body = action(kind, kind === "update" ? { descriptors: { MolWt: 33 }, parameters: { candidate_count: 3 } }
      : kind === "reference" ? { reference_smiles: "*CC*" } : {})
    const response = await f.handle(context(body), () => {})
    assert.equal(response.status, 200)
  }
  assert.deepEqual(f.calls.map(call => call.name), ["get_polytao_draft", "update_polytao_draft", "update_polytao_draft", "cancel_polytao_draft"])
  assert.ok(f.calls.every(call => call.args.conversation_id === sessionId && !Object.hasOwn(call.args, "session_id")))
  assert.deepEqual(f.calls[2].args, { draft_id: draftId, conversation_id: sessionId, revision: 1, reference_smiles: "*CC*" })
  assert.equal(f.receipts.length, 0)
})

test("cross-conversation, invalid directory and forged output cannot reach the MCP bridge", async () => {
  for (const override of [{ getMessages: async () => [] }, { getMessages: async () => { throw new Error("private storage path") } },
    { validateDirectory: async () => { throw new Error("private token") } }]) {
    const f = fixture(override)
    const response = await f.handle(context(action("confirm")), () => {})
    assert.ok([403, 503].includes(response.status)); assert.equal(f.calls.length, 0)
    assert.ok(!(await response.text()).includes("private"))
  }
})

test("confirmation persists one receipt and resumes without any native generation API call", async () => {
  const f = fixture({ callMcp: async () => ({ ...sourceDraft(), status: "submitted", job_id: "polytao.test",
    submission: { ok: true, job_id: "polytao.test", status: "queued" } }) })
  const first = await f.handle(context(action("confirm")), () => {})
  assert.equal((await first.json()).continuation.status, "started")
  const reconfirm = await f.handle(context(action("confirm")), () => {})
  assert.equal((await reconfirm.json()).continuation.status, "existing")
  assert.equal(f.resumes.length, 1)
  const repeat = await f.handle(context(action("continue")), () => {})
  assert.equal((await repeat.json()).continuation.status, "existing")
  assert.equal(f.receipts.length, 1); assert.equal(f.resumes.length, 2)
  assert.equal(f.receipts[0].receipt.job_id, "polytao.test")
})

test("a continuation failure preserves accepted task and can retry only through get draft", async () => {
  let fail = true
  const f = fixture({ assertNotBusy: () => { if (fail) throw new Error("busy") },
    callMcp: async (name, args) => { f.calls.push({ name, args }); return { ...sourceDraft(), status: "submitted", job_id: "polytao.test" } } })
  const first = await f.handle(context(action("confirm")), () => {})
  const value = await first.json()
  assert.equal(value.draft.job_id, "polytao.test"); assert.equal(value.continuation.status, "failed")
  fail = false
  const next = await f.handle(context(action("continue")), () => {})
  assert.equal((await next.json()).continuation.status, "started")
  assert.deepEqual(f.calls.map(call => call.name), ["confirm_polytao_generation", "get_polytao_draft"])
})

test("concurrent confirmation is serialized and a rejected action cannot release another lock", async () => {
  let unlock
  const waiting = new Promise(resolve => { unlock = resolve })
  const f = fixture({ callMcp: async () => { await waiting; return sourceDraft() } })
  const first = f.handle(context(action("confirm")), () => {})
  await new Promise(resolve => setTimeout(resolve, 10))
  for (let index = 0; index < 2; index++) {
    const second = await f.handle(context(action("confirm")), () => {})
    assert.equal(second.status, 409); assert.equal((await second.json()).error.code, "DRAFT_BUSY")
  }
  unlock(); assert.equal((await first).status, 200)
})

test("the integrated gateway exposes only the bounded JSON POST draft route", async () => {
  const f = fixture()
  assert.equal((await f.handle(context(action("get"), { method: "GET" }), () => {})).status, 405)
  assert.equal((await f.handle(context(action("get"), { type: "text/plain" }), () => {})).status, 415)
  assert.equal((await f.handle(context("x".repeat(32769)), () => {})).status, 413)
  let passed = false
  await f.handle(context(action("get"), { endpoint: "/session" }), () => { passed = true })
  assert.ok(passed)
})

test("raw MCP client uses only fixed endpoint, exact UI credential and JSON-RPC tool call", async () => {
  let sent
  const call = createUiMcpCaller({ readToken: async filename => {
    assert.equal(filename, "/run/openscience/gateway-ui-token"); return "t".repeat(48)
  }, fetchImpl: async (url, init) => {
    sent = { url, init }; return new Response(JSON.stringify({ result: { structuredContent: sourceDraft() } }),
      { headers: { "content-type": "application/json" } })
  } })
  assert.equal((await call("get_polytao_draft", { draft_id: draftId, conversation_id: sessionId })).ok, true)
  assert.equal(sent.url, "http://capability-gateway-dev:3100/mcp")
  assert.equal(sent.init.headers.authorization, "Bearer " + "t".repeat(48))
  assert.equal(JSON.parse(sent.init.body).method, "tools/call")
  assert.equal(sent.init.redirect, "error")
  await assert.rejects(call("submit_polytao_generation", {}), error => error.code === "TOOL_DENIED")
})

test("UI credentials remain private regular files and transport diagnostics are redacted", async () => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "polytao-ui-token-"))
  try {
    const filename = path.join(temporary, "token")
    await fs.writeFile(filename, "t".repeat(48), { mode: 0o600 })
    const call = createUiMcpCaller({ tokenFile: filename, fetchImpl: async () => { throw new Error("Bearer private-native-cookie") } })
    await assert.rejects(call("get_polytao_draft", {}), error => error.code === "MCP_UNAVAILABLE" && !error.message.includes("private"))
    await fs.chmod(filename, 0o644)
    let fetched = false
    const unsafe = createUiMcpCaller({ tokenFile: filename, fetchImpl: async () => { fetched = true } })
    await assert.rejects(unsafe("get_polytao_draft", {})); assert.ok(!fetched)
    await fs.chmod(filename, 0o600)
    await fs.symlink(filename, path.join(temporary, "link"))
    await assert.rejects(createUiMcpCaller({ tokenFile: path.join(temporary, "link") })("get_polytao_draft", {}))
  } finally { await fs.rm(temporary, { recursive: true, force: true }) }
})

test("saved legacy and gateway draft evidence are accepted only by their exact completed tool names", () => {
  const messages = sourceMessages()
  messages[0].parts[0].tool = "nexpoly_polytao_prepare_polytao_generation"
  assert.ok(sessionContainsDraft(messages, sessionId, draftId))
  for (const tool of ["capability_gateway_get_polytao_draft", "capability_gateway_prepare_polytao_generation_extra",
    "fake_capability_gateway_prepare_polytao_generation", "capability_gateway_search_polymers"]) {
    messages[0].parts[0].tool = tool
    assert.ok(!sessionContainsDraft(messages, sessionId, draftId))
  }
})

test("parameter confirmation crosses the fixed gateway MCP transport and saves a single server receipt", async () => {
  const transport = []
  const callMcp = createUiMcpCaller({ readToken: async filename => {
    assert.equal(filename, "/run/openscience/gateway-ui-token"); return "u".repeat(48)
  }, fetchImpl: async (url, init) => {
    const request = JSON.parse(init.body)
    transport.push({ url, token: init.headers.authorization, request })
    return new Response(JSON.stringify({ jsonrpc: "2.0", id: request.id, result: { structuredContent: {
      ...sourceDraft(), status: "submitted", job_id: "polytao.gateway-confirmed",
      submission: { ok: true, job_id: "polytao.gateway-confirmed", status: "pending" },
    } } }), { headers: { "content-type": "application/json" } })
  } })
  const f = fixture({ callMcp })
  for (const actionName of ["confirm", "confirm", "continue"]) {
    const response = await f.handle(context(action(actionName)), () => {})
    assert.equal(response.status, 200)
    assert.equal((await response.json()).draft.job_id, "polytao.gateway-confirmed")
  }
  assert.deepEqual(transport.map(call => call.request.params.name),
    ["confirm_polytao_generation", "confirm_polytao_generation", "get_polytao_draft"])
  assert.ok(transport.every(call => call.url === "http://capability-gateway-dev:3100/mcp"
    && call.token === "Bearer " + "u".repeat(48)
    && call.request.params.arguments.conversation_id === sessionId))
  assert.equal(f.receipts.length, 1)
})
