import { expect, test } from "bun:test";
import { createHash, randomUUID } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const digest = (s: string) => createHash("sha256").update(s).digest("hex");
const operationA = "a".repeat(43), operationB = "b".repeat(43);
const gatewayKey = "s".repeat(43), ingressKey = "r".repeat(43);
const owners = new Map([[operationA, 11], [operationB, 22]]);
const identity = new Map<string, { bodyHash: string; path: string; op: string; method: string }>();
const operationIds = new Map<string, string>();

test("pinned MCP schemas and each call's own narrowed native identity", async () => {
  const dir = await mkdtemp(join(tmpdir(), "np-gateway-test-"));
  const socket = join(dir, "upstream.sock");
  process.env.OPENSCIENCE_M3_AUTH_SOCKET = socket;
  for (const [name, value] of [["INGRESS", ingressKey], ["GATEWAY", gatewayKey]]) {
    const filename = join(dir, name);
    await writeFile(filename, value, {mode: 0o600});
    process.env["OPENSCIENCE_M3_" + name + "_KEY_FILE"] = filename;
  }
  const observed: string[] = [];
  const upstream = Bun.serve({ unix: socket, async fetch(req) {
    expect(req.headers.get("Authorization")).toBe("Bearer " + gatewayKey);
    expect(req.headers.get("Cookie")).toBeNull();
    expect(req.headers.get("X-Session-Context")).toBeNull();
    const url = new URL(req.url);
    if (url.pathname === "/internal/openscience/delegations/exchange") {
      const grant = await req.json();
      if (!owners.has(grant.operation_token)) return new Response(null, { status: 403 });
      if (grant.tool === "predict_polymer_properties") {
        expect(grant.arguments_sha256).toBe(digest('{"properties":["Glass transition temperature"],"smiles":"*CC*"}'));
        expect(grant.query).toBe("");
      }
      const token = randomUUID().replaceAll("-", "") + "n".repeat(11);
      if (!operationIds.has(grant.operation_token)) operationIds.set(grant.operation_token, randomUUID());
      identity.set(token, {op:grant.operation_token, bodyHash:grant.request_sha256, path:grant.path, method:grant.method});
      return Response.json({access_token:token, operation_id:operationIds.get(grant.operation_token), valid_until:Date.now()/1000+20});
    }
    const token = req.headers.get("X-OpenScience-Delegation") ?? "";
    const claim = identity.get(token)!;
    expect(claim).toBeDefined();
    identity.delete(token);
    expect(req.method).toBe(claim.method);
    expect(url.pathname).toBe(claim.path);
    expect(url.pathname).toBe("/api/v1/predict");
    expect(digest(await req.text())).toBe(claim.bodyHash);
    expect(req.headers.get("Idempotency-Key")).toBe("openscience-" + operationIds.get(claim.op));
    observed.push(claim.op);
    return Response.json({predictions:{"Glass transition temperature":owners.get(claim.op)}, query_time_ms:1});
  }});
  try {
    const { createHandler } = await import("/app/src/server.ts");
    const handle = createHandler();
    const call = async (method: string, params?: unknown, operation?: string, auth = ingressKey) => {
      const headers = new Headers({"Content-Type":"application/json", "Accept":"application/json, text/event-stream",
        Authorization:"Bearer "+auth});
      if (operation) headers.set("X-OpenScience-Operation", operation);
      return handle(new Request("http://localhost/mcp", {method:"POST",headers,body:JSON.stringify({
        jsonrpc:"2.0",id:1,method, ...(params ? {params}:{}),
      })}));
    };
    expect((await call("tools/list", undefined, undefined, "incorrect")).status).toBe(401);
    const catalog = await (await call("tools/list")).json();
    expect(catalog.result.tools.map((tool:any)=>tool.name).sort()).toEqual([
      "dft_job_list","dft_job_result","dft_job_status","dft_job_submit","polymerize_monomers","predict_polymer_properties",
    ]);
    const params = {name:"predict_polymer_properties",arguments:{smiles:"*CC*",properties:["Glass transition temperature"]}};
    const results = await Promise.all([operationA,operationB,operationA].map(async op=>{
      const body = await (await call("tools/call",params,op)).json();
      expect(body.result.isError).toBeUndefined();
      return JSON.parse(body.result.content[0].text).predictions["Glass transition temperature"];
    }));
    expect(results).toEqual([11,22,11]);
    const rejected = await (await call("tools/call",params)).json();
    expect(rejected.result.isError).toBe(true);
    expect(observed.sort()).toEqual([operationA, operationA, operationB]);
    expect(identity.size).toBe(0);
  } finally {
    await upstream.stop(true);
    await rm(dir,{recursive:true,force:true});
  }
});
