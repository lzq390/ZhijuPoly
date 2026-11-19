import assert from "node:assert/strict"
import { once } from "node:events"
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises"
import http from "node:http"
import net from "node:net"
import os from "node:os"
import { join } from "node:path"
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
  let repairs = 0, writes = 0
  let forwarded
  for (const [idx,owner] of ["test-user-a","test-user-b"].entries()) {
    const workspace = String(idx+1).repeat(32), directory = join(root,workspace)
    await mkdir(join(directory,"transport"),{recursive:true})
    await mkdir(join(directory,"secret"))
    await writeFile(join(directory,"secret/token"),String(idx+1).repeat(64))
    const backend = await listen(http.createServer((req,res) => {
      assert.equal(req.headers.authorization, "Bearer " + String(idx+1).repeat(64))
      assert.equal(req.headers.cookie, undefined)
      assert.equal(req.headers["x-nexpoly-page"], undefined)
      if (req.method === "POST") writes++
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
  const supervisor = await listen(net.createServer(socket => socket.once("data", raw => {
    const r = JSON.parse(raw)
    assert.equal(r.operation, "ensure")
    if (disconnected.delete(r.user_id)) repairs++
    socket.end(JSON.stringify({ok:true,result:{...r,workspace:owners.get(r.user_id),state:"running"}})+"\n")
  })),supervisorSocket)
  t.after(() => stop(supervisor))
  const reserve = await listen(net.createServer(),0,"127.0.0.1")
  const port = reserve.address().port, base = "http://127.0.0.1:"+port
  await stop(reserve)
  const router = await startRouter({environment:"dev-m1",listen_port:port,public_origin:base,parent_origins:[parent],
    authority_url:"http://127.0.0.1:"+authority.address().port, service_token_file:join(root,"service-token"),
    supervisor_socket:supervisorSocket,ui_root:root}, async () => ({boot:"",index:"",patchedApp:""}))
  t.after(() => stop(router))
  async function exchange(login,owner,profile,issued,loginIssued = issued,window = 30) {
    const ticket = "one_time_ticket_"+Math.random().toString().slice(2)
    const data = {user_id:"user-"+owner,workspace_user_id:owner,session_id:login,environment:"dev-m1",agent_session:ticket,
      handoff_id:"handoff_"+ticket,issued_at:issued,login_issued_at:loginIssued,window}
    tickets.set(ticket,data);logins.set(ticket,data)
    const response = await fetch(base+"/_nexpoly/auth/exchange",{method:"POST",headers:{Origin:parent},
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
