import assert from "node:assert/strict"
import { EventEmitter, once } from "node:events"
import { copyFile, mkdtemp, mkdir, rm, writeFile } from "node:fs/promises"
import http from "node:http"
import net from "node:net"
import os from "node:os"
import { join } from "node:path"
import { pathToFileURL } from "node:url"
import test from "node:test"
import { startRouter } from "./m2-router.mjs"

const parent = "http://app.m2.test:19333"

async function listen(server, ...args) {
  server.listen(...args)
  await once(server, "listening")
  return server
}
async function stop(server) {
  const closed = once(server, "close")
  server.close()
  server.closeAllConnections?.()
  await closed
}

test("the second candidate rejects a mixed authority, parent or workspace profile before listening", async () => {
  const site = {environment:"dev-m6",listen_port:18781,public_origin:"https://agent.m6.test:18781",
    parent_origins:["https://app.m6.test:18780"],authority_url:"http://127.0.0.1:18782"}
  for (const change of [{parent_origins:["https://app.m2.test:18760"]}, {authority_url:"http://127.0.0.1:18762"},
      {public_origin:"https://agent.m2.test:18761",listen_port:18761}, {m3:{}}, {quota_files:true}]) {
    await assert.rejects(startRouter({...site,...change}), /Wrong origins, services or authority/)
  }
  await assert.rejects(startRouter({...site,environment:"unknown"}), /Invalid isolated router environment/)
})

test("scoped reads, late handoffs, two logins and live revocation", {timeout:20000}, async t => {
  const root = await mkdtemp(join(os.tmpdir(),"np-m2-router-"))
  t.after(() => rm(root,{recursive:true,force:true}))
  const serviceKey = "x".repeat(48), supervisorSocket = join(root,"supervisor.sock")
  await writeFile(join(root,"service-token"),serviceKey)
  const tickets = new Map(), logins = new Map()
  const authority = await listen(http.createServer(async (req,res) => {
    assert.equal(req.headers.authorization, "Bearer " + serviceKey)
    let text = ""
    for await (const chunk of req) text += chunk
    const form = JSON.parse(text)
    let entry
    if (req.url.endsWith("/redeem")) {
      entry = tickets.get(form.ticket)
      tickets.delete(form.ticket)
    } else entry = logins.get(form.agent_session)
    if (!entry || entry.revoked) { res.writeHead(401);res.end();return }
    if (entry.unavailable) { res.writeHead(503);res.end();return }
    const now = Date.now()/1000
    res.end(JSON.stringify({...entry,valid_until:now+(entry.window || 30),expires_at:now+3600}))
  }),0,"127.0.0.1")
  t.after(() => stop(authority))
  const owners = new Map(), disconnected = new Set()
  let capacityRefusal
  let startupPause
  let diskAdmission, nativeOwner, forwardedCount = 0
  const checkedOwners = []
  const cleanupCalls = []
  let repairs = 0, writes = 0, fileDeletes = 0
  let forwarded
  for (const [idx,owner] of ["test-user-a","test-user-b"].entries()) {
    const workspace = String(idx+1).repeat(32), directory = join(root,workspace)
    await mkdir(join(directory,"transport"),{recursive:true})
    await mkdir(join(directory,"secret"))
    await writeFile(join(directory,"secret/token"),String(idx+1).repeat(64))
    const backend = await listen(http.createServer((req,res) => {
      forwardedCount++
      assert.equal(req.headers.authorization, "Bearer " + String(idx+1).repeat(64))
      assert.equal(req.headers.cookie, undefined)
      assert.equal(req.headers["x-nexpoly-page"], undefined)
      if (req.method === "POST") writes++
      if (req.method === "DELETE" && req.url === "/file/content") fileDeletes++
      if (disconnected.has(owner)) { req.socket.destroy(); return }
      forwarded = req.url
      if (req.url === "/global/event") {
        res.writeHead(200,{"Content-Type":"text/event-stream"})
        res.write("data: ready\n\n")
        return
      }
      res.setHeader("Set-Cookie","upstream=ignored")
      res.end(JSON.stringify({owner}))
    }),join(directory,"transport/api"))
    t.after(() => stop(backend))
    owners.set(owner,workspace)
  }
  const supervisor = await listen(net.createServer(socket => socket.once("data", async raw => {
    const r = JSON.parse(raw)
    if (r.operation === "cleanup") {
      cleanupCalls.push(r)
      socket.end(JSON.stringify({ok:true,result:{entries:[],next:null}})+"\n")
      return
    }
    if (r.operation === "check-write") {
      checkedOwners.push(r.user_id)
      assert.deepEqual(Object.keys(r).sort(), ["environment","operation","user_id"])
      socket.end(JSON.stringify(diskAdmission)+"\n")
      return
    }
    assert.equal(r.operation, "ensure")
    if (startupPause && r.user_id === "test-user-b") {
      startupPause.emit("started")
      await once(startupPause, "release")
    }
    if (r.user_id === "test-user-b" && capacityRefusal) {
      socket.end(JSON.stringify({ok:false,error:"workspace_capacity: " + capacityRefusal})+"\n")
      return
    }
    if (disconnected.delete(r.user_id)) repairs++
    socket.end(JSON.stringify({ok:true,result:{...r,workspace:owners.get(r.user_id),state:"running",
      storage_backend:r.user_id === nativeOwner ? "nfs-ext4-user-quota" : undefined}})+"\n")
  })),supervisorSocket)
  t.after(() => stop(supervisor))
  const reserve = await listen(net.createServer(),0,"127.0.0.1")
  const port = reserve.address().port, base = "http://127.0.0.1:"+port
  await stop(reserve)
  const config = {environment:"dev-m1",listen_port:port,public_origin:base,parent_origins:[parent],
    authority_url:"http://127.0.0.1:"+authority.address().port, service_token_file:join(root,"service-token"),
    supervisor_socket:supervisorSocket,ui_root:root}
  const testUi = async () => ({boot:"",index:"",patchedApp:""})
  const router = await startRouter(config, testUi)
  t.after(() => stop(router))
  async function exchange(login,owner,profile,issued,loginIssued = issued,window = 30,origin = base) {
    const ticket = "one_time_ticket_"+Math.random().toString().slice(2)
    const data = {user_id:"user-"+owner,workspace_user_id:owner,session_id:login,environment:"dev-m1",agent_session:ticket,
      handoff_id:"handoff_"+ticket,issued_at:issued,login_issued_at:loginIssued,window}
    tickets.set(ticket,data);logins.set(ticket,data)
    const response = await fetch(origin+"/_nexpoly/auth/exchange",{method:"POST",headers:{Origin:parent},
      body:new URLSearchParams({ticket,handoff_id:data.handoff_id,browser_id:profile})})
    const html = await response.text()
    return {html,page:html.match(/"page_context":"([A-Za-z0-9_-]+)"/)?.[1],cookie:response.headers.get("set-cookie")?.split(";")[0],data}
  }
  const a = await exchange("login-A1","test-user-a","browser_profile_1",10)
  const a2 = await exchange("login-A2","test-user-a","browser_profile_2",11)
  async function read(session,path,custom={}) {
    return fetch(base+path,{headers:{Cookie:session.cookie,"X-Nexpoly-Page":session.page,...custom}})
  }
  assert.ok(a.page && a2.page)
  await t.test("file removal requires the quota profile, matching page cookie and write origin", async t => {
    const route = "/openscience-api/file/content"
    const remove = (origin, headers, query = "") => fetch(origin+route+query, {
      method:"DELETE", headers:{"Content-Type":"application/json", ...headers},
      body:JSON.stringify({path:"notebook.txt"}),
    })
    const plain = await remove(base, {Origin:base, Cookie:a.cookie, "X-Nexpoly-Page":a.page})
    assert.equal(plain.status, 403)
    assert.equal((await plain.json()).error, "route_disabled")

    // This test exercises the HTTP boundary with the unchanged router module.
    // Replace only the proprietary editor-asset installer in its temporary
    // fixture. The real November editor is exercised by the M5 browser drill.
    for (const name of ["m2-router.mjs", "m2-ui.mjs"])
      await copyFile(new URL(name, import.meta.url), join(root,name))
    await writeFile(join(root,"quota-ui.mjs"), "export function installQuotaUi() {}")
    const {startRouter: startQuotaRouter} = await import(pathToFileURL(join(root,"m2-router.mjs")))
    const reserved = await listen(net.createServer(),0,"127.0.0.1")
    const quotaPort = reserved.address().port, quotaOrigin = "http://127.0.0.1:"+quotaPort
    await stop(reserved)
    const quotaRouter = await startQuotaRouter({...config, quota_files:true, listen_port:quotaPort, public_origin:quotaOrigin}, testUi)
    t.after(() => stop(quotaRouter))
    const grant = await exchange("quota-login","test-user-b","browser_quota_test",15,15,30,quotaOrigin)
    const other = await exchange("other-quota-login","test-user-a","browser_quota_other",16,16,30,quotaOrigin)
    assert.ok(grant.page && grant.cookie && other.page && other.cookie)
    const headers = {Origin:quotaOrigin, Cookie:grant.cookie, "X-Nexpoly-Page":grant.page}
    assert.equal((await remove(quotaOrigin, {Origin:quotaOrigin, "X-Nexpoly-Page":grant.page})).status, 401)
    assert.equal((await remove(quotaOrigin, {...headers, Cookie:other.cookie})).status, 401)
    const wrongOrigin = await remove(quotaOrigin, {...headers, Origin:parent})
    assert.equal(wrongOrigin.status, 403)
    assert.equal((await wrongOrigin.json()).error, "write_origin_denied")
    const onlyQuery = await remove(quotaOrigin, {Origin:quotaOrigin, Cookie:grant.cookie}, "?np_page="+grant.page)
    assert.equal(onlyQuery.status, 403)
    assert.equal((await onlyQuery.json()).error, "write_origin_denied")
    assert.equal(fileDeletes, 0)
    const accepted = await remove(quotaOrigin, headers)
    assert.equal(accepted.status, 200)
    assert.deepEqual(await accepted.json(), {owner:"test-user-b"})
    assert.equal(fileDeletes, 1)

    await t.test("only a current low-water handoff can browse its own cold volume", async () => {
      const list = (session, changes={}, data={path:"",after:""}) => fetch(quotaOrigin+"/api/workspace-cleanup/list", {
        method:"POST", headers:{Origin:quotaOrigin, Cookie:session.cookie, "X-Nexpoly-Page":session.page,...changes},
        body:JSON.stringify(data),
      })
      assert.equal((await list(grant)).status,403)
      capacityRefusal = "global_disk (workspace_disk_monitor_unavailable)"
      const unhealthy = await exchange("no-sample","test-user-b","browser_monitor_down",18,18,30,quotaOrigin)
      assert.equal(unhealthy.page, undefined)
      capacityRefusal = "global_disk (workspace_disk_low_watermark)"
      let cold
      try {
        cold = await exchange("cold-login","test-user-b","browser_cold_only",19,19,30,quotaOrigin)
      } finally { capacityRefusal = undefined }
      assert.match(cold.html, /"type":"nexpoly:openscience:cleanup"/)
      assert.equal((await list(cold,{Cookie:other.cookie})).status,401)
      assert.equal((await list(cold,{Origin:parent})).status,403)
      const restricted = {Cookie:cold.cookie,"X-Nexpoly-Page":cold.page}
      for (const path of ["/openscience-api/session","/openscience-api/global/event","/knowledge-api/documents"]) {
        assert.equal((await fetch(quotaOrigin+path,{headers:restricted})).status,403)
      }
      assert.equal(cleanupCalls.length,0)
      assert.equal((await list(cold)).status,200)
      assert.deepEqual(cleanupCalls.map(r=>[r.environment,r.user_id,r.action,r.data]),
        [["dev-m1","test-user-b","list",{path:"",after:""}]])
      const component = '"'.repeat(255)
      const continuation = {path:Array(8).fill(component).join("/"), after:"/1/" + "a".repeat(64) + "/123/" + component}
      assert.equal((await list(cold,{},continuation)).status,200)
      assert.deepEqual(cleanupCalls.at(-1).data,continuation)
      cold.data.revoked = true
      await new Promise(done=>setTimeout(done,2100))
      assert.equal((await list(cold)).status,401)
      assert.equal(cleanupCalls.length,2)
    })

    await t.test("shared disk admission covers growing routes, preserves reads and deletion, and fails closed", async () => {
      async function request(method, path) {
        return fetch(quotaOrigin+"/openscience-api"+path, {method,headers:{...headers,"Content-Type":"application/json"},
          body:method === "GET" ? undefined : "{}"})
      }
      diskAdmission = {ok:false,error:"workspace_disk_low_watermark"}
      const before = forwardedCount
      for (const [method, path] of [
        ["PUT","/file/content"], ["POST","/api/resolve-folder/create"], ["POST","/session"],
        ["PATCH","/session/ses_test"], ["POST","/session/ses_test/message"], ["POST","/session/ses_test/prompt_async"],
      ]) {
        const blocked = await request(method,path)
        assert.equal(blocked.status,507)
        assert.equal((await blocked.json()).error,"workspace_disk_low_watermark")
      }
      assert.equal(forwardedCount,before)
      assert.deepEqual(checkedOwners,Array(6).fill("test-user-b"))
      assert.equal((await request("GET","/file/content")).status,200)
      assert.equal((await request("DELETE","/file/content")).status,200)
      assert.equal(checkedOwners.length,6)
      const after = forwardedCount
      for (const bad of [{ok:false,error:"workspace_disk_monitor_unavailable"}, {ok:true,result:{}}, {ok:true,result:{write_allowed:false}}]) {
        diskAdmission = bad
        const unavailable = await request("PUT","/file/content")
        assert.equal(unavailable.status,503)
        assert.equal((await unavailable.json()).error,"workspace_disk_monitor_unavailable")
      }
      assert.equal(forwardedCount,after)
      diskAdmission = {ok:true,result:{write_allowed:true}}
      assert.equal((await request("PUT","/file/content")).status,200)
      assert.equal(forwardedCount,after+1)

      // Disabling the editor's optional file controls cannot disable the
      // storage policy on a volume identified by the actual workspace route.
      nativeOwner = "test-user-a"
      const scoped = await exchange("plain-ui-quota-login",nativeOwner,"browser_plain_ui_quota",18)
      diskAdmission = {ok:false,error:"workspace_disk_low_watermark"}
      const hiddenControls = await fetch(base+route, {method:"PUT",body:"{}",
        headers:{Origin:base,Cookie:scoped.cookie,"X-Nexpoly-Page":scoped.page}})
      assert.equal(hiddenControls.status,507)
      assert.equal(checkedOwners.at(-1),nativeOwner)
      assert.equal(forwardedCount,after+1)
      nativeOwner = undefined
    })
  })
  for (capacityRefusal of ["instance_count", "workspace_memory", "host_memory", "volume_free", "probe_unavailable"]) {
    const denied = await exchange("login-B-pending","test-user-b","browser_capacity_test",11)
    assert.match(denied.html,/"reason":"workspace_capacity"/)
    assert.equal(denied.page,undefined)
    assert.equal(denied.cookie,undefined)
    assert.deepEqual(await (await read(a,"/openscience-api/session")).json(),{owner:"test-user-a"})
  }
  capacityRefusal = undefined
  // Two logins can share one pending ensure. Revoking one must end that
  // exchange before startup finishes, without issuing a cookie or page.
  startupPause = new EventEmitter()
  const started = once(startupPause, "started")
  const issued = Date.now() / 1000
  const pending = exchange("waiting-revoked","test-user-b","browser_waiting_1",issued,issued,1.5)
  await started
  const stillAllowed = exchange("waiting-allowed","test-user-b","browser_waiting_2",issued)
  for (const entry of logins.values()) if (entry.session_id === "waiting-revoked") entry.revoked = true
  let revoked
  try {
    revoked = await pending
    assert.match(revoked.html, /session_expired/)
    assert.equal(revoked.page, undefined)
    assert.equal(revoked.cookie, undefined)
  } finally {
    startupPause.emit("release")
    startupPause = undefined
  }
  assert.ok((await stillAllowed).page)
  await t.test("a bounded startup timeout returns a retryable refusal without a page", async () => {
    const reserved = await listen(net.createServer(), 0, "127.0.0.1")
    const port = reserved.address().port, origin = "http://127.0.0.1:" + port
    await stop(reserved)
    const bounded = await startRouter({...config, public_origin:origin, listen_port:port, startup_timeout_seconds:1}, testUi)
    startupPause = new EventEmitter()
    try {
      const denied = await exchange("timed-out","test-user-b","browser_timed_out",issued,issued,30,origin)
      assert.match(denied.html, /"reason":"workspace_timeout"/)
      assert.equal(denied.page, undefined)
      assert.equal(denied.cookie, undefined)
    } finally {
      startupPause.emit("release")
      startupPause = undefined
      await stop(bounded)
    }
  })
  const sibling = await exchange("login-A1","test-user-a","browser_profile_1",10)
  const leave = (cookie, origin = parent) => fetch(base+"/_nexpoly/auth/leave", {method:"POST",
    headers:{Origin:origin, Cookie:cookie}, body:new URLSearchParams({page_context:sibling.page})})
  assert.notEqual(a.cookie,sibling.cookie)
  assert.equal((await read(sibling,"/_nexpoly/status",{Cookie:a.cookie})).status,401)
  assert.equal((await leave(a.cookie)).status,401)
  assert.equal((await leave(sibling.cookie, base)).status,403)
  assert.equal((await read(sibling,"/_nexpoly/status")).status,200)
  const siblingStream = (await read(sibling,"/openscience-api/global/event")).body.getReader()
  assert.equal((await siblingStream.read()).done,false)
  const closedSibling = await leave(sibling.cookie)
  assert.equal(closedSibling.status,204)
  assert.match(closedSibling.headers.get("set-cookie"),/Max-Age=0/)
  assert.equal((await read(sibling,"/_nexpoly/status")).status,401)
  assert.equal(await siblingStream.read().then(r=>r.done,()=>true),true)
  assert.equal((await read(a,"/_nexpoly/status")).status,200)
  const mine = await read(a,"/openscience-api/session",{Authorization:"Bearer browser-supplied"})
  assert.deepEqual(await mine.json(),{owner:"test-user-a"})
  assert.equal(mine.headers.get("set-cookie"),null)
  assert.equal(mine.headers.get("cache-control"),"no-store")
  // The fixture socket can still exist after the backend has disappeared.
  disconnected.add("test-user-a")
  assert.deepEqual(await (await read(a,"/openscience-api/session")).json(),{owner:"test-user-a"})
  assert.equal(repairs,1)
  disconnected.add("test-user-a")
  const uncertain = await fetch(base+"/openscience-api/session",{method:"POST",
    headers:{Origin:base,Cookie:a.cookie,"X-Nexpoly-Page":a.page},body:"{}"})
  assert.equal(uncertain.status,503)
  assert.equal((await uncertain.json()).error,"workspace_write_unconfirmed")
  assert.equal(repairs,2)
  assert.equal(writes,1)
  assert.deepEqual(await (await read(a,"/openscience-api/session")).json(),{owner:"test-user-a"})
  const b = await exchange("login-B1","test-user-b","browser_profile_1",12)
  assert.ok(b.page)
  assert.equal((await read(a,"/openscience-api/session")).status,401)
  assert.equal((await read(b,"/openscience-api/session",{"X-Nexpoly-Page":a.page})).status,401)
  assert.deepEqual(await (await read(b,"/api/resolve-folder/list")).json(),{owner:"test-user-b"})
  assert.equal(forwarded,"/api/resolve-folder/list")
  const stale = await exchange("login-A1","test-user-a","browser_profile_1",13,10)
  assert.match(stale.html,/stale_handoff/)
  assert.equal(stale.cookie,undefined)
  const stream = await read(a2,"/openscience-api/global/event")
  const reader = stream.body.getReader()
  assert.match(new TextDecoder().decode((await reader.read()).value),/data: ready/)
  const start = performance.now()
  a2.data.revoked = true
  const closed = await reader.read().then(r=>r.done,()=>true)
  assert.equal(closed,true)
  assert.ok(performance.now()-start < 10000)
  assert.equal((await read(a2,"/_nexpoly/status")).status,401)
  assert.equal((await read(b,"/_nexpoly/status")).status,200)

  // When the authority cannot renew, an already open stream must end even
  // if its backend is silent. Polling is not a renewal of the original deadline.
  const short = await exchange("short-login","test-user-b","browser_profile_3",14,14,5)
  const stream2 = await read(short,"/openscience-api/global/event")
  const reader2 = stream2.body.getReader()
  await reader2.read()
  short.data.unavailable = true
  const deadlineStart = performance.now()
  assert.equal(await reader2.read().then(r=>r.done,()=>true),true)
  assert.ok(performance.now()-deadlineStart < 6000)
})
