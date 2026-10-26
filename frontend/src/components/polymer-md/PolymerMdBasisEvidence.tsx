import { polymerMdUserConfigurationSpec, type PolymerMdUserField } from "./userConfiguration";

const isRecord = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const basisNames = new Set(["lanl2dz", ...polymerMdUserConfigurationSpec.fields.filter(field => field.id.endsWith("_basis")).flatMap(field => field.options.map(option => option.value))]);
const elements = ["Br", "I"] as const;

export function basisFieldLabel(field: PolymerMdUserField): string {
  return field.element_basis_overrides ? field.label.replace(/基组$/, "主基组") : field.label;
}

function basisEntries(value: unknown): Array<[string, string]> {
  if (!isRecord(value)) return [];
  return elements.flatMap(element => Object.hasOwn(value, element) && typeof value[element] === "string" && basisNames.has(value[element])
    ? [[element, value[element]] as [string, string]] : []);
}

export function declaredBasisOverridesText(field: PolymerMdUserField): string {
  const entries = basisEntries(field.element_basis_overrides);
  return entries.length ? `锁定逐元素覆盖：${entries.map(([element, basis]) => `${element} → ${basis}`).join("；")}。仅在分子含对应元素时生效；其他元素使用所选主基组。I 使用 LANL2DZ 基组/ECP。` : "";
}

export function DeclaredBasisOverrides({ field }: { field: PolymerMdUserField }) {
  const text = declaredBasisOverridesText(field);
  return text ? <section aria-label={`${basisFieldLabel(field)}的锁定元素覆盖`}>
    <p>{text}</p>
  </section> : null;
}

export function SavedBasisOverrides({ value }: { value: unknown }) {
  if (value === undefined) return null;
  const entries = basisEntries(value);
  return <section aria-label="保存的逐元素基组证据"><h5>保存的逐元素基组</h5>
    {entries.length ? <dl className="np-polymer-md-evidence-list">{entries.map(([element, basis]) => <div key={element}><dt>{element}</dt><dd>{basis}</dd></div>)}</dl> : <p className="np-polymer-md-muted">未提供可识别的 Br/I 逐元素基组记录。</p>}
    <p className="np-polymer-md-muted">以上来自本条性质保存的 basis_gen；实际元素分配以该任务的 Psi4 日志为准。</p>
  </section>;
}

export function LockedBasisRules({ configuration, applicability, stage, status }: {
  configuration: Record<string, unknown>; applicability: unknown; stage?: string; status?: string;
}) {
  if (stage !== "terminal_qm" && stage !== "monomer_qm") return null;
  const saved = isRecord(applicability) ? applicability : {};
  const targets = Array.isArray(configuration.properties) ? configuration.properties : [];
  const fieldIds = ["opt_basis", "charge_basis", ...(stage === "monomer_qm" ? ["polar_basis",
    ...(targets.some(key => ["qm_total_energy", "qm_homo", "qm_lumo", "qm_dipole"].includes(String(key))) ? ["sp_basis"] : [])] : [])];
  const fields = polymerMdUserConfigurationSpec.fields.filter(field => fieldIds.includes(field.id));
  function applicabilityText(field: PolymerMdUserField) {
    const key = stage === "terminal_qm" && field.id === "opt_basis" ? "terminal_optimization" : field.id;
    const evidence = saved[key];
    if (!isRecord(evidence) || typeof evidence.applicable !== "boolean") return "未保存角色适用性；不推定执行";
    if (!evidence.applicable) return "保存证据：本角色不适用";
    return status === "completed" ? "角色适用；阶段已完成，元素分配见日志" : "角色适用；阶段未完成，不能确认本角色已完成";
  }
  return <section aria-label="锁定协议的 Br/I 基组覆盖规则"><h5>当前锁定协议：Br/I 覆盖规则（v9）</h5>
    <p className="np-polymer-md-muted">此表是 v9 协议参考，不代表该任务采用 v9 或已经完成相应计算，也不是实测元素分配。仅含对应元素时生效；其他元素使用所选主基组。实际元素分配以该任务的 Psi4 日志为准，I 使用 LANL2DZ 基组/ECP。</p>
    <div className="np-polymer-md-table-wrap"><table><thead><tr><th>计算角色</th><th>Br</th><th>I</th><th>保存的适用性与阶段状态</th></tr></thead><tbody>{fields.map(field => {
      const entries = new Map(basisEntries(field.element_basis_overrides));
      return <tr key={field.id}><th scope="row">{basisFieldLabel(field)}</th><td>{entries.get("Br") ?? "未声明"}</td><td>{entries.get("I") ?? "未声明"}</td><td>{applicabilityText(field)}</td></tr>;
    })}</tbody></table></div>
  </section>;
}
