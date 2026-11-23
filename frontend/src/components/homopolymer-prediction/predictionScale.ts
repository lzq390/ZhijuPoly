import type { PredictableProperty } from "../../types";

/**
 * Fixed display ranges for the readout scale beside each result. They are axes chosen to span
 * common homopolymers, not statistics of any dataset; values outside stop at the end of the scale.
 */
export type PropertyScale = {
  min: number;
  max: number;
  log?: boolean;
  ticks: readonly number[];
};

const PERMEABILITY: PropertyScale = { min: 0.01, max: 10_000, log: true, ticks: [0.01, 0.1, 1, 10, 100, 1000, 10_000] };

export const PROPERTY_SCALES: Record<PredictableProperty, PropertyScale> = {
  "Glass transition temperature": { min: -150, max: 450, ticks: [-150, 0, 150, 300, 450] },
  "Melting temperature": { min: 0, max: 500, ticks: [0, 100, 200, 300, 400, 500] },
  "Thermal decomposition temperature": { min: 200, max: 700, ticks: [200, 300, 400, 500, 600, 700] },
  "Thermal decomposition weight loss": { min: 0, max: 100, ticks: [0, 25, 50, 75, 100] },
  "Elongation at break": { min: 1, max: 1000, log: true, ticks: [1, 10, 100, 1000] },
  "Tensile stress strength at break": { min: 0, max: 200, ticks: [0, 50, 100, 150, 200] },
  "O2 Permeability Barrer": PERMEABILITY,
  "Co2 Permeability Barrer": PERMEABILITY,
  "H2 Permeability Barrer": PERMEABILITY
};

export type ScalePosition = { percent: number; outside: "below" | "above" | null };

function fraction(scale: PropertyScale, value: number) {
  if (scale.log) return (Math.log10(value) - Math.log10(scale.min)) / (Math.log10(scale.max) - Math.log10(scale.min));
  return (value - scale.min) / (scale.max - scale.min);
}

/** Where a value sits on its property's scale, as a percentage clamped to the ends. */
export function scalePosition(property: PredictableProperty, value: number | null): ScalePosition | null {
  if (value === null || !Number.isFinite(value)) return null;
  const scale = PROPERTY_SCALES[property];
  if (value < scale.min || (scale.log && value <= 0)) return { percent: 0, outside: "below" };
  if (value > scale.max) return { percent: 100, outside: "above" };
  return { percent: Math.round(fraction(scale, value) * 1000) / 10, outside: null };
}

export function tickPercent(property: PredictableProperty, tick: number) {
  return Math.round(fraction(PROPERTY_SCALES[property], tick) * 1000) / 10;
}

function formatBound(value: number) {
  if (Math.abs(value) >= 10_000) return `10${toSuperscript(Math.round(Math.log10(value)))}`;
  return String(value);
}

function toSuperscript(value: number) {
  const digits = "⁰¹²³⁴⁵⁶⁷⁸⁹";
  return String(value).split("").map((char) => (char === "-" ? "⁻" : digits[Number(char)])).join("");
}

/** Short description of the scale, for the hover title and the help text. */
export function scaleRangeLabel(property: PredictableProperty, unit: string) {
  const scale = PROPERTY_SCALES[property];
  return `${formatBound(scale.min)} – ${formatBound(scale.max)} ${unit}${scale.log ? "，对数刻度" : ""}`;
}

export function scaleBounds(property: PredictableProperty) {
  const scale = PROPERTY_SCALES[property];
  return [formatBound(scale.min), formatBound(scale.max)] as const;
}
