// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { getSessionEpoch, retireSession } from "../auth/session";
import type { PolymerMdJob } from "../types/polymerMd";
import { POLYMER_MD_DEFAULTS } from "../components/polymer-md/config";
import { clearPolymerMdViewState, forgetPolymerMdJobView, polymerMdTrajectoryIdentity, readPolymerMdResultView, readPolymerMdWorkspace, savePolymerMdResultTab, savePolymerMdTrajectoryView, savePolymerMdWorkspace } from "./polymerMdViewState";

const job = (jobId = "job-1"): PolymerMdJob => ({ job_id: jobId, status: "running", quality_status: "passed", scientific_request: POLYMER_MD_DEFAULTS,
  stage: "eq3", stage_runs: [{ stage_run_id: "eq1-1", stage: "eq1", attempt: 1, status: "completed", quality_status: "passed", results: {}, quality: {}, errors: [] }],
  artifacts: [{ artifact_id: "xtc", stage_run_id: "eq1-1", kind: "trajectory", filename: "eq1.xtc", size_bytes: 100, sha256: "a".repeat(64), media_type: "application/octet-stream" }],
  results: {}, created_at: "2026-10-28T00:00:00Z", started_at: "2026-10-28T00:00:01Z", updated_at: "2026-10-30T00:00:00Z" });
function save(value: PolymerMdJob, epoch = getSessionEpoch()) {
  savePolymerMdResultTab(value, "conformation", epoch);
  savePolymerMdTrajectoryView(value, { sourceIdentity: polymerMdTrajectoryIdentity(value, "eq1-1")!, stageId: "eq1-1", offset: 0,
    trajectory: { job_id: value.job_id, stage_run_id: "eq1-1", total_frames: 1, frames: [{ frame_index: 0, time_ps: 0, points: [], box: { lx: 10, ly: 10, lz: 10 } }] },
    selections: [null, null], pbc: false, distance: null, distanceSampling: null }, epoch);
}
beforeEach(clearPolymerMdViewState);
describe("private polymer MD view memory", () => {
  it("keeps the displayed frame through later progress, log publication and unrelated-stage artifacts", () => {
    const value = job(); save(value);
    const current = { ...value, updated_at: "2026-10-30T01:00:00Z", artifacts: [...value.artifacts,
      { ...value.artifacts[0], artifact_id: "later-xtc", stage_run_id: "eq3-1" },
      { ...value.artifacts[0], artifact_id: "log", filename: "engine.log", kind: "log" }] };
    expect(readPolymerMdResultView(current)?.trajectory?.pbc).toBe(false);
  });
  it.each(["delete_requested", "deleted"] as const)("retires scientific results when artifacts become %s", state => {
    const value = job(); save(value);
    expect(readPolymerMdResultView({ ...value, artifact_state: state })?.trajectory).toBeNull();
    expect(readPolymerMdResultView(value)?.trajectory).toBeNull();
  });
  it("retires changed source checksums, stage attempts, metadata and removed stages", () => {
    const value = job();
    for (const changed of [
      { ...value, artifacts: [{ ...value.artifacts[0], sha256: "b".repeat(64) }] },
      { ...value, stage_runs: [{ ...value.stage_runs[0], attempt: 2 }] },
      { ...value, stage_runs: [{ ...value.stage_runs[0], results: { trajectory: { frame_count: 2 } } }] },
      { ...value, stage_runs: [] }
    ]) { save(value); expect(readPolymerMdResultView(changed)?.trajectory).toBeNull(); }
  });
  it("evicts the least recently viewed job at four entries", () => {
    for (let i = 1; i <= 4; i++) save(job(`job-${i}`));
    expect(readPolymerMdResultView(job("job-1"))?.trajectory).toBeTruthy();
    save(job("job-5"));
    expect(readPolymerMdResultView(job("job-2"))).toBeNull();
    expect(readPolymerMdResultView(job("job-1"))?.trajectory).toBeTruthy();
  });
  it("rejects another execution attempt even when all visible stage metadata is unchanged", () => {
    const value = { ...job(), attempt_fingerprint: "a".repeat(64) }; save(value);
    expect(readPolymerMdResultView({ ...value, attempt_fingerprint: "b".repeat(64) })).toBeNull();
  });
  it("clears navigation and cached data on retirement and rejects writes from that old identity", () => {
    const value = job(), oldEpoch = getSessionEpoch(); save(value);
    savePolymerMdWorkspace({ selectedId: value.job_id, view: "results" }, oldEpoch);
    retireSession(); save(value, oldEpoch);
    savePolymerMdWorkspace({ selectedId: value.job_id, view: "results" }, oldEpoch);
    expect(readPolymerMdResultView(value)).toBeNull(); expect(readPolymerMdWorkspace()).toBeNull();
  });
  it("forgets an inaccessible job and its last workspace selection", () => {
    const value = job(); save(value);
    savePolymerMdWorkspace({ selectedId: value.job_id, view: "results" }, getSessionEpoch());
    forgetPolymerMdJobView(value.job_id);
    expect(readPolymerMdResultView(value)).toBeNull(); expect(readPolymerMdWorkspace()).toBeNull();
  });
});
