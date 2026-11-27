import { PREDICT_PROPERTY_META } from "../../constants/predictableProperties";
import type { PredictableProperty } from "../../types";

export type PredictionRun = {
  id: number;
  smiles: string;
  properties: PredictableProperty[];
  predictions: Partial<Record<PredictableProperty, number>>;
  queryTimeMs: number;
  finishedAt: Date;
};

export const MINUS = "−";

/** Shown numbers use the typographic minus (U+2212), so a negative value and a negative change
    carry the same sign in one row. Exports keep the ASCII hyphen so spreadsheets read them as numbers. */
export function withDisplayMinus(text: string) {
  return text.startsWith("-") ? `${MINUS}${text.slice(1)}` : text;
}

/** Display precision follows magnitude; exports keep the raw model value. */
export function formatPredictionValue(value: number | undefined | null): string | null {
  if (value === undefined || value === null || !Number.isFinite(value)) return null;
  const magnitude = Math.abs(value);
  if (magnitude === 0) return "0";
  const text = magnitude >= 100
    ? value.toFixed(0)
    : magnitude >= 1
      ? String(Number(value.toFixed(1)))
      : Number(value.toPrecision(2)).toString();
  return withDisplayMinus(text);
}

export function predictionValue(run: PredictionRun | null | undefined, property: PredictableProperty) {
  const value = run?.predictions[property];
  return value !== undefined && Number.isFinite(value) ? value : null;
}

export type PredictionDelta = { text: string; large: boolean };

/** Neutral signed change; a higher value is not inherently better or worse. */
export function predictionDelta(current: number | null, previous: number | null): PredictionDelta | null {
  if (current === null || previous === null) return null;
  const delta = current - previous;
  const formatted = formatPredictionValue(Math.abs(delta));
  if (formatted === null) return null;
  if (formatted === "0") return { text: "0", large: false };
  const large = previous !== 0 && Math.abs(delta) / Math.abs(previous) >= 0.2;
  return { text: `${delta > 0 ? "+" : MINUS}${formatted}`, large };
}

/** Drops a trailing CXSMILES extension block (e.g. Ketcher's |$star_e;;$| labels) for display only. */
export function displaySmiles(smiles: string) {
  return smiles.replace(/\s+\|[^|]*\|\s*$/, "");
}

export function formatRunTime(date: Date) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function csvCell(value: string) {
  return /[",\n\r]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
}

export function predictionTable(run: PredictionRun, comparison: PredictionRun | null) {
  const header = ["property_key", "property", "unit", "value", ...(comparison ? ["previous_value", "change"] : []), "smiles"];
  const rows = run.properties.map((property) => {
    const meta = PREDICT_PROPERTY_META[property];
    const value = predictionValue(run, property);
    const previous = comparison ? predictionValue(comparison, property) : null;
    return [
      property,
      meta.label,
      meta.unit,
      value === null ? "" : String(value),
      ...(comparison ? [previous === null ? "" : String(previous), value !== null && previous !== null ? String(value - previous) : ""] : []),
      run.smiles
    ];
  });
  return [header, ...rows];
}

export function toCsv(table: string[][]) {
  return `﻿${table.map((row) => row.map(csvCell).join(",")).join("\r\n")}\r\n`;
}

export function toTsv(table: string[][]) {
  return table.map((row) => row.map((cell) => cell.replace(/[\t\r\n]+/g, " ")).join("\t")).join("\n");
}
