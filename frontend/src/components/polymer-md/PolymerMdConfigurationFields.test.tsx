// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import type { PolymerMdCapabilities, PolymerMdRequest } from "../../types/polymerMd";
import { PolymerMdConfigurationSelects, PolymerMdPropertyChoices } from "./PolymerMdConfigurationFields";
import { POLYMER_MD_DEFAULTS, validatePolymerMdRequest } from "./config";
import { defaultPolymerMdUserConfiguration, mapPolymerMdUserConfiguration,
  polymerMdUserConfigurationSpec, type PolymerMdUserConfiguration } from "./userConfiguration";
import { POLYMER_MD_BASE_PROPERTIES } from "./propertyGroups";

afterEach(cleanup);

function Form({ initial = [], changed = vi.fn(), methods = false, capabilities }: {
  initial?: string[]; changed?: (change: Partial<PolymerMdUserConfiguration>) => void; methods?: boolean; capabilities?: PolymerMdCapabilities;
}) {
  const [configuration, setConfiguration] = useState<PolymerMdUserConfiguration>(() => ({
    ...defaultPolymerMdUserConfiguration(), properties: initial
  }));
  const compiled = mapPolymerMdUserConfiguration(configuration, POLYMER_MD_DEFAULTS);
  const errors = compiled.request ? validatePolymerMdRequest(compiled.request, capabilities) : {};
  const update = (change: Partial<PolymerMdUserConfiguration>) => {
    changed(change);
    setConfiguration(current => ({ ...current, ...change } as PolymerMdUserConfiguration));
  };
  return <>{methods ? <><PolymerMdConfigurationSelects section="model" configuration={configuration} onChange={update} errors={errors} />
    <PolymerMdConfigurationSelects section="quantum" configuration={configuration} onChange={update} /></> : null}
    <PolymerMdPropertyChoices configuration={configuration} onChange={update} />
    <output aria-label="编译后的提交请求">{JSON.stringify(compiled.request)}</output>
    <output aria-label="保留的科研配置">{JSON.stringify(configuration)}</output>
  </>;
}
const payload = () => JSON.parse(screen.getByLabelText("编译后的提交请求").textContent!) as PolymerMdRequest | null;
const selected = () => payload()!.native_configuration!.properties;
const checkbox = (label: string) => screen.getByRole("checkbox", { name: label }) as HTMLInputElement;
function openChoices(name: string) {
  const trigger = screen.getByRole("combobox", { name });
  fireEvent.click(trigger);
  return { trigger, menu: within(screen.getByRole("listbox", { name })) };
}
function choose(name: string, label: string) {
  const { menu } = openChoices(name);
  fireEvent.click(menu.getByText(label, { selector: "strong", exact: true }).closest('[role="option"]')!);
}


describe("polymer scientific configuration controls", () => {
  it("explains a server-restricted saved forcefield without changing it and clears the error after an allowed choice", () => {
    const capabilities = { supported_forcefields: ["Dreiding"], limits: { max_total_atoms: 20000, max_rnemd_atoms: 60000 } } as PolymerMdCapabilities;
    render(<Form methods capabilities={capabilities} />);
    const trigger = screen.getByRole("combobox", { name: "聚合物力场" });
    const quantumChoice = screen.getByRole("combobox", { name: "几何优化方法" }).textContent;
    const error = screen.getByRole("alert");
    expect(error.textContent).toBe("当前协议不支持此力场。");
    expect(trigger.getAttribute("aria-invalid")).toBe("true");
    expect(trigger.getAttribute("aria-describedby")?.split(" ")).toEqual(["polymer-md-choice-help-forcefield", error.id]);
    expect(payload()?.forcefield).toBe("GAFF2_mod");

    choose("聚合物力场", "Dreiding");
    expect(payload()?.forcefield).toBe("Dreiding");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(trigger.getAttribute("aria-invalid")).toBe("false");
    expect(trigger.getAttribute("aria-describedby")).toBe("polymer-md-choice-help-forcefield");
    expect(screen.getByRole("combobox", { name: "几何优化方法" }).textContent).toBe(quantumChoice);
  });

  it("binds the eight MD method controls to the native request without exposing single-point tools", () => {
    render(<Form methods />);
    const selects = screen.getAllByRole("combobox") as HTMLButtonElement[];
    expect(selects).toHaveLength(8);
    expect(document.getElementById("polymer-md-choice-sp_method")).toBeNull();
    expect(document.getElementById("polymer-md-choice-sp_basis")).toBeNull();
    const forcefields = openChoices("聚合物力场");
    expect(forcefields.menu.getAllByRole("option")).toHaveLength(5);
    fireEvent.keyDown(forcefields.trigger, { key: "Escape" });
    const optimization = openChoices("几何优化方法");
    expect(optimization.menu.getAllByRole("option").map(option => option.querySelector("strong")?.textContent)).toEqual(["HF（Hartree–Fock）", "ωB97M-D3(BJ)"]);
    fireEvent.keyDown(optimization.trigger, { key: "Escape" });
    for (const select of selects) {
      expect(select.disabled).toBe(false);
      fireEvent.click(select);
      for (const option of screen.getAllByRole("option") as HTMLButtonElement[]) {
        expect(option.disabled).toBe(false);
        const label = option.querySelector("strong");
        expect(label).not.toBeNull();
        expect(option.textContent).toBe(label!.textContent);
        expect(option.querySelector("small")).toBeNull();
      }
      fireEvent.keyDown(select, { key: "Escape" });
    }
    choose("聚合物力场", "Dreiding");
    choose("几何优化方法", "HF（Hartree–Fock）");
    choose("原子电荷模型", "Gasteiger");
    expect(payload()).toMatchObject({ forcefield: "Dreiding", calculation: "basic",
      native_configuration: { opt_method: "HF", charge: "gasteiger" } });
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES].sort());
    expect(selected().some(key => key.startsWith("qm_"))).toBe(false);
  });

  it("explains unused charge and polarizability settings while retaining their choices", () => {
    render(<Form methods />);
    choose("原子电荷模型", "全零部分电荷");
    expect(screen.getAllByTitle(/当前电荷模型不使用量化电荷方法与基组；此处选择保留，不会触发相应计算。/)).toHaveLength(2);
    expect(screen.getAllByTitle(/未选择静态折射率，本次不执行极化率计算；此处选择保留。/)).toHaveLength(2);
    expect(payload()?.native_configuration).toMatchObject({ charge: "zero", charge_method: "HF", polar_method: "wb97m-d3bj" });
    expect(JSON.parse(screen.getByLabelText("保留的科研配置").textContent!)).toMatchObject({ charge: "zero", charge_method: "HF", polar_method: "wb97m-d3bj" });
    fireEvent.click(checkbox("静态近似折射率"));
    expect(screen.queryByTitle(/未选择静态折射率，本次不执行极化率计算/)).toBeNull();
    expect(payload()?.native_configuration?.polar_method).toBe("wb97m-d3bj");
    expect(JSON.parse(screen.getByLabelText("保留的科研配置").textContent!).polar_method).toBe("wb97m-d3bj");
  });

  it("labels general basis choices and shows each role's declared Br/I overrides without changing submissions", () => {
    const changed = vi.fn();
    render(<Form methods changed={changed} />);
    for (const [label, bromine] of [["几何优化主基组", "6-31G(d,p)"], ["电荷量化主基组", "6-31G(d)"], ["静态极化率主基组", "6-311G(d,p)"]]) {
      const { trigger, menu } = openChoices(label);
      expect(menu.getAllByRole("option").map(option => option.querySelector("strong")?.textContent)).toEqual(["6-31G(d)", "6-31G(d,p)", "6-311G(d,p)", "6-311+G(2d,p)"]);
      fireEvent.keyDown(trigger, { key: "Escape" });
      const field = screen.getByRole("combobox", { name: label }).closest(".np-polymer-md-configuration-field")!;
      const help = field.querySelector(".np-polymer-md-field-help")!;
      expect(help.getAttribute("title")).toContain(`锁定逐元素覆盖：Br → ${bromine}；I → lanl2dz。仅在分子含对应元素时生效；其他元素使用所选主基组。I 使用 LANL2DZ 基组/ECP。`);
    }
    expect(changed).not.toHaveBeenCalled();
    choose("几何优化主基组", "6-311+G(2d,p)");
    expect(payload()?.native_configuration?.opt_basis).toBe("6-311+G(2d,p)");
    const optimizationField = screen.getByRole("combobox", { name: "几何优化主基组" }).closest(".np-polymer-md-configuration-field")!;
    expect(optimizationField.querySelector(".np-polymer-md-field-help")?.getAttribute("title")).toContain("Br → 6-31G(d,p)");
  });

  it("keeps the fourteen included analyses collapsed by default and opens their groups without per-property dropdowns", () => {
    const changed = vi.fn();
    render(<Form changed={changed} />);
    expect(payload()?.calculation).toBe("basic");
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES].sort());
    expect(screen.getAllByRole("checkbox")).toHaveLength(5);
    const field = polymerMdUserConfigurationSpec.fields.find(item => item.id === "properties")!;
    for (const key of POLYMER_MD_BASE_PROPERTIES) {
      const item = field.options.find(option => option.value === key)!;
      expect(screen.getByText(item.label)).toBeTruthy();
      expect(screen.queryByRole("checkbox", { name: item.label })).toBeNull();
    }
    expect(screen.queryByRole("button", { name: "全部选择" })).toBeNull();
    expect(screen.queryByRole("button", { name: "清空选择" })).toBeNull();
    const disclosure = screen.getByText("平衡后自动分析", { exact: false }).closest("details")!;
    const summary = disclosure.querySelector("summary")!;
    expect(disclosure.open).toBe(false);
    expect(summary.textContent).toContain("14 项默认包含");
    fireEvent.click(summary);
    expect(disclosure.open).toBe(true);
    const base = disclosure.closest("section")!;
    expect(within(base).getAllByRole("listitem")).toHaveLength(14);
    for (const category of ["热力学", "结构", "输运", "介电"]) {
      expect(within(base).getByRole("region", { name: category })).toBeTruthy();
    }
    expect(within(base).getByText("平衡态质量密度")).toBeTruthy();
    expect(disclosure.querySelector("details")).toBeNull();
    fireEvent.click(summary);
    expect(disclosure.open).toBe(false);
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES].sort());
    const additions = screen.getByRole("heading", { name: "追加计算" }).closest("section")!;
    expect(additions.querySelector("details")).toBeNull();
    expect(within(additions).queryByText("说明")).toBeNull();
    expect(within(additions).getByText(/至少需要两条链/)).toBeTruthy();
    expect(changed).not.toHaveBeenCalled();
  });

  it("includes RNEMD for diffusivity and removes both thermal targets when RNEMD is unchecked", () => {
    render(<Form />);
    fireEvent.click(checkbox("静态近似折射率"));
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "refractive_index"].sort());
    expect(payload()?.calculation).toBe("basic");
    expect(selected()).not.toContain("qm_polarizability");
    fireEvent.click(checkbox("热扩散率"));
    expect(checkbox("MP/RNEMD总热导率").checked).toBe(true);
    expect(checkbox("热扩散率").checked).toBe(true);
    expect(payload()?.calculation).toBe("basic_tc");
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "refractive_index", "thermal_conductivity", "thermal_diffusivity"].sort());
    fireEvent.click(checkbox("MP/RNEMD总热导率"));
    expect(checkbox("热扩散率").checked).toBe(false);
    expect(payload()?.calculation).toBe("basic");
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "refractive_index"].sort());
    fireEvent.click(checkbox("静态近似折射率"));
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES].sort());
  });

  it("keeps an explicitly selected conductivity calculation when diffusivity alone is removed", () => {
    render(<Form />);
    fireEvent.click(checkbox("MP/RNEMD总热导率"));
    fireEvent.click(checkbox("热扩散率"));
    fireEvent.click(checkbox("热扩散率"));
    expect(payload()?.calculation).toBe("basic_tc");
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "thermal_conductivity"].sort());
  });

  it("selects Tg and the paired SP/CED workflow without requiring a heat-conductivity run", () => {
    render(<Form />);
    fireEvent.click(checkbox("玻璃化转变温度 Tg"));
    expect(payload()?.calculation).toBe("basic");
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "tg"].sort());
    fireEvent.click(checkbox("溶解度参数 SP / 内聚能密度 CED"));
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "tg", "sp_total", "sp_ced"].sort());
    expect(selected()).not.toContain("thermal_conductivity");
    fireEvent.click(checkbox("溶解度参数 SP / 内聚能密度 CED"));
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "tg"].sort());
    fireEvent.click(checkbox("玻璃化转变温度 Tg"));
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES].sort());
  });

  it("preserves every base analysis and all additional outputs when all five options are selected", () => {
    render(<Form />);
    for (const label of ["MP/RNEMD总热导率", "静态近似折射率", "热扩散率", "玻璃化转变温度 Tg", "溶解度参数 SP / 内聚能密度 CED"]) {
      fireEvent.click(checkbox(label));
    }
    expect(screen.getAllByRole("checkbox").every(item => (item as HTMLInputElement).checked)).toBe(true);
    expect(payload()?.calculation).toBe("basic_tc");
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "thermal_conductivity", "thermal_diffusivity", "refractive_index", "tg", "sp_total", "sp_ced"].sort());
  });

  it("requires explicit removal of molecular targets and separately repairs unknown choices", () => {
    const changed = vi.fn();
    render(<Form changed={changed} initial={["qm_homo", "qm_polarizability", "refractive_index", "legacy_saved_property"]} />);
    expect(payload()).toBeNull();
    expect(changed).not.toHaveBeenCalled();
    expect(screen.queryByRole("checkbox", { name: "单体单点电子结构" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "单体静态电子极化率" })).toBeNull();
    fireEvent.click(checkbox("MP/RNEMD总热导率"));
    const saved = () => JSON.parse(screen.getByLabelText("保留的科研配置").textContent!) as PolymerMdUserConfiguration;
    expect(saved().properties).toContain("legacy_saved_property");
    expect(saved().properties).toContain("qm_homo");
    expect(payload()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "移除单体量化目标" }));
    expect((saved().properties as string[]).some(key => key.startsWith("qm_"))).toBe(false);
    expect(saved().properties).toContain("legacy_saved_property");
    expect(payload()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "移除保存的性质 legacy_saved_property" }));
    expect(selected()).toEqual([...POLYMER_MD_BASE_PROPERTIES, "refractive_index", "thermal_conductivity"].sort());
    expect(payload()?.calculation).toBe("basic_tc");
  });

  it("does not offer removed workflow catalogs or independent molecular tools", () => {
    render(<Form />);
    for (const label of ["其他 RadonPy 计算流程", "频率介电响应",
      "光学色散与阿贝数", "其他热导计算方法", "拉伸形变与力学性质"]) expect(screen.queryByText(label)).toBeNull();
    expect(screen.getAllByRole("checkbox").map(item => item.getAttribute("aria-label"))).toEqual([
      "MP/RNEMD总热导率", "静态近似折射率", "热扩散率", "玻璃化转变温度 Tg", "溶解度参数 SP / 内聚能密度 CED"
    ]);
    expect(payload()?.native_configuration?.properties).toEqual([...POLYMER_MD_BASE_PROPERTIES].sort());
  });
});
