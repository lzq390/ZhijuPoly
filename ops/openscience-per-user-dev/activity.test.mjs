import assert from "node:assert/strict"
import test from "node:test"
import { createActivityTracker } from "./activity.mjs"

const blank = () => ({ statuses: {}, permissions: [], questions: [] })
const req = (method, path) => ({ req: { method, path }, json: (body, status) => ({ body, status }) })

test("examines all current directories, including prompts awaiting an answer", async () => {
  const project = blank()
  const tracker = createActivityTracker(async () => [blank(), project])
  for (const type of ["busy", "retry", "compacting"]) {
    project.statuses = { example: { type } }
    assert.equal((await tracker.inspect()).reason, "active_session")
  }
  project.statuses = {}
  for (const field of ["permissions", "questions"]) {
    project[field].push({})
    assert.equal((await tracker.inspect()).reason, "pending_input")
    project[field].pop()
  }
  assert.equal((await tracker.inspect()).idle, true)
})

test("a failed or malformed activity check keeps the workspace", async () => {
  for (const read of [async () => { throw Error("context changed") }, async () => [{ statuses: [] }]]) {
    assert.deepEqual(await createActivityTracker(read).inspect(), {
      idle: false, reason: "activity_unknown", last_activity_ms: null,
    })
  }
})

test("an open event stream and health polls do not extend the idle time", async () => {
  const tracker = createActivityTracker(async () => [blank()], () => false)
  let close
  const stream = tracker.middleware(req("GET", "/global/event"), () => new Promise(resolve => { close = resolve }))
  await tracker.middleware(req("GET", "/global/health"), async () => {})
  await tracker.middleware(req("GET", "/session/status"), async () => {})
  await tracker.middleware(req("POST", "/log"), async () => {})
  assert.deepEqual(await tracker.inspect(), { idle: true, reason: "idle", last_activity_ms: null })
  close()
  await stream
})

test("draining rejects new work and a write crossing the snapshot keeps the instance", async () => {
  let drain = false
  let finish
  let snapshot
  const tracker = createActivityTracker(
    () => new Promise(resolve => { snapshot = () => resolve([blank()]) }), () => drain,
  )
  const observing = tracker.inspect()
  const write = tracker.middleware(req("POST", "/session/example/message"),
    () => new Promise(resolve => { finish = resolve }))
  assert.equal((await tracker.inspect()).reason, "active_request")
  drain = true
  const denied = await tracker.middleware(req("PUT", "/file/content"), () => assert.fail("write admitted"))
  assert.equal(denied.status, 503)
  finish()
  await write
  snapshot()
  const result = await observing
  assert.equal(result.idle, false)
  assert.ok(result.last_activity_ms > 0)
  const next = tracker.inspect()
  snapshot()
  assert.equal((await next).idle, true)
})

test("background turn completion resets idle time while heartbeat events do not", async () => {
  const tracker = createActivityTracker(async () => [])
  tracker.onEvent({ payload: { type: "server.heartbeat" } })
  assert.equal((await tracker.inspect()).last_activity_ms, null)
  tracker.onEvent({ payload: { type: "session.status", properties: { status: { type: "idle" } } } })
  assert.ok((await tracker.inspect()).last_activity_ms > 0)
})
