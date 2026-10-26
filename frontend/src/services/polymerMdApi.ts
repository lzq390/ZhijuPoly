import { assertSessionEpoch, getSessionEpoch, onSessionRetired, privateFetch } from "../auth/session";
import type { PolymerMdCapabilities, PolymerMdDistance, PolymerMdJob, PolymerMdJobPage, PolymerMdRequest, PolymerMdServiceStatus, PolymerMdTrajectory } from "../types/polymerMd";
export const POLYMER_MD_API_BASE = `${import.meta.env.VITE_API_BASE_URL ?? "/api/v1"}/polymer-md`;
export class PolymerMdApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly code?: string) {
    super(message);
    this.name = "PolymerMdApiError";
  }
}
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await privateFetch(`${POLYMER_MD_API_BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    throw new PolymerMdApiError(response.status, typeof detail === "string" ? detail : detail?.message ?? body?.message ?? `请求失败（${response.status}）`, detail?.code ?? body?.code);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
const jobPath = (id: string) => `/jobs/${encodeURIComponent(id)}`;
const stagePath = (id: string, stage: string) => `${jobPath(id)}/stages/${encodeURIComponent(stage)}`;
export const fetchPolymerMdCapabilities = (signal?: AbortSignal) => request<PolymerMdCapabilities>("/capabilities", {
  signal
});
export const fetchPolymerMdStatus = (signal?: AbortSignal) => request<PolymerMdServiceStatus>("/status", {
  signal
});
export const fetchPolymerMdJobs = (page = 1, signal?: AbortSignal) => request<PolymerMdJobPage>(`/jobs?page=${page}&page_size=10`, {
  signal
});
export const fetchPolymerMdJob = (id: string, signal?: AbortSignal) => request<PolymerMdJob>(jobPath(id), {
  signal
});
export const fetchPolymerMdStageLogs = (id: string, stage: string, signal?: AbortSignal) => request<{
  job_id: string;
  stage_run_id: string;
  lines: string[];
  truncated: boolean;
  status?: string;
}>(`${stagePath(id, stage)}/logs?tail_lines=200`, { signal });
export const createPolymerMdJob = (body: PolymerMdRequest, key: string, signal?: AbortSignal) => request<PolymerMdJob>("/jobs", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Idempotency-Key": key
  },
  body: JSON.stringify(body),
  signal
});
export const cancelPolymerMdJob = (id: string, signal?: AbortSignal) => request<PolymerMdJob>(`${jobPath(id)}/cancel`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json"
  },
  body: "{}",
  signal
});
export const clearPolymerMdArtifacts = (id: string, signal?: AbortSignal) => request<{
  job_id: string;
  deleted: boolean;
  artifact_state: string;
}>(`${jobPath(id)}/artifacts`, {
  method: "DELETE",
  signal
});
export const deletePolymerMdJob = (id: string, signal?: AbortSignal) => request<void>(jobPath(id), {
  method: "DELETE",
  signal
});
export async function fetchPolymerMdTrajectory(id: string, stage: string, offset = 0, signal?: AbortSignal): Promise<PolymerMdTrajectory> {
  const raw = await request<PolymerMdTrajectory & {
    frame_count?: number;
    frame_index?: number;
    time_ps?: number;
    points?: PolymerMdTrajectory["frames"][number]["points"];
    box?: PolymerMdTrajectory["frames"][number]["box"];
  }>(`${stagePath(id, stage)}/trajectory?frame_index=${offset}&limit=2000`, {
    signal
  });
  if (Array.isArray(raw.frames)) return raw;
  if (!Array.isArray(raw.points) || typeof raw.frame_index !== "number" || typeof raw.time_ps !== "number" || !raw.box) throw new Error("当前阶段的轨迹格式不完整。");
  return {
    job_id: raw.job_id,
    stage_run_id: raw.stage_run_id,
    total_frames: raw.frame_count ?? 0,
    frames: [{
      frame_index: raw.frame_index,
      time_ps: raw.time_ps,
      points: raw.points,
      box: raw.box
    }]
  };
}
export async function calculatePolymerMdDistance(id: string, stage: string, body: {
  atom_id_1: number;
  atom_id_2: number;
  use_pbc: boolean;
  frame_count: number;
}, signal?: AbortSignal): Promise<PolymerMdDistance> {
  if (!Number.isSafeInteger(body.frame_count) || body.frame_count < 1) throw new Error("当前阶段的真实轨迹帧数不可用，请重新读取轨迹。");
  const stride = Math.ceil(body.frame_count / 10000);
  if (stride > 10000) throw new Error("当前轨迹超过距离分析范围，请先选取不超过一亿帧的轨迹范围。");
  const raw = await request<PolymerMdDistance & {
    points?: Array<{
      frame: number;
      time_ps: number;
      value: number;
    }>;
  }>(`${stagePath(id, stage)}/distance`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      atom_id_1: body.atom_id_1,
      atom_id_2: body.atom_id_2,
      use_pbc: body.use_pbc,
      start_frame: 0,
      stop_frame: body.frame_count,
      stride
    }),
    signal
  });
  const result = Array.isArray(raw.distance) && Array.isArray(raw.time_ps) ? raw : Array.isArray(raw.points) ? {
    ...raw, frames: raw.points.map(p => p.frame), time_ps: raw.points.map(p => p.time_ps), distance: raw.points.map(p => p.value)
  } : null;
  if (!result || result.job_id !== id || result.stage_run_id !== stage || !Array.isArray(result.frames)
    || result.frames.length !== result.distance.length || result.frames.length !== result.time_ps.length || !result.frames.length || result.frames.length > 10000
    || result.frames.some((frame, i) => !Number.isSafeInteger(frame) || frame < 0 || frame >= body.frame_count || frame % stride !== 0 || i > 0 && frame <= result.frames[i - 1])
    || result.time_ps.some(t => !Number.isFinite(t)) || result.distance.some(d => !Number.isFinite(d))) throw new Error("距离结果与当前任务阶段不匹配或采样数据不完整。");
  return { ...result, sampling: { source_frame_count: body.frame_count, start_frame: 0, stop_frame: body.frame_count, stride, sampled_frames: result.frames.length,
    first_frame: result.frames[0], last_frame: result.frames.at(-1)!, start_ps: result.time_ps[0], stop_ps: result.time_ps.at(-1)! } };
}
type DownloadWriter = { write: (chunk: Uint8Array) => Promise<void>; close: () => Promise<void>; abort: () => Promise<void> };
type SavePicker = (options: { suggestedName: string }) => Promise<{ createWritable: () => Promise<DownloadWriter> }>;
export const POLYMER_MD_MEMORY_DOWNLOAD_LIMIT = 256 * 1024 * 1024;
export async function downloadPolymerMdArtifact(id: string, artifact: string, filename: string, sizeBytes?: number, signal?: AbortSignal) {
  const epoch = getSessionEpoch();
  const picker = (window as Window & { showSaveFilePicker?: SavePicker }).showSaveFilePicker;
  if (!picker && (sizeBytes === undefined || !Number.isFinite(sizeBytes) || sizeBytes > POLYMER_MD_MEMORY_DOWNLOAD_LIMIT)) throw new Error("此文件较大或大小未知，请使用支持流式文件保存的浏览器（如桌面 Chrome 或 Edge）下载。当前浏览器仅支持最多 256 MiB 的文件。");
  const controller = new AbortController();
  let writer: DownloadWriter | null = null;
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  const abort = () => { controller.abort(); if (writer) void writer.abort().catch(() => {}); };
  const retire = onSessionRetired(abort);
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  try {
    // Invoke the picker before the first await/network request to preserve the
    // user's click activation. The writable commits only on successful close.
    if (picker) {
      const handle = await picker.call(window, { suggestedName: filename });
      assertSessionEpoch(epoch);
      if (controller.signal.aborted) throw new DOMException("下载已取消。", "AbortError");
      writer = await handle.createWritable();
    }
    const response = await privateFetch(`${POLYMER_MD_API_BASE}${jobPath(id)}/artifacts/${encodeURIComponent(artifact)}`, { signal: controller.signal });
    if (!response.ok) {
      await response.text();
      throw new PolymerMdApiError(response.status, "下载失败，请刷新任务后重试。");
    }
    if (!response.body) throw new Error("下载内容为空，请刷新任务后重试。");
    reader = response.body.getReader();
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let received = 0;
    while (true) {
      assertSessionEpoch(epoch);
      if (controller.signal.aborted) throw new DOMException("下载已取消。", "AbortError");
      const { done, value } = await reader.read();
      assertSessionEpoch(epoch);
      if (done) break;
      received += value.byteLength;
      if (sizeBytes !== undefined && received > sizeBytes) throw new Error("下载大小与产物记录不符，请刷新任务后重试。");
      if (writer) await writer.write(value);
      else {
        if (received > POLYMER_MD_MEMORY_DOWNLOAD_LIMIT) throw new Error("此浏览器的下载内存上限为 256 MiB，请改用支持流式文件保存的浏览器。");
        chunks.push(Uint8Array.from(value));
      }
      assertSessionEpoch(epoch);
    }
    if (sizeBytes !== undefined && received !== sizeBytes) throw new Error("下载未完整接收，请刷新任务后重试。");
    if (writer) {
      await writer.close();
      writer = null;
    } else {
      const blob = new Blob(chunks, { type: response.headers.get("Content-Type") ?? "application/octet-stream" });
      assertSessionEpoch(epoch);
      const url = URL.createObjectURL(blob);
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.append(link);
        link.click();
        link.remove();
      } finally { URL.revokeObjectURL(url); }
    }
  } catch (error) {
    controller.abort();
    if (writer) await writer.abort().catch(() => {});
    if (reader) await reader.cancel().catch(() => {});
    throw error;
  } finally {
    retire();
    signal?.removeEventListener("abort", abort);
    reader?.releaseLock();
  }
}
