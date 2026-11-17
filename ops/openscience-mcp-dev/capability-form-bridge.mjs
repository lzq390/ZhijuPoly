/** Local UI actions use a separate MCP credential; no native generation REST calls. */
import fs from "node:fs/promises"
import { constants } from "node:fs"
import { randomUUID } from "node:crypto"

const MCP_URL = "http://capability-gateway-dev:3100/mcp"
const TOKEN_FILE = "/run/openscience/gateway-ui-token"
const SESSION = /^ses_[A-Za-z0-9_-]{1,124}$/
const object = value => value !== null && typeof value === "object" && !Array.isArray(value)

class BridgeError extends Error {
  constructor(code, message, status = 400) { super(message); this.code = code; this.status = status }
}

export function validateCapabilityDraftAction(adapter, body) {
  const common = ["session_id", "directory", "action", "draft_id", "revision"]
  if (!object(body) || !adapter.actions.includes(body.action) || !SESSION.test(body.session_id)
      || !adapter.draftPattern.test(body.draft_id) || typeof body.directory !== "string"
      || !Number.isSafeInteger(body.revision) || body.revision < 1) {
    throw new BridgeError("INVALID_ACTION", "Invalid draft action or conversation identifier.")
  }
  const extra = adapter.extraFields[body.action] ?? []
  if (Object.keys(body).some(key => ![...common, ...extra].includes(key)) || !adapter.validExtra(body)) {
    throw new BridgeError("INVALID_ACTION", "Unsupported or invalid draft fields.")
  }
  return body
}

function toolOutput(adapter, part) {
  if (part?.type !== "tool" || !adapter.prepareTools.includes(part.tool)
      || part.state?.status !== "completed" || typeof part.state.output !== "string") return null
  const output = part.state.output.trim()
  for (const candidate of [output, output.slice(output.lastIndexOf("\n") + 1).trim()]) {
    try {
      const value = JSON.parse(candidate)
      if (object(value) && value.ok === true && !Object.hasOwn(value, "error")) return value
    } catch { /* Only completed authoritative tool output can establish ownership. */ }
  }
  return null
}

export function capabilitySessionContainsDraft(adapter, messages, sessionId, draftId) {
  return Array.isArray(messages) && messages.some(message => message.info?.role === "assistant"
    && message.info.sessionID === sessionId && message.parts?.some(part => {
      const value = toolOutput(adapter, part)
      return value?.draft_id === draftId && value.conversation_id === sessionId
    }))
}

function receiptExists(adapter, messages, body, jobId) {
  return messages.some(message => message.info?.role === "user" && message.parts?.some(part => {
    const receipt = part.type === "text" && part.synthetic === true && part.metadata?.[adapter.receiptKey]
    return receipt?.draft_id === body.draft_id && receipt.conversation_id === body.session_id
      && receipt.job_id === jobId
  }))
}

async function privateToken(filename) {
  const handle = await fs.open(filename, constants.O_RDONLY | constants.O_NOFOLLOW)
  try {
    const stat = await handle.stat()
    if (!stat.isFile() || (stat.mode & 0o077) || stat.uid !== process.geteuid()
        || stat.nlink !== 1 || stat.size > 512) throw new Error("Invalid private credential")
    const token = (await handle.readFile("utf8")).trim()
    if (!/^[A-Za-z0-9_-]{32,256}$/.test(token)) throw new Error("Invalid private credential")
    return token
  } finally { await handle.close() }
}

async function limitedText(source, maximum, tooLarge = () => new Error("MCP response too large")) {
  if (!source.body) return ""
  const chunks = [], reader = source.body.getReader()
  let size = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > maximum) throw tooLarge()
      chunks.push(value)
    }
    const bytes = new Uint8Array(size)
    let offset = 0
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
    return new TextDecoder().decode(bytes)
  } finally { await reader.cancel().catch(() => {}) }
}

async function limitedJson(response) {
  return JSON.parse(await limitedText(response, 256 * 1024))
}

/** Exact transport endpoint and client credential are never supplied by the browser. */
export function createCapabilityUiMcpCaller(adapter, { fetchImpl = fetch, tokenFile = TOKEN_FILE, readToken = privateToken } = {}) {
  return async (name, args) => {
    if (!adapter.uiTools.includes(name)) {
      throw new BridgeError("TOOL_DENIED", "Unsupported draft tool.", 403)
    }
    try {
      const token = await readToken(tokenFile)
      const response = await fetchImpl(MCP_URL, { method: "POST", redirect: "error",
        signal: AbortSignal.timeout(30000), headers: { "content-type": "application/json",
          accept: "application/json, text/event-stream", authorization: `Bearer ${token}`,
          "mcp-protocol-version": "2025-11-25" }, body: JSON.stringify({ jsonrpc: "2.0", id: randomUUID(),
          method: "tools/call", params: { name, arguments: args } }) })
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("MCP unavailable")
      const result = (await limitedJson(response)).result
      const output = result?.structuredContent
      if (!object(output)) throw new Error("Invalid MCP result")
      if (result.isError || output.ok !== true) {
        const error = output.error
        throw new BridgeError(typeof error?.code === "string" ? error.code : "MCP_TOOL_FAILED",
          typeof error?.message === "string" ? error.message : "Draft operation failed.", 409)
      }
      return output
    } catch (error) {
      if (error instanceof BridgeError) throw error
      throw new BridgeError("MCP_UNAVAILABLE", "Draft service is unavailable. Retry this same draft; do not create another generation.", 503)
    }
  }
}

/** Hooks run in OpenScience's own session context; UI requests cannot forge them. */
export function createCapabilityFormBoundary(adapter, { withSession, getMessages, persistReceipt, resume, assertNotBusy,
  callMcp = createCapabilityUiMcpCaller(adapter), validateDirectory,
  } = {}) {
  const active = new Set()
  return async (c, next) => {
    if (c.req.path.replace(/\/+$/, "") !== adapter.route) return next()
    if (c.req.method !== "POST") return c.json({ error: "Use a draft action." }, 405)
    c.header?.("cache-control", "no-store")
    let body, lock, acquired = false
    try {
      if (!c.req.header("content-type")?.toLowerCase().startsWith("application/json")) throw new BridgeError("INVALID_ACTION", "JSON draft action required.", 415)
      const raw = await limitedText(c.req.raw, 32768,
        () => new BridgeError("ACTION_TOO_LARGE", "Draft action exceeds 32 KiB.", 413))
      try { body = validateCapabilityDraftAction(adapter, JSON.parse(raw)) }
      catch (error) { if (error instanceof BridgeError) throw error; throw new BridgeError("INVALID_ACTION", "Invalid JSON draft action.") }
      const directory = await validateDirectory(body.directory)
      lock = `${directory}\0${body.session_id}`
      if (active.has(lock)) throw new BridgeError("DRAFT_BUSY", "A draft action is already in progress for this conversation.", 409)
      active.add(lock)
      acquired = true
      return await withSession(directory, async () => {
        let messages
        try { messages = await getMessages(body.session_id) }
        catch { throw new BridgeError("DRAFT_NOT_IN_SESSION", "Draft is not part of this conversation.", 403) }
        if (!capabilitySessionContainsDraft(adapter, messages, body.session_id, body.draft_id)) {
          throw new BridgeError("DRAFT_NOT_IN_SESSION", "Draft is not part of this conversation.", 403)
        }
        const { name, args } = adapter.call(body)
        const draft = await callMcp(name, { ...args, conversation_id: body.session_id })
        if (body.action === "resolve") return c.json({ ok: true, resolution: draft })
        if (draft.draft_id !== body.draft_id || draft.conversation_id !== body.session_id) {
          throw new BridgeError("INVALID_DRAFT_RESPONSE", "Draft service returned an unexpected conversation.", 502)
        }
        let continuation
        if (["confirm", "continue"].includes(body.action) && draft.status === "submitted" && typeof draft.job_id === "string") {
          try {
            const existing = receiptExists(adapter, messages, body, draft.job_id)
            if (existing && body.action === "confirm") {
              return c.json({ ok: true, draft, continuation: { status: "existing" } })
            }
            if (assertNotBusy) await assertNotBusy(body.session_id)
            if (!existing) {
              const receipt = { draft_id: draft.draft_id, conversation_id: body.session_id,
                revision: draft.revision, job_id: draft.job_id, submission: draft.submission }
              const text = adapter.receiptText + "\n" + JSON.stringify(receipt)
              await persistReceipt(body.session_id, text, receipt, adapter.receiptKey)
            }
            // loop() failures are surfaced by OpenScience's normal session event channel.
            Promise.resolve(resume(body.session_id)).catch(() => {})
            continuation = { status: existing ? "existing" : "started" }
          } catch { continuation = { status: "failed", message: "任务已提交，继续对话暂不可用。请点击继续查询；不会再次提交生成。" } }
        }
        return c.json({ ok: true, draft, ...(continuation ? { continuation } : {}) })
      })
    } catch (error) {
      const known = error instanceof BridgeError
      return c.json({ ok: false, error: { code: known ? error.code : "DRAFT_UNAVAILABLE",
        message: known ? error.message : "Draft action is unavailable." } }, known ? error.status : 503)
    } finally { if (acquired) active.delete(lock) }
  }
}
