import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Atom,
  BarChart3,
  Database,
  FlaskConical,
  Layers3,
  Network,
  Orbit,
  PieChart,
  Search,
  Sigma,
  TableProperties
} from "lucide-react";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";

type DatasetKey = "process" | "property" | "structureEffect" | "dft" | "reserved";

type RankedItem = { label: string; value: number };
type DonutItem = { label: string; value: number; color: string };
type RangeItem = {
  label: string;
  count: number;
  min: number;
  median: number;
  max: number;
  p5?: number;
  p95?: number;
};
type AtomCoordinate = [number, number, number, number];

const colors = ["#0f766e", "#2563eb", "#f59e0b", "#e11d48", "#7c3aed", "#64748b", "#14b8a6"];
const D3MOL_SRC = "/vendor/3Dmol-min.js";

function loadScriptOnce(src: string, id: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.getElementById(id) as HTMLScriptElement | null;
    if (existing) {
      if (existing.dataset.loaded === "true") {
        resolve();
        return;
      }
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error(`Failed to load script: ${src}`)), { once: true });
      return;
    }

    const script = document.createElement("script");
    script.id = id;
    script.src = src;
    script.async = true;
    script.onload = () => {
      script.dataset.loaded = "true";
      resolve();
    };
    script.onerror = () => reject(new Error(`Failed to load script: ${src}`));
    document.head.appendChild(script);
  });
}

const processData = {
  rows: 163587,
  uniqueRecordIds: 31888,
  uniquePolymers: 31738,
  uniqueProducts: 37646,
  avgProcessTextLength: 620.7,
  topTerms: [
    { label: "solution", value: 153240 },
    { label: "temperature", value: 82950 },
    { label: "added", value: 80031 },
    { label: "min", value: 66503 },
    { label: "water", value: 59358 },
    { label: "dried", value: 52502 },
    { label: "room", value: 49906 },
    { label: "stirred", value: 48733 },
    { label: "acid", value: 45603 },
    { label: "reaction", value: 45389 }
  ],
  topProducts: [
    { label: "PI-1", value: 513 },
    { label: "PI-2", value: 434 },
    { label: "PI", value: 351 },
    { label: "PI-3", value: 347 },
    { label: "PI-4", value: 285 },
    { label: "polyimide film", value: 259 },
    { label: "PI-5", value: 227 },
    { label: "PI film", value: 191 }
  ],
  topMaterials: [
    { label: "ODA", value: 107 },
    { label: "DMAc", value: 70 },
    { label: "NMP", value: 66 },
    { label: "DMF", value: 63 },
    { label: "PI", value: 51 },
    { label: "BTDA", value: 49 },
    { label: "PAA", value: 43 },
    { label: "PMDA", value: 41 }
  ]
};

const propertyData = {
  rows: 172449,
  uniquePolymers: 32233,
  uniqueProperties: 1120,
  categories: [
    { label: "other", value: 97166, color: "#64748b" },
    { label: "mechanical", value: 30632, color: "#2563eb" },
    { label: "thermal", value: 28339, color: "#0f766e" },
    { label: "surface", value: 7873, color: "#f59e0b" },
    { label: "barrier", value: 4555, color: "#e11d48" },
    { label: "optical", value: 2081, color: "#7c3aed" },
    { label: "electrical", value: 1803, color: "#14b8a6" }
  ],
  topProperties: [
    { label: "glass transition temperature", value: 9199 },
    { label: "tensile strength", value: 7892 },
    { label: "tensile modulus", value: 6612 },
    { label: "density", value: 5308 },
    { label: "thickness", value: 4872 },
    { label: "comparative tracking index", value: 3726 },
    { label: "intrinsic viscosity", value: 3411 },
    { label: "weight loss", value: 3034 }
  ],
  ranges: [
    { label: "glass transition temperature", count: 9199, min: -272, p5: -22, median: 255, p95: 400, max: 649.334 },
    { label: "tensile strength", count: 7892, min: -43, p5: 0.761, median: 70.2, p95: 500.49, max: 44570 },
    { label: "tensile modulus", count: 6612, min: -0.0548, p5: 0.7055, median: 1210, p95: 12100, max: 500000 },
    { label: "density", count: 5308, min: -147.7, p5: 0.076, median: 1.38, p95: 682.55, max: 444444 },
    { label: "thickness", count: 4872, min: -36, p5: 0.2222, median: 25, p95: 444.5, max: 42800 }
  ],
  categoryTop: [
    { label: "thermal: glass transition", value: 9199 },
    { label: "mechanical: tensile strength", value: 7892 },
    { label: "surface: water contact angle", value: 2448 },
    { label: "barrier: permeability", value: 1854 },
    { label: "electrical: resistivity", value: 1369 },
    { label: "optical: transmittance", value: 677 }
  ]
};

const structureEffectData = {
  rows: 2114,
  uniqueSmiles: 735,
  properties: [
    { label: "Glass transition temperature", value: 440 },
    { label: "Thermal decomposition temperature", value: 371 },
    { label: "Thermal decomposition weight loss", value: 346 },
    { label: "Melting temperature", value: 211 },
    { label: "CO2 Permeability Barrer", value: 194 },
    { label: "O2 Permeability Barrer", value: 194 },
    { label: "H2 Permeability Barrer", value: 156 },
    { label: "Tensile stress strength at break", value: 102 },
    { label: "Elongation at break", value: 100 }
  ],
  units: [
    { label: "C", value: 1022, color: "#0f766e" },
    { label: "Barrer", value: 544, color: "#2563eb" },
    { label: "%", value: 446, color: "#f59e0b" },
    { label: "GPa", value: 102, color: "#e11d48" }
  ],
  sources: [
    { label: "exp", value: 1669, color: "#0f766e" },
    { label: "sim", value: 309, color: "#2563eb" },
    { label: "N/A", value: 136, color: "#64748b" }
  ],
  sourceMatrix: [
    { label: "Tg", exp: 336, sim: 37, na: 67 },
    { label: "Tm", exp: 142, sim: 0, na: 69 },
    { label: "Td", exp: 371, sim: 0, na: 0 },
    { label: "Td loss", exp: 346, sim: 0, na: 0 },
    { label: "CO2", exp: 97, sim: 97, na: 0 },
    { label: "O2", exp: 97, sim: 97, na: 0 },
    { label: "H2", exp: 78, sim: 78, na: 0 },
    { label: "Elongation", exp: 100, sim: 0, na: 0 },
    { label: "Stress", exp: 102, sim: 0, na: 0 }
  ],
  ranges: [
    { label: "Glass transition temperature", count: 440, min: -356.15, median: 183.5, max: 386 },
    { label: "Melting temperature", count: 211, min: -314.15, median: 162.5, max: 478 },
    { label: "Thermal decomposition temperature", count: 371, min: 150, median: 485, max: 877 },
    { label: "Thermal decomposition weight loss", count: 346, min: 0, median: 10, max: 96 },
    { label: "CO2 Permeability Barrer", count: 194, min: 4.96, median: 1100, max: 47000 }
  ]
};

const dftCoordinates: AtomCoordinate[] = [
  [8, -7.351685, 0.462768, -1.540367],
  [6, -6.935456, 1.302362, -0.787285],
  [8, -7.650348, 2.362276, -0.366597],
  [7, -5.687492, 1.327659, -0.236607],
  [6, -4.725015, 0.358813, -0.487133],
  [6, -4.686786, -0.779742, -1.231638],
  [6, -3.357919, -1.30219, -1.036171],
  [6, -2.720512, -0.445374, -0.201328],
  [6, -1.349654, -0.410272, 0.379392],
  [8, -0.716623, -1.619462, 0.049177],
  [6, 0.640142, -1.611409, 0.272986],
  [8, 0.881507, -1.432484, 1.639211],
  [6, 2.231217, -1.563819, 2.011496],
  [6, 3.14114, -0.532039, 1.433288],
  [6, 3.545692, 0.7023, 1.835292],
  [6, 4.394257, 1.23399, 0.80597],
  [6, 4.428448, 0.259615, -0.14786],
  [7, 5.057229, 0.131721, -1.376149],
  [6, 5.853655, 1.104712, -1.913309],
  [8, 6.10007, 2.16399, -1.402762],
  [8, 6.32815, 0.70702, -3.105743],
  [8, 3.687761, -0.802891, 0.204684],
  [8, -3.553726, 0.577776, 0.141552],
  [1, -8.51887, 2.291891, -0.786376],
  [1, -5.442329, 2.084082, 0.390591],
  [1, -5.499892, -1.17581, -1.827586],
  [1, -2.926773, -2.205197, -1.45634],
  [1, -1.398138, -0.273348, 1.472966],
  [1, -0.776641, 0.446909, -0.029192],
  [1, 1.12938, -0.798458, -0.306206],
  [1, 1.033884, -2.586388, -0.067795],
  [1, 2.61034, -2.565254, 1.731567],
  [1, 2.254812, -1.482373, 3.104826],
  [1, 3.27059, 1.185561, 2.768867],
  [1, 4.911818, 2.184367, 0.762612],
  [1, 4.917277, -0.728845, -1.89196],
  [1, 6.880494, 1.427543, -3.440082]
];

const dftData = {
  rows: 100,
  molCount: 5,
  selectedMol: "276001_Conf03",
  selectedStep: 18,
  selectedAtoms: 37,
  finalEnergy: -1213.73634697,
  finalGap: 7.540006826,
  finalDipole: 2.6104,
  finalLowestFreq: 5.7149,
  energyRange: { min: -2431.42353977, max: -947.533225336, count: 95 },
  gapRange: { min: 5.047170472, median: 7.536469344, max: 7.890217544 },
  atomTotals: [
    { label: "C", value: 1730, color: "#334155" },
    { label: "H", value: 1638, color: "#94a3b8" },
    { label: "O", value: 555, color: "#2563eb" },
    { label: "N", value: 200, color: "#7c3aed" },
    { label: "S", value: 148, color: "#f59e0b" }
  ],
  convergence: [
    { label: "04", value: 63, color: "#0f766e" },
    { label: "24", value: 15, color: "#2563eb" },
    { label: "14", value: 9, color: "#f59e0b" },
    { label: "44", value: 6, color: "#e11d48" },
    { label: "blank", value: 5, color: "#64748b" },
    { label: "34", value: 2, color: "#7c3aed" }
  ],
  moleculeFinals: [
    { label: "276001_Conf03", steps: 19, atoms: 37, energy: -1213.73634697, gap: 7.540006826 },
    { label: "276678_Conf02", steps: 10, atoms: 28, energy: -947.579102685, gap: 5.047170472 },
    { label: "277665_Conf01", steps: 32, atoms: 57, energy: -2321.12782467, gap: 6.325017816 },
    { label: "278090_Conf03", steps: 18, atoms: 37, energy: -1139.50774324, gap: 7.890217544 },
    { label: "278446_Conf03", steps: 21, atoms: 38, energy: -2431.42144459, gap: 7.536469344 }
  ],
  trajectory: [
    { step: 1, energy: -1213.71965989 },
    { step: 2, energy: -1213.73495746 },
    { step: 3, energy: -1213.7352429 },
    { step: 4, energy: -1213.73593709 },
    { step: 5, energy: -1213.70906032 },
    { step: 6, energy: -1213.73624855 },
    { step: 7, energy: -1213.73624962 },
    { step: 8, energy: -1213.73629127 },
    { step: 9, energy: -1213.73631697 },
    { step: 10, energy: -1213.7363287 },
    { step: 11, energy: -1213.73633501 },
    { step: 12, energy: -1213.73633668 },
    { step: 13, energy: -1213.73634115 },
    { step: 14, energy: -1213.73634543 },
    { step: 18, energy: -1213.73634697 }
  ],
  coordinates: dftCoordinates
};

const datasets = [
  {
    key: "process" as const,
    order: "01",
    title: "实验工艺数据",
    englishTitle: "Experimental Process",
    description: "从工艺流程文本、产物名称和材料片段中统计工艺动作与材料实体。",
    status: "Ready",
    recordCount: processData.rows,
    fieldCount: 5,
    icon: <FlaskConical className="h-5 w-5" />
  },
  {
    key: "property" as const,
    order: "02",
    title: "实验性质数据",
    englishTitle: "Experimental Properties",
    description: "围绕属性名、属性类别和数值范围展示实验性质数据画像。",
    status: "Ready",
    recordCount: propertyData.rows,
    fieldCount: 4,
    icon: <Sigma className="h-5 w-5" />
  },
  {
    key: "structureEffect" as const,
    order: "03",
    title: "聚合物构效数据",
    englishTitle: "Structure-Property",
    description: "展示 SMILES 关联的九类性质、单位、来源和数值覆盖范围。",
    status: "Ready",
    recordCount: structureEffectData.rows,
    fieldCount: 5,
    icon: <Network className="h-5 w-5" />
  },
  {
    key: "dft" as const,
    order: "04",
    title: "单体构象数据",
    englishTitle: "DFT Conformation",
    description: "使用 preview_100.csv 中的 DFT 坐标、能量轨迹、原子组成和收敛状态作图。",
    status: "Ready",
    recordCount: dftData.rows,
    fieldCount: 21,
    icon: <Orbit className="h-5 w-5" />
  },
  {
    key: "reserved" as const,
    order: "05",
    title: "预留数据模块",
    englishTitle: "Reserved Dataset",
    description: "保留给后续扩展的数据接口和分析面板。",
    status: "Reserved",
    recordCount: null,
    fieldCount: null,
    icon: <Database className="h-5 w-5" />
  }
];

function formatCount(value: number | null) {
  return value === null ? "Reserved" : new Intl.NumberFormat("en-US").format(value);
}

function formatNumber(value: number, digits = 2) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);
}

function MetricPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/80 bg-white/75 px-3 py-2 shadow-sm">
      <div className="text-[10px] font-medium uppercase tracking-[0.16em] text-mutedForeground">{label}</div>
      <div className="font-heading mt-1 truncate text-base font-semibold text-slate-950">{value}</div>
    </div>
  );
}

function ChartPanel({
  icon,
  title,
  children,
  className = "",
  bodyClassName = ""
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={[
        "overflow-hidden rounded-[30px] border border-white/70 bg-white/85 shadow-sm backdrop-blur",
        className
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="flex items-center justify-between gap-4 border-b border-slate-200/80 px-5 py-4">
        <h3 className="font-heading text-lg font-semibold tracking-tight text-slate-950">{title}</h3>
        <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-slate-950 text-white">{icon}</div>
      </div>
      <div className={["p-5", bodyClassName].filter(Boolean).join(" ")}>{children}</div>
    </section>
  );
}

function HorizontalBars({ data, valueLabel = "count" }: { data: RankedItem[]; valueLabel?: string }) {
  const max = Math.max(...data.map((item) => item.value));

  return (
    <div className="space-y-4">
      {data.map((item, index) => (
        <div key={item.label} className="grid grid-cols-[minmax(7rem,11rem)_minmax(0,1fr)_4.5rem] items-center gap-3">
          <div className="truncate text-sm font-medium text-slate-700" title={item.label}>
            {item.label}
          </div>
          <div className="h-3 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full"
              style={{
                width: `${Math.max((item.value / max) * 100, 4)}%`,
                background: `linear-gradient(90deg, ${colors[index % colors.length]} 0%, #38bdf8 100%)`
              }}
            />
          </div>
          <div className="font-mono-ui text-right text-xs text-slate-500" title={valueLabel}>
            {formatCount(item.value)}
          </div>
        </div>
      ))}
    </div>
  );
}

function DonutChart({ data }: { data: DonutItem[] }) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  let offset = 0;

  return (
    <div className="grid gap-5 md:grid-cols-[210px_minmax(0,1fr)] md:items-center">
      <svg viewBox="0 0 160 160" className="mx-auto h-52 w-52 -rotate-90">
        <circle cx="80" cy="80" r="54" fill="none" stroke="#e2e8f0" strokeWidth="24" />
        {data.map((item) => {
          const ratio = (item.value / total) * 100;
          const currentOffset = offset;
          offset += ratio;
          return (
            <circle
              key={item.label}
              cx="80"
              cy="80"
              r="54"
              fill="none"
              stroke={item.color}
              strokeDasharray={`${ratio} ${100 - ratio}`}
              strokeDashoffset={-currentOffset}
              strokeLinecap="round"
              strokeWidth="24"
              pathLength="100"
            />
          );
        })}
        <circle cx="80" cy="80" r="31" fill="#f8fafc" />
      </svg>
      <div className="space-y-3">
        {data.map((item) => (
          <div key={item.label} className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
              <span className="truncate text-sm font-medium text-slate-700">{item.label}</span>
            </div>
            <span className="font-mono-ui text-sm text-slate-500">{formatCount(item.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function BubbleCloud({ data }: { data: RankedItem[] }) {
  const max = Math.max(...data.map((item) => item.value));

  return (
    <div className="flex min-h-[260px] flex-wrap content-center items-center justify-center gap-3 rounded-[24px] bg-[radial-gradient(circle_at_top,rgba(20,184,166,0.12),transparent_34%),linear-gradient(180deg,#f8fafc_0%,#eef5f6_100%)] p-5">
      {data.map((item, index) => (
        <div
          key={item.label}
          className="flex items-center justify-center rounded-full border border-white/80 bg-white/85 text-center font-heading font-semibold text-slate-800 shadow-sm"
          style={{
            width: `${76 + (item.value / max) * 54}px`,
            height: `${76 + (item.value / max) * 54}px`,
            color: colors[index % colors.length]
          }}
        >
          <div>
            <div className="text-sm">{item.label}</div>
            <div className="font-mono-ui mt-1 text-xs text-slate-500">{item.value}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

function RangePlot({ data }: { data: RangeItem[] }) {
  return (
    <div className="space-y-5">
      {data.map((item) => {
        const usesPercentileRange = item.p5 !== undefined || item.p95 !== undefined;
        const visualMin = item.p5 ?? item.min;
        const visualMax = item.p95 ?? item.max;
        const span = visualMax - visualMin || 1;
        const medianPosition = Math.min(Math.max(((item.median - visualMin) / span) * 100, 0), 100);

        return (
          <div key={item.label} className="space-y-2">
            <div className="flex items-center justify-between gap-3">
              <div className="truncate text-sm font-semibold text-slate-800" title={item.label}>
                {item.label}
              </div>
              <div className="font-mono-ui text-xs text-slate-500">{formatCount(item.count)} values</div>
            </div>
            <div className="relative h-8 rounded-full bg-slate-100">
              <div className="absolute inset-x-2 top-1/2 h-2 -translate-y-1/2 rounded-full bg-teal-600/35">
                <div
                  className="absolute top-1/2 h-4 w-4 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-teal-700 shadow-sm"
                  style={{ left: `${medianPosition}%` }}
                  title={`median ${formatNumber(item.median)}`}
                />
              </div>
            </div>
            <div className="grid grid-cols-3 font-mono-ui text-[11px] text-slate-500">
              <span>{usesPercentileRange ? "P5 " : "Min "}{formatNumber(visualMin)}</span>
              <span className="text-center">Median {formatNumber(item.median)}</span>
              <span className="text-right">{usesPercentileRange ? "P95 " : "Max "}{formatNumber(visualMax)}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SourceMatrix() {
  const max = Math.max(...structureEffectData.sourceMatrix.flatMap((item) => [item.exp, item.sim, item.na]));
  const columns = [
    { key: "exp" as const, label: "exp" },
    { key: "sim" as const, label: "sim" },
    { key: "na" as const, label: "N/A" }
  ];

  return (
    <div className="overflow-x-auto">
      <div className="min-w-[520px] space-y-2">
        <div className="grid grid-cols-[9rem_repeat(3,1fr)] gap-2 text-xs font-medium uppercase tracking-[0.14em] text-mutedForeground">
          <span>property</span>
          {columns.map((column) => (
            <span key={column.key} className="text-center">
              {column.label}
            </span>
          ))}
        </div>
        {structureEffectData.sourceMatrix.map((row) => (
          <div key={row.label} className="grid grid-cols-[9rem_repeat(3,1fr)] gap-2">
            <div className="truncate rounded-xl bg-slate-50 px-3 py-2 text-sm font-medium text-slate-700" title={row.label}>
              {row.label}
            </div>
            {columns.map((column) => {
              const value = row[column.key];
              return (
                <div
                  key={column.key}
                  className="rounded-xl px-3 py-2 text-center font-mono-ui text-sm text-slate-800"
                  style={{ backgroundColor: `rgba(15, 118, 110, ${0.08 + (value / max) * 0.62})` }}
                >
                  {value}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function EnergyTrace() {
  const values = dftData.trajectory;
  const min = Math.min(...values.map((item) => item.energy));
  const max = Math.max(...values.map((item) => item.energy));
  const span = max - min || 1;
  const points = values.map((item, index) => {
    const x = 24 + (index / (values.length - 1)) * 452;
    const y = 182 - ((item.energy - min) / span) * 136;
    return { ...item, x, y };
  });

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-[28px] border border-white/80 bg-[linear-gradient(180deg,#fbfdff_0%,#f2f7f9_100%)] p-4">
      <div className="inline-flex w-fit shrink-0 rounded-full border border-white/80 bg-white/80 px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm">
        {dftData.selectedMol} · {values.length} energy points
      </div>
      <div className="flex aspect-[13/9] min-h-[360px] flex-1 items-center">
        <svg viewBox="0 0 500 220" className="h-full w-full" preserveAspectRatio="xMidYMid meet">
        <line x1="24" y1="182" x2="476" y2="182" stroke="#cbd5e1" />
        <line x1="24" y1="42" x2="24" y2="182" stroke="#cbd5e1" />
        <polyline
          points={points.map((point) => `${point.x},${point.y}`).join(" ")}
          fill="none"
          stroke="#0f766e"
          strokeWidth="4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {points.map((point) => (
          <circle key={point.step} cx={point.x} cy={point.y} r="5" fill="#0f766e" stroke="white" strokeWidth="2" />
        ))}
        <text x="24" y="206" className="fill-slate-500 text-[11px]">
          step 1
        </text>
        <text x="424" y="206" className="fill-slate-500 text-[11px]">
          step 18
        </text>
        </svg>
      </div>
      <div className="grid shrink-0 gap-3 sm:grid-cols-3">
        <MetricPill label="min energy" value={formatNumber(min, 6)} />
        <MetricPill label="max energy" value={formatNumber(max, 6)} />
        <MetricPill label="final step" value={String(dftData.selectedStep)} />
      </div>
    </div>
  );
}

function atomStyle(atom: number) {
  if (atom === 1) return { label: "H", color: "#cbd5e1", radius: 5 };
  if (atom === 6) return { label: "C", color: "#334155", radius: 8 };
  if (atom === 7) return { label: "N", color: "#7c3aed", radius: 8 };
  if (atom === 8) return { label: "O", color: "#2563eb", radius: 8 };
  if (atom === 16) return { label: "S", color: "#f59e0b", radius: 10 };
  return { label: String(atom), color: "#64748b", radius: 7 };
}

function distance(a: AtomCoordinate, b: AtomCoordinate) {
  return Math.hypot(a[1] - b[1], a[2] - b[2], a[3] - b[3]);
}

function dftBonds() {
  const bonds: { from: number; to: number }[] = [];
  dftData.coordinates.forEach((a, from) => {
    dftData.coordinates.slice(from + 1).forEach((b, offset) => {
      const to = from + offset + 1;
      const threshold = a[0] === 1 || b[0] === 1 ? 1.22 : 1.72;
      if (distance(a, b) <= threshold) {
        bonds.push({ from, to });
      }
    });
  });
  return bonds;
}

function toMolBlock(coordinates: AtomCoordinate[], bonds: { from: number; to: number }[]) {
  const atomLines = coordinates.map((coord) => {
    const element = atomStyle(coord[0]).label;
    const x = coord[1].toFixed(4).padStart(10);
    const y = coord[2].toFixed(4).padStart(10);
    const z = coord[3].toFixed(4).padStart(10);
    return `${x}${y}${z} ${element.padEnd(3)} 0  0  0  0  0  0  0  0  0  0  0  0`;
  });
  const bondLines = bonds.map((bond) => {
    const from = String(bond.from + 1).padStart(3);
    const to = String(bond.to + 1).padStart(3);
    return `${from}${to}  1  0  0  0  0`;
  });
  const counts = `${String(coordinates.length).padStart(3)}${String(bonds.length).padStart(3)}  0  0  0  0            999 V2000`;

  return [
    dftData.selectedMol,
    "  PolyProp DFT",
    "",
    counts,
    ...atomLines,
    ...bondLines,
    "M  END"
  ].join("\n");
}

function DftMolecule3D() {
  const viewerRef = useRef<HTMLDivElement | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const molBlock = useMemo(() => {
    return toMolBlock(dftData.coordinates, dftBonds());
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function renderMolecule() {
      setIsLoading(true);
      setError(null);

      try {
        await loadScriptOnce(D3MOL_SRC, "3dmol-script");
        if (!window.$3Dmol) {
          throw new Error("3Dmol not available");
        }

        if (cancelled || !viewerRef.current) {
          return;
        }

        viewerRef.current.innerHTML = "";
        const viewer = window.$3Dmol.createViewer(viewerRef.current, {
          backgroundColor: "#f8fbff"
        });
        viewer.addModel(molBlock, "mol");
        viewer.setStyle(
          {},
          {
            stick: { radius: 0.17, color: "0x64748b" },
            sphere: { scale: 0.32, colorscheme: "Jmol" }
          }
        );
        viewer.zoomTo();
        viewer.render();
      } catch (nextError) {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : "3D 构象加载失败");
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    void renderMolecule();

    return () => {
      cancelled = true;
      if (viewerRef.current) {
        viewerRef.current.innerHTML = "";
      }
    };
  }, [molBlock]);

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-[28px] border border-white/80 bg-[radial-gradient(circle_at_30%_15%,rgba(56,189,248,0.22),transparent_30%),radial-gradient(circle_at_76%_24%,rgba(245,158,11,0.16),transparent_26%),linear-gradient(180deg,#fbfdff_0%,#edf5f8_100%)] p-4">
      <div className="inline-flex w-fit shrink-0 rounded-full border border-white/80 bg-white/80 px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm">
        {dftData.selectedMol} · step {dftData.selectedStep}
      </div>
      <div className="relative flex aspect-[13/9] min-h-[360px] flex-1 items-center overflow-hidden rounded-[24px]">
        <div ref={viewerRef} className="absolute inset-0 cursor-grab active:cursor-grabbing" />
        {isLoading ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="rounded-full border border-white/80 bg-white/90 px-4 py-2 text-sm font-medium text-slate-700 shadow-sm">
              正在加载 3D 构象
            </div>
          </div>
        ) : null}
        {error ? (
          <div className="absolute inset-0 flex items-center justify-center p-6">
            <div className="max-w-[85%] rounded-2xl border border-slate-200 bg-white px-5 py-4 text-center text-sm font-medium leading-6 text-slate-700 shadow-sm">
              {error}
            </div>
          </div>
        ) : null}
      </div>
      <div className="grid shrink-0 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricPill label="atoms" value={String(dftData.selectedAtoms)} />
        <MetricPill label="energy" value={formatNumber(dftData.finalEnergy, 4)} />
        <MetricPill label="gap eV" value={formatNumber(dftData.finalGap, 3)} />
        <MetricPill label="dipole" value={formatNumber(dftData.finalDipole, 3)} />
      </div>
    </div>
  );
}

function DatasetTile({
  dataset,
  onOpen
}: {
  dataset: (typeof datasets)[number];
  onOpen: (key: DatasetKey) => void;
}) {
  const isReserved = dataset.status === "Reserved";

  return (
    <section
      className={[
        "flex min-h-[254px] flex-col justify-between rounded-[28px] border p-5 transition-all duration-300",
        isReserved
          ? "border-white/70 bg-white/55 text-slate-500"
          : "border-white/80 bg-white/85 text-slate-950 shadow-sm hover:-translate-y-1 hover:shadow-panel"
      ].join(" ")}
      aria-disabled={isReserved}
    >
      <div>
        <div className="flex items-start justify-between gap-4">
          <div
            className={[
              "flex h-12 w-12 items-center justify-center rounded-2xl border shadow-sm",
              isReserved ? "border-slate-200 bg-slate-100 text-slate-400" : "border-white bg-slate-950 text-white"
            ].join(" ")}
          >
            {dataset.icon}
          </div>
          <div className="text-right">
            <div className="font-mono-ui text-xs text-mutedForeground">{dataset.order}</div>
            <Badge className={isReserved ? "mt-2 bg-slate-100 text-slate-500" : "mt-2 bg-teal-50 text-teal-800"}>
              {dataset.status}
            </Badge>
          </div>
        </div>
        <div className="mt-6 text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">
          {dataset.englishTitle}
        </div>
        <h3 className="font-heading mt-3 text-[1.4rem] font-semibold tracking-tight text-slate-950">{dataset.title}</h3>
        <p className="mt-3 text-sm leading-6 text-mutedForeground">{dataset.description}</p>
        <div className="mt-5 grid grid-cols-2 gap-3">
          <MetricPill label="Records" value={formatCount(dataset.recordCount)} />
          <MetricPill label="Fields" value={dataset.fieldCount === null ? "Reserved" : String(dataset.fieldCount)} />
        </div>
      </div>
      <button
        type="button"
        disabled={isReserved}
        onClick={() => onOpen(dataset.key)}
        className={[
          "mt-7 inline-flex min-h-11 items-center justify-between rounded-2xl px-4 text-sm font-semibold transition-all duration-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          isReserved ? "cursor-not-allowed bg-slate-100 text-slate-400" : "bg-slate-950 text-white hover:bg-teal-700"
        ].join(" ")}
      >
        <span>{isReserved ? "预留接口" : "查看分析图"}</span>
        <ArrowRight className="h-4 w-4" />
      </button>
    </section>
  );
}

function DatabaseHome({ onBackHome, onOpenDataset }: { onBackHome: () => void; onOpenDataset: (key: DatasetKey) => void }) {
  const readyDatasets = datasets.filter((dataset) => dataset.status === "Ready");
  const totalRecords = readyDatasets.reduce((sum, dataset) => sum + (dataset.recordCount ?? 0), 0);

  return (
    <>
      <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex items-center gap-3">
          <Button type="button" variant="outline" onClick={onBackHome}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            首页
          </Button>
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">当前模块</div>
            <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">数据库分析</div>
          </div>
        </div>
        <Badge className="bg-teal-50 text-teal-800">Database Analytics</Badge>
      </nav>

      <section className="hero-glow relative overflow-hidden rounded-[36px] border border-white/70 bg-slate-950 px-6 py-7 text-white md:px-8 md:py-9">
        <div className="pointer-events-none absolute inset-y-0 right-0 w-[42%] bg-[radial-gradient(circle_at_center,rgba(45,212,191,0.26),transparent_60%)]" />
        <div className="relative grid gap-8 lg:grid-cols-[minmax(0,1fr)_360px] lg:items-center">
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-200">Polymer Data Platform</div>
            <h1 className="font-heading mt-4 text-[2.55rem] font-semibold leading-none tracking-[-0.02em] md:text-[4rem]">
              聚合物数据平台
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-7 text-slate-300">
              入口页展示五类数据模块；进入模块后只展示基于真实 CSV 汇总出的分析图，不展示原始表格。
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <MetricPill label="Ready" value={`${readyDatasets.length} modules`} />
            <MetricPill label="Reserved" value="1 module" />
            <MetricPill label="Records" value={formatCount(totalRecords)} />
            <MetricPill label="Raw tables" value="Hidden" />
          </div>
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2 xl:grid-cols-5">
        {datasets.map((dataset) => (
          <DatasetTile key={dataset.key} dataset={dataset} onOpen={onOpenDataset} />
        ))}
      </section>
    </>
  );
}

function DatasetHero({
  dataset,
  onBackHome,
  onBackDatabase,
  children,
  hideDescription = false
}: {
  dataset: (typeof datasets)[number];
  onBackHome: () => void;
  onBackDatabase: () => void;
  children: ReactNode;
  hideDescription?: boolean;
}) {
  return (
    <>
      <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" variant="outline" onClick={onBackDatabase}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            数据库分析
          </Button>
          <Button type="button" variant="outline" onClick={onBackHome}>
            首页
          </Button>
        </div>
        <Badge className="bg-teal-50 text-teal-800">{dataset.englishTitle}</Badge>
      </nav>

      <section className="hero-glow mesh-surface rounded-[36px] border border-white/70 px-6 py-7 md:px-8 md:py-9">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_420px] lg:items-center">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-sm">
                {dataset.icon}
              </div>
              <Badge>{dataset.order}</Badge>
              <Badge className="bg-teal-50 text-teal-800">{dataset.status}</Badge>
            </div>
            <h1 className="font-heading mt-5 text-[2.35rem] font-semibold leading-none tracking-[-0.02em] text-slate-950 md:text-[3.6rem]">
              {dataset.title}
            </h1>
            {hideDescription ? null : (
              <p className="mt-4 max-w-2xl text-base leading-7 text-slate-600">{dataset.description}</p>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <MetricPill label="Records" value={formatCount(dataset.recordCount)} />
            <MetricPill label="Fields" value={dataset.fieldCount === null ? "Reserved" : String(dataset.fieldCount)} />
            <MetricPill label="Source" value="CSV summary" />
            <MetricPill label="Raw Data" value="Hidden" />
          </div>
        </div>
      </section>

      {children}
    </>
  );
}

function ProcessPage(props: { onBackHome: () => void; onBackDatabase: () => void }) {
  const dataset = datasets[0];
  return (
    <DatasetHero dataset={dataset} {...props}>
      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.12fr)_minmax(0,0.88fr)]">
        <ChartPanel icon={<BarChart3 className="h-4 w-4" />} title="工艺流程高频词">
          <HorizontalBars data={processData.topTerms} />
        </ChartPanel>
        <ChartPanel icon={<Atom className="h-4 w-4" />} title="材料实体气泡图">
          <BubbleCloud data={processData.topMaterials} />
        </ChartPanel>
      </section>
      <section className="grid gap-5 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <ChartPanel icon={<TableProperties className="h-4 w-4" />} title="产物名称排行">
          <HorizontalBars data={processData.topProducts} />
        </ChartPanel>
        <ChartPanel icon={<Layers3 className="h-4 w-4" />} title="文本覆盖摘要">
          <div className="grid gap-4 sm:grid-cols-2">
            <MetricPill label="unique polymer ids" value={formatCount(processData.uniqueRecordIds)} />
            <MetricPill label="unique polymers" value={formatCount(processData.uniquePolymers)} />
            <MetricPill label="unique products" value={formatCount(processData.uniqueProducts)} />
            <MetricPill label="avg process chars" value={formatNumber(processData.avgProcessTextLength, 1)} />
          </div>
        </ChartPanel>
      </section>
    </DatasetHero>
  );
}

function PropertyPage(props: { onBackHome: () => void; onBackDatabase: () => void }) {
  const dataset = datasets[1];
  return (
    <DatasetHero dataset={dataset} {...props}>
      <section className="grid gap-5 xl:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
        <ChartPanel icon={<PieChart className="h-4 w-4" />} title="性质类别占比">
          <DonutChart data={propertyData.categories} />
        </ChartPanel>
        <ChartPanel icon={<BarChart3 className="h-4 w-4" />} title="高频性质名称">
          <HorizontalBars data={propertyData.topProperties} />
        </ChartPanel>
      </section>
      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
        <ChartPanel icon={<Sigma className="h-4 w-4" />} title="高频性质值域范围">
          <RangePlot data={propertyData.ranges} />
        </ChartPanel>
        <ChartPanel icon={<Search className="h-4 w-4" />} title="类别代表性质">
          <HorizontalBars data={propertyData.categoryTop} />
        </ChartPanel>
      </section>
    </DatasetHero>
  );
}

function StructureEffectPage(props: { onBackHome: () => void; onBackDatabase: () => void }) {
  const dataset = datasets[2];
  return (
    <DatasetHero dataset={dataset} {...props}>
      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
        <ChartPanel icon={<Network className="h-4 w-4" />} title="性质-来源矩阵">
          <SourceMatrix />
        </ChartPanel>
        <ChartPanel icon={<BarChart3 className="h-4 w-4" />} title="九类性质样本量">
          <HorizontalBars data={structureEffectData.properties} />
        </ChartPanel>
      </section>
      <section className="grid gap-5 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <ChartPanel icon={<PieChart className="h-4 w-4" />} title="单位分布">
          <DonutChart data={structureEffectData.units} />
        </ChartPanel>
        <ChartPanel icon={<Sigma className="h-4 w-4" />} title="构效性质值域">
          <RangePlot data={structureEffectData.ranges} />
        </ChartPanel>
      </section>
    </DatasetHero>
  );
}

function DftPage(props: { onBackHome: () => void; onBackDatabase: () => void }) {
  const dataset = datasets[3];
  return (
    <DatasetHero dataset={dataset} {...props}>
      <section className="grid gap-5 xl:grid-cols-2 xl:items-stretch">
        <ChartPanel
          className="flex h-full flex-col"
          bodyClassName="flex flex-1 flex-col"
          icon={<Orbit className="h-4 w-4" />}
          title="DFT 3D 构象图"
        >
          <DftMolecule3D />
        </ChartPanel>
        <ChartPanel
          className="flex h-full flex-col"
          bodyClassName="flex flex-1 flex-col"
          icon={<BarChart3 className="h-4 w-4" />}
          title="优化能量轨迹"
        >
          <EnergyTrace />
        </ChartPanel>
      </section>
      <section className="grid gap-5 xl:grid-cols-3">
        <ChartPanel icon={<PieChart className="h-4 w-4" />} title="原子组成">
          <DonutChart data={dftData.atomTotals} />
        </ChartPanel>
        <ChartPanel icon={<Database className="h-4 w-4" />} title="is_converged 原始编码分布">
          <DonutChart data={dftData.convergence} />
        </ChartPanel>
        <ChartPanel icon={<TableProperties className="h-4 w-4" />} title="分子最终态摘要">
          <div className="space-y-3">
            {dftData.moleculeFinals.map((item) => (
              <div key={item.label} className="rounded-2xl border border-white/80 bg-white/75 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="truncate text-sm font-semibold text-slate-800">{item.label}</div>
                  <div className="font-mono-ui text-xs text-slate-500">{item.steps} steps</div>
                </div>
                <div className="mt-2 grid grid-cols-3 gap-2 font-mono-ui text-[11px] text-slate-500">
                  <span>{item.atoms} atoms</span>
                  <span>{formatNumber(item.energy, 3)}</span>
                  <span>{formatNumber(item.gap, 3)} eV</span>
                </div>
              </div>
            ))}
          </div>
        </ChartPanel>
      </section>
    </DatasetHero>
  );
}

export function DatabaseAnalysis({ onBackHome }: { onBackHome: () => void }) {
  const [selectedKey, setSelectedKey] = useState<DatasetKey | null>(null);
  const commonProps = {
    onBackHome,
    onBackDatabase: () => setSelectedKey(null)
  };

  if (selectedKey === "process") return <ProcessPage {...commonProps} />;
  if (selectedKey === "property") return <PropertyPage {...commonProps} />;
  if (selectedKey === "structureEffect") return <StructureEffectPage {...commonProps} />;
  if (selectedKey === "dft") return <DftPage {...commonProps} />;

  return <DatabaseHome onBackHome={onBackHome} onOpenDataset={setSelectedKey} />;
}
