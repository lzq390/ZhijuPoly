import catalogData from "../../../../polymer_md/capability_catalog.json";
import type { PolymerMdCapabilities, PolymerMdCapabilityCatalog, PolymerMdCatalogStatus, PolymerMdRequest } from "../../types/polymerMd";
import { polymerMdAcceptanceView } from "./PolymerMdAcceptanceEvidence";

export const polymerMdCatalog = (capabilities: PolymerMdCapabilities | null): PolymerMdCapabilityCatalog => capabilities?.capability_catalog ?? catalogData as PolymerMdCapabilityCatalog;
const STATUS: Record<PolymerMdCatalogStatus, string> = { connected: "已接入开发协议", upstream_only: "待接入", derived: "由已接入结果推导", diagnostic: "诊断指标", fixed: "固定协议 · 只读", experimental: "实验能力 · 待接入" };
const available = (status: PolymerMdCatalogStatus) => !["upstream_only", "experimental"].includes(status);
const unitText = (unit: string) => ({ "g/cm3": "g/cm³", "J/(kg K)": "J/(kg·K)", "W/(m K)": "W/(m·K)", "1/Pa": "Pa⁻¹", "1/K": "K⁻¹", "nm2": "nm²", "m2/s": "m²/s", A: "Å", "1": "无量纲" } as Record<string, string>)[unit] ?? unit;
function valueText(value: unknown): string {
  if (value == null) return "由协议确定";
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (Array.isArray(value)) return value.map(valueText).join(" / ");
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${key}: ${valueText(item)}`).join("；");
  return ({ pending_manual_scientific_review: "待人工科学验收", Muller_Plathe: "Müller–Plathe", passed: "质量通过" } as Record<string, string>)[String(value)] ?? String(value);
}

export function PolymerMdPropertyCoverage({ capabilities, calculation }: { capabilities: PolymerMdCapabilities | null; calculation: PolymerMdRequest["calculation"] }) {
  const catalog = polymerMdCatalog(capabilities);
  const groupIds = catalog.calculations.find(item => item.id === calculation)?.property_groups;
  const groups = catalog.property_groups.filter(group => available(group.status) && (groupIds ? groupIds.includes(group.id) : group.calculation === "basic" || group.calculation === calculation));
  return <section className="np-polymer-md-card np-polymer-md-coverage" aria-labelledby="polymer-md-coverage-title">
    <header><div><span className="np-polymer-md-eyebrow">本次计算</span><h2 id="polymer-md-coverage-title">覆盖的性质</h2></div><span className="np-polymer-md-tag">{calculation === "basic_tc" ? "基础物性 + 所选方向热导率" : "基础物性"}</span></header>
    <p className="np-polymer-md-muted">基础性质共享同一平衡与采样流程，无需逐项勾选。仅在相应数据、收敛和质量判据满足时给出数值；选择流程不保证所有性质可用。</p>
    <div className="np-polymer-md-property-groups">{groups.map(group => <section key={group.id}><h3>{group.label}</h3><ul>{group.properties.filter(property => available(property.status)).map(property => <li key={property.key}><div><strong>{property.label}</strong><span>{unitText(property.unit) || "无量纲"}</span></div><p>{property.description}</p>{property.dependencies?.length ? <small>条件：{property.dependencies.join("；")}</small> : null}</li>)}</ul></section>)}</div>
  </section>;
}

export function PolymerMdProtocolInfo({ capabilities }: { capabilities: PolymerMdCapabilities | null }) {
  const catalog = polymerMdCatalog(capabilities);
  const fixed = catalog.parameters.filter(parameter => parameter.group === "protocol");
  const pending = !polymerMdAcceptanceView(capabilities).accepted;
  return <>
    <details className="np-polymer-md-card np-polymer-md-disclosure" aria-label="只读科学协议">
      <summary><span><strong>只读科学协议</strong><small>QM、建模、平衡与分析的固定条件</small></span><span className="np-polymer-md-tag">{pending ? "待科学验收" : "查看验收状态"}</span></summary>
      <p>以下条件由当前开发协议固定，不能在本页编辑。{pending ? "独立科学验收登记尚未通过核验；已接入不等于科学验收通过。" : "仅所登记协议及其适用范围已通过人工科学审阅；具体性质仍以任务结果中的质量判据为准。"}</p>
      <dl className="np-polymer-md-protocol-list">{fixed.map(parameter => <div key={parameter.key}><dt>{parameter.label}</dt><dd><strong>{valueText(parameter.default)}{parameter.unit ? ` ${unitText(parameter.unit)}` : ""}</strong><p>{parameter.description}</p></dd></div>)}</dl>
      <p className="np-polymer-md-muted">目录版本 {catalog.catalog_version} · RadonPy 源码 {catalog.source_sha.slice(0, 12)}{capabilities?.capability_catalog ? " · 服务返回的协议目录" : " · 本地协议目录，实际执行条件以任务记录为准"}</p>
    </details>
    <details className="np-polymer-md-card np-polymer-md-disclosure" aria-label="RadonPy 能力目录">
      <summary><span><strong>RadonPy 能力目录</strong><small>查看当前接入范围与上游待接入能力</small></span><span className="np-polymer-md-tag">目录 · 只读</span></summary>
      <p>{catalog.description}</p>
      <p className="np-polymer-md-muted">待接入项目仅供了解上游范围，当前不能选择或提交；计算目标只使用上方已接入的两个流程。</p>
      {[{ title: "力场", entries: catalog.forcefields }, { title: "计算流程", entries: catalog.calculations }].map(section => <section className="np-polymer-md-catalog-section" key={section.title}><h3>{section.title}</h3><ul>{section.entries.map(entry => <li key={entry.id} aria-disabled={!available(entry.status)}><div><strong>{entry.label}</strong><span className="np-polymer-md-tag">{STATUS[entry.status]}</span></div><p>{entry.description}</p>{entry.requirements?.length ? <small>依赖：{entry.requirements.join("、")}</small> : null}</li>)}</ul></section>)}
      <section className="np-polymer-md-catalog-section"><h3>性质范围</h3><ul>{catalog.property_groups.map(group => <li key={group.id} aria-disabled={!available(group.status)}><div><strong>{group.label}</strong><span className="np-polymer-md-tag">{STATUS[group.status]}</span></div><p>{group.properties.map(property => `${property.label}${property.unit ? `（${unitText(property.unit)}）` : ""}`).join("、")}</p></li>)}</ul></section>
      <section className="np-polymer-md-catalog-section"><h3>可调参数与待开放参数</h3><ul>{catalog.parameters.filter(parameter => parameter.group !== "protocol").map(parameter => <li key={parameter.key} aria-disabled={parameter.group === "upstream" || !available(parameter.status)}><div><strong>{parameter.label}</strong><span className="np-polymer-md-tag">{parameter.group === "upstream" ? "待接入 · 不可编辑" : parameter.group === "basic" ? "基本配置" : "高级设置"}</span></div><p>{parameter.description}</p></li>)}</ul></section>
    </details>
  </>;
}
