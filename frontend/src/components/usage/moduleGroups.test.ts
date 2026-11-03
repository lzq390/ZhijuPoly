import { describe, expect, it } from "vitest";
import { shortUsageTaskId, usageModuleGroupLabel, usageTaskTypeLabel } from "./moduleGroups";
import catalog from "../../../scripts/fixtures/usage-catalog.json";

describe("usage display groups", () => {
  it("uses sidebar membership without combining DFT execution with database analysis", () => {
    const groups = catalog.module_groups;
    const modules = [{ id: "monomer_dft", label: "DFT 计算" }, { id: "dft_history", label: "DFT 历史查询" }];
    expect(usageModuleGroupLabel("monomer_dft", groups)).toBe("单体 DFT");
    expect(usageModuleGroupLabel("dft_history", groups)).toBe("数据库分析");
    expect(usageTaskTypeLabel("monomer_dft", modules)).toBe("DFT 计算");
    expect(usageTaskTypeLabel("dft_history", modules)).toBe("DFT 历史查询");
    expect(usageModuleGroupLabel("new-module", groups)).toBe("未归属侧栏模块");
    expect(usageModuleGroupLabel("polytao", groups)).toBe("聚合物生成");
    expect(usageModuleGroupLabel("conditional_generation", groups)).toBe("条件聚合物生成");
    expect(usageModuleGroupLabel("tg_assistant", groups)).toBe("Tg 逆向设计");
  });
  it("distinguishes monomer and polymer MD without replacing other catalog labels", () => {
    expect(usageModuleGroupLabel("monomer_md", catalog.module_groups)).toBe("单体 MD 模拟");
    expect(usageModuleGroupLabel("polymer_md", catalog.module_groups)).toBe("MD 模拟");
    expect(usageModuleGroupLabel("md_demo", catalog.module_groups)).toBe("MD 模拟");
    expect(usageTaskTypeLabel("monomer_md", { monomer_md: "分子动力学" })).toBe("单体分子动力学");
    expect(usageTaskTypeLabel("polymer_md", {})).toBe("聚合物分子动力学");
    expect(usageTaskTypeLabel("md_demo", { md_demo: "分子动力学演示" })).toBe("分子动力学演示");
    expect(usageTaskTypeLabel("unknown", [])).toBe("unknown");
  });
  it("does not invent sidebar attribution for historical or shared observations", () => {
    for (const module of ["assistant", "recording_summary", "database_browser", "openscience"]) {
      expect(usageModuleGroupLabel(module, catalog.module_groups)).toBe("未归属侧栏模块");
    }
    expect(usageTaskTypeLabel("database_browser", { database_browser: "材料数据库浏览" })).toBe("历史数据库操作（未细分）");
    expect(usageTaskTypeLabel("openscience", [])).toBe("OpenScience 独立服务");
  });
  it("shows UUID identity after a matching module prefix while retaining unmatched IDs", () => {
    expect(shortUsageTaskId("polymer_md:12345678-1234-1234-1234-123456789abc", "polymer_md")).toBe("12345678…9abc");
    expect(shortUsageTaskId("polymer_md:87654321-1234-1234-1234-123456789abc", "polymer_md")).toBe("87654321…9abc");
    expect(shortUsageTaskId("monomer_md:short", "monomer_md")).toBe("short");
    expect(shortUsageTaskId("other:short", "monomer_md")).toBe("other:short");
    expect(shortUsageTaskId("small-id", "monomer_md")).toBe("small-id");
  });
});
