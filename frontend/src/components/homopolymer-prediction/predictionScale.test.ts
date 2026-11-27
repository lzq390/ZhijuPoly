import { describe, expect, it } from "vitest";
import { PREDICTABLE_PROPERTIES } from "../../constants/predictableProperties";
import { PROPERTY_SCALES, scaleBounds, scalePosition, scaleRangeLabel, tickPercent } from "./predictionScale";

describe("readout scales", () => {
  it("covers every predictable property with ticks at both ends", () => {
    for (const property of PREDICTABLE_PROPERTIES) {
      const scale = PROPERTY_SCALES[property];
      expect(scale.ticks[0]).toBe(scale.min);
      expect(scale.ticks.at(-1)).toBe(scale.max);
      expect(tickPercent(property, scale.min)).toBe(0);
      expect(tickPercent(property, scale.max)).toBe(100);
    }
  });

  it("places linear values proportionally", () => {
    expect(scalePosition("Glass transition temperature", 150)).toEqual({ percent: 50, outside: null });
    expect(scalePosition("Tensile stress strength at break", 50)).toEqual({ percent: 25, outside: null });
  });

  it("places permeability and elongation on a log scale", () => {
    expect(scalePosition("O2 Permeability Barrer", 1)).toEqual({ percent: 33.3, outside: null });
    expect(scalePosition("Elongation at break", 100)).toEqual({ percent: 66.7, outside: null });
  });

  it("stops values outside the range at the ends and says which side", () => {
    expect(scalePosition("Glass transition temperature", -200)).toEqual({ percent: 0, outside: "below" });
    expect(scalePosition("Co2 Permeability Barrer", 30_000)).toEqual({ percent: 100, outside: "above" });
    expect(scalePosition("H2 Permeability Barrer", 0)).toEqual({ percent: 0, outside: "below" });
    expect(scalePosition("Melting temperature", null)).toBeNull();
    expect(scalePosition("Melting temperature", Number.NaN)).toBeNull();
  });

  it("labels ranges in plain units", () => {
    expect(scaleRangeLabel("Glass transition temperature", "°C")).toBe("−150 – 450 °C");
    expect(scaleBounds("Glass transition temperature")).toEqual(["−150", "450"]);
    expect(scaleRangeLabel("O2 Permeability Barrer", "Barrer")).toBe("0.01 – 10⁴ Barrer，对数刻度");
    expect(scaleBounds("Elongation at break")).toEqual(["1", "1000"]);
  });
});
