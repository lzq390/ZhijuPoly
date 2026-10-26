import type { PolymerMdAcceptedScope, PolymerMdCapabilities } from "../../types/polymerMd";
import { MODEL_LABELS } from "./PolymerMdModelFields";
import { basisFieldLabel } from "./PolymerMdBasisEvidence";
import { polymerMdUserConfigurationSpec } from "./userConfiguration";

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const hash = (value: unknown, length = 64): value is string => typeof value === "string" && new RegExp(`^[a-f0-9]{${length}}$`).test(value);
const id = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const publicText = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0 && value.length <= 200
  && !/[\u0000-\u001f\u007f]|(?:file|unix):\/\/|(?:^|\s)\/(?:home|data|private|tmp|run)\/|(?:token|password|secret|cookie|authorization|bearer)\s*[=:]/i.test(value);
const list = (value: unknown, maximum = 128): value is unknown[] => Array.isArray(value) && value.length <= maximum;
const methods = polymerMdUserConfigurationSpec.fields.filter(field => field.section === "quantum" && field.selection === "single");
const properties = polymerMdUserConfigurationSpec.fields.find(field => field.id === "properties")!.options;
const forcefields = polymerMdUserConfigurationSpec.fields.find(field => field.id === "forcefield")!.options;
const conditionFields = [
  ["temperature_k", "目标温度", "K"], ["pressure_atm", "目标压力", "atm"], ["eq_ns", "EQ3 时长", "ns"],
  ["additional_ns", "每段追加平衡时长", "ns"], ["max_additional", "最多追加次数", ""],
  ["sample_interval_ps", "采样间隔", "ps"], ["tc_ns", "RNEMD 时长", "ns"], ["tc_rep", "RNEMD 复制倍数", ""],
  ["seed", "登记请求的随机种子", ""]
] as const;

function validScope(value: unknown): value is PolymerMdAcceptedScope {
  if (!record(value) || !id(value.receipt_id) || !id(value.protocol_id) || !hash(value.receipt_sha256)
      || !hash(value.request_sha256) || !hash(value.reviewer_ref) || !hash(value.decision_sha256)
      || !publicText(value.material_label) || !Object.hasOwn(MODEL_LABELS, String(value.model_mode))
      || !forcefields.some(field => field.value === value.forcefield) || value.scope_policy !== "exact_scientific_request_only"
      || typeof value.reviewed_at !== "string" || !/^\d{4}-\d{2}-\d{2}T/.test(value.reviewed_at) || !Number.isFinite(Date.parse(value.reviewed_at))) return false;
  const qm = value.qm, conditions = value.conditions;
  if (!record(qm) || !methods.every(field => field.options.some(option => option.value === qm[field.id])) || !record(conditions)
      || !conditionFields.every(([key]) => typeof conditions[key] === "number" && Number.isFinite(conditions[key]) && Number(conditions[key]) >= 0)
      || !["x", "y", "z"].includes(String(conditions.tc_axis))) return false;
  if (!list(value.properties) || new Set(value.properties).size !== value.properties.length
      || !value.properties.every(key => properties.some(field => field.value === key)) || !list(value.evidence, 256)
      || value.evidence.length === 0 || !value.evidence.every(item => record(item) && publicText(item.label)
        && hash(item.sha256) && Number.isSafeInteger(item.size_bytes) && Number(item.size_bytes) >= 0)) return false;
  const evidence = value.evidence;
  return ["raw_benchmark", "independent_recompute", "review_report"].every(label => evidence.some(item => record(item) && item.label === label));
}

export function polymerMdAcceptanceView(capabilities: PolymerMdCapabilities | null) {
  const summary = capabilities?.acceptance_summary;
  const pending = { accepted: false as const, scopes: [] as PolymerMdAcceptedScope[], count: 0, binding: null,
    status: summary?.registry_status ?? "unconfigured" };
  if (!record(summary) || summary.source !== "external_registry" || summary.registry_status !== "verified"
      || summary.scientific_acceptance !== "scoped_external_review") return pending;
  const binding = summary.binding, runtime = capabilities?.runtime;
  if (!record(binding) || !runtime || !hash(binding.runtime_fingerprint) || !hash(binding.source_sha, 40)
      || !id(binding.patch_version) || !hash(binding.scientific_protocol_sha256)
      || binding.runtime_fingerprint !== runtime.runtime_fingerprint || binding.source_sha !== runtime.source_sha
      || binding.patch_version !== runtime.patch_version) return { ...pending, status: "mismatch" };
  const scopes = summary.accepted_scopes, protocols = summary.accepted_protocols;
  if (!list(scopes, 8192) || scopes.length === 0 || !scopes.every(validScope) || !list(protocols, 256)
      || protocols.length === 0 || !protocols.every(id) || new Set(protocols).size !== protocols.length
      || summary.accepted_protocol_count !== protocols.length || scopes.some(scope => !protocols.includes(scope.protocol_id))
      || protocols.some(protocol => !scopes.some(scope => scope.protocol_id === protocol))) return { ...pending, status: "invalid" };
  return { accepted: true as const, scopes, count: protocols.length, binding, status: "verified" };
}

const PENDING: Record<string, string> = {
  unconfigured: "尚未配置独立科学验收登记。", missing: "尚无可核验的独立科学验收记录。",
  invalid: "独立科学验收记录未通过完整性检查。", mismatch: "验收记录与当前运行时或协议绑定不匹配。",
  revoked: "独立科学验收记录已撤销。", verified: "尚无已通过审阅的精确请求范围。"
};
function EvidenceRows({ rows }: { rows: Array<[string, string]> }) {
  return <dl className="np-polymer-md-protocol-list">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd className={value.length > 0 && !/[\u3400-\u9fff]/.test(value) ? "np-pmd-code" : undefined}>{value}</dd></div>)}</dl>;
}

export function PolymerMdAcceptanceEvidence({ capabilities }: { capabilities: PolymerMdCapabilities | null }) {
  const view = polymerMdAcceptanceView(capabilities);
  const runtime = capabilities?.runtime;
  return <section aria-label="独立科学验收登记">
    <p>{view.accepted ? "独立人工科学审阅已登记，仅适用于下列精确请求范围。" : "当前独立科学验收尚未核验通过；流程可运行与材料性质验收分别记录。"}已验收协议：{view.count} 项。</p>
    {!view.accepted ? <p>{Object.hasOwn(PENDING, view.status) ? PENDING[view.status] : PENDING.invalid}</p> : <>
      <p>适用范围按完整科学请求 SHA256 精确绑定；相同模型或力场名称不表示其他材料、组分、长度、温压或采样条件已经验收。</p>
      <details><summary>查看验收绑定、适用范围与审阅证据</summary>
        <EvidenceRows rows={[["绑定运行时 SHA256", String(view.binding.runtime_fingerprint)], ["RadonPy 源码 SHA", String(view.binding.source_sha)],
          ["适配器版本", String(view.binding.patch_version)], ["科学协议 SHA256", String(view.binding.scientific_protocol_sha256)]]} />
        {view.scopes.map((scope, index) => <section key={`${scope.receipt_id}:${scope.request_sha256}:${index}`} aria-label={`验收范围 ${index + 1}`}>
          <h4>{scope.material_label} · {scope.protocol_id}</h4>
          <EvidenceRows rows={[["适用规则", "仅该完整科学请求"], ["模型", MODEL_LABELS[scope.model_mode]], ["力场", scope.forcefield],
            ["科学请求 SHA256", scope.request_sha256], ["登记凭据", scope.receipt_id], ["凭据 SHA256", scope.receipt_sha256],
            ["人工审阅时间", scope.reviewed_at], ["审阅者匿名引用 SHA256", scope.reviewer_ref], ["审阅决定 SHA256", scope.decision_sha256]]} />
          <details><summary>量化方法与温压采样摘要</summary><p>以下为被审阅请求的设置；电荷、极化率与 RNEMD 等角色是否执行，以对应任务的适用性及阶段证据为准。</p><EvidenceRows rows={[
            ...methods.map(field => [basisFieldLabel(field), field.options.find(option => option.value === scope.qm[field.id as keyof typeof scope.qm])!.label] as [string, string]),
            ...conditionFields.map(([key, label, unit]) => [label, `${scope.conditions[key]}${unit ? ` ${unit}` : ""}`] as [string, string]),
            ["RNEMD 方向", scope.conditions.tc_axis], ["登记性质", scope.properties.length ? scope.properties.map(key => properties.find(option => option.value === key)!.label).join("、") : "历史请求未保存逐项性质；不据当前目录推定单项性质已验收"]
          ]} /><p>基组为主基组；Br/I 逐元素分配仍以对应协议和该任务 Psi4 日志为准。摘要不包含完整私有请求。</p></details>
          <details><summary>验收证据 SHA256（{scope.evidence.length} 项）</summary>{scope.evidence.map((item, evidenceIndex) => <EvidenceRows key={evidenceIndex} rows={[["证据", item.label], ["SHA256", item.sha256], ["大小", `${item.size_bytes} bytes`]]} />)}</details>
        </section>)}
      </details>
    </>}
    {runtime ? <details><summary>运行时封存时记录（历史元数据）</summary><p>以下记录保留封存时状态；当前正式验收以独立登记为准。</p><EvidenceRows rows={[
      ["运行时 SHA256", hash(runtime.runtime_fingerprint) ? runtime.runtime_fingerprint : "未提供有效指纹"],
      ["封存时材料科学状态", runtime.scientific_acceptance === "passed" ? "封存记录为 passed" : "封存时未登记通过"],
      ["封存时适配器验证", runtime.adapter_scientific_validation === "passed" ? "封存记录为 passed" : "封存时未登记通过"]
    ]} /></details> : null}
  </section>;
}
