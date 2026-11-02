/** HTTP boundary for the fixed, three-tool development UI; never used upstream. */
import fs from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const DEFAULT_ROOTS = ["/openscience/files/projects", "/openscience/files/general"]
export const ALLOWED_TOOL_KEYS = new Set([
  "nexpoly_property_filter_list_filter_properties",
  "nexpoly_property_filter_search_polymers",
  "nexpoly_property_filter_get_property_histogram",
])
const SENSITIVE_KEYS = new Set([
  "key", "apikey", "xapikey", "authorization", "proxyauthorization", "password", "secret",
  "clientsecret", "token", "accesstoken", "refreshtoken", "idtoken", "cookie", "setcookie",
])

function routePath(input) {
  try { input = decodeURIComponent(input) } catch { /* Invalid paths cannot match a protected route. */ }
  return input.replace(/\/+/g, "/").replace(/\/+$/, "")
}

export function configurationPath(input) {
  const name = routePath(input)
  return name === "/config" || name === "/config/providers" || name === "/provider"
    || name === "/global/config" || name.startsWith("/global/config/")
}

function normalized(key) {
  return key.toLowerCase().replace(/[-_]/g, "")
}

/** Redact a response copy. Internal Config/Provider objects retain their secrets. */
export function sanitizeConfiguration(value, parentKey = "") {
  if (Array.isArray(value)) return value.map((item) => sanitizeConfiguration(item, parentKey))
  if (value === null || typeof value !== "object") return value
  return Object.fromEntries(Object.entries(value).map(([key, item]) => {
    const privateField = SENSITIVE_KEYS.has(normalized(key))
      || ["headers", "environment", "env"].includes(normalized(parentKey))
    if (privateField) {
      // The supported configuration/provider schema uses strings for credentials.
      // Preserve collection/value types when a future extension nests them.
      if (typeof item === "string") return [key, ""]
      if (Array.isArray(item)) return [key, []]
      if (item !== null && typeof item === "object") return [key, {}]
      return [key, item]
    }
    if (["url", "baseurl", "api"].includes(normalized(key)) && typeof item === "string") {
      try {
        const address = new URL(item)
        address.username = ""
        address.password = ""
        for (const parameter of address.searchParams.keys()) {
          if (SENSITIVE_KEYS.has(normalized(parameter))) address.searchParams.set(parameter, "")
        }
        return [key, address.toString()]
      } catch { /* Ordinary model IDs or strings are not URLs. */ }
    }
    return [key, sanitizeConfiguration(item, key)]
  }))
}

function within(root, candidate) {
  const relative = path.relative(root, candidate)
  return relative === "" || (!relative.startsWith(".." + path.sep) && relative !== ".." && !path.isAbsolute(relative))
}

async function canonicalExistingOrNew(candidate) {
  try {
    // A dangling symlink must fail closed, rather than being treated as a new file.
    await fs.lstat(candidate)
    return await fs.realpath(candidate)
  } catch (error) {
    if (error.code !== "ENOENT") throw error
    try {
      await fs.lstat(candidate)
      throw new Error("Unresolvable symlink")
    } catch (check) {
      if (check.code !== "ENOENT") throw check
    }
    const parent = path.dirname(candidate)
    if (parent === candidate) throw new Error("No existing workspace parent")
    return path.join(await canonicalExistingOrNew(parent), path.basename(candidate))
  }
}

/** Resolve both directory and file through symlinks, including new-file parents. */
export async function workspacePath(directory, filename, roots = DEFAULT_ROOTS) {
  if (typeof directory !== "string" || typeof filename !== "string" || directory.includes("\0") || filename.includes("\0")) {
    throw new Error("Invalid workspace path")
  }
  const realRoots = await Promise.all(roots.map((root) => fs.realpath(root)))
  const realDirectory = await fs.realpath(directory)
  if (!realRoots.some((root) => within(root, realDirectory))) throw new Error("Directory outside development workspaces")
  const candidate = path.resolve(realDirectory, filename)
  if (!within(realDirectory, candidate)) throw new Error("Path outside selected workspace")
  const realCandidate = await canonicalExistingOrNew(candidate)
  if (!within(realDirectory, realCandidate)) throw new Error("Symlink outside selected workspace")
  return realCandidate
}

export function forbiddenOperation(method, pathname) {
  const name = routePath(pathname)
  if (name === "/pty" || name.startsWith("/pty/") || name === "/api/repo" || name.startsWith("/api/repo/")) return true
  if (/^\/session\/[^/]+\/(shell|command|init)$/.test(name)) return true
  if (name === "/experimental/worktree" && !["GET", "HEAD"].includes(method)) return true
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    if (configurationPath(name) || name === "/mcp" || name.startsWith("/mcp/")) return true
  }
  return false
}

export function sessionInputAllowed(body) {
  if (!body || typeof body !== "object") return false
  if (body.agent !== undefined && body.agent !== "property-filter") return false
  if (body.tools && Object.entries(body.tools).some(([name, allowed]) => allowed === true && !ALLOWED_TOOL_KEYS.has(name))) return false
  if (body.permission !== undefined) {
    if (!Array.isArray(body.permission)) return false
    for (const rule of body.permission) {
      if (rule.action !== "allow") continue
      if (rule.permission === "mcp" && ALLOWED_TOOL_KEYS.has(rule.pattern)) continue
      if (ALLOWED_TOOL_KEYS.has(rule.permission) && rule.pattern === "*") continue
      return false
    }
  }
  if ((body.parts ?? []).some((part) => ["agent", "subtask"].includes(part.type))) return false
  return true
}

function requestDirectory(c) {
  let directory = c.req.query("directory") || c.req.header("x-openscience-directory") || DEFAULT_ROOTS[1]
  try { directory = decodeURIComponent(directory) } catch { /* Upstream uses the original value too. */ }
  return directory
}

/** Register after Host/Origin validation and before any API route. */
export async function devBoundary(c, next) {
  const pathname = c.req.path
  if (forbiddenOperation(c.req.method, pathname)) {
    return c.json({ error: "This operation is disabled in the fixed property-filter development UI." }, 403)
  }
  const name = routePath(pathname)
  const fileRoute = name === "/file" || name === "/file/content"
  const promptRoute = c.req.method === "POST" && /^\/session\/[^/]+\/(message|prompt_async)$/.test(name)
  const createSession = c.req.method === "POST" && name === "/session"
  if (fileRoute || promptRoute || createSession) {
    try {
      const directory = requestDirectory(c)
      if (fileRoute) {
        const filename = c.req.method === "GET" || c.req.method === "HEAD"
          ? (c.req.query("path") || "")
          : (await c.req.raw.clone().json()).path
        await workspacePath(directory, filename)
      }
      if (promptRoute || createSession) {
        const raw = await c.req.raw.clone().text()
        const body = raw ? JSON.parse(raw) : {}
        if (!sessionInputAllowed(body)) {
          return c.json({ error: "Only the property-filter agent and its three read-only MCP permissions are supported." }, 403)
        }
        for (const part of body.parts ?? []) {
          if (part.type === "file" && typeof part.url === "string" && part.url.startsWith("file:")) {
            await workspacePath(directory, fileURLToPath(part.url))
          }
        }
      }
    } catch {
      return c.json({ error: "File access must remain inside the selected development workspace." }, 403)
    }
  }
  await next()
  if (!configurationPath(pathname)) return
  if (!c.res.ok || !c.res.headers.get("content-type")?.includes("application/json")) {
    c.res = c.json({ error: "Development configuration response is unavailable." }, c.res.ok ? 502 : c.res.status)
    return
  }
  try {
    const payload = await c.res.clone().json()
    const sanitized = sanitizeConfiguration(payload)
    // The advanced raw editor is outside this fixed-config client. Never return
    // original JSONC text, whose secrets cannot be identified by response keys.
    if (name === "/global/config/raw" && typeof sanitized.content === "string") sanitized.content = "{}\n"
    const headers = new Headers(c.res.headers)
    headers.delete("content-length")
    headers.set("cache-control", "no-store")
    c.res = new Response(JSON.stringify(sanitized), { status: c.res.status, headers })
  } catch {
    c.res = c.json({ error: "Development configuration response is unavailable." }, 502)
  }
}
