import type { PolymerMdChainLength, PolymerMdModelConfiguration, PolymerMdRequest } from "../../types/polymerMd";
import { PolymerMdSelect } from "./PolymerMdSelect";

export const MODEL_LABELS = { homopolymer: "线性均聚物", alternating: "交替共聚物", random: "随机共聚物", block: "嵌段共聚物", blend: "聚合物共混物" };
const LENGTH_LABELS = { atoms: "每链目标原子数", dp: "聚合度", molecular_weight: "目标分子量" };
const freshLength = (mode: PolymerMdChainLength["mode"]): PolymerMdChainLength => ({ mode, value: mode === "atoms" ? 1000 : mode === "dp" ? 20 : 10000 });
export function effectiveModel(request: PolymerMdRequest): PolymerMdModelConfiguration {
  return request.model_configuration ?? { version: 1, mode: "homopolymer", components: [{ id: "A", smiles: request.smiles }], chain_length: { mode: "atoms", value: request.n_atom } };
}
export function PolymerMdModelFields({ request, onChange, error }: {
  request: PolymerMdRequest; onChange: (change: Partial<PolymerMdRequest>) => void; error?: string;
}) {
  const model = effectiveModel(request);
  function commit(next: PolymerMdModelConfiguration) {
    onChange({ model_configuration: next, smiles: next.components[0].smiles,
      ...(next.mode === "blend" ? { n_chain: next.components.reduce((sum, component) => sum + (component.n_chain ?? 0), 0) } : {}),
      ...(next.chain_length?.mode === "atoms" ? { n_atom: next.chain_length.value } : {}) });
  }
  function changeMode(mode: PolymerMdModelConfiguration["mode"]) {
    const units = model.components.map(({ id, smiles }) => ({ id, smiles }));
    if (mode === "homopolymer") units.splice(1);
    else if (units.length === 1) units.push({ id: "B", smiles: "*CCO*" });
    const next: PolymerMdModelConfiguration = { version: 1, mode, components: units };
    if (mode === "block") next.blocks = units.map(component => ({ component_id: component.id, dp: 10 }));
    else if (mode === "blend") {
      const total = Math.max(request.n_chain, units.length);
      next.components = units.map((component, index) => ({ ...component, n_chain: Math.floor(total / units.length) + (index < total % units.length ? 1 : 0), chain_length: freshLength("atoms") }));
    } else {
      next.chain_length = model.chain_length ?? { mode: "atoms", value: request.n_atom };
      if (mode === "random") next.ratios = units.map(() => 1 / units.length);
    }
    commit(next);
  }
  function changeComponent(index: number, change: Partial<PolymerMdModelConfiguration["components"][number]>) {
    commit({ ...model, components: model.components.map((component, position) => position === index ? { ...component, ...change } : component) });
  }
  function changeUnits(add: boolean) {
    if (add) {
      const id = ["A", "B", "C", "D"].find(candidate => !model.components.some(component => component.id === candidate))!;
      const components = [...model.components, { id, smiles: "*CCO*", ...(model.mode === "blend" ? { n_chain: 1, chain_length: freshLength("atoms") } : {}) }];
      commit({ ...model, components, ...(model.mode === "random" ? { ratios: components.map(() => 1 / components.length) } : {}),
        ...(model.mode === "block" ? { blocks: [...model.blocks!, { component_id: id, dp: 10 }] } : {}) });
    } else {
      const components = model.components.slice(0, -1);
      commit({ ...model, components, ...(model.mode === "random" ? { ratios: components.map(() => 1 / components.length) } : {}),
        ...(model.mode === "block" ? { blocks: model.blocks!.filter(block => components.some(component => component.id === block.component_id)) } : {}) });
    }
  }
  function lengthInput(length: PolymerMdChainLength, label: string, set: (value: PolymerMdChainLength) => void, hideAtoms = false) {
    return <><PolymerMdSelect label={`${label}方式`} value={length.mode}
      options={Object.entries(LENGTH_LABELS).map(([value, text]) => ({ value: value as PolymerMdChainLength["mode"], label: value === "dp" && model.mode === "alternating" ? "交替周期数" : text }))}
      onChange={value => set(freshLength(value))} />
      {!(hideAtoms && length.mode === "atoms") ? <label className="np-polymer-md-field"><span>{length.mode === "dp" && model.mode === "alternating" ? "交替周期数" : LENGTH_LABELS[length.mode]}</span><input type="number" aria-label={`${label}${length.mode === "dp" && model.mode === "alternating" ? "交替周期数" : LENGTH_LABELS[length.mode]}`} min={length.mode === "dp" ? 1 : 100} max={length.mode === "molecular_weight" ? 1000000 : 5000} step={length.mode === "molecular_weight" ? "any" : 1} value={Number.isFinite(length.value) ? length.value : ""} onChange={event => set({ ...length, value: event.target.value === "" ? NaN : Number(event.target.value) })} /><small className="np-polymer-md-field-help">{length.mode === "molecular_weight" ? "100–1,000,000 g/mol；实际分子量随聚合度取整记录。" : length.mode === "dp" ? model.mode === "alternating" ? `1–5000 个交替周期；总重复单元数为周期数 × ${model.components.length}。` : "1–5000 个重复单元，整数；仍须满足总原子数上限。" : "100–5000 个原子/链，整数。"}</small></label> : null}</>;
  }
  return <section className="np-polymer-md-model-fields" aria-label="聚合物组成与建模方式">
    <fieldset className="np-polymer-md-model-modes"><legend>聚合物类型</legend><div>{Object.entries(MODEL_LABELS).map(([value, label]) => <label key={value} className={model.mode === value ? "is-selected" : undefined}><input type="radio" name="polymer-md-model-mode" value={value} checked={model.mode === value} onChange={() => changeMode(value as PolymerMdModelConfiguration["mode"])} /><span>{label}</span></label>)}</div></fieldset>
    {model.chain_length ? <div className="np-polymer-md-fields">{lengthInput(model.chain_length, "链长", chain_length => commit({ ...model, chain_length }), true)}</div> : null}
    <p className="np-polymer-md-muted">{model.mode === "homopolymer" ? "一种重复单元构成聚合链。" : model.mode === "blend" ? "各组分分别建链，再共同装入非晶盒；按各组分链数设置组成。" : model.mode === "alternating" ? "按单体顺序循环连接；A、B 对应 ABAB，A、B、C 对应 ABCABC。" : model.mode === "random" ? "按重复单元数比例分配各单体，再随机打乱排列；实际比例随聚合度取整记录。" : "按下方嵌段顺序连接，各段分别指定聚合度，可设置 AB、ABA 等结构。"}所有重复单元均须中性、闭壳层，并包含两个 * 连接点。</p>
    {model.mode !== "homopolymer" ? <>
      <div className="np-polymer-md-model-components">{model.components.map((component, index) => <fieldset key={component.id}><legend>{model.mode === "blend" ? "组分" : "单体"} {component.id}</legend>
        {index === 0 ? <p className="np-polymer-md-muted">使用下方结构输入中的重复单元 A：{component.smiles}</p> : <label className="np-polymer-md-field"><span>重复单元 SMILES</span><textarea aria-label={`单体 ${component.id} 重复单元 SMILES`} value={component.smiles} spellCheck={false} onChange={event => changeComponent(index, { smiles: event.target.value })} /></label>}
        {model.mode === "random" ? <label className="np-polymer-md-field"><span>重复单元数比例（%）</span><input type="number" aria-label={`单体 ${component.id} 数目比例`} min={0} max={100} step="any" value={Number.isFinite(model.ratios![index]) ? Number((model.ratios![index] * 100).toPrecision(10)) : ""} onChange={event => commit({ ...model, ratios: model.ratios!.map((ratio, position) => position === index ? Number(event.target.value) / 100 : ratio) })} /></label> : null}
        {model.mode === "blend" ? <div className="np-polymer-md-fields"><label className="np-polymer-md-field"><span>组分链数</span><input type="number" aria-label={`组分 ${component.id} 链数`} min={1} max={20} step={1} value={component.n_chain ?? ""} onChange={event => changeComponent(index, { n_chain: Number(event.target.value) })} /></label>{lengthInput(component.chain_length!, `组分 ${component.id} `, chain_length => changeComponent(index, { chain_length }))}</div> : null}
      </fieldset>)}</div>
      <div className="np-polymer-md-actions"><button type="button" disabled={model.components.length >= 4} onClick={() => changeUnits(true)}>添加{model.mode === "blend" ? "组分" : "单体"}</button><button type="button" disabled={model.components.length <= 2} onClick={() => changeUnits(false)}>移除末项</button></div>
      {model.mode === "random" ? <p className="np-polymer-md-muted">比例均须大于 0，合计 100%；当前合计 {(model.ratios!.reduce((sum, value) => sum + value, 0) * 100).toFixed(3)}%。</p> : null}
      {model.mode === "blend" ? <p className="np-polymer-md-muted">当前共 {request.n_chain} 条链；所有组分共享本任务的端基、温压及力场设置。</p> : null}
    </> : null}
    {model.mode === "block" ? <fieldset className="np-polymer-md-blocks"><legend>嵌段顺序与长度</legend>{model.blocks!.map((block, index) => <div className="np-polymer-md-fields" key={index}><PolymerMdSelect label={`第 ${index + 1} 段单体`} value={block.component_id} options={model.components.map(component => ({ value: component.id, label: component.id }))} onChange={componentId => commit({ ...model, blocks: model.blocks!.map((value, position) => position === index ? { ...value, component_id: componentId } : value) })} /><label className="np-polymer-md-field"><span>聚合度</span><input type="number" aria-label={`第 ${index + 1} 段聚合度`} min={1} max={5000} step={1} value={block.dp} onChange={event => commit({ ...model, blocks: model.blocks!.map((value, position) => position === index ? { ...value, dp: Number(event.target.value) } : value) })} /></label><button type="button" disabled={model.blocks!.length <= 2} onClick={() => commit({ ...model, blocks: model.blocks!.filter((_, position) => position !== index) })}>移除第 {index + 1} 段</button></div>)}<button type="button" disabled={model.blocks!.length >= 16} onClick={() => commit({ ...model, blocks: [...model.blocks!, { component_id: model.components[0].id, dp: 10 }] })}>添加嵌段</button><p className="np-polymer-md-muted">2–16 段，每段 1–5000 个重复单元；所有单体均须在序列中使用。</p></fieldset> : null}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
