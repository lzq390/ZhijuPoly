import { describe, expect, it } from "vitest";
import { displaySmiles, formatPredictionValue, predictionDelta, predictionTable, toCsv } from "./predictionFormat";

describe("predictionFormat", () => {
  it("按数量级显示精度", () => {
    expect(formatPredictionValue(101.34)).toBe("101");
    expect(formatPredictionValue(44.46)).toBe("44.5");
    expect(formatPredictionValue(0.0347)).toBe("0.035");
    expect(formatPredictionValue(-36.24)).toBe("−36.2");
    expect(formatPredictionValue(-130.4)).toBe("−130");
    expect(formatPredictionValue(-0.0347)).toBe("−0.035");
    expect(formatPredictionValue(-0)).toBe("0");
    expect(formatPredictionValue(undefined)).toBeNull();
    expect(formatPredictionValue(Number.NaN)).toBeNull();
  });

  it("变化值带符号且超过 20% 标记为明显", () => {
    expect(predictionDelta(150, 100)).toEqual({ text: "+50", large: true });
    expect(predictionDelta(95, 100)).toEqual({ text: "−5", large: false });
    expect(predictionDelta(100, null)).toBeNull();
  });

  it("只在显示时去掉 CXSMILES 扩展块", () => {
    expect(displaySmiles("*CC* |$star_e;;;star_e$|")).toBe("*CC*");
    expect(displaySmiles("CCO")).toBe("CCO");
  });

  it("导出保留 ASCII 负号，表格软件可按数字读取", () => {
    const run = { id: 2, smiles: "*CC*", properties: ["Glass transition temperature" as const],
      predictions: { "Glass transition temperature": -125.5 }, queryTimeMs: 1, finishedAt: new Date() };
    const previous = { ...run, id: 1, predictions: { "Glass transition temperature": 10 } };
    const csv = toCsv(predictionTable(run, previous));
    expect(csv).toContain("°C,-125.5,10,-135.5,*CC*");
    expect(csv).not.toContain("−");
  });

  it("CSV 保留原始值并转义逗号", () => {
    const run = { id: 1, smiles: "C(C)C,x", properties: ["Glass transition temperature" as const],
      predictions: { "Glass transition temperature": 101.345 }, queryTimeMs: 1, finishedAt: new Date() };
    const csv = toCsv(predictionTable(run, null));
    expect(csv.startsWith("﻿property_key,")).toBe(true);
    expect(csv).toContain('Glass transition temperature,玻璃化转变温度,°C,101.345,"C(C)C,x"');
  });
});
