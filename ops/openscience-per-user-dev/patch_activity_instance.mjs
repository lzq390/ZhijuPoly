// The current server exposes per-directory status. Iterate the same live contexts
// used by requests, including contexts whose directory has since been renamed.
import { createHash } from "node:crypto"
import { readFile, writeFile } from "node:fs/promises"

const target = "/app/backend/cli/src/project/instance.ts"
const expected = "45389dd32e82cc23e4c2b55440c2bfc9386dfe4c7c86a25880eb283de07fe832"
const anchor = "  async disposeAll() {"
const addition = `  async inspectActive<T>(read: () => Promise<T>): Promise<T[]> {
    const entries = [...cache.entries()]
    const states = await Promise.all(entries.map(async ([, pending]) => {
      const ctx = await pending
      return context.provide(ctx, read)
    }))
    if (entries.length !== cache.size || entries.some(([key, value]) => cache.get(key) !== value)) {
      throw new Error("Workspace contexts changed during activity check")
    }
    return states
  },
`
if (process.argv.length !== 3 || process.argv[2] !== target) throw new Error("Unexpected M1 source path")
const source = await readFile(target, "utf8")
if (createHash("sha256").update(source).digest("hex") !== expected || source.split(anchor).length !== 2) {
  throw new Error("M1 activity adapter requires the reviewed November instance source")
}
await writeFile(target, source.replace(anchor, addition + anchor))
