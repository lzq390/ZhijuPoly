import { polymerMdUserConfigurationSpec, type PolymerMdUserConfiguration } from "./userConfiguration";
import { basisFieldLabel, declaredBasisOverridesText } from "./PolymerMdBasisEvidence";
import { PolymerMdSelect } from "./PolymerMdSelect";
import {
  expandPolymerMdProperties, POLYMER_MD_BASE_PROPERTIES,
  POLYMER_MD_REFRACTIVE_INDEX_PROPERTY, POLYMER_MD_THERMAL_CONDUCTIVITY_PROPERTY, POLYMER_MD_THERMAL_DIFFUSIVITY_PROPERTY,
  POLYMER_MD_TG_PROPERTY, POLYMER_MD_SOLUBILITY_PROPERTIES
} from "./propertyGroups";

const fields = polymerMdUserConfigurationSpec.fields;
const scopeLabel: Record<string, string> = { molecular: "分子性质", bulk: "聚合物体相", model: "模型", diagnostic: "运行诊断", mixed: "组合性质" };
const FIELD_BRIEFS: Record<string, string> = {
  forcefield: "用于聚合物 MD 建模",
  opt_method: "优化重复单元几何结构",
  opt_basis: "几何优化所用主基组",
  charge: "设置原子部分电荷模型",
  charge_method: "电荷计算所用量化方法",
  charge_basis: "电荷计算所用主基组",
  polar_method: "静态极化率所用量化方法",
  polar_basis: "静态极化率所用主基组"
};
const BASE_PROPERTY_BRIEFS: Record<string, string> = {
  density: "平衡态质量密度",
  Rg: "链构象的空间尺度",
  "self-diffusion": "全原子均方位移拟合",
  Cp: "恒压条件下的质量比热",
  Cv: "恒容条件下的质量比热",
  compressibility: "恒温条件下的体积响应",
  isentropic_compressibility: "等熵条件下的体积响应",
  bulk_modulus: "等温压缩率的倒数",
  isentropic_bulk_modulus: "等熵压缩率的倒数",
  volume_expansion: "平衡态体积热响应",
  linear_expansion: "按各向同性假设估计",
  r2: "链端残基间的距离平方",
  static_dielectric_const: "固定部分电荷模型估计",
  nematic_order_parameter: "残基取向的有序程度"
};
type Props = { configuration: PolymerMdUserConfiguration; onChange: (change: Partial<PolymerMdUserConfiguration>) => void };

export function PolymerMdConfigurationSelects({ section, configuration, onChange, inline = false, errors = {} }: Props & { section: "model" | "quantum"; inline?: boolean; errors?: Record<string, string> }) {
  const controls = fields.filter(field => field.section === section && field.selection === "single" && !["sp_method", "sp_basis"].includes(field.id)).map(field => {
    const selected = field.options.find(option => option.value === configuration[field.id]);
    const label = basisFieldLabel(field);
    const savedValue = typeof configuration[field.id] === "string" ? configuration[field.id] as string : "";
    const noQuantumCharge = ["gasteiger", "zero"].includes(String(configuration.charge));
    const chargeUnused = ["charge_method", "charge_basis"].includes(field.id) && noQuantumCharge;
    const applicability = chargeUnused
      ? "当前电荷模型不使用量化电荷方法与基组；此处选择保留，不会触发相应计算。"
      : null;
    const polarUnused = ["polar_method", "polar_basis"].includes(field.id) && Array.isArray(configuration.properties)
      && !configuration.properties.includes(POLYMER_MD_REFRACTIVE_INDEX_PROPERTY);
    const polarNotice = polarUnused ? "未选择静态折射率，本次不执行极化率计算；此处选择保留。" : null;
    const brief = chargeUnused ? "当前电荷模型无需此量化配置" : polarUnused ? "未选折射率，本次不计算" : FIELD_BRIEFS[field.id];
    const helpId = `polymer-md-choice-help-${field.id}`;
    const error = errors[field.id];
    const errorId = `polymer-md-choice-error-${field.id}`;
    const helpTitle = [field.description, selected?.description, declaredBasisOverridesText(field), applicability, polarNotice].filter(Boolean).join(" ");
    return <div className="np-polymer-md-configuration-field" key={field.id}>
      <PolymerMdSelect id={`polymer-md-choice-${field.id}`} label={label} value={savedValue}
        options={field.options.map(option => ({ value: option.value, label: option.label }))}
        invalid={Boolean(error)}
        describedBy={[helpId, error ? errorId : ""].filter(Boolean).join(" ")}
        onChange={value => onChange({ [field.id]: value })}>
        <small id={helpId} className="np-polymer-md-field-help" title={helpTitle}>{brief}</small>
        {error ? <em id={errorId} role="alert">{error}</em> : null}
      </PolymerMdSelect>
    </div>;
  });
  return inline ? <>{controls}</> : <div className={`np-polymer-md-fields ${section === "quantum" ? "np-pmd-quantum-fields" : "np-polymer-md-scientific-fields"}`}>{controls}</div>;
}

export function PolymerMdPropertyChoices({ configuration, onChange }: Props) {
  const field = fields.find(item => item.id === "properties");
  if (!field) return null;
  const selected = new Set(expandPolymerMdProperties(Array.isArray(configuration.properties) ? configuration.properties : []));
  const unknown = [...selected].filter(value => !field.options.some(option => option.value === value));
  const molecular = [...selected].filter(value => value.startsWith("qm_"));
  const baseOptions = field.options.filter(option => POLYMER_MD_BASE_PROPERTIES.some(key => key === option.value));
  const categories = [...new Set(baseOptions.map(option => option.category ?? option.scope))];
  const option = (key: string) => field.options.find(item => item.value === key)!;
  function toggle(values: readonly string[], checked: boolean) {
    const next = new Set(selected);
    for (const value of values) { if (checked) next.add(value); else next.delete(value); }
    if (values.includes(POLYMER_MD_THERMAL_CONDUCTIVITY_PROPERTY) && !checked) next.delete(POLYMER_MD_THERMAL_DIFFUSIVITY_PROPERTY);
    onChange({ properties: expandPolymerMdProperties([...next]) });
  }
  const checkbox = (key: string | readonly string[], description: string, label?: string) => {
    const keys = typeof key === "string" ? [key] : key;
    const title = label ?? option(keys[0]).label;
    const checked = keys.some(value => selected.has(value));
    return <li key={keys[0]} className={checked ? "is-selected" : undefined}><label><input type="checkbox" aria-label={title} checked={checked} onChange={event => toggle(keys, event.target.checked)} /><span><strong>{title}</strong><span className="np-polymer-md-flow-description">{description}</span></span></label></li>;
  };
  return <section className="np-polymer-md-card np-polymer-md-property-choices" aria-labelledby="polymer-md-properties-title">
    <header><div><span className="np-polymer-md-eyebrow">计算目标</span><h2 id="polymer-md-properties-title">计算内容</h2></div><span className="np-polymer-md-tag">基础 {POLYMER_MD_BASE_PROPERTIES.length} 项默认包含</span></header>
    <section className="np-polymer-md-automatic-properties" aria-labelledby="polymer-md-base-properties-title">
      <details className="np-polymer-md-base-disclosure">
      <summary><h3 id="polymer-md-base-properties-title">平衡后自动分析 <small>{POLYMER_MD_BASE_PROPERTIES.length} 项默认包含</small></h3></summary>
      <p className="np-polymer-md-muted">密度、热容、压缩率、体积模量、热膨胀、链结构和静态介电等共用平衡段日志与轨迹，无需逐项勾选。使用 EQ3 或追加平衡后的合格数据，结果是否可用由采样与收敛检查决定。</p>
      <div className="np-polymer-md-base-groups">{categories.map(category => {
          const options = baseOptions.filter(item => (item.category ?? item.scope) === category);
          const label = options[0]?.category_label ?? scopeLabel[category] ?? category;
          return <section key={category} aria-labelledby={`polymer-md-base-group-${category}`}><header><h4 id={`polymer-md-base-group-${category}`}>{label}</h4><span>{options.length} 项</span></header><ul>{options.map(item => <li className="np-polymer-md-automatic-property" key={item.value}><span>{item.label}</span><small>{BASE_PROPERTY_BRIEFS[item.value]}</small></li>)}</ul></section>;
        })}</div>
      </details>
    </section>
    <section className="np-polymer-md-additional-properties" aria-labelledby="polymer-md-additional-properties-title">
      <h3 id="polymer-md-additional-properties-title">追加计算</h3>
      <p className="np-polymer-md-muted">基础物性始终保留。可追加热导、玻璃化转变温度 Tg、溶解度参数 SP 等流程；每项按对应条件采样和分析。</p>
      <div className="np-polymer-md-property-selection-groups">
        <fieldset><legend>体相性质</legend><ul>
          {checkbox(POLYMER_MD_THERMAL_CONDUCTIVITY_PROPERTY, "平衡与取向检查通过后，追加 MP/RNEMD 采样，分析温度梯度与交换能量，得到总热导率。")}
          {checkbox(POLYMER_MD_REFRACTIVE_INDEX_PROPERTY, "由单体静态电子极化率、分子量与 MD 平衡密度，按 Lorentz–Lorenz 关系估计；不包含波长色散。")}
          {checkbox(POLYMER_MD_THERMAL_DIFFUSIVITY_PROPERTY, "自动包含所需的 RNEMD 热导率；仅在热导率、密度和定压热容均有效时计算热扩散率，不额外启动另一套 MD。")}
        </ul></fieldset>
        <fieldset><legend>温度扫描与内聚能</legend><ul>
          {checkbox(POLYMER_MD_TG_PROPERTY, "追加高温平衡及分段降温，以温度—密度双线拟合估计 Tg。平台温区为 800→100 K、间隔 10 K；未覆盖转变或拟合不合格时不返回数值。")}
          {checkbox(POLYMER_MD_SOLUBILITY_PROPERTIES, "在目标温压追加采样并重算分子间作用能，计算总溶解度参数及内聚能密度。至少需要两条链；用于材料内聚性质，不表示任意溶剂中的溶解度。", "溶解度参数 SP / 内聚能密度 CED")}
        </ul></fieldset>
      </div>
    </section>
    {molecular.length ? <section className="np-polymer-md-saved-properties"><h3>此前保存的单体量化目标</h3><p className="np-polymer-md-muted">这些选择已保留。当前模块聚焦聚合物 MD，请移除后提交；已提交和待确认的历史任务仍按原请求处理。</p><button type="button" onClick={() => onChange({ properties: expandPolymerMdProperties([...selected].filter(value => !value.startsWith("qm_"))) })}>移除单体量化目标</button></section> : null}
    {unknown.length ? <section className="np-polymer-md-saved-properties" aria-labelledby="polymer-md-saved-properties-title"><h3 id="polymer-md-saved-properties-title">此前保存的其他性质</h3><p className="np-polymer-md-muted">原选择已保留，当前执行接口无法识别这些项目；移除后可继续使用已接入的流程。</p><ul>{unknown.map(value => <li key={value}><span>{value}</span><button type="button" aria-label={`移除保存的性质 ${value}`} onClick={() => toggle([value], false)}>移除</button></li>)}</ul></section> : null}
  </section>;
}
