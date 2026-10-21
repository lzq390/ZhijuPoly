import { randomId } from "./randomId";
import { requestFingerprint } from "./usageRequestIdentity";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";
const STORAGE = "nexpoly.usage.pending.v1";
type Pending = { fingerprint: string; key: string; operationId?: string; nativeKey?: boolean };
export type UsageTicket = Pending & { owner: string };
let owner = "";
let enabled: boolean | undefined;
let featureRequest: Promise<boolean> | undefined;
let entries = new Map<string, Pending>();
const responseTickets = new WeakMap<Response, UsageTicket>();
const KEY = /^[A-Za-z0-9._:-]{8,128}$/;
const nativeKey = (key: string) => key.length <= 128 && key.trim().length > 0 && !/[\r\n]/.test(key);
const nativePaths = new Set(["/monomer-dft/jobs", "/monomer-polymerization/batch/jobs"]);
const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const paths = new Set([
  "/predict", "/structure/3d", "/structure/recognize-image",
  "/monomer-retrosynthesis", "/monomer-polymerization", "/monomer-polymerization/batch/jobs",
  "/conditional-generation/tg/jobs", "/conditional-generation/polytao/jobs",
  "/online-knowledge/search", "/online-knowledge/jobs", "/monomer-md/jobs", "/monomer-dft/jobs",
  "/assistant/chat/stream", "/assistant/chat/image-stream", "/assistant/tg/chat/stream", "/assistant/tg/chat/image-stream"
]);
export function apiPath(input: string) {
  const origin = typeof location === "undefined" ? "http://localhost" : location.href;
  const url = new URL(input, origin), base = new URL(BASE, origin);
  return url.origin === base.origin && url.pathname.startsWith(base.pathname + "/") ? url.pathname.slice(base.pathname.length) : null;
}
export function isUsageSubmission(input: string, method: string, body?: BodyInit | null) {
  const path = apiPath(input);
  if (method !== "POST" || path === null) return false;
  if (path === "/query/smiles") {
    // Match the server's property/similarity selector; structure/default and
    // malformed bodies retain their existing free-query/validation path.
    if (typeof body !== "string") return false;
    try {
      const value = JSON.parse(body);
      return value !== null && typeof value === "object" && !Array.isArray(value)
        && (value.match_mode === "property" || value.match_mode === "similarity");
    } catch { return false; }
  }
  return paths.has(path) || /^\/knowledge\/recordings\/[^/]+\/summary$/.test(path);
}
export function installUsageIdentity(next: string) {
  if (owner === next) return;
  owner = next; enabled = undefined; featureRequest = undefined; entries = new Map();
  if (!owner) return;
  try {
    const value = JSON.parse(sessionStorage.getItem(STORAGE) ?? "null");
    if (value?.owner === owner && Array.isArray(value.entries) && value.entries.length <= 128) {
      for (const item of value.entries) if (typeof item?.fingerprint === "string" && /^[0-9a-f]{64}$/.test(item.fingerprint) && typeof item.key === "string" && (item.nativeKey === true ? nativeKey(item.key) : KEY.test(item.key)) && (!item.operationId || ID.test(item.operationId))) entries.set(item.fingerprint, item);
    }
  } catch { /* Writes must succeed before a counted request can be dispatched. */ }
}
export function observeUsageEnabled(value: unknown) {
  if (value && typeof value === "object" && "enabled" in value && typeof value.enabled === "boolean") enabled = value.enabled;
}
function persist() {
  try { sessionStorage.setItem(STORAGE, JSON.stringify({ owner, entries: [...entries.values()] })); }
  catch { throw new Error("无法保存安全重试标识，未发送新的计算请求。请允许本标签页存储后重试。"); }
}
async function feature(check: () => void, fetcher: (url: string) => Promise<Response>) {
  if (enabled !== undefined) return enabled;
  if (!featureRequest) {
    const startedOwner = owner;
    const request = (async () => {
      const response = await fetcher(`${BASE}/usage/entitlements`);
      check();
      const value = await response.json().catch(() => null); check();
      if (response.status === 404) { enabled = false; return false; } // An older server has no entitlement feature.
      if (!response.ok) throw new Error("无法确认次数策略，未发送计算请求。请稍后重试。");
      if (typeof value?.enabled !== "boolean") throw new Error("次数策略响应无效，未发送计算请求。");
      if (owner !== startedOwner) throw new DOMException("登录状态已改变。", "AbortError");
      enabled = value.enabled; return value.enabled as boolean;
    })();
    featureRequest = request;
    void request.finally(() => { if (featureRequest === request) featureRequest = undefined; }).catch(() => {});
  }
  const value = await featureRequest; check(); return value;
}
export async function prepareUsageSubmission(options: {
  url: string; method: string; body?: BodyInit | null; intent?: string; headers: Headers;
  check: () => void; fetcher: (url: string) => Promise<Response>;
}): Promise<UsageTicket | undefined> {
  if (!isUsageSubmission(options.url, options.method, options.body) || !await feature(options.check, options.fetcher)) return;
  options.check();
  const fingerprint = await requestFingerprint(options.url, options.body, options.intent); options.check();
  const explicit = options.headers.get("Idempotency-Key");
  const native = nativePaths.has(apiPath(options.url) ?? "");
  if (explicit && !(native ? nativeKey(explicit) : KEY.test(explicit))) throw new Error("提交标识无效，未发送计算请求。");
  let item = entries.get(fingerprint);
  // Existing DFT/batch callers own their keys; never silently substitute another.
  if (explicit && item && item.key !== explicit) {
    throw new Error("此请求仍有未确认的提交。请先在我的用量中读取原操作回执，再开始新的计算。");
  }
  if (!item) {
    if (entries.size >= 128) throw new Error("未确认操作较多，请先在我的用量中读取已有结果。");
    item = { fingerprint, key: explicit || `usage-${randomId()}`, ...(native && explicit ? { nativeKey: true } : {}) }; entries.set(fingerprint, item);
  }
  persist(); options.headers.set("Idempotency-Key", item.key);
  return { ...item, owner };
}
export function attachUsageResponse(response: Response, ticket?: UsageTicket) {
  if (!ticket || ticket.owner !== owner) return;
  const operationId = response.headers.get("X-Usage-Operation-ID");
  if (operationId && ID.test(operationId)) {
    ticket.operationId = operationId;
    const current = entries.get(ticket.fingerprint);
    if (current?.key === ticket.key && current.operationId !== operationId) { current.operationId = operationId; try { persist(); } catch { /* The original key remains durable; receipt list can recover the ID. */ } }
  }
  responseTickets.set(response, ticket);
}
export function confirmUsageResponse(response: Response) {
  if (response.headers.get("X-Usage-State") === "unknown") return;
  const ticket = responseTickets.get(response);
  if (!ticket || ticket.owner !== owner) return;
  const current = entries.get(ticket.fingerprint);
  if (current?.key === ticket.key) { entries.delete(ticket.fingerprint); try { persist(); } catch { /* A retained key can only replay this confirmed operation. */ } }
}
export function confirmUsageOperation(operationId: string) {
  for (const [fingerprint, item] of entries) if (item.operationId === operationId) entries.delete(fingerprint);
  if (owner) { try { persist(); } catch { /* No submission is performed by receipt reads. */ } }
}
export class UsageOperationError extends Error {
  readonly retryable = false;
  constructor(readonly status: number, readonly code: string, readonly operationId?: string) {
    super(code === "usage_operation_pending" ? "操作仍在处理中。请到“我的用量 → 操作记录”查看进度或读取已有结果；不会自动重算。"
      : code === "usage_outcome_unknown" ? "操作结果待确认，预留次数仍保留。请到“我的用量 → 操作记录”读取回执；不要重复计算。"
      : code === "membership_required" ? "当前为免费账号，此服务需要会员权限。本地画板和公开示例仍可使用。"
      : "本次重试的参数与原操作不同。请先读取原操作回执，确认后再发起新操作。");
    this.name = "UsageOperationError";
  }
}
export function usageFailure(response: Response, payload: unknown, ticket?: UsageTicket) {
  const value = payload as { detail?: { code?: string; operation_id?: string }; code?: string; operation_id?: string } | null;
  const code = value?.detail?.code ?? value?.code;
  const operationId = value?.detail?.operation_id ?? value?.operation_id ?? response.headers.get("X-Usage-Operation-ID") ?? undefined;
  if (ticket && ticket.owner === owner && operationId && ID.test(operationId)) {
    const current = entries.get(ticket.fingerprint);
    if (current?.key === ticket.key) { current.operationId = operationId; persist(); }
  }
  if (code && ["usage_operation_pending", "usage_outcome_unknown", "idempotency_conflict", "membership_required"].includes(code)) return new UsageOperationError(response.status, code, operationId);
}
