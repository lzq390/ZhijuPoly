import assert from "node:assert/strict"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import test from "node:test"
import { createHash } from "node:crypto"
import { ALLOWED_TOOL_KEYS, configurationPath, devBoundary, forbiddenOperation,
  sanitizeConfiguration, sessionInputAllowed, workspacePath } from "./dev-boundary.mjs"
import { patchServer } from "./patch.mjs"

const SECRET = "synthetic-secret-do-not-return"

test("configuration endpoints include provider lists, global raw and mutation responses", () => {
  for (const endpoint of ["/config", "/config/", "/config/providers", "/provider", "/global/config",
    "/global/config/raw", "/global/config/unset", "/%63onfig"]) assert.ok(configurationPath(endpoint))
  assert.ok(!configurationPath("/session"))
})

test("response redaction preserves shape and never mutates internal configuration", () => {
  const original = { provider: { lab: { name: "lab", models: { "model-a": {} },
    options: { apiKey: SECRET, baseURL: `https://name:${SECRET}@models.example/v1?api_key=${SECRET}` }, key: SECRET } },
    mcp: { dataset: { type: "remote", headers: { Authorization: "Bearer " + SECRET, "Custom-Credential": SECRET },
      environment: { SECRET_ENV: SECRET } } }, providers: [{ options: { apiKey: SECRET }, key: SECRET }],
    token: SECRET, clientSecret: SECRET, count: 3, enabled: true, unknown: null }
  const sanitized = sanitizeConfiguration(original)
  assert.ok(!JSON.stringify(sanitized).includes(SECRET))
  assert.ok(JSON.stringify(original).includes(SECRET))
  assert.equal(sanitized.provider.lab.options.apiKey, "")
  assert.equal(sanitized.provider.lab.name, "lab")
  assert.deepEqual(sanitized.provider.lab.models, { "model-a": {} })
  assert.equal(sanitized.count, 3)
  assert.equal(sanitized.enabled, true)
  assert.equal(sanitized.unknown, null)
})

test("workspace files stay usable while traversal and symlink escape fail closed", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "nexpoly-boundary-"))
  try {
    const projects = path.join(root, "projects"), general = path.join(root, "general")
    const project = path.join(projects, "test"), outside = path.join(root, "private")
    for (const directory of [project, general, outside]) await fs.mkdir(directory, { recursive: true })
    await fs.writeFile(path.join(project, "data.txt"), "workspace data")
    await fs.writeFile(path.join(outside, "secret"), SECRET)
    await fs.symlink(outside, path.join(project, "escape"))
    await fs.symlink(path.join(outside, "missing"), path.join(project, "dangling"))
    const roots = [projects, general]
    assert.equal(await workspacePath(project, "data.txt", roots), path.join(project, "data.txt"))
    assert.equal(await workspacePath(project, "new/subdir/file.txt", roots), path.join(project, "new/subdir/file.txt"))
    for (const file of ["../../private/secret", path.join(outside, "secret"), "escape/secret", "dangling"]) {
      await assert.rejects(workspacePath(project, file, roots))
    }
    await assert.rejects(workspacePath(outside, "secret", roots))
  } finally { await fs.rm(root, { recursive: true, force: true }) }
})

test("fixed-config client cannot spawn shells, replace MCP transports or change credentials", () => {
  for (const [method, endpoint] of [["POST", "/pty"], ["GET", "/pty/id/connect"],
    ["POST", "/session/id/shell"], ["POST", "/session/id/command"], ["PUT", "/mcp/name/config"],
    ["POST", "/mcp"], ["PATCH", "/config"], ["PUT", "/global/config/raw"],
    ["POST", "/experimental/worktree"], ["POST", "/api/repo/push"], ["POST", "/%70ty"]]) {
    assert.ok(forbiddenOperation(method, endpoint), method + " " + endpoint)
  }
  for (const [method, endpoint] of [["POST", "/session"], ["POST", "/session/id/prompt_async"],
    ["POST", "/session/id/message"], ["GET", "/config"], ["GET", "/mcp"], ["GET", "/event"]]) {
    assert.ok(!forbiddenOperation(method, endpoint))
  }
})

test("normal UI prompts remain valid and session overrides cannot broaden tool grants", () => {
  assert.ok(sessionInputAllowed({ parts: [{ type: "text", text: "Find polymers" }] }))
  assert.ok(sessionInputAllowed({ agent: "property-filter", parts: [{ type: "text", text: "Find polymers" }] }))
  assert.ok(sessionInputAllowed({ permission: [...ALLOWED_TOOL_KEYS].map(name => ({ permission: "mcp", pattern: name, action: "allow" })) }))
  assert.ok(sessionInputAllowed({ tools: { "*": false } }))
  for (const body of [{ agent: "research" }, { permission: [{ permission: "*", pattern: "*", action: "allow" }] },
    { permission: [{ permission: "mcp", pattern: "*", action: "allow" }] }, { tools: { bash: true } },
    { parts: [{ type: "subtask", agent: "general" }] }]) assert.ok(!sessionInputAllowed(body))
})

function context(url, { method = "GET", body, payload = {} } = {}) {
  const request = new Request("http://localhost:4096" + url, { method,
    ...(body === undefined ? {} : { body: JSON.stringify(body), headers: { "content-type": "application/json" } }) })
  const parsed = new URL(request.url)
  return { req: { path: parsed.pathname, method, raw: request, query: name => parsed.searchParams.get(name),
    header: name => request.headers.get(name) },
    res: new Response(JSON.stringify(payload), { headers: { "content-type": "application/json" } }),
    json: (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } }) }
}

test("middleware sanitizes live responses and suppresses raw editor content", async () => {
  const c = context("/config", { payload: { provider: { demo: { options: { apiKey: SECRET } } } } })
  await devBoundary(c, async () => {})
  assert.ok(!(await c.res.text()).includes(SECRET))
  assert.equal(c.res.headers.get("cache-control"), "no-store")
  const raw = context("/global/config/raw", { payload: { content: `// comment\n{\"apiKey\":\"${SECRET}\"}`, path: "/configuration.jsonc" } })
  await devBoundary(raw, async () => {})
  assert.deepEqual(await raw.res.json(), { content: "{}\n", path: "/configuration.jsonc" })
  const forbidden = context("/mcp", { method: "POST", body: { type: "local" } })
  let called = false
  const response = await devBoundary(forbidden, async () => { called = true })
  assert.equal(response.status, 403)
  assert.ok(!called)
})

test("server hook validates its upstream source and registers before global APIs", () => {
  const source = 'import { Session } from "../session"\n        .route("/global", GlobalRoutes())'
  const digest = createHash("sha256").update(source).digest("hex")
  const patched = patchServer(source, digest)
  assert.ok(patched.includes('.use(devBoundary)\n        .route("/global", GlobalRoutes())'))
  assert.throws(() => patchServer(source), /Unexpected/)
  assert.throws(() => patchServer(patched), /already installed/)
})
