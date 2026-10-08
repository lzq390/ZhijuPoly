// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { installSession, privateFetch, retireSession } from "./session";
import { confirmUsageOperation, observeUsageEnabled } from "./usageAdmission";
import { requestFingerprint, sha256 } from "./usageRequestIdentity";
import { streamTgAssistant } from "../services/tgAssistantStream";
const operationId = "00000000-0000-4000-8000-000000000001";
const mock = vi.fn<typeof fetch>();
function identity(id="a", account_type: "member"|"free"="member") {
  retireSession(); installSession({authenticated:true,user:{id,username:id,must_change_password:false},session_id:`session-${id}`,csrf_token:"fixture",capabilities:{},account_type});
}
const json = (value: unknown, status=200, state="succeeded") => new Response(JSON.stringify(value), {status,headers:{"Content-Type":"application/json","X-Usage-Operation-ID":operationId,"X-Usage-State":state}});
const send = (body='{"smiles":"fixture-private-structure"}', path="/predict", headers?: HeadersInit) => privateFetch(`/api/v1${path}`, {method:"POST",body,headers:{"Content-Type":"application/json",...headers}}).then(r=>r.json());
const key = (index: number) => new Headers(mock.mock.calls[index][1]?.headers).get("Idempotency-Key");
beforeEach(()=>{sessionStorage.clear();identity();observeUsageEnabled({enabled:true});mock.mockReset();vi.stubGlobal("fetch",mock);});
describe("counted submission identity",()=>{
  it("supports SHA256 on insecure development origins without SubtleCrypto",()=>{
    expect(sha256(new TextEncoder().encode("abc"))).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    expect(sha256(new Uint8Array())).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  });
  it("uses stable payload fingerprints and hashes form file bytes without storing content",async()=>{
    expect(await requestFingerprint("/x",'{"b":2,"a":1}')).toBe(await requestFingerprint("/x",'{"a":1,"b":2}'));
    const form=()=>{const value=new FormData();value.append("file",new File(["synthetic"],"image.png",{type:"image/png"}));return value;};
    expect(await requestFingerprint("/x",form())).toBe(await requestFingerprint("/x",form()));
  });
  it("keeps one key after a lost response and rotates only after confirmed JSON result",async()=>{
    mock.mockRejectedValueOnce(new TypeError("network")).mockImplementation(async()=>json({result:"ok"}));
    await expect(send()).rejects.toThrow("network"); await send(); await send();
    expect(key(0)).toMatch(/^usage-/);expect(key(1)).toBe(key(0));expect(key(2)).not.toBe(key(1));
    expect(sessionStorage.getItem("nexpoly.usage.pending.v1")).not.toContain("fixture-private-structure");
  });
  it("preserves an uncertain key across reload of the same owner/session",async()=>{
    mock.mockRejectedValueOnce(new TypeError("lost")).mockImplementation(async()=>json({ok:true}));
    await expect(send()).rejects.toThrow();identity();observeUsageEnabled({enabled:true});await send();expect(key(1)).toBe(key(0));
  });
  it("never adopts another account's pending key",async()=>{
    mock.mockRejectedValueOnce(new TypeError("lost")).mockImplementation(async()=>json({ok:true}));
    await expect(send()).rejects.toThrow();identity("b");observeUsageEnabled({enabled:true});await send();expect(key(1)).not.toBe(key(0));
    expect(new Headers(mock.mock.calls[1][1]?.headers).get("X-Session-Context")).toBe("session-b");
  });
  it.each(["usage_operation_pending","usage_outcome_unknown"])("keeps the key for %s without automatically retrying",async code=>{
    mock.mockImplementation(async()=>json({detail:{code,operation_id:operationId}},409,"unknown"));
    await expect(send()).rejects.toMatchObject({code,operationId,retryable:false});expect(mock).toHaveBeenCalledTimes(1);
    await expect(send()).rejects.toMatchObject({code});expect(key(1)).toBe(key(0));
    confirmUsageOperation(operationId);await expect(send()).rejects.toMatchObject({code});expect(key(2)).not.toBe(key(1));
  });
  it("keeps native unknown202 but rotates an accepted job after confirmation",async()=>{
    mock.mockImplementationOnce(async()=>json({job_id:"original-job"},202,"unknown"))
      .mockImplementation(async()=>json({job_id:"original-job"},202,"running"));
    await send('{}',"/monomer-md/jobs");await send('{}',"/monomer-md/jobs");await send('{}',"/monomer-md/jobs");
    expect(key(1)).toBe(key(0));expect(key(2)).not.toBe(key(1));
  });
  it("does not confirm a malformed successful job response without its job ID",async()=>{
    mock.mockImplementation(async()=>json({unexpected:true},202,"running"));await send('{}',"/monomer-md/jobs");await send('{}',"/monomer-md/jobs");expect(key(1)).toBe(key(0));
  });
  it.each(["/monomer-dft/jobs","/monomer-polymerization/batch/jobs"])("preserves native caller key for %s",async path=>{
    mock.mockImplementation(async()=>json({job_id:"native"},202,"running"));await send('{}',path,{"Idempotency-Key":"native-existing-key"});expect(key(0)).toBe("native-existing-key");
  });
  it.each(["/query/smiles","/online-knowledge/search","/online-knowledge/jobs","/conditional-generation/tg/jobs","/conditional-generation/polytao/jobs","/structure/3d","/structure/recognize-image","/monomer-retrosynthesis","/monomer-polymerization","/knowledge/recordings/fixture/summary"])("adds a stable admission key to %s",async path=>{
    mock.mockImplementation(async()=>json({ok:true}));await send('{}',path);expect(key(0)).toMatch(/^usage-/);
  });
  it("leaves default-off submissions unchanged",async()=>{
    observeUsageEnabled({enabled:false});mock.mockImplementation(async()=>json({ok:true}));await send();expect(key(0)).toBeNull();expect(mock).toHaveBeenCalledTimes(1);
  });
  it("probes the explicit feature flag before the first submission",async()=>{
    identity("probe");mock.mockImplementation(async url=>String(url).endsWith('/entitlements')?json({enabled:true}):json({ok:true}));await send();expect(String(mock.mock.calls[0][0])).toContain('/usage/entitlements');expect(key(1)).toMatch(/^usage-/);
  });
  it("does not send a POST after failed or malformed feature detection",async()=>{
    identity("probe");mock.mockImplementation(async()=>json({detail:"unavailable"},503));await expect(send()).rejects.toThrow("未发送计算请求");expect(mock).toHaveBeenCalledTimes(1);expect(mock.mock.calls[0][1]?.method).not.toBe("POST");
  });
  it("does not add keys to GET result reads or task cancellation",async()=>{
    mock.mockImplementation(async()=>json({ok:true}));await (await privateFetch(`/api/v1/usage/operations/${operationId}/result`)).json();await send('{}',"/monomer-md/jobs/fixture/cancel");expect(key(0)).toBeNull();expect(key(1)).toBeNull();
  });
  it("blocks free member services locally while allowing own usage reads",async()=>{
    identity("free","free");mock.mockImplementation(async()=>json({enabled:false}));await expect(send()).rejects.toMatchObject({code:"membership_required"});expect(mock).not.toHaveBeenCalled();await (await privateFetch('/api/v1/usage/entitlements')).json();expect(mock).toHaveBeenCalledOnce();
  });
  it("keeps SSE identity on clean EOF without done, and rotates after done",async()=>{
    const payload={messages:[{role:"user" as const,content:"fixture"}]};
    mock.mockImplementationOnce(async()=>new Response('event: delta\ndata: {"text":"partial"}\n\n',{headers:{"Content-Type":"text/event-stream","X-Usage-Operation-ID":operationId}}))
      .mockImplementation(async()=>new Response('event: done\ndata: {"message":"complete"}\n\n',{headers:{"Content-Type":"text/event-stream","X-Usage-Operation-ID":operationId}}));
    await expect(streamTgAssistant(payload,()=>{},new AbortController().signal,undefined,"same-user-item")).rejects.toThrow("terminal");
    await streamTgAssistant(payload,()=>{},new AbortController().signal,undefined,"same-user-item");
    await streamTgAssistant(payload,()=>{},new AbortController().signal,undefined,"same-user-item");
    expect(key(1)).toBe(key(0));expect(key(2)).not.toBe(key(1));
  });
  it("does not silently rotate a retry intent when its context changes",async()=>{
    mock.mockRejectedValueOnce(new TypeError("lost")).mockImplementation(async()=>json({detail:{code:"idempotency_conflict"}},409));
    await expect(privateFetch('/api/v1/assistant/chat/stream',{method:"POST",body:'{"a":1}',usageIntent:"user-item"})).rejects.toThrow();
    await expect(privateFetch('/api/v1/assistant/chat/stream',{method:"POST",body:'{"a":2}',usageIntent:"user-item"})).rejects.toMatchObject({code:"idempotency_conflict"});expect(key(1)).toBe(key(0));
  });
});
