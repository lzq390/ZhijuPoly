// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { POLYMER_MD_DEFAULTS } from "../components/polymer-md/config";
import { retireSession } from "../auth/session";
import { calculatePolymerMdDistance, createPolymerMdJob, fetchPolymerMdTrajectory, fetchPolymerMdJob, clearPolymerMdArtifacts, fetchPolymerMdStageLogs } from "./polymerMdApi";
afterEach(() => vi.unstubAllGlobals());
describe("formal polymer MD transport", () => {
  it("sends a formal request with authentication and its stable idempotency key", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "real-job" }), { status: 202 })); vi.stubGlobal("fetch", fetch);
    await createPolymerMdJob(POLYMER_MD_DEFAULTS, "same-attempt");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/v1/polymer-md/jobs"); expect(init.headers.get("Idempotency-Key")).toBe("same-attempt"); expect(init.headers.get("X-Session-Context")).toBe("test-session"); expect(init.headers.get("X-CSRF-Token")).toBe("test-csrf");
    expect(JSON.parse(init.body)).toMatchObject({ smiles: "*CC*", temp: 300, calculation: "basic", terminal_head: "CH3", seed: null });
  });
  it("reads a real frame through both job and stage identity", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "j", stage_run_id: "eq3-1", stage_id: "eq3", frame_index: 4, frame_count: 8, time_ps: 4, points: [{ atom_id: 7, chain_id: 1, atom_type: "C", x: 1, y: 2, z: 3 }], box: { lx: 10, ly: 10, lz: 10 } }))); vi.stubGlobal("fetch", fetch);
    const response = await fetchPolymerMdTrajectory("j", "eq3-1", 4);
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/polymer-md/jobs/j/stages/eq3-1/trajectory?frame_index=4&limit=2000"); expect(response.frames[0].points[0].atom_id).toBe(7); expect(response.total_frames).toBe(8);
  });
  it("normalizes the scientific distance series without using demo fixtures", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "j", stage_run_id: "eq4-1", unit: "A", points: [{ frame: 3, time_ps: 6, value: 2.5 }] }))); vi.stubGlobal("fetch", fetch);
    const data = await calculatePolymerMdDistance("j", "eq4-1", { atom_id_1: 2, atom_id_2: 6, use_pbc: true, frame_count: 4 });
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/polymer-md/jobs/j/stages/eq4-1/distance"); expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ atom_id_1: 2, atom_id_2: 6, use_pbc: true, start_frame: 0, stop_frame: 4, stride: 1 }); expect(data.distance).toEqual([2.5]); expect(data.frames).toEqual([3]);
    expect(data.sampling).toMatchObject({ source_frame_count: 4, sampled_frames: 1, first_frame: 3, last_frame: 3, stride: 1 });
  });
  it.each([10000, 10001, 20000, 20001, 100000])("bounds a %i-frame RNEMD trajectory to at most 10000 actual distance samples", async frameCount => {
    const fetch = vi.fn(async (_url, init) => {
      const body = JSON.parse(init.body);
      const points = Array.from({ length: Math.ceil((body.stop_frame - body.start_frame) / body.stride) }, (_, i) => ({ frame: i * body.stride, time_ps: i * body.stride / 10, value: 2 }));
      return new Response(JSON.stringify({ job_id: "j", stage_run_id: "tc-1", points }));
    });
    vi.stubGlobal("fetch", fetch);
    const result = await calculatePolymerMdDistance("j", "tc-1", { atom_id_1: 1, atom_id_2: 2, use_pbc: true, frame_count: frameCount });
    const body = JSON.parse(fetch.mock.calls[0][1].body);
    expect(body).toMatchObject({ stride: Math.ceil(frameCount / 10000), stop_frame: frameCount });
    expect(body).not.toHaveProperty("frame_count");
    expect(result.distance.length).toBeLessThanOrEqual(10000);
    expect(result.sampling).toMatchObject({ source_frame_count: frameCount, sampled_frames: result.distance.length, stride: body.stride, last_frame: result.frames.at(-1), stop_ps: result.time_ps.at(-1) });
  });
  it("refuses unknown frame counts and cross-stage distance responses", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "j", stage_run_id: "wrong", points: [{ frame: 0, time_ps: 0, value: 1 }] })));
    vi.stubGlobal("fetch", fetch);
    await expect(calculatePolymerMdDistance("j", "tc-1", { atom_id_1: 1, atom_id_2: 2, use_pbc: true, frame_count: 0 })).rejects.toThrow("真实轨迹帧数不可用");
    expect(fetch).not.toHaveBeenCalled();
    await expect(calculatePolymerMdDistance("j", "tc-1", { atom_id_1: 1, atom_id_2: 2, use_pbc: true, frame_count: 20001 })).rejects.toThrow("当前任务阶段不匹配");
  });
  it.each(["session", "cancel"])("retires a delayed distance response after %s retirement", async reason => {
    let resolve!: (response: Response) => void;
    const fetch = vi.fn((_url: string, _init: RequestInit) => new Promise<Response>(r => { resolve = r; }));
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    const result = calculatePolymerMdDistance("j", "tc-1", { atom_id_1: 1, atom_id_2: 2, use_pbc: true, frame_count: 20001 }, controller.signal);
    const rejected = expect(result).rejects.toMatchObject({ name: "AbortError" });
    if (reason === "session") retireSession(); else controller.abort();
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
    resolve(new Response(JSON.stringify({ job_id: "j", stage_run_id: "tc-1", points: [{ frame: 0, time_ps: 0, value: 1 }] })));
    await rejected;
  });
  it("reports backend readiness failures rather than substituting fixture data", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "计算服务尚未就绪" }), { status: 503 })));
    await expect(fetchPolymerMdJob("j")).rejects.toThrow("计算服务尚未就绪");
  });
  it("uses an authenticated DELETE for separate artifact cleanup", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "j", artifacts: [] }))); vi.stubGlobal("fetch", fetch);
    await clearPolymerMdArtifacts("j"); expect(fetch.mock.calls[0][0]).toBe("/api/v1/polymer-md/jobs/j/artifacts"); expect(fetch.mock.calls[0][1].method).toBe("DELETE");
  });
  it("reads a bounded log tail using job, stage and the current session", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ job_id: "j", stage_run_id: "eq3-1", lines: ["running"], truncated: true }))); vi.stubGlobal("fetch", fetch);
    const data = await fetchPolymerMdStageLogs("j", "eq3-1");
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/polymer-md/jobs/j/stages/eq3-1/logs?tail_lines=200");
    expect(fetch.mock.calls[0][1].headers.get("X-Session-Context")).toBe("test-session"); expect(data.lines).toEqual(["running"]);
  });
});
