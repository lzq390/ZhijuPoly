import { getSessionEpoch, onSessionRetired } from "../auth/session";
import type { MdDemoAtomDistanceResponse } from "../types";
import type { PolymerMdJob, PolymerMdTrajectory } from "../types/polymerMd";
import type { MdAtomSelectionSlots } from "../components/md-simulation/MdTrajectoryExplorer";

export type PolymerMdResultTab = "overview" | "curves" | "conformation" | "files";
export type PolymerMdWorkspaceView = { selectedId: string | null; view: "config" | "tasks" | "results" };
export type PolymerMdTrajectoryView = {
  sourceIdentity: string; stageId: string; offset: number; trajectory: PolymerMdTrajectory;
  selections: MdAtomSelectionSlots; pbc: boolean;
  distance: MdDemoAtomDistanceResponse | null; distanceSampling: unknown;
};
type SavedResult = { identity: string; tab: PolymerMdResultTab; trajectory: PolymerMdTrajectoryView | null };
// Memory only: leaving a module releases its UI and requests. A maximum of four
// jobs retain one displayed frame and at most the existing 10,000 distance points.
const results = new Map<string, SavedResult>();
let workspace: PolymerMdWorkspaceView | null = null;
const jobIdentity = (job: PolymerMdJob) => JSON.stringify([job.job_id, job.attempt_fingerprint ?? null, job.created_at, job.started_at ?? null, job.scientific_request]);

export function clearPolymerMdViewState() { results.clear(); workspace = null; }
onSessionRetired(clearPolymerMdViewState);

export function readPolymerMdWorkspace() { return workspace; }
export function savePolymerMdWorkspace(value: PolymerMdWorkspaceView, epoch: number) {
  if (epoch === getSessionEpoch()) workspace = { ...value };
}
export function forgetPolymerMdJobView(jobId: string) {
  results.delete(jobId);
  if (workspace?.selectedId === jobId) workspace = null;
}

/** Revalidate against the fresh, owner-checked job before displaying any cache. */
export function polymerMdTrajectoryIdentity(job: PolymerMdJob, stageId: string): string | null {
  if (job.artifact_state === "deleted" || job.artifact_state === "delete_requested") return null;
  const stage = job.stage_runs.find(item => item.stage_run_id === stageId && item.status === "completed");
  if (!stage) return null;
  const sources = job.artifacts.filter(item => item.stage_run_id === stageId && /\.(xtc|pdb)$/i.test(item.filename))
    .map(item => [item.artifact_id, item.kind, item.filename, item.size_bytes, item.sha256])
    .sort((a, b) => String(a[0]).localeCompare(String(b[0])));
  return JSON.stringify([jobIdentity(job), stage.stage_run_id, stage.stage, stage.attempt,
    stage.started_at ?? null, stage.finished_at ?? null, stage.results.trajectory ?? null, sources]);
}

export function readPolymerMdResultView(job: PolymerMdJob): SavedResult | null {
  const saved = results.get(job.job_id);
  if (!saved) return null;
  if (saved.identity !== jobIdentity(job)) { results.delete(job.job_id); return null; }
  if (saved.trajectory && saved.trajectory.sourceIdentity !== polymerMdTrajectoryIdentity(job, saved.trajectory.stageId)) {
    saved.trajectory = null;
  }
  results.delete(job.job_id); results.set(job.job_id, saved);
  return saved;
}
function retain(job: PolymerMdJob): SavedResult {
  const saved = readPolymerMdResultView(job) ?? { identity: jobIdentity(job), tab: "overview", trajectory: null };
  results.delete(job.job_id); results.set(job.job_id, saved);
  while (results.size > 4) results.delete(results.keys().next().value!);
  return saved;
}
export function savePolymerMdResultTab(job: PolymerMdJob, tab: PolymerMdResultTab, epoch: number) {
  if (epoch === getSessionEpoch()) retain(job).tab = tab;
}
export function savePolymerMdTrajectoryView(job: PolymerMdJob, value: PolymerMdTrajectoryView, epoch: number) {
  if (epoch !== getSessionEpoch() || value.sourceIdentity !== polymerMdTrajectoryIdentity(job, value.stageId)
    || value.trajectory.job_id !== job.job_id || value.trajectory.stage_run_id !== value.stageId
    || value.trajectory.frames[0]?.frame_index !== value.offset) return;
  retain(job).trajectory = value;
}
export function clearPolymerMdTrajectoryView(jobId: string, epoch: number) {
  if (epoch === getSessionEpoch()) { const saved = results.get(jobId); if (saved) saved.trajectory = null; }
}
