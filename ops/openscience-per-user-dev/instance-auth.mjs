import { createHash, timingSafeEqual } from "node:crypto"
import { readFile } from "node:fs/promises"

const tokenFile = "/run/nexpoly-m1/token"
const digest = (value) => createHash("sha256").update(value).digest()

// Wrap Bun.serve's network fetch; trusted in-process OpenScience plugin calls
// keep their existing internal transport. Forward the original upgrade request.
export function withInstanceAuth(dispatch) {
  return async (request, server) => {
    const header = request.headers.get("authorization") ?? ""
    const match = /^Bearer ([A-Za-z0-9._~+/-]+=*)$/i.exec(header)
    if (match && match[1].length <= 512) {
      const expected = (await readFile(tokenFile, "utf8").catch(() => "")).trim()
      if (expected.length >= 32 && timingSafeEqual(digest(expected), digest(match[1]))) {
        // The per-instance secret must never be forwarded to downstream services.
        request.headers.delete("authorization")
        return dispatch(request, server)
      }
    }
    return new Response(null, {
      status: 401,
      headers: { "Cache-Control": "no-store", "WWW-Authenticate": 'Bearer realm="OpenScience"' },
    })
  }
}
