// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { PolymerMdCapabilities, PolymerMdCapabilityCatalog } from "../../types/polymerMd";
import { PolymerMdPropertyCoverage, PolymerMdProtocolInfo } from "./PolymerMdProtocolInfo";

afterEach(cleanup);
const catalog: PolymerMdCapabilityCatalog = {
  catalog_version: 1, source_sha: "12345678901234567890", description: "connected 不代表科学验收通过。",
  forcefields: [{ id: "GAFF2_mod", label: "GAFF2_mod", status: "connected", description: "当前开发协议" }, { id: "other", label: "其他力场", status: "upstream_only", description: "上游具备，当前未接入" }],
  calculations: [{ id: "basic", label: "基础物性", status: "connected", description: "共同采样", property_groups: ["basic"] }, { id: "basic_tc", label: "基础与热导", status: "connected", description: "单方向热导", property_groups: ["basic", "thermal"] }, { id: "other", label: "其他流程", status: "upstream_only", description: "待接入" }],
  property_groups: [
    { id: "basic", label: "基础性质", calculation: "basic", status: "connected", properties: [{ key: "density", label: "密度", unit: "g/cm³", status: "connected", description: "平衡分析", dependencies: ["平衡收敛"] }] },
    { id: "thermal", label: "热输运", calculation: "basic_tc", status: "connected", properties: [{ key: "thermal_conductivity", label: "总热导率", unit: "W/(m·K)", status: "connected", description: "所选方向", dependencies: ["热流稳态", "温度梯度线性"] }] },
    { id: "future", label: "上游性质", calculation: null, status: "upstream_only", properties: [{ key: "future", label: "待开放性质", unit: "K", status: "upstream_only", description: "未接入" }] }
  ],
  parameters: [{ key: "dt", label: "平衡时间步长", group: "protocol", status: "fixed", default: 1, unit: "fs", description: "服务端固定" }, { key: "future", label: "待开放调节", group: "upstream", status: "upstream_only", description: "当前不可编辑" }]
};
const capabilities = { capability_catalog: catalog } as PolymerMdCapabilities;

describe("polymer MD scientific scope", () => {
  it("lists units and conditions only for properties in the selected connected workflow", () => {
    const view = render(<PolymerMdPropertyCoverage capabilities={capabilities} calculation="basic" />);
    expect(screen.getByText("密度")).toBeTruthy();
    expect(screen.getByText("g/cm³")).toBeTruthy();
    expect(screen.getByText(/条件：平衡收敛/)).toBeTruthy();
    expect(screen.queryByText("总热导率")).toBeNull();
    expect(screen.queryByText("待开放性质")).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    view.rerender(<PolymerMdPropertyCoverage capabilities={capabilities} calculation="basic_tc" />);
    expect(screen.getByText("总热导率")).toBeTruthy();
    expect(screen.getByText("W/(m·K)")).toBeTruthy();
    expect(screen.getByText(/热流稳态；温度梯度线性/)).toBeTruthy();
  });
  it("keeps fixed protocol settings and upstream capabilities read only and separate", () => {
    render(<PolymerMdProtocolInfo capabilities={capabilities} />);
    const protocol = screen.getByLabelText("只读科学协议") as HTMLDetailsElement;
    const directory = screen.getByLabelText("RadonPy 能力目录") as HTMLDetailsElement;
    expect(protocol.open).toBe(false);
    expect(directory.open).toBe(false);
    fireEvent.click(within(protocol).getByText("只读科学协议", { selector: "strong" }));
    expect(within(protocol).getByText("平衡时间步长")).toBeTruthy();
    expect(within(protocol).getByText("1 fs")).toBeTruthy();
    expect(within(protocol).getByText("待科学验收")).toBeTruthy();
    expect(within(protocol).queryByRole("spinbutton")).toBeNull();
    expect(within(directory).getByText("其他力场").closest("li")?.getAttribute("aria-disabled")).toBe("true");
    expect(within(directory).getByText("待开放调节").closest("li")?.getAttribute("aria-disabled")).toBe("true");
    expect(within(directory).queryByRole("radio")).toBeNull();
    expect(within(directory).queryByRole("checkbox")).toBeNull();
  });
  it("makes the canonical local catalog available without a private service request", () => {
    render(<PolymerMdProtocolInfo capabilities={null} />);
    const directory = screen.getByLabelText("RadonPy 能力目录");
    expect(within(directory).getByText("GAFF2_mod")).toBeTruthy();
    expect(screen.getByText(/本地协议目录，实际执行条件以任务记录为准/)).toBeTruthy();
  });
});
