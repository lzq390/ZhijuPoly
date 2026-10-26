// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useState } from "react";
import type { PolymerMdRequest } from "../../types/polymerMd";
import { MODEL_LABELS, PolymerMdModelFields } from "./PolymerMdModelFields";
import { POLYMER_MD_DEFAULTS } from "./config";
import { defaultPolymerMdUserConfiguration, mapPolymerMdUserConfiguration } from "./userConfiguration";

afterEach(cleanup);

function Form() {
  const [request, setRequest] = useState<PolymerMdRequest>({ ...POLYMER_MD_DEFAULTS });
  const compiled = mapPolymerMdUserConfiguration(defaultPolymerMdUserConfiguration(), request);
  return <><PolymerMdModelFields request={request} onChange={change => setRequest(current => ({ ...current, ...change }))} />
    <output aria-label="模型提交请求">{JSON.stringify(compiled.request)}</output></>;
}
const payload = () => JSON.parse(screen.getByLabelText("模型提交请求").textContent!) as PolymerMdRequest;
const model = () => payload().model_configuration!;
const change = (name: string, value: string) => fireEvent.change(screen.getByLabelText(name), { target: { value } });
function choose(name: string, label: string) {
  fireEvent.click(screen.getByRole("combobox", { name }));
  const menu = within(screen.getByRole("listbox", { name }));
  fireEvent.click(menu.getByText(label, { selector: "strong", exact: true }).closest('[role="option"]')!);
}

const mode = (value: string) => fireEvent.click(screen.getByRole("radio", { name: MODEL_LABELS[value as keyof typeof MODEL_LABELS] }));

describe("native polymer model controls", () => {
  it("shows all five model choices without opening a menu", () => {
    render(<Form />);
    expect(screen.getByRole("group", { name: "聚合物类型" })).toBeTruthy();
    expect(screen.getAllByRole("radio").map(item => item.getAttribute("value"))).toEqual(Object.keys(MODEL_LABELS));
    expect((screen.getByRole("radio", { name: "线性均聚物" }) as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByRole("combobox", { name: "聚合物类型" })).toBeNull();
  });
  it("compiles alternating units and uses complete alternating cycles for the editable DP", () => {
    render(<Form />);
    mode("alternating");
    expect(model()).toEqual({ version: 1, mode: "alternating", components: [
      { id: "A", smiles: "*CC*" }, { id: "B", smiles: "*CCO*" }
    ], chain_length: { mode: "atoms", value: 1000 } });
    change("单体 B 重复单元 SMILES", "*CO*");
    choose("链长方式", "交替周期数");
    change("链长交替周期数", "7");
    expect(model().chain_length).toEqual({ mode: "dp", value: 7 });
    expect(model().components[1]).toEqual({ id: "B", smiles: "*CO*" });
    expect(screen.getByText(/总重复单元数为周期数 × 2/)).toBeTruthy();
    expect(payload()).toMatchObject({ smiles: "*CC*", n_chain: 10, calculation: "basic" });
    expect(model()).not.toHaveProperty("ratios");
    expect(model()).not.toHaveProperty("blocks");
  });

  it("submits random mole fractions in component order and repairs dimensions when units change", () => {
    render(<Form />);
    mode("random");
    change("单体 A 数目比例", "25");
    change("单体 B 数目比例", "75");
    expect(model().ratios).toEqual([.25, .75]);
    choose("链长方式", "聚合度");
    change("链长聚合度", "40");
    fireEvent.click(screen.getByRole("button", { name: "添加单体" }));
    change("单体 A 数目比例", "20");
    change("单体 B 数目比例", "30");
    change("单体 C 数目比例", "50");
    change("单体 C 重复单元 SMILES", "*CCN*");
    expect(model()).toMatchObject({ mode: "random", chain_length: { mode: "dp", value: 40 }, ratios: [.2, .3, .5] });
    expect(model().components).toEqual([{ id: "A", smiles: "*CC*" }, { id: "B", smiles: "*CCO*" }, { id: "C", smiles: "*CCN*" }]);
    fireEvent.click(screen.getByRole("button", { name: "移除末项" }));
    expect(model().components.map(component => component.id)).toEqual(["A", "B"]);
    expect(model().ratios).toEqual([.5, .5]);
    expect(model().chain_length).toEqual({ mode: "dp", value: 40 });
    expect(model()).not.toHaveProperty("blocks");
  });

  it("preserves an explicit ABA block order with a separate DP for each occurrence", () => {
    render(<Form />);
    mode("block");
    change("第 1 段聚合度", "12");
    change("第 2 段聚合度", "7");
    fireEvent.click(screen.getByRole("button", { name: "添加嵌段" }));
    choose("第 3 段单体", "B");
    choose("第 3 段单体", "A");
    change("第 3 段聚合度", "4");
    expect(model()).toEqual({ version: 1, mode: "block", components: [
      { id: "A", smiles: "*CC*" }, { id: "B", smiles: "*CCO*" }
    ], blocks: [{ component_id: "A", dp: 12 }, { component_id: "B", dp: 7 }, { component_id: "A", dp: 4 }] });
    expect(model()).not.toHaveProperty("chain_length");
    expect(model()).not.toHaveProperty("ratios");
  });

  it("builds blend counts and independent DP or molecular-weight targets in the actual request", () => {
    render(<Form />);
    mode("blend");
    change("组分 A 链数", "2");
    change("组分 B 链数", "3");
    choose("组分 A 方式", "聚合度");
    change("组分 A 聚合度", "30");
    choose("组分 B 方式", "目标分子量");
    change("组分 B 目标分子量", "15000.5");
    expect(payload().n_chain).toBe(5);
    expect(model()).toEqual({ version: 1, mode: "blend", components: [
      { id: "A", smiles: "*CC*", n_chain: 2, chain_length: { mode: "dp", value: 30 } },
      { id: "B", smiles: "*CCO*", n_chain: 3, chain_length: { mode: "molecular_weight", value: 15000.5 } }
    ] });
    fireEvent.click(screen.getByRole("button", { name: "添加组分" }));
    expect(payload().n_chain).toBe(6);
    expect(model().components[2]).toEqual({ id: "C", smiles: "*CCO*", n_chain: 1, chain_length: { mode: "atoms", value: 1000 } });
    fireEvent.click(screen.getByRole("button", { name: "移除末项" }));
    expect(payload().n_chain).toBe(5);
    expect(model().components).toHaveLength(2);
  });

  it("replaces mode-specific fields without carrying ratios, blocks or blend counts into another protocol", () => {
    render(<Form />);
    mode("random");
    change("单体 A 数目比例", "20");
    change("单体 B 数目比例", "80");
    mode("block");
    expect(model().blocks).toEqual([{ component_id: "A", dp: 10 }, { component_id: "B", dp: 10 }]);
    expect(model()).not.toHaveProperty("ratios");
    expect(model()).not.toHaveProperty("chain_length");
    mode("blend");
    expect(model()).not.toHaveProperty("blocks");
    expect(model().components.every(component => component.n_chain! > 0 && component.chain_length?.mode === "atoms")).toBe(true);
    mode("homopolymer");
    expect(model()).toEqual({ version: 1, mode: "homopolymer", components: [{ id: "A", smiles: "*CC*" }],
      chain_length: { mode: "atoms", value: 1000 } });
    choose("链长方式", "目标分子量");
    change("链长目标分子量", "25000.25");
    expect(model().chain_length).toEqual({ mode: "molecular_weight", value: 25000.25 });
    expect(model().components[0]).not.toHaveProperty("n_chain");
  });

  it("bounds editable composition to four components and removes obsolete component blocks", () => {
    render(<Form />);
    mode("block");
    fireEvent.click(screen.getByRole("button", { name: "添加单体" }));
    fireEvent.click(screen.getByRole("button", { name: "添加单体" }));
    expect(model().components.map(component => component.id)).toEqual(["A", "B", "C", "D"]);
    expect((screen.getByRole("button", { name: "添加单体" }) as HTMLButtonElement).disabled).toBe(true);
    expect(model().blocks!.map(block => block.component_id)).toEqual(["A", "B", "C", "D"]);
    fireEvent.click(screen.getByRole("button", { name: "移除末项" }));
    expect(model().components.map(component => component.id)).toEqual(["A", "B", "C"]);
    expect(model().blocks!.map(block => block.component_id)).toEqual(["A", "B", "C"]);
    fireEvent.click(screen.getByRole("button", { name: "移除末项" }));
    expect((screen.getByRole("button", { name: "移除末项" }) as HTMLButtonElement).disabled).toBe(true);
    expect(model().blocks!.map(block => block.component_id)).toEqual(["A", "B"]);
  });
});
