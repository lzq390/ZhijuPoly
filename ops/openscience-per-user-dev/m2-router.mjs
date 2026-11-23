/** Local development Agent origin. Browsers never choose an upstream address or workspace identity. */
import http from "node:http"
import https from "node:https"
import net from "node:net"
import { randomBytes } from "node:crypto"
import { createReadStream } from "node:fs"
import { readFile, realpath, stat } from "node:fs/promises"
import { dirname, extname, resolve, sep } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { once } from "node:events"
import { patchUiAsset } from "./m2-ui.mjs"

const HERE = dirname(fileURLToPath(import.meta.url))
const id = () => randomBytes(24).toString("base64url")
const opaque = value => typeof value === "string" && /^[a-zA-Z0-9_-]{12,160}$/.test(value)
const cookies = req => new Map((req.headers.cookie || "").split(/;\s*/).map(entry => entry.split("=")))
const mainJs = "/assets/index-production-52b20bca83f6.js"
const SESSION = "ses_[A-Za-z0-9_-]+"
// Fixed backend operations used by the pinned OpenScience UI and scoped project/files views.
const readRoutes = new Set([
  "/global/health", "/global/event", "/global/config", "/event", "/path", "/project", "/project/current",
  "/session", "/session/status", "/permission", "/question", "/config", "/config/providers", "/provider",
  "/provider/auth", "/agent", "/command", "/skill", "/lsp", "/formatter", "/vcs", "/mcp", "/file",
  "/file/status", "/file/content", "/find/file", "/api/resolve-folder/list", "/api/resolve-folder/probe",
])
const sessionDetails = new RegExp("^/session/" + SESSION + "(?:/(?:message(?:/msg_[A-Za-z0-9_-]+)?|children|todo|diff))?$")
const sessionRow = new RegExp("^/session/" + SESSION + "$")
const send = new RegExp("^/session/" + SESSION + "/(?:message|prompt_async|abort)$")

function allows(method, path) {
  switch (method) {
    case "GET": return readRoutes.has(path) || sessionDetails.test(path)
    case "POST": return ["/session","/api/resolve-folder/create","/api/resolve-folder/validate"].includes(path) || send.test(path)
    case "PUT": return path === "/file/content"
    case "PATCH":
    case "DELETE": return sessionRow.test(path)
    default: return false
  }
}
const MIME = {".js":"text/javascript", ".mjs":"text/javascript", ".css":"text/css", ".html":"text/html; charset=utf-8",
  ".woff2":"font/woff2", ".woff":"font/woff", ".ttf":"font/ttf", ".png":"image/png", ".svg":"image/svg+xml",
  ".ico":"image/x-icon", ".webmanifest":"application/manifest+json", ".wasm":"application/wasm", ".json":"application/json",
  ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".webp":"image/webp"}
class Refusal extends Error {
  constructor(status, message) { super(message); this.status = status }
}
function reply(res, status, data, type = "application/json") {
  if (res.destroyed || res.writableEnded) return
  res.writeHead(status, {"Content-Type":type, "Cache-Control":"no-store"})
  res.end(type === "application/json" ? JSON.stringify(data) : data)
}
async function body(req, limit) {
  const parts = []
  let total = 0
  for await (const data of req) {
    total += data.length
    if (total > limit) throw new Refusal(413, "request_too_large")
    parts.push(data)
  }
  return Buffer.concat(parts)
}
function rpc(socketPath, data) {
  return new Promise((accept, reject) => {
    const client = net.createConnection(socketPath)
    let result = ""
    client.setEncoding("utf8")
    client.setTimeout(35000, () => client.destroy(new Error("workspace_timeout")))
    client.on("connect", () => client.write(JSON.stringify(data) + "\n"))
    client.on("error", reject)
    client.on("data", chunk => {
      result += chunk
      if (result.length > 16384) client.destroy(new Error("invalid_supervisor_reply"))
      if (!result.includes("\n")) return
      client.end()
      try {
        const r = JSON.parse(result)
        if (!r.ok) throw new Refusal(503, r.error?.includes("workspace_capacity") ? "workspace_capacity" : "workspace_unavailable")
        accept(r.result)
      } catch (e) { reject(e) }
    })
    client.on("close", () => reject(new Error("workspace_unavailable")))
  })
}
function expires(verified) {
  // Leave margin for transport delay and scheduling. Cache hits never extend this clock.
  if (!Number.isFinite(verified.valid_until) || !Number.isFinite(verified.expires_at)) return 0
  return Math.min(verified.valid_until * 1000 - 800, verified.expires_at * 1000, Date.now() + 25000)
}
function shell(res, origin, message, clean = false) {
  const nonce = id()
  res.setHeader("Content-Security-Policy", "default-src 'none'; frame-ancestors " + origin + "; script-src 'nonce-" + nonce + "'")
  const args = JSON.stringify([message, origin]).replaceAll("<","\\u003c")
  // This is a dedicated agent origin. The reviewed app uses Web Storage for
  // cached recents/drafts, and its scoped bootstrap keeps subsequent writes in this document.
  const reset = clean ? "try { localStorage.clear(); sessionStorage.clear() } catch {};" : ""
  reply(res, 200, '<!doctype html><meta charset=utf-8><script nonce="'+nonce+'">'+reset+'parent.postMessage(...'+args+')</script>', "text/html; charset=utf-8")
}
function staticPath(path) {
  if (/^\/(assets|fonts)\/(?:[a-zA-Z0-9@_.-]+\/)*[a-zA-Z0-9@_.-]+$/.test(path) ||
      /^\/(?:favicon[-\w.]*|apple-touch-icon[-\w.]*|atlas-favicon\.svg|openscience-theme-preload\.js|site\.webmanifest|openscience-logo\.png|web-app-manifest-[\w.-]+|social-share\.png)$/.test(path)) return path
  if (path === "/" || path === "/index.html" || path === "/projects" || /^\/chat\/ses_[A-Za-z0-9_-]+\/?$/.test(path)) return "/index.html"
  const project = /^\/([A-Za-z0-9_-]+)(?:\/session(?:\/ses_[A-Za-z0-9_-]+)?)?\/?$/.exec(path)
  if (project) {
    const directory = Buffer.from(project[1], "base64url").toString("utf8")
    if (directory === "/openscience/files/general" || directory.startsWith("/openscience/files/projects/")) return "/index.html"
  }
}
async function pinnedUi(uiRoot, parents) {
  const boot = await readFile(resolve(HERE, "m2-browser.js"))
  const patchedApp = patchUiAsset(mainJs, await readFile(resolve(uiRoot, "." + mainJs), "utf8"), parents)
  const index = (await readFile(resolve(uiRoot, "index.html"), "utf8")).replace("<head>", '<head><script src="/_nexpoly/browser.js"></script>')
  return {boot, index, patchedApp}
}
export async function startRouter(c, loadUi = pinnedUi) {
  const origin = new URL(c.public_origin)
  const parents = c.parent_origins
  if (c.environment !== "dev-m1" || origin.origin !== c.public_origin || origin.port !== String(c.listen_port) ||
      !Array.isArray(parents) || !parents.length || parents.some(p => new URL(p).origin !== p || p === origin.origin) ||
      !["127.0.0.1","localhost","[::1]"].includes(new URL(c.authority_url).hostname)) throw Error("Invalid isolated router configuration")
  if (new URL(c.authority_url).port === "9011" || Number(c.listen_port) === 9011) throw Error("Reserved production port")
  const authorityKey = (await readFile(c.service_token_file, "utf8")).trim()
  if (authorityKey.length < 32) throw Error("Missing trusted authority identity")
  const uiRoot = await realpath(c.ui_root)
  const {boot, index:baseIndex, patchedApp} = await loadUi(uiRoot, parents)
  const developmentAssets = new Map()
  let index = baseIndex
  if (c.quota_files === true) {
    const {installQuotaUi} = await import("./quota-ui.mjs")
    await installQuotaUi(developmentAssets, uiRoot)
  }
  if (c.m3?.adapters) {
    for (const file of ["results-controller.mjs","results-view.mjs","results-view.css"]) {
      developmentAssets.set("/assets/m3/"+file, await readFile(resolve(HERE,"../openscience-mcp-dev",file)))
    }
    index = baseIndex.replace("</head>", '<link rel="stylesheet" href="/assets/m3/results-view.css"><script type="module" src="/assets/m3/results-controller.mjs"></script></head>')
  }
  if (c.m4) {
    const {knowledgeChunk, privateKnowledgeChunk} = await import("../openscience-m4-dev/workspace-ui.mjs")
    developmentAssets.set(knowledgeChunk, await privateKnowledgeChunk(uiRoot))
    developmentAssets.set("/assets/m4/knowledge-ui.mjs", await readFile(resolve(HERE,"../openscience-m4-dev/knowledge-ui.mjs")))
    index = index.replace("</head>", '<script type="module" src="/assets/m4/knowledge-ui.mjs"></script></head>')
  }
  const pages = new Map(), profiles = new Map(), startups = new Map()
  const secure = origin.protocol === "https:"
  const cookieName = key => (secure ? "__Host-" : "") + "np_os_m2_" + key
  const cookieAttributes = "; Path=/; HttpOnly; SameSite=Lax" + (secure ? "; Secure" : "")
  function retire(page) {
    pages.delete(page.key)
    for (const cancel of page.active) cancel.abort()
    page.active.clear()
  }
  async function auth(path, data) {
    let r
    try {
      r = await fetch(c.authority_url + "/internal/openscience/" + path, {method:"POST", redirect:"error",
        headers:{"Authorization":"Bearer " + authorityKey, "Content-Type":"application/json"},
        body:JSON.stringify({...data, agent_origin:c.public_origin}), signal:AbortSignal.timeout(2500)})
    } catch { throw new Refusal(503, "authority_unavailable") }
    if (!r.ok) throw new Refusal(r.status === 401 || r.status === 403 || r.status === 409 || r.status === 410 ? 401 : 503,
      r.status < 500 ? "session_expired" : "authority_unavailable")
    return r.json()
  }
  function gate(page, fresh = true) {
    if (!page || pages.get(page.key) !== page || profiles.get(page.profile)?.login !== page.login || Date.now() >= page.expiry || (fresh && Date.now() >= page.until)) {
      if (page) retire(page)
      throw new Refusal(401, "session_expired")
    }
  }
  async function validate(page) {
    gate(page, false)
    if (Date.now() - page.checked < 2000 && Date.now() < page.until) return
    if (!page.check) {
      page.check = (async () => {
        try {
          const r = await auth("sessions/validate", {agent_session:page.grant})
          gate(page, false)
          if (r.user_id !== page.user || r.workspace_user_id !== page.owner || r.session_id !== page.login || r.environment !== c.environment)
            throw new Refusal(401, "session_expired")
          page.checked = Date.now()
          page.until = expires(r)
          gate(page)
        } catch (e) {
          if (e.status === 401) retire(page)
          throw e
        }
      })().finally(() => { page.check = null })
    }
    return page.check
  }
  function workspace(owner) {
    if (!startups.has(owner)) {
      startups.set(owner, rpc(c.supervisor_socket, {operation:"ensure",environment:c.environment,user_id:owner})
        .then(async r => {
          if (r.environment !== c.environment || r.user_id !== owner || r.state !== "running" || !/^[0-9a-f]{32}$/.test(r.workspace))
            throw new Refusal(503,"workspace_unavailable")
          const runtime = resolve(dirname(c.supervisor_socket), r.workspace)
          const socket = resolve(runtime, "transport/api")
          if (!(await stat(socket)).isSocket()) throw new Refusal(503,"workspace_transport_unavailable")
          return {socket, tokenFile:resolve(runtime,"secret/token")}
        }).finally(() => startups.delete(owner)))
    }
    return startups.get(owner)
  }
  function scope(req, url) {
    const header = req.headers["x-nexpoly-page"], query = url.searchParams.get("np_page")
    if (header && query && header !== query) throw new Refusal(401,"page_mismatch")
    const key = header || query
    const page = opaque(key) && pages.get(key)
    if (!page || page.clientClosed || cookies(req).get(cookieName(key)) !== page.cookie) throw new Refusal(401, "session_expired")
    gate(page, false)
    return page
  }
  async function leave(req, res) {
    const parent = req.headers.origin
    if (!parents.includes(parent) || (req.headers["content-type"] || "").split(";")[0] !== "application/x-www-form-urlencoded")
      throw new Refusal(403, "origin_denied")
    const form = new URLSearchParams((await body(req, 512)).toString())
    const key = form.get("page_context")
    const page = opaque(key) && pages.get(key)
    if (!page || !page.profile.startsWith(parent + "/") || cookies(req).get(cookieName(key)) !== page.cookie)
      throw new Refusal(401, "session_expired")
    // Retire browser access separately from work already accepted for this login.
    page.clientClosed = true
    for (const controller of page.active) controller.abort()
    res.writeHead(204, {"Cache-Control":"no-store", "Set-Cookie":cookieName(key) + "=; Max-Age=0" + cookieAttributes})
    res.end()
  }
  async function exchange(req, res) {
    const parent = req.headers.origin
    if (!parents.includes(parent) || (req.headers["content-type"] || "").split(";")[0] !== "application/x-www-form-urlencoded")
      throw new Refusal(403, "origin_denied")
    const form = new URLSearchParams((await body(req, 4096)).toString())
    const handoff = form.get("handoff_id")
    const profileId = form.get("browser_id")
    if (!opaque(handoff) || !opaque(profileId) || !opaque(form.get("ticket"))) throw new Refusal(400, "invalid_handoff")
    try {
      const r = await auth("tickets/redeem", {ticket:form.get("ticket"), handoff_id:handoff, parent_origin:parent})
      if (r.handoff_id !== handoff || r.environment !== c.environment || !r.workspace_user_id || !r.session_id ||
          !r.agent_session || !Number.isFinite(r.issued_at) || !Number.isFinite(r.login_issued_at)) throw new Refusal(403, "invalid_handoff")
      const profile = parent + "/" + profileId
      const prior = profiles.get(profile)
      // Ticket issuance can race logout/re-login. A delayed ticket from an
      // older login cannot supersede the newer device login, even if issued last.
      if (prior && prior.login !== r.session_id && prior.loginIssued >= r.login_issued_at)
        throw new Refusal(401, "stale_handoff")
      if (!prior || prior.login !== r.session_id || r.issued_at >= prior.issued) {
        profiles.set(profile, {login:r.session_id, loginIssued:r.login_issued_at, issued:r.issued_at})
        if (prior && prior.login !== r.session_id)
          for (const page of pages.values()) if (page.profile === profile && page.login !== r.session_id) retire(page)
      }
      const route = await workspace(r.workspace_user_id)
      const current = await auth("sessions/validate", {agent_session:r.agent_session})
      if (current.session_id !== r.session_id || current.user_id !== r.user_id || current.workspace_user_id !== r.workspace_user_id ||
          current.environment !== c.environment || profiles.get(profile)?.login !== r.session_id || !(expires(current) > Date.now()))
        throw new Refusal(401,"stale_handoff")
      const key = id(), cookie = id()
      pages.set(key, {key,cookie,grant:r.agent_session,login:r.session_id,user:r.user_id,owner:r.workspace_user_id,profile,
        checked:Date.now(),until:expires(current),expiry:Number(current.expires_at)*1000,route,active:new Set()})
      const prefix = cookieName("")
      const expired = [...cookies(req).keys()].filter(name => {
        const key = name.slice(prefix.length)
        return name.startsWith(prefix) && opaque(key) && (!pages.has(key) || pages.get(key).clientClosed)
      })
      res.setHeader("Set-Cookie", [...expired.map(name => name + "=; Max-Age=0" + cookieAttributes),
        cookieName(key) + "=" + cookie + "; Max-Age=" + Math.max(1, Math.floor(current.expires_at-Date.now()/1000)) + cookieAttributes])
      shell(res,parent,{type:"nexpoly:openscience:ready",handoff_id:handoff,page_context:key},true)
    } catch (e) { shell(res,parent,{type:"nexpoly:openscience:error",handoff_id:handoff,reason:e instanceof Refusal ? e.message : "workspace_unavailable"}) }
  }
  let tools, formAdapter, m4
  if (c.m4) {
    const { services } = await import("../openscience-m4-dev/services.mjs")
    m4 = await services(c.m4, validate)
  }
  if (c.m3 || m4) {
    const { toolRouter } = await import("../openscience-m3-dev/tool-router.mjs")
    tools = await toolRouter(c.m3 || c.m4, auth, validate, m4)
    if (c.m3?.adapters) ({formAdapter} = await import("../openscience-m3-dev/adapter-router.mjs"))
  }
  async function forward(req, res, url, path, page) {
    const toolRequest = tools && req.method === "POST"
    const permissionReply = toolRequest && /^\/permission\/per_[a-zA-Z0-9_-]+\/reply$/.test(path)
    const form = toolRequest && formAdapter?.(path)
    const quotaDelete = c.quota_files === true && req.method === "DELETE" && path === "/file/content"
    if (!form && !permissionReply && !quotaDelete && !allows(req.method, path)) throw new Refusal(403, "route_disabled")
    const writing = !["GET","HEAD"].includes(req.method)
    if (writing && (req.headers.origin !== c.public_origin || req.headers["x-nexpoly-page"] !== page.key))
      throw new Refusal(403, "write_origin_denied")
    const payload = writing ? await body(req, 8 * 1024 * 1024) : null
    await validate(page)
    if (page.clientClosed) throw new Refusal(401, "session_expired")
    const headers = {host:"127.0.0.1:4096", connection:"close", "accept-encoding":"identity"}
    for (const k of ["accept","content-type","x-openscience-directory"]) if (typeof req.headers[k] === "string") headers[k] = req.headers[k]
    if (payload) headers["content-length"] = String(payload.length)
    url.searchParams.delete("np_page")
    const controller = new AbortController()
    page.active.add(controller)
    const close = () => controller.abort()
    res.once("close",close)
    async function connect(allowRecovery) {
      let submitted = false
      try {
        const token = (await readFile(page.route.tokenFile,"utf8")).trim()
        if (!/^[a-f0-9]{64}$/.test(token)) throw new Refusal(503,"workspace_unavailable")
        if (!(await stat(page.route.socket)).isSocket()) throw new Refusal(503,"workspace_restarting")
        gate(page)
        controller.signal.throwIfAborted()
        if (form) Object.assign(headers, await tools.form(page, form, payload, token))
        if (permissionReply) await tools.confirm(page, path, url.search, payload)
        const prompt = toolRequest && /^\/session\/(ses_[a-zA-Z0-9_-]+)\/(?:message|prompt_async)$/.exec(path)
        if (prompt) headers["x-nexpoly-tool-turn"] = tools.begin(page, prompt[1], token)
        const up = http.request({socketPath:page.route.socket,path:path+url.search,method:req.method,
          headers:{...headers,authorization:"Bearer " + token},agent:false,signal:controller.signal})
        up.on("error", () => {})  // The response iterator handles errors after headers arrive.
        const ready = once(up,"response")
        submitted = true
        up.end(payload)
        const [incoming] = await ready
        if (incoming.statusCode !== 401) return incoming
        incoming.destroy()
        throw new Refusal(503,"workspace_restarting")  // The token may have rotated with the instance.
      } catch (error) {
        const restarting = error.message === "workspace_restarting" || ["ENOENT","ECONNREFUSED","ECONNRESET","EPIPE"].includes(error.code)
        if (!restarting) throw error
        if (!allowRecovery) throw new Refusal(503,"workspace_unavailable")
        gate(page)
        controller.signal.throwIfAborted()
        page.route = await workspace(page.owner)
        await validate(page)
        // A write may already have committed before the old transport closed.
        if (writing && submitted) throw new Refusal(503,"workspace_write_unconfirmed")
        return connect(false)
      }
    }
    try {
      const incoming = await connect(true)
      gate(page)
      if (incoming.statusCode >= 300 && incoming.statusCode < 400) {incoming.destroy(); throw new Refusal(502,"upstream_redirect_denied")}
      const out = {"Cache-Control":"no-store", "X-Accel-Buffering":"no", "Content-Type":incoming.headers["content-type"] || "application/octet-stream"}
      res.writeHead(incoming.statusCode, out)
      res.flushHeaders()
      for await (const chunk of incoming) {
        gate(page)
        if (!res.write(chunk)) await once(res, "drain", {signal:controller.signal})
      }
      res.end()
    } finally {
      controller.abort()
      page.active.delete(controller)
      res.removeListener("close",close)
    }
  }
  const heartbeat = setInterval(() => {
    for (const page of pages.values()) {
      if (page.expiry <= Date.now() || (page.until <= Date.now() && page.active.size)) retire(page)
      else if (page.active.size && Date.now() - page.checked >= 3000) validate(page).catch(() => {})
    }
    for (const [key, value] of profiles) if (Date.now() - value.issued * 1000 > 86400000 && ![...pages.values()].some(p => p.profile === key)) profiles.delete(key)
  }, 500).unref()
  async function handle(req, res) {
    if (req.headers.host !== origin.host || !req.url?.startsWith("/") || req.url.startsWith("//")) throw new Refusal(403,"origin_denied")
    const url = new URL(req.url, origin), path = url.pathname
    res.setHeader("Referrer-Policy","no-referrer")
    res.setHeader("X-Content-Type-Options","nosniff")
    res.setHeader("Content-Security-Policy","frame-ancestors " + parents.join(" ") + "; connect-src 'self'; object-src 'none'; base-uri 'self'")
    if (path === "/_nexpoly/expired" && req.method === "GET") return reply(res,401,"<!doctype html><html lang=zh><meta charset=utf-8><title>会话已失效</title><p>工作区会话已失效，请从平台重新进入。</p></html>",MIME[".html"])
    if (path === "/_nexpoly/auth/exchange" && req.method === "POST") return exchange(req,res)
    if (path === "/_nexpoly/auth/leave" && req.method === "POST") return leave(req,res)
    if (path === "/_nexpoly/browser.js" && req.method === "GET") return reply(res,200,boot,MIME[".js"])
    if (path === "/_nexpoly/status" && req.method === "GET") {
      const page = scope(req,url)
      await validate(page)
      gate(page)
      return reply(res,200,{authorized:true,valid_until_ms:page.until})
    }
    if (path.startsWith("/openscience-api/") || path.startsWith("/api/") || path.startsWith("/knowledge-api/")) {
      const page = scope(req,url)
      if (m4 && (path.startsWith("/knowledge-api/") || path === "/openscience-api/m4/model/usage"))
        return m4.browser(req, res, url, page, body, c.public_origin)
      if (path.startsWith("/knowledge-api/")) throw new Refusal(403,"knowledge_disabled")
      const upstream = path.startsWith("/openscience-api/") ? path.slice("/openscience-api".length) : path
      return forward(req,res,url,upstream,page)
    }
    const fixed = req.method === "GET" && staticPath(path)
    if (!fixed) throw new Refusal(404,"not_found")
    if (fixed === "/index.html") return reply(res,200,index,MIME[".html"])
    if (developmentAssets.has(fixed)) return reply(res,200,developmentAssets.get(fixed),MIME[extname(fixed)])
    if (fixed === mainJs) return reply(res,200,patchedApp,MIME[".js"])
    const file = await realpath(resolve(uiRoot, "." + fixed)).catch(() => null)
    if (!file || !file.startsWith(uiRoot + sep) || !MIME[extname(file)] || !(await stat(file)).isFile()) throw new Refusal(404,"not_found")
    res.writeHead(200,{"Content-Type":MIME[extname(file)],"Cache-Control":"public, max-age=300"})
    const stream = createReadStream(file)
    res.once("close",()=>stream.destroy())
    stream.on("error",()=>res.destroy())
    stream.pipe(res)
  }
  const handler = (req,res) => handle(req,res).catch(e => {
    if (res.headersSent) return res.destroy()
    if (e.code === "DRAFT_NOT_IN_SESSION" || e.code === "INVALID_ACTION") return reply(res,e.status,{ok:false,error:{code:e.code}})
    reply(res,e.status || 502,{error:e instanceof Refusal ? e.message : "workspace_unavailable"})
  })
  const srv = secure ? https.createServer({key:await readFile(c.tls_key_file),cert:await readFile(c.tls_cert_file)},handler) : http.createServer(handler)
  srv.on("close",() => {clearInterval(heartbeat); tools?.close(); for (const p of pages.values()) retire(p)})
  srv.listen(c.listen_port,"127.0.0.1")
  await once(srv,"listening")
  return srv
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  if (process.argv[2] !== "--config" || !process.argv[3]) throw new Error("Usage: node m2-router.mjs --config <router.json>")
  const config = JSON.parse(await readFile(process.argv[3], "utf8"))
  await startRouter(config)
  console.log("Independent M2 router ready on " + config.public_origin)
}
