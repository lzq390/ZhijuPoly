import { getSession, sessionIsReady } from "../auth/session";
import type { PolymerMdRequest } from "../types/polymerMd";
import { POLYMER_MD_API_BASE } from "./polymerMdApi";
import { POLYMER_MD_FORCEFIELDS, validPolymerMdNativeConfiguration, validPolymerMdModelRequest, validPolymerMdPostEquilibriumRequest } from "./polymerMdNativeConfiguration";

export const POLYMER_MD_PENDING_KEY = "nexpoly:polymer-md-simulation:pending:v1";
export const POLYMER_MD_PENDING_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const STORAGE_ERROR = "浏览器无法可靠保存待确认的提交，已停止提交以避免重复计算。请启用此标签页的会话存储后重试。";
type Scope = { owner: string; session: string; api: string };
export type PolymerMdPendingSubmission = {
  version: 1;
  scope: Scope;
  request: PolymerMdRequest;
  key: string;
  createdAt: number;
  conflict: boolean;
};

function scope(): Scope {
  const current = getSession();
  if (!sessionIsReady() || !current.authenticated || !current.user?.id || !current.session_id) throw new Error("请先确认登录状态，再恢复或提交计算。");
  return { owner: current.user.id, session: current.session_id, api: new URL(POLYMER_MD_API_BASE, window.location.href).href };
}
const sameScope = (a: Scope, b: Scope) => a.owner === b.owner && a.session === b.session && a.api === b.api;
export function polymerMdRequestFingerprint(request: PolymerMdRequest): string {
  const normalized = { ...request, seed: request.seed ?? null };
  if (request.native_configuration) normalized.native_configuration = Object.fromEntries(
    Object.entries({ ...request.native_configuration, properties: [...request.native_configuration.properties].sort() })
      .sort(([a], [b]) => a.localeCompare(b))) as typeof request.native_configuration;
  const canonical = (value: unknown): unknown => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => [key, canonical(item)])) : value;
  if (request.model_configuration) normalized.model_configuration = canonical(request.model_configuration) as typeof request.model_configuration;
  return JSON.stringify(Object.fromEntries(Object.entries(normalized).sort(([a], [b]) => a.localeCompare(b))));
}
function validRequest(value: unknown): value is PolymerMdRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const r = value as Record<string, unknown>;
  const keys = ["smiles", "temp", "pressure", "n_atom", "n_chain", "forcefield", "terminal_head", "terminal_tail", "tacticity",
    "calculation", "eq_ns", "additional_ns", "max_additional", "sample_interval_ps", "tc_axis", "tc_ns", "tc_rep", "seed", "native_configuration", "model_configuration"];
  const choice = (key: string, allowed: readonly string[]) => typeof r[key] === "string" && allowed.includes(r[key]);
  const native = r.native_configuration;
  return Object.keys(r).every(key => keys.includes(key))
    && typeof r.smiles === "string" && r.smiles.length <= 2048 && choice("forcefield", POLYMER_MD_FORCEFIELDS)
    && choice("terminal_head", ["CH3", "H", "OH"]) && choice("terminal_tail", ["CH3", "H", "OH"])
    && choice("tacticity", ["atactic", "isotactic", "syndiotactic"]) && choice("calculation", ["basic", "basic_tc"])
    && choice("tc_axis", ["x", "y", "z"])
    && ["temp", "pressure", "n_atom", "n_chain", "eq_ns", "additional_ns", "max_additional", "sample_interval_ps", "tc_ns", "tc_rep"].every(key => typeof r[key] === "number" && Number.isFinite(r[key]))
    && (r.seed == null || typeof r.seed === "number" && Number.isFinite(r.seed))
    && validPolymerMdModelRequest(r as PolymerMdRequest)
    && validPolymerMdPostEquilibriumRequest(r as PolymerMdRequest)
    && (!("native_configuration" in r) || validPolymerMdNativeConfiguration(native)
      && r.calculation === (native.properties.some(key => ["thermal_conductivity", "thermal_diffusivity"].includes(key)) ? "basic_tc" : "basic"));
}
function validPending(value: PolymerMdPendingSubmission): boolean {
  return value.version === 1 && validRequest(value.request) && typeof value.key === "string"
    && /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(value.key)
    && Number.isFinite(value.createdAt) && typeof value.conflict === "boolean";
}
export function readPolymerMdPending(): PolymerMdPendingSubmission | null {
  const current = scope();
  let raw: string | null;
  try { raw = window.sessionStorage.getItem(POLYMER_MD_PENDING_KEY); } catch { throw new Error(STORAGE_ERROR); }
  if (!raw) return null;
  let value: PolymerMdPendingSubmission;
  try { value = JSON.parse(raw) as PolymerMdPendingSubmission; } catch { throw new Error("待确认提交记录已损坏。请先检查我的任务，再放弃本地记录。"); }
  if (!value?.scope || typeof value.scope.owner !== "string" || typeof value.scope.session !== "string" || typeof value.scope.api !== "string") throw new Error("待确认提交记录已损坏。请先检查我的任务，再放弃本地记录。");
  if (!sameScope(value.scope, current)) {
    // Never expose or replay a record from another account, login session or API.
    try { window.sessionStorage.removeItem(POLYMER_MD_PENDING_KEY); } catch { throw new Error(STORAGE_ERROR); }
    return null;
  }
  if (!validPending(value)) throw new Error("待确认提交记录已损坏。请先检查我的任务，再放弃本地记录。");
  return value;
}
export function writePolymerMdPending(value: PolymerMdPendingSubmission) {
  if (!sameScope(value.scope, scope())) throw new Error("登录状态已改变，不能恢复之前的提交。");
  if (!validPending(value)) throw new Error("提交配置不符合当前原生选项，请核对配置后重试。");
  const raw = JSON.stringify(value);
  try {
    window.sessionStorage.setItem(POLYMER_MD_PENDING_KEY, raw);
    if (window.sessionStorage.getItem(POLYMER_MD_PENDING_KEY) !== raw) throw new Error();
  } catch { throw new Error(STORAGE_ERROR); }
}
export function createPolymerMdPending(request: PolymerMdRequest): PolymerMdPendingSubmission {
  if (!validRequest(request)) throw new Error("提交配置不符合当前原生选项，请核对配置后重试。");
  const value: PolymerMdPendingSubmission = { version: 1, scope: scope(), request: JSON.parse(JSON.stringify(request)) as PolymerMdRequest, key: crypto.randomUUID(), createdAt: Date.now(), conflict: false };
  writePolymerMdPending(value);
  return value;
}
export function clearPolymerMdPending(expectedKey?: string) {
  // A stale successful response must not remove another attempt's record.
  try {
    if (expectedKey) {
      const value = readPolymerMdPending();
      if (!value || value.key !== expectedKey) return;
    } else scope();
    window.sessionStorage.removeItem(POLYMER_MD_PENDING_KEY);
    if (window.sessionStorage.getItem(POLYMER_MD_PENDING_KEY) !== null) throw new Error();
  } catch { throw new Error("无法清除待确认提交记录。请先检查我的任务；恢复存储后再提交新计算。"); }
}
