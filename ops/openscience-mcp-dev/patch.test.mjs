import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import test from "node:test"
import { CONTENT_ADAPTER, PATCH_MARKER, patchPrompt } from "./patch.mjs"

const source = `// representative MCP adapter, with exact upstream anchors
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
