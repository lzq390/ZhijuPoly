#!/usr/bin/env node
/** Development-image compatibility patch for OpenScience's MCP result adapter.
 * The source hash and unique anchors fail closed on an unexpected upstream image.
 * Never run this against the existing OpenScience container or host installation.
 */
import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"
import { resolve } from "node:path"
import { pathToFileURL } from "node:url"

export const EXPECTED_BASE_IMAGE = "sha256:41a2b6929bc74a1bf3c0bac54df09f8e5f835954b3ba58dfba81f703b3ffe17a"
export const EXPECTED_PROMPT_SHA256 = "d9f9a16c6a2710502143942271ce22c49fe7ccfe78694522b5a76d3a48d9866a"
export const EXPECTED_SERVER_SHA256 = "e9d7a5a148f60e3f329c981be73502ca5453f0c3136596d8dd986fb442be4fd8"
export const PATCH_MARKER = "nexpoly-mcp-structured-content-v1"

// Export the emitted expression so the test evaluates exactly the installed adapter.
export const CONTENT_ADAPTER = `        // ${PATCH_MARKER}: preserve structured results for both model input and saved tool output.
        const modelContent = result.structuredContent === undefined
          ? result.content
          : [
              ...result.content,
              { type: "text" as const, text: JSON.stringify(result.structuredContent) },
            ]
`

const INSERT_ANCHOR = '        const textParts: string[] = []\n        const attachments: MessageV2.FilePart[] = []\n\n        for (const contentItem of result.content) {'
const RETURN_ANCHOR = '          content: result.content, // directly return content to preserve ordering when outputting to model'

function replaceOnce(source, needle, replacement) {
  if (source.split(needle).length !== 2) throw new Error("OpenScience compatibility anchor is absent or ambiguous")
  return source.replace(needle, replacement)
}

export function patchPrompt(source, expectedHash = EXPECTED_PROMPT_SHA256) {
  if (source.includes(PATCH_MARKER)) throw new Error("OpenScience compatibility patch is already installed")
  const actualHash = createHash("sha256").update(source).digest("hex")
  if (actualHash !== expectedHash) throw new Error("Unexpected OpenScience prompt source; refusing compatibility patch")
  let patched = replaceOnce(source, INSERT_ANCHOR,
    CONTENT_ADAPTER + '\n' + INSERT_ANCHOR.replace("of result.content", "of modelContent"))
  patched = replaceOnce(patched, RETURN_ANCHOR,
    '          content: modelContent, // preserve MCP content and serialized structuredContent for the model')
  return patched
}

export function patchServer(source, expectedHash = EXPECTED_SERVER_SHA256) {
  if (source.includes("./nexpoly-dev-boundary.mjs")) throw new Error("Development HTTP boundary is already installed")
  if (createHash("sha256").update(source).digest("hex") !== expectedHash) {
    throw new Error("Unexpected OpenScience server source; refusing HTTP boundary patch")
  }
  let patched = replaceOnce(source, 'import { Session } from "../session"',
    'import { Session } from "../session"\nimport { devBoundary } from "./nexpoly-dev-boundary.mjs"')
  return replaceOnce(patched, '        .route("/global", GlobalRoutes())',
    '        .use(devBoundary)\n        .route("/global", GlobalRoutes())')
}

async function main() {
  const target = process.argv[2]
  if (process.argv.length !== 3 || target !== "/app/backend/cli/src/session/prompt.ts") {
    throw new Error("Use only during the isolated development image build, targeting its prompt.ts")
  }
  const source = await readFile(target, "utf8")
  const patched = patchPrompt(source)
  const serverTarget = "/app/backend/cli/src/server/server.ts"
  const serverSource = await readFile(serverTarget, "utf8")
  const patchedServer = patchServer(serverSource)
  // Validate both sources before changing either image-layer file.
  await writeFile(target, patched, "utf8")
  await writeFile(serverTarget, patchedServer, "utf8")
  console.log(JSON.stringify({ patch: PATCH_MARKER, base_image: EXPECTED_BASE_IMAGE,
    original_sha256: EXPECTED_PROMPT_SHA256,
    patched_sha256: createHash("sha256").update(patched).digest("hex"),
    server_patched_sha256: createHash("sha256").update(patchedServer).digest("hex") }))
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(error.message)
    process.exitCode = 1
  })
}
