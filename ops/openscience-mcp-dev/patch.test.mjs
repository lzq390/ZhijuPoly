import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import test from "node:test"
import { CONTENT_ADAPTER, CONVERSATION_ADAPTER, CONVERSATION_SCHEMA_ADAPTER, TRUSTED_USER_PART_ADAPTER, PATCH_MARKER, patchPrompt } from "./patch.mjs"

const bindingGuard = `        if (key === "knowledge_service_kb_search") {
          const bound = await bindings(ctx.sessionID)
          if (!bound.length) {
            const output = "No knowledge bases are bound to this session"
            return {
              title: "",
              metadata: {},
              output,
              attachments: [],
              content: [{ type: "text", text: output }],
              isError: true,
            }
          }
          args = { ...(args as Record<string, unknown>), kb_ids: bound }
        }
`
const source = `// representative deployed MCP adapter, with session binding and exact upstream anchors
    for (const [key, item] of Object.entries(mcp)) {
${bindingGuard}        const result = await execute(args, opts)

        const textParts: string[] = []
        const attachments: MessageV2.FilePart[] = []

        for (const contentItem of result.content) {
          if (contentItem.type === "text") textParts.push(contentItem.text)
        }
        const truncated = await Truncate.output(textParts.join("\\n\\n"), {}, input.agent, input.session.id)
        return {
          output: truncated.content,
          content: result.content, // directly return content to preserve ordering when outputting to model
        }
`
const hash = (value) => createHash("sha256").update(value).digest("hex")
const adapt = new Function("result", CONTENT_ADAPTER.replace(" as const", "") + "\nreturn modelContent")

test("source transformation makes structured JSON available in saved output and model content", () => {
  const patched = patchPrompt(source, hash(source))
  assert.ok(patched.includes(PATCH_MARKER))
  assert.ok(patched.includes("for (const contentItem of modelContent)"))
  assert.ok(patched.includes("content: modelContent,"))
  assert.ok(patched.includes("output: truncated.content"))
  assert.ok(!patched.includes("of result.content"))
  assert.ok(patched.includes(bindingGuard), "The deployed session knowledge-base binding guard must stay intact")
  assert.ok(patched.indexOf(bindingGuard) < patched.indexOf(PATCH_MARKER))
})

test("unexpected source, repeated anchors and already applied patches fail closed", () => {
  assert.throws(() => patchPrompt(source), /Unexpected/)
  assert.throws(() => patchPrompt(source + source, hash(source + source)), /ambiguous/)
  const patched = patchPrompt(source, hash(source))
  assert.throws(() => patchPrompt(patched, hash(patched)), /already installed/)
  const missing = "unrelated source"
  assert.throws(() => patchPrompt(missing, hash(missing)), /absent/)
})

test("catalog structured content preserves revision, exact keys, Unicode units and all fields", () => {
  const structuredContent = { ok: true, dataset_revision: "revision", options: [
    { option_key: "std:tg:C", unit: "°C", min_value: 200, max_value: 300 },
  ] }
  const original = { content: [{ type: "text", text: "Returned one property." }], structuredContent }
  const result = adapt(original)
  assert.equal(result[0], original.content[0])
  assert.deepEqual(JSON.parse(result[1].text), structuredContent)
  assert.equal(original.content.length, 1)
  assert.equal(result[1].type, "text")
})

test("measurement evidence, errors and media retain order and values", () => {
  const content = [{ type: "text", text: "summary" }, { type: "image", data: "encoded", mimeType: "image/png" }]
  const structuredContent = { ok: false, error: { code: "DATASET_REVISION_CHANGED", message: "Reload catalog" } }
  const result = adapt({ content, structuredContent, isError: true })
  assert.deepEqual(result.slice(0, 2), content)
  assert.deepEqual(JSON.parse(result[2].text), structuredContent)
})

test("tools without structured results keep their original content unchanged", () => {
  const content = [{ type: "text", text: "legacy tool result" }]
  assert.equal(adapt({ content }), content)
  assert.deepEqual(JSON.parse(adapt({ content: [], structuredContent: {} })[0].text), {})
})

const injectConversation = new Function("key", "args", "input", CONVERSATION_ADAPTER + "\nreturn args")
const hideConversation = new Function("key", "item", "jsonSchema", CONVERSATION_SCHEMA_ADAPTER + "\nreturn item")

test("trusted receipt and continuation callbacks create new synthetic user turns with their own metadata", async () => {
  const persisted = [], model = { providerID: "provider", modelID: "model" }
  const persist = new Function("Session", "SessionPrompt", `return (${TRUSTED_USER_PART_ADAPTER})`)(
    { messages: async () => [{ info: { role: "user", agent: "research", model } }] },
    { prompt: async input => { persisted.push(input); return input } },
  )
  const receipt = { job_id: "original-job", draft_id: "original-draft" }
  const event = { ...receipt, event: "readback_requested" }
  await persist("ses_actual", "Initial server receipt", receipt, "nexpoly_retrosynthesis_receipt")
  await persist("ses_actual", "Continue original task", event, "nexpoly_retrosynthesis_continuation")
  assert.equal(persisted.length, 2)
  for (const turn of persisted) {
    assert.equal(turn.noReply, true); assert.equal(turn.agent, "research"); assert.equal(turn.model, model)
    assert.equal(turn.parts[0].synthetic, true); assert.equal(turn.parts[0].type, "text")
    assert.equal(Object.hasOwn(turn, "messageID"), false, "prompt creates a new message instead of editing the receipt")
  }
  assert.deepEqual(persisted[0].parts[0].metadata, { nexpoly_retrosynthesis_receipt: receipt })
  assert.deepEqual(persisted[1].parts[0].metadata, { nexpoly_retrosynthesis_continuation: event })
})

test("draft ownership is injected only for exact gateway prepare/get tools and model schema hides it", () => {
  for (const key of ["capability_gateway_prepare_polytao_generation", "capability_gateway_get_polytao_draft",
    "capability_gateway_prepare_monomer_retrosynthesis", "capability_gateway_resolve_retrosynthesis_target",
    "capability_gateway_get_retrosynthesis_draft", "capability_gateway_get_retrosynthesis_job"]) {
    const args = { conversation_id: "ses_forged", descriptors: { MolWt: 28 } }
    assert.deepEqual(injectConversation(key, args, { session: { id: "ses_actual" } }),
      { ...args, conversation_id: "ses_actual" })
    const native = { type: "object", properties: { conversation_id: { type: "string" }, draft_id: { type: "string" } },
      required: ["conversation_id", "draft_id"] }
    const item = hideConversation(key, { inputSchema: { jsonSchema: native } }, schema => ({ jsonSchema: schema }))
    assert.deepEqual(item.inputSchema.jsonSchema.required, ["draft_id"])
    assert.ok(!Object.hasOwn(item.inputSchema.jsonSchema.properties, "conversation_id"))
    assert.ok(Object.hasOwn(native.properties, "conversation_id"))
  }
  for (const key of ["knowledge_service_kb_search", "capability_gateway_search_polymers", "capability_gateway_polymerize_monomers",
    "fake_capability_gateway_prepare_polytao_generation", "capability_gateway_confirm_polytao_generation"]) {
    const args = { conversation_id: "original" }
    assert.equal(injectConversation(key, args, { session: { id: "ses_actual" } }), args)
    const item = { inputSchema: { jsonSchema: { properties: { conversation_id: {} }, required: ["conversation_id"] } } }
    assert.equal(hideConversation(key, item, () => { throw new Error("Unrelated schema must stay intact") }), item)
  }
})
