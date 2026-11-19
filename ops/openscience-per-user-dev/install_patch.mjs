import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"
import { patchPrompt, patchServer } from "./patch.mjs"

// The same adapters and exact November input/output hashes verified in M0.
const sourceRoot = "/app/backend/cli/src"
const files = [
  {
    path: sourceRoot + "/session/prompt.ts",
    patch: patchPrompt,
    input: "e46ab01515c64c97682dd4a78a79ef79c1dc0bcb156329811b6531b0433b417f",
    output: "0b8c811b1872384f2ca22a576eb655fca8eb468a5d31aae307cb1b82f66704a1",
  },
  {
    path: sourceRoot + "/server/server.ts",
    patch: patchServer,
    input: "e9d7a5a148f60e3f329c981be73502ca5453f0c3136596d8dd986fb442be4fd8",
    output: "6cd905fbbe5ae5986ac3fa6d409b6e5b8a09dc691cf08c0868e73d964e240d4a",
  },
]

function replaceOnce(text, anchor, replacement) {
  if (text.split(anchor).length !== 2) throw new Error("Missing or ambiguous M1 server anchor")
  return text.replace(anchor, replacement)
}

function installInstanceBoundary(server) {
  const imports = [
    'import { withInstanceAuth } from "./nexpoly-m1-auth.mjs"',
    'import { createActivityMiddleware } from "./nexpoly-m1-activity.mjs"',
    'import { SessionStatus } from "../session/status"',
    'import { Question } from "../question"',
    'import { GlobalBus } from "@/bus/global"',
  ]
  server = replaceOnce(server, 'import { Session } from "../session"', imports.join("\n") + '\nimport { Session } from "../session"')
  server = replaceOnce(server, "      fetch: App().fetch,", "      fetch: withInstanceAuth(App().fetch),")
  return replaceOnce(server, "        .use(devBoundary)", [
    "        .use(createActivityMiddleware(() => Instance.inspectActive(async () => ({",
    "          statuses: SessionStatus.list(), permissions: await PermissionNext.list(), questions: await Question.list(),",
    '        })), listener => GlobalBus.on("event", listener)))',
    "        .use(devBoundary)",
  ].join("\n"))
}

const transpiler = new Bun.Transpiler({ loader: "ts" })
for (const file of files) {
  let patched = file.patch(await readFile(file.path, "utf8"), file.input)
  if (createHash("sha256").update(patched).digest("hex") !== file.output) {
    throw new Error("Unexpected patch output for " + file.path)
  }
  if (file.patch === patchServer) patched = installInstanceBoundary(patched)
  transpiler.transformSync(patched)
  await writeFile(file.path, patched)
}
