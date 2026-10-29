import { FlaskConical } from "lucide-react";
import type { PolymerMdChainLength, PolymerMdRequest } from "../../types/polymerMd";
import { MODEL_LABELS } from "./PolymerMdModelFields";
import { PolymerMdNumericText } from "./PolymerMdNumericText";
import { requestedCalculationDescription } from "./presentation";

function requestedLength(value: PolymerMdChainLength | undefined, alternating = false) {
  if (!value) return "";
  const count = value.value.toLocaleString("zh-CN");
  return value.mode === "atoms" ? `每链目标 ${count} 个原子`
    : value.mode === "molecular_weight" ? `每链目标分子量 ${count} g/mol`
    : `每链目标${alternating ? "交替周期" : "聚合度 DP"} ${count}`;
}
export function PolymerMdRequestSummary({ request }: { request: PolymerMdRequest }) {
  const model = request.model_configuration;
  const components = model?.components ?? [{ id: "", smiles: request.smiles }];
  const sizing = model ? requestedLength(model.chain_length, model.mode === "alternating")
    : `每链目标 ${request.n_atom.toLocaleString("zh-CN")} 个原子`;
  return <section className="np-pmd-request-summary" aria-label="任务请求摘要">
    <header className="np-pmd-result-section-heading"><h3><FlaskConical aria-hidden="true" />提交的计算配置</h3><span>提交参数</span></header>
    <dl className="np-pmd-request-facts">
      <div><dt>模型</dt><dd>{model ? MODEL_LABELS[model.mode] : "聚合物 MD"}</dd></div>
      <div><dt>目标温压</dt><dd><PolymerMdNumericText>{`${request.temp} K · ${request.pressure} atm`}</PolymerMdNumericText></dd></div>
      <div><dt>请求链数</dt><dd><PolymerMdNumericText>{`${request.n_chain} 条链`}</PolymerMdNumericText></dd></div>
      <div><dt>力场</dt><dd><code>{request.forcefield}</code></dd></div>
    </dl>
    {sizing ? <p className="np-pmd-request-sizing"><PolymerMdNumericText>{sizing}</PolymerMdNumericText></p> : null}
    <div className="np-pmd-request-components">{components.map((component, index) => <div key={component.id || index}><span>{component.id ? `组分 ${component.id}` : "重复单元"}</span><code tabIndex={0} title={component.smiles} aria-label={`${component.id ? `组分 ${component.id}` : "重复单元"} SMILES`}>{component.smiles}</code>
      {model?.mode === "blend" ? <small><PolymerMdNumericText>{`请求 ${component.n_chain ?? ""} 条链 · ${requestedLength(component.chain_length)}`}</PolymerMdNumericText></small>
        : model?.mode === "random" && model.ratios?.[index] !== undefined ? <small><PolymerMdNumericText>{`请求重复单元数比例 ${(model.ratios[index] * 100).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`}</PolymerMdNumericText></small> : null}
    </div>)}</div>
    {model?.mode === "block" && model.blocks?.length ? <p className="np-pmd-request-sizing"><PolymerMdNumericText>{`目标嵌段：${model.blocks.map(block => `${block.component_id}×${block.dp}`).join(" → ")}`}</PolymerMdNumericText></p> : null}
    <p className="np-pmd-request-targets"><span>计算范围</span><span>{requestedCalculationDescription(request)}</span></p>
  </section>;
}
