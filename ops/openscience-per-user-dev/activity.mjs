// The lifecycle socket is private to the backend mount namespace. No management
// endpoint is exposed through the application listener or the model sidecar.
import { chmodSync, lstatSync, unlinkSync } from "node:fs"

const SOCKET = "/tmp/nexpoly-m1-activity.sock"
const CONTROL = "/run/nexpoly-m1-control"
const passiveMethods = new Set(["GET", "HEAD", "OPTIONS"])
const businessEvents = new Set(["session.status", "message.updated", "message.part.updated"])
let probeServer

function draining() {
  try {
    lstatSync(CONTROL + "/draining")
    return true
  } catch (error) {
    if (error.code !== "ENOENT") return true
    try { return !lstatSync(CONTROL).isDirectory() } catch { return true }
  }
}

function directoryState(snapshot) {
  if (!snapshot || typeof snapshot.statuses !== "object" || !snapshot.statuses ||
      Array.isArray(snapshot.statuses) || !Array.isArray(snapshot.permissions) || !Array.isArray(snapshot.questions)) {
    throw new Error("Unreadable workspace status")
  }
  if (snapshot.permissions.length || snapshot.questions.length) return "pending_input"
  for (const value of Object.values(snapshot.statuses)) {
    if (!value || typeof value.type !== "string") throw new Error("Unreadable session status")
    if (value.type !== "idle") return "active_session"
  }
  return "idle"
}

export function createActivityTracker(inspectDirectories, isDraining = draining) {
  let inFlight = 0
  let revision = 0
  let lastActivity = null
  function mark() {
    revision++
    lastActivity = Date.now()
  }
  return {
    async inspect() {
      const startRevision = revision
      let reason = "active_request"
      if (inFlight === 0) {
        try {
          const snapshots = await inspectDirectories()
          const states = snapshots.map(directoryState)
          reason = states.find(state => state !== "idle") ?? "idle"
          if (inFlight || revision !== startRevision) reason = "active_request"
        } catch {
          reason = "activity_unknown"
        }
      }
      return { idle: reason === "idle", reason, last_activity_ms: lastActivity }
    },
    onEvent(event) {
      if (businessEvents.has(event?.payload?.type)) mark()
    },
    async middleware(c, next) {
      const method = c.req.method
      const path = c.req.path
      const business = (!passiveMethods.has(method) && !(method === "POST" && path === "/log")) ||
        (method === "GET" && path === "/file/content")
      if (!business) return next()
      if (isDraining()) return c.json({ error: "workspace_draining" }, 503)
      inFlight++
      mark()
      try { return await next() } finally {
        inFlight--
        mark()
      }
    },
  }
}

export function createActivityMiddleware(inspectDirectories, onBusinessEvent) {
  if (probeServer) return probeServer.middleware
  const tracker = createActivityTracker(inspectDirectories)
  try {
    if (!lstatSync(SOCKET).isSocket()) throw new Error("Unexpected lifecycle socket path")
    unlinkSync(SOCKET)
  } catch (error) {
    if (error.code !== "ENOENT") throw error
  }
  const server = Bun.serve({
    unix: SOCKET,
    fetch: async () => Response.json(await tracker.inspect()),
  })
  chmodSync(SOCKET, 0o600)
  onBusinessEvent(tracker.onEvent)
  probeServer = { server, middleware: tracker.middleware }
  return tracker.middleware
}

if (import.meta.main && process.argv[2] === "--probe") {
  const response = await fetch("http://localhost/activity", {
    unix: SOCKET, signal: AbortSignal.timeout(4500),
  })
  if (!response.ok) throw new Error("Workspace activity probe failed")
  console.log(JSON.stringify(await response.json()))
}
