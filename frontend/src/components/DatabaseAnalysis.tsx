import { type FormEvent, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Atom,
  BarChart3,
  ChevronLeft,
  ChevronRight,
  Database,
  Filter,
  FlaskConical,
  Layers3,
  Network,
  Orbit,
  PieChart,
  Plus,
  RefreshCw,
  Search,
  Sigma,
  TableProperties,
  Trash2,
  X
} from "lucide-react";
import {
  browseDftEnergySteps,
  browseDftMolecules,
  browseExperimentalProcessRecords,
  browseExperimentalPropertyRecords,
  browseFormulationRecords,
  browseStructurePropertyRecords,
  fetchDatabaseAnalytics,
  fetchDatabaseDatasetSummary,
  fetchDftMolecule,
  fetchDftPcaSample,
  fetchPropertyFilterOptions,
  searchPropertyFilterRecords
} from "../services/api";
import type {
  DatasetSummaryResponse,
  DftEnergyStepBrowseResponse,
  DftMoleculeBrowseResponse,
  DftMoleculeDetail,
  DftPcaPoint,
  ExperimentalProcessBrowseResponse,
  ExperimentalPropertyBrowseResponse,
  FormulationBrowseResponse,
  PropertyFilterCondition,
  PropertyFilterOption,
  PropertyFilterOptionsResponse,
  PropertyFilterSearchResponse,
  StructurePropertyBrowseResponse
} from "../types";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

export type DatasetKey = "process" | "property" | "structureEffect" | "propertyFilter" | "dft" | "formulation";

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
type CoverageItem = { label: string; value: number; total: number };
type HistogramBin = { start: number; end: number; value: number };
type OrbitalDistribution = {
  label: string;
  color: string;
  count: number;
  min: number;
  p5: number;
  median: number;
  p95: number;
  max: number;
  bins: HistogramBin[];
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
  processSignalSummary: {
    extractedRows: 2293,
    uniqueSnippets: 658,
    medianChars: 347
  },
  processSignals: [
    { label: "temperature", value: 371, total: 658 },
    { label: "time", value: 352, total: 658 },
    { label: "solvent", value: 305, total: 658 },
    { label: "dried", value: 215, total: 658 },
    { label: "added", value: 157, total: 658 },
    { label: "stirred", value: 144, total: 658 },
    { label: "vacuum", value: 115, total: 658 },
    { label: "washed", value: 104, total: 658 }
  ],
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
  rows: 82041,
  uniqueSmiles: 26017,
  properties: [
    { label: "Tg K", value: 16942 },
    { label: "Glass transition temperature", value: 8191 },
    { label: "Thermal decomposition temperature", value: 6395 },
    { label: "Thermal decomposition weight loss", value: 5293 },
    { label: "Melting temperature", value: 3902 },
    { label: "bandgap (chain)", value: 3380 },
    { label: "Intrinsic viscosity eta", value: 1994 },
    { label: "Density", value: 1756 },
    { label: "Specific volume", value: 1756 }
  ],
  units: [
    { label: "C", value: 21301, color: "#0f766e" },
    { label: "K", value: 19354, color: "#2563eb" },
    { label: "%", value: 8137, color: "#f59e0b" },
    { label: "eV", value: 4931, color: "#e11d48" },
    { label: "not labeled", value: 4494, color: "#64748b" },
    { label: "Barrer", value: 3964, color: "#7c3aed" },
    { label: "GPa", value: 3837, color: "#14b8a6" }
  ],
  sources: [
    { label: "experimental", value: 62721, color: "#0f766e" },
    { label: "simulated", value: 19320, color: "#2563eb" },
    { label: "not labeled", value: 0, color: "#64748b" }
  ],
  sourceMatrix: [
    { label: "Tg K", exp: 8471, sim: 8471, na: 0 },
    { label: "Tg C", exp: 8191, sim: 0, na: 0 },
    { label: "Td", exp: 6395, sim: 0, na: 0 },
    { label: "Td loss", exp: 5293, sim: 0, na: 0 },
    { label: "Tm", exp: 3902, sim: 0, na: 0 },
    { label: "bandgap", exp: 0, sim: 3380, na: 0 },
    { label: "viscosity", exp: 1994, sim: 0, na: 0 },
    { label: "density", exp: 1756, sim: 0, na: 0 },
    { label: "specific volume", exp: 1756, sim: 0, na: 0 }
  ],
  ranges: [
    { label: "Tg K", count: 16942, min: 130, median: 409.15, max: 975.15 },
    { label: "Glass transition temperature", count: 8191, min: -123, median: 138, max: 495 },
    { label: "Thermal decomposition temperature", count: 6395, min: 18, median: 403, max: 1000 },
    { label: "Thermal decomposition weight loss", count: 5293, min: 0, median: 5.25, max: 100 },
    { label: "Melting temperature", count: 3902, min: -54, median: 186, max: 580 }
  ]
};

const dftCoordinates: AtomCoordinate[] = [
  [7, -6.92476, 3.205737, 3.651556],
  [7, -6.309176, 2.752159, 2.837125],
  [7, -5.662965, 2.310025, 1.878159],
  [6, -4.819618, 1.21389, 2.206058],
  [8, -4.726147, 0.742038, 3.30702],
  [6, -4.095055, 0.754729, 1.01891],
  [6, -4.090362, 1.169018, -0.285262],
  [6, -3.172228, 0.315688, -0.964873],
  [6, -2.697575, -0.543275, -0.012372],
  [6, -1.715775, -1.661341, -0.077112],
  [8, -1.257874, -1.752789, -1.401161],
  [6, -0.23127, -2.675159, -1.574042],
  [8, 0.93261, -2.30842, -0.906889],
  [6, 1.515604, -1.129552, -1.39742],
  [6, 2.703846, -0.824653, -0.552643],
  [6, 3.268921, -1.479957, 0.50618],
  [6, 4.390325, -0.688207, 0.890995],
  [6, 4.411393, 0.37747, 0.034507],
  [6, 5.346908, 1.502647, -0.028911],
  [8, 6.2795, 1.63377, 0.721871],
  [7, 5.031408, 2.412346, -1.06741],
  [7, 5.807155, 3.375269, -1.142214],
  [7, 6.458986, 4.272079, -1.277145],
  [8, 3.383171, 0.292873, -0.84359],
  [8, -3.246573, -0.286994, 1.181316],
  [1, -4.680595, 1.986886, -0.68805],
  [1, -2.888756, 0.317406, -2.012096],
  [1, -2.198742, -2.605794, 0.240033],
  [1, -0.882193, -1.468711, 0.621398],
  [1, -0.509297, -3.662855, -1.170619],
  [1, -0.064439, -2.736792, -2.662249],
  [1, 1.825387, -1.253169, -2.453454],
  [1, 0.803512, -0.285966, -1.36276],
  [1, 2.911306, -2.409003, 0.937515],
  [1, 5.103367, -0.861391, 1.691896]
];

const dftData = {
  rows: 1582959,
  molCount: 49301,
  selectedMol: "987779_Conf02",
  selectedStep: 201,
  selectedAtoms: 35,
  finalEnergy: -1279.63469116,
  finalGap: 114.247335,
  finalDipole: 4.5614,
  finalLowestFreq: 5.7387,
  energyRange: { min: -3945.714671, median: -1561.785924, max: -381.881933, count: 48540 },
  gapRange: { min: 97.190685, median: 108.959888, max: 119.045793, count: 49301 },
  orbitalDistributions: [
    {
      label: "HOMO",
      color: "#0f766e",
      count: 49301,
      min: -9.899235206,
      p5: -8.286687642,
      median: -7.320410828,
      p95: -6.40420299,
      max: -5.464321234,
      bins: [
        { start: -9.899235, end: -9.529659, value: 6 },
        { start: -9.529659, end: -9.160083, value: 49 },
        { start: -9.160083, end: -8.790507, value: 296 },
        { start: -8.790507, end: -8.420931, value: 1150 },
        { start: -8.420931, end: -8.051354, value: 3561 },
        { start: -8.051354, end: -7.681778, value: 8211 },
        { start: -7.681778, end: -7.312202, value: 11703 },
        { start: -7.312202, end: -6.942626, value: 12535 },
        { start: -6.942626, end: -6.57305, value: 6821 },
        { start: -6.57305, end: -6.203474, value: 4072 },
        { start: -6.203474, end: -5.833897, value: 855 },
        { start: -5.833897, end: -5.464321, value: 42 }
      ]
    },
    {
      label: "LUMO",
      color: "#2563eb",
      count: 49301,
      min: 89.973405644,
      p5: 94.840436648,
      median: 101.555121712,
      p95: 107.438498506,
      max: 110.977885304,
      bins: [
        { start: 89.973406, end: 91.723779, value: 91 },
        { start: 91.723779, end: 93.474152, value: 554 },
        { start: 93.474152, end: 95.224526, value: 2115 },
        { start: 95.224526, end: 96.974899, value: 913 },
        { start: 96.974899, end: 98.725272, value: 2293 },
        { start: 98.725272, end: 100.475645, value: 6828 },
        { start: 100.475645, end: 102.226019, value: 17399 },
        { start: 102.226019, end: 103.976392, value: 10425 },
        { start: 103.976392, end: 105.726765, value: 3442 },
        { start: 105.726765, end: 107.477139, value: 2879 },
        { start: 107.477139, end: 109.227512, value: 2235 },
        { start: 109.227512, end: 110.977885, value: 127 }
      ]
    }
  ] satisfies OrbitalDistribution[],
  stepRange: { min: 5, median: 26, p95: 76, max: 202 },
  atomRange: { min: 9, median: 52, p95: 77, max: 100 },
  atomTotals: [
    { label: "C", value: 1099029, color: "#334155" },
    { label: "H", value: 1043640, color: "#94a3b8" },
    { label: "O", value: 294304, color: "#2563eb" },
    { label: "N", value: 97377, color: "#7c3aed" },
    { label: "F", value: 39587, color: "#14b8a6" },
    { label: "S", value: 9743, color: "#f59e0b" },
    { label: "Cl", value: 4336, color: "#0f766e" },
    { label: "B", value: 1701, color: "#e11d48" },
    { label: "P", value: 1431, color: "#64748b" },
    { label: "Si", value: 1398, color: "#38bdf8" }
  ],
  convergence: [
    { label: "44", value: 28074, color: "#0f766e" },
    { label: "34", value: 16748, color: "#2563eb" },
    { label: "24", value: 3718, color: "#f59e0b" },
    { label: "blank", value: 761, color: "#64748b" }
  ],
  trajectory: [
    { step: 1, energy: -1279.61882133 },
    { step: 2, energy: -1279.63373601 },
    { step: 3, energy: -1279.6346027 },
    { step: 4, energy: -1279.63428792 },
    { step: 5, energy: -1279.63406367 },
    { step: 6, energy: -1279.63310735 },
    { step: 7, energy: -1279.63404447 },
    { step: 8, energy: -1279.63007818 },
    { step: 9, energy: -1279.63380553 },
    { step: 10, energy: -1279.63186288 },
    { step: 20, energy: -1279.63313271 },
    { step: 30, energy: -1279.63322141 },
    { step: 40, energy: -1279.63330445 },
    { step: 50, energy: -1279.63338241 },
    { step: 60, energy: -1279.63345536 },
    { step: 70, energy: -1279.63352309 },
    { step: 80, energy: -1279.63358529 },
    { step: 201, energy: -1279.63469116 }
  ],
  coordinates: dftCoordinates
};

const formulationData = {
  files: 143,
  rows: 65840,
  coverage: [
    { label: "Polymer IUPAC", count: 60942, pct: 92.6 },
    { label: "Formula and dosage", count: 61092, pct: 92.8 },
    { label: "Catalyst", count: 30187, pct: 45.8 },
    { label: "Temperature", count: 46620, pct: 70.8 },
    { label: "Time", count: 37267, pct: 56.6 },
    { label: "Solvent", count: 30970, pct: 47.0 }
  ],
  componentCounts: [
    { label: "1", value: 122 },
    { label: "2", value: 35567 },
    { label: "3", value: 10934 },
    { label: "4", value: 4772 },
    { label: "5", value: 2085 },
    { label: "6", value: 1023 },
    { label: "7", value: 575 },
    { label: "8+", value: 914 }
  ],
  topComponents: [
    { label: "Ethylene glycol", value: 3090 },
    { label: "Terephthalic acid", value: 2958 },
    { label: "Epoxy resin", value: 2117 },
    { label: "Isocyanate", value: 1956 },
    { label: "Polyol", value: 1848 },
    { label: "Diisocyanate", value: 1675 },
    { label: "PET", value: 1305 },
    { label: "Polyisocyanate", value: 1239 },
    { label: "1,4-Butanediol", value: 1188 },
    { label: "Polyurethane", value: 1179 },
    { label: "Polyether polyol", value: 998 },
    { label: "Styrene", value: 981 }
  ],
  polymerFamilies: [
    { label: "polyolefin", value: 17513, color: "#0f766e" },
    { label: "polyurethane", value: 16676, color: "#2563eb" },
    { label: "polyester", value: 13069, color: "#f59e0b" },
    { label: "other", value: 11546, color: "#64748b" },
    { label: "polyamide / aramid", value: 5430, color: "#e11d48" },
    { label: "acrylic", value: 4297, color: "#7c3aed" },
    { label: "epoxy", value: 3180, color: "#14b8a6" },
    { label: "polyimide", value: 2490, color: "#38bdf8" }
  ],
  ratioTypes: [
    { label: "ratio colon", value: 61062 },
    { label: "range", value: 11657 },
    { label: "not specified", value: 5557 },
    { label: "missing", value: 4748 },
    { label: "percent", value: 3129 },
    { label: "molar", value: 2578 },
    { label: "parts / wt", value: 1875 }
  ],
  tempBands: [
    { label: "<80 C", value: 13723 },
    { label: "80-119 C", value: 10188 },
    { label: "120-179 C", value: 9207 },
    { label: "180-239 C", value: 6232 },
    { label: ">=240 C", value: 6499 },
    { label: "missing", value: 19991 }
  ],
  timeUnits: [
    { label: "hours", value: 21130, color: "#0f766e" },
    { label: "minutes", value: 11085, color: "#2563eb" },
    { label: "days", value: 340, color: "#f59e0b" },
    { label: "other", value: 4712, color: "#7c3aed" },
    { label: "missing", value: 28573, color: "#64748b" }
  ],
  topCatalysts: [
    { label: "Dibutyltin dilaurate", value: 2288 },
    { label: "Triethylamine", value: 1179 },
    { label: "Sb2O3", value: 467 },
    { label: "Benzoyl peroxide", value: 433 },
    { label: "p-Toluenesulfonic acid", value: 405 },
    { label: "NaOH", value: 403 },
    { label: "Pd", value: 361 },
    { label: "Titanium butoxide", value: 347 },
    { label: "Sodium hydroxide", value: 323 },
    { label: "Stannous octoate", value: 317 }
  ],
  topSolvents: [
    { label: "Water", value: 5366 },
    { label: "DMF / dimethylformamide", value: 3346 },
    { label: "Toluene", value: 2247 },
    { label: "Acetone", value: 1488 },
    { label: "Ethanol", value: 1211 },
    { label: "Deionized water", value: 1176 },
    { label: "DMAc", value: 896 },
    { label: "Methanol", value: 822 },
    { label: "Tetrahydrofuran", value: 745 }
  ],
  examples: [
    {
      title: "Inner cylinder and manufacturing method",
      polymer: "poly(bisphenol A epoxy resin)",
      formula: "bisphenol A : epoxy resin = 1 : 1",
      condition: "235-280 C · 15-20 minutes"
    },
    {
      title: "Resin composition and laminated prepreg",
      polymer: "poly(bisphenol A epoxy resin)",
      formula: "bisphenol-type epoxy resin : novolac-type phenol resin = 20-60 : 15-60",
      condition: "190 C · 5 min · MEK and butyl cellosolve"
    }
  ]
};

const propertyFilterData = {
  rows: 615159
};

const datasets = [
  {
    key: "process" as const,
    order: "01",
    title: "Experimental Process Data",
    englishTitle: "Experimental Process",
    description: "Summarizes process actions, product names, and material entities from preparation records.",
    status: "Ready",
    recordCount: processData.rows,
    icon: <FlaskConical className="h-5 w-5" />
  },
  {
    key: "property" as const,
    order: "02",
    title: "Experimental Property Data",
    englishTitle: "Experimental Properties",
    description: "Summarizes property names, categories, value ranges, and representative measurements.",
    status: "Ready",
    recordCount: propertyData.rows,
    icon: <Sigma className="h-5 w-5" />
  },
  {
    key: "structureEffect" as const,
    order: "03",
    title: "Polymer Structure-Property Data",
    englishTitle: "Structure-Property",
    description: "Links polymer structures with key properties, measurement origins, units, and value ranges.",
    status: "Ready",
    recordCount: structureEffectData.rows,
    icon: <Network className="h-5 w-5" />
  },
  {
    key: "propertyFilter" as const,
    order: "04",
    title: "Property Threshold Filter",
    englishTitle: "Property Threshold",
    description: "Search polymers by standardized and raw property thresholds from the high-confidence standardized table.",
    status: "Ready",
    recordCount: propertyFilterData.rows,
    icon: <Filter className="h-5 w-5" />
  },
  {
    key: "dft" as const,
    order: "05",
    title: "DFT Conformation Data",
    englishTitle: "DFT Conformation",
    description: "Browse optimized molecular conformations, energy-change curves, atom composition, and orbital levels.",
    status: "Ready",
    recordCount: dftData.rows,
    icon: <Orbit className="h-5 w-5" />
  },
  {
    key: "formulation" as const,
    order: "06",
    title: "Formulation Ratio Data",
    englishTitle: "Formulation Ratios",
    description: "Summarizes formulation components, ratio expressions, catalysts, solvents, and conditions.",
    status: "Ready",
    recordCount: formulationData.rows,
    icon: <TableProperties className="h-5 w-5" />
  }
];

type DatasetDefinition = (typeof datasets)[number];
type DisplayDataset = Omit<DatasetDefinition, "status" | "recordCount"> & {
  status: "Ready" | "Reserved";
  recordCount: number | null;
  sourceStatus?: string;
  sourceMessage?: string | null;
  latestImportStatus?: string | null;
  latestImportFinishedAt?: string | null;
};

type DatabaseAnalyticsPayload = Partial<{
  process: typeof processData;
  property: typeof propertyData;
  structureEffect: typeof structureEffectData;
  propertyFilter: typeof propertyFilterData;
  dft: typeof dftData;
  formulation: typeof formulationData;
}>;

type DatasetPageProps = {
  onBackHome: () => void;
  onBackDatabase: () => void;
  dataset: DisplayDataset;
  analytics: DatabaseAnalyticsPayload | null;
  analyticsLoading: boolean;
  analyticsRefreshing: boolean;
  analyticsError: string | null;
  analyticsSource: string | null;
  analyticsGeneratedAt: string | null;
  onRefreshAnalytics: () => void;
};

function useDatabaseDatasetSummary() {
  const [summary, setSummary] = useState<DatasetSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchDatabaseDatasetSummary()
      .then((response) => {
        if (!cancelled) {
          setSummary(response);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setSummary(null);
          setError(nextError instanceof Error ? nextError.message : "Failed to load database source status");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { summary, loading, error };
}


function useDatabaseAnalytics() {
  const [analytics, setAnalytics] = useState<DatabaseAnalyticsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const loadAnalytics = useCallback((refresh = false) => {
    if (refresh) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);

    return fetchDatabaseAnalytics({ refresh })
      .then((response) => {
        if (!mountedRef.current) {
          return;
        }
        setAnalytics(response.datasets as DatabaseAnalyticsPayload);
        setSource(response.source);
        setGeneratedAt(response.generated_at ?? null);
      })
      .catch((nextError) => {
        if (!mountedRef.current) {
          return;
        }
        if (!refresh) {
          setAnalytics(null);
        }
        setError(nextError instanceof Error ? nextError.message : "Failed to load database analytics");
      })
      .finally(() => {
        if (!mountedRef.current) {
          return;
        }
        if (refresh) {
          setRefreshing(false);
        } else {
          setLoading(false);
        }
      });
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void loadAnalytics(false);
    return () => {
      mountedRef.current = false;
    };
  }, [loadAnalytics]);

  return { analytics, loading, refreshing, error, source, generatedAt, refresh: () => void loadAnalytics(true) };
}

function emptyAnalyticsValue<T>(value: T): T {
  if (Array.isArray(value)) {
    return [] as T;
  }
  if (typeof value === "number") {
    return 0 as T;
  }
  if (typeof value === "string") {
    return "" as T;
  }
  if (typeof value === "boolean") {
    return false as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, nestedValue]) => [key, emptyAnalyticsValue(nestedValue)])
    ) as T;
  }
  return value;
}

function mergeAnalyticsData<T extends object>(schema: T, override: Partial<T> | undefined): T {
  const emptySchema = emptyAnalyticsValue(schema);
  return override ? { ...emptySchema, ...override } : emptySchema;
}

function componentBucketValue(label: string) {
  if (label === "8+") return 8;
  const value = Number.parseInt(label, 10);
  return Number.isFinite(value) ? value : 0;
}

function averageComponentCount(data: typeof formulationData.componentCounts) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  if (!total) return "0";
  const weighted = data.reduce((sum, item) => sum + componentBucketValue(item.label) * item.value, 0);
  return formatNumber(weighted / total, 2);
}

function medianComponentCount(data: typeof formulationData.componentCounts) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  if (!total) return "0";
  const midpoint = total / 2;
  let running = 0;
  for (const item of data) {
    running += item.value;
    if (running >= midpoint) {
      return item.label;
    }
  }
  return data[data.length - 1]?.label ?? "0";
}

function resolveDatasets(summary: DatasetSummaryResponse | null, summaryError: string | null): DisplayDataset[] {
  if (summaryError) {
    return datasets.map((dataset) => ({
      ...dataset,
      recordCount: null,
      status: "Reserved" as const,
      sourceStatus: "unavailable",
      sourceMessage: summaryError
    }));
  }

  const summaryByKey = new Map(summary?.datasets.map((item) => [item.key, item]) ?? []);

  return datasets.map((dataset) => {
    const item = summaryByKey.get(dataset.key);
    if (!item) {
      return summary
        ? {
            ...dataset,
            recordCount: null,
            status: "Reserved" as const,
            sourceStatus: "unknown",
            sourceMessage: "Dataset was not reported by the source status endpoint."
          }
        : {
            ...dataset,
            recordCount: null,
            status: "Reserved" as const,
            sourceStatus: "loading",
            sourceMessage: "Checking database source status..."
          };
    }

    return {
      ...dataset,
      recordCount: item.total_records,
      status: item.source_status === "missing" ? ("Reserved" as const) : ("Ready" as const),
      sourceStatus: item.source_status,
      sourceMessage: item.source_message,
      latestImportStatus: item.latest_import_status,
      latestImportFinishedAt: item.latest_import_finished_at
    };
  });
}


function getDisplayDataset(displayDatasets: DisplayDataset[], key: DatasetKey): DisplayDataset {
  const match = displayDatasets.find((dataset) => dataset.key === key);
  if (match) {
    return match;
  }

  const fallback = datasets.find((dataset) => dataset.key === key);
  if (!fallback) {
    throw new Error(`Unknown dataset key: ${key}`);
  }

  return {
    ...fallback,
    recordCount: null,
    status: "Reserved" as const,
    sourceStatus: "unknown",
    sourceMessage: "Dataset was not reported by the source status endpoint."
  };
}

function formatImportMeta(dataset: DisplayDataset) {
  if (dataset.latestImportStatus) {
    const importedAt = dataset.latestImportFinishedAt ? new Date(dataset.latestImportFinishedAt) : null;
    const timeLabel = importedAt && !Number.isNaN(importedAt.getTime()) ? importedAt.toLocaleString() : null;
    return timeLabel ? `Import ${dataset.latestImportStatus} - ${timeLabel}` : `Import ${dataset.latestImportStatus}`;
  }

  return dataset.sourceMessage ?? (dataset.status === "Reserved" ? "Source unavailable" : "Import metadata pending");
}

function formatCount(value: number | null) {
  return value === null ? "Reserved" : new Intl.NumberFormat("en-US").format(value);
}

function formatNumber(value: number, digits = 2) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);
}

function formatPercent(value: number) {
  return `${formatNumber(value, 1)}%`;
}

function MetricPill({
  label,
  value,
  onClick,
  ariaLabel
}: {
  label: string;
  value: string;
  onClick?: () => void;
  ariaLabel?: string;
}) {
  const className =
    "flex h-16 flex-col justify-center rounded-2xl border border-white/80 bg-white/75 px-3 py-2 text-left shadow-sm";
  const content = (
    <>
      <div className="text-[10px] font-medium uppercase tracking-[0.16em] text-mutedForeground">{label}</div>
      <div className="font-heading mt-1 truncate text-base font-semibold text-slate-950">{value}</div>
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        aria-label={ariaLabel ?? `${label}: ${value}`}
        onClick={onClick}
        className={`${className} transition-all hover:-translate-y-0.5 hover:border-teal-200 hover:bg-teal-50/85 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring`}
      >
        {content}
      </button>
    );
  }

  return (
    <div className={className}>
      {content}
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

function ChartEmptyState({ message = "No chart data returned from Postgres." }: { message?: string }) {
  return (
    <div className="flex min-h-[180px] items-center justify-center rounded-[24px] border border-dashed border-slate-200 bg-slate-50/70 px-4 py-8 text-center text-sm leading-6 text-slate-500">
      {message}
    </div>
  );
}

function HorizontalBars({ data, valueLabel = "count" }: { data: RankedItem[]; valueLabel?: string }) {
  if (!data.length) {
    return <ChartEmptyState />;
  }
  const max = Math.max(1, ...data.map((item) => item.value));

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

function RankedBarsWithSummary({
  data,
  limit = 8,
  valueLabel = "count"
}: {
  data: RankedItem[];
  limit?: number;
  valueLabel?: string;
}) {
  const visible = data.slice(0, limit);
  const shownTotal = visible.reduce((sum, item) => sum + item.value, 0);
  const topThreeTotal = visible.slice(0, 3).reduce((sum, item) => sum + item.value, 0);
  const topItem = visible[0];

  return (
    <div className="flex h-full flex-1 flex-col justify-between gap-4">
      <HorizontalBars data={visible} valueLabel={valueLabel} />
      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label={`top ${visible.length} sum`} value={formatCount(shownTotal)} />
        <MetricPill label="top item" value={topItem?.label ?? "-"} />
        <MetricPill label="top 3 sum" value={formatCount(topThreeTotal)} />
      </div>
    </div>
  );
}

function CoverageSignals({
  data,
  summary
}: {
  data: CoverageItem[];
  summary: { extractedRows: number; uniqueSnippets: number; medianChars: number };
}) {
  return (
    <div className="flex h-full flex-1 flex-col justify-between gap-4">
      <div className="space-y-4">
        {data.map((item, index) => {
          const ratio = item.total > 0 ? (item.value / item.total) * 100 : 0;
          return (
            <div key={item.label} className="grid grid-cols-[minmax(7rem,10rem)_minmax(0,1fr)_5.75rem] items-center gap-3">
              <div className="truncate text-sm font-medium text-slate-700" title={item.label}>
                {item.label}
              </div>
              <div className="h-3 overflow-hidden rounded-full bg-slate-100">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.max(ratio, 4)}%`,
                    background: `linear-gradient(90deg, ${colors[index % colors.length]} 0%, #38bdf8 100%)`
                  }}
                />
              </div>
              <div className="font-mono-ui text-right text-xs text-slate-500">
                {formatPercent(ratio)}
              </div>
            </div>
          );
        })}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label="extracted rows" value={formatCount(summary.extractedRows)} />
        <MetricPill label="unique snippets" value={formatCount(summary.uniqueSnippets)} />
        <MetricPill label="median chars" value={formatCount(summary.medianChars)} />
      </div>
    </div>
  );
}

function CategoryRepresentativeCards({
  data,
  categories
}: {
  data: RankedItem[];
  categories: DonutItem[];
}) {
  const categoryMap = new Map(categories.map((item) => [item.label, item]));
  const representativeTotal = data.reduce((sum, item) => sum + item.value, 0);
  const topRepresentative = data[0]?.label.split(":").slice(1).join(":").trim() || data[0]?.label || "-";

  return (
    <div className="flex h-full flex-1 flex-col justify-between gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        {data.map((item, index) => {
          const [categoryName, ...propertyParts] = item.label.split(":");
          const category = categoryName.trim();
          const propertyName = propertyParts.join(":").trim() || item.label;
          const categoryInfo = categoryMap.get(category);
          const color = categoryInfo?.color ?? colors[index % colors.length];
          const share = categoryInfo ? (item.value / categoryInfo.value) * 100 : 0;

          return (
            <article key={item.label} className="rounded-2xl border border-white/80 bg-white/75 p-3 shadow-sm">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: color }} />
                  <span className="truncate text-xs font-semibold capitalize text-slate-600" title={category}>
                    {category}
                  </span>
                </div>
                <span className="font-mono-ui shrink-0 text-xs text-slate-500">{formatCount(item.value)}</span>
              </div>
              <div className="mt-2 truncate text-sm font-semibold text-slate-900" title={propertyName}>
                {propertyName}
              </div>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-100">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${categoryInfo ? Math.max(share, 4) : 100}%`,
                    background: `linear-gradient(90deg, ${color} 0%, #38bdf8 100%)`
                  }}
                />
              </div>
              <div className="font-mono-ui mt-2 text-[11px] text-slate-500">
                {categoryInfo ? `${formatPercent(share)} of category` : "representative count"}
              </div>
            </article>
          );
        })}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label="categories shown" value={`${data.length} / ${categories.length}`} />
        <MetricPill label="representative sum" value={formatCount(representativeTotal)} />
        <MetricPill label="top representative" value={topRepresentative} />
      </div>
    </div>
  );
}

function DonutChart({ data }: { data: DonutItem[] }) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  if (!data.length || total <= 0) {
    return <ChartEmptyState />;
  }
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

function DonutChartWithSummary({ data, topLabel = "top segment" }: { data: DonutItem[]; topLabel?: string }) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  if (!data.length || total <= 0) {
    return <ChartEmptyState />;
  }
  const topItem = data.reduce((current, item) => (item.value > current.value ? item : current), data[0]);
  const topThreeShare = data
    .slice(0, 3)
    .reduce((sum, item) => sum + item.value, 0) / total * 100;

  return (
    <div className="flex h-full flex-1 flex-col justify-between gap-4">
      <DonutChart data={data} />
      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label={topLabel} value={topItem.label} />
        <MetricPill label="top count" value={formatCount(topItem.value)} />
        <MetricPill label="top 3 share" value={formatPercent(topThreeShare)} />
      </div>
    </div>
  );
}

function AtomCompositionCompact({ data }: { data: DonutItem[] }) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  if (!data.length || total <= 0) {
    return <ChartEmptyState />;
  }
  const topItem = data.reduce((current, item) => (item.value > current.value ? item : current), data[0]);
  const topThreeShare = data.slice(0, 3).reduce((sum, item) => sum + item.value, 0) / total * 100;

  return (
    <div className="space-y-4">
      <div className="overflow-hidden rounded-full border border-white/80 bg-white/80 p-1 shadow-sm">
        <div className="flex h-4 overflow-hidden rounded-full bg-slate-100">
          {data.map((item) => (
            <div
              key={item.label}
              className="h-full"
              style={{ width: `${(item.value / total) * 100}%`, backgroundColor: item.color }}
              title={`${item.label}: ${formatCount(item.value)}`}
            />
          ))}
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {data.map((item) => {
          const share = (item.value / total) * 100;
          return (
            <div key={item.label} className="grid grid-cols-[1.75rem_minmax(0,1fr)_5.5rem] items-center gap-2 rounded-2xl bg-white/70 px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: item.color }} />
                <span className="font-mono-ui text-xs font-semibold text-slate-700">{item.label}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                <div className="h-full rounded-full" style={{ width: `${Math.max(share, 3)}%`, backgroundColor: item.color }} />
              </div>
              <div className="font-mono-ui text-right text-xs text-slate-500">{formatCount(item.value)}</div>
            </div>
          );
        })}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label="top atom" value={topItem.label} />
        <MetricPill label="top count" value={formatCount(topItem.value)} />
        <MetricPill label="top 3 share" value={formatPercent(topThreeShare)} />
      </div>
    </div>
  );
}

function OrbitalDistributionChart({ data }: { data: OrbitalDistribution[] }) {
  if (!data.length) {
    return <ChartEmptyState />;
  }
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      {data.map((series) => {
        const peak = Math.max(1, ...series.bins.map((bin) => bin.value));

        return (
          <div key={series.label} className="flex h-full flex-col gap-4 rounded-[24px] bg-[linear-gradient(180deg,#fbfdff_0%,#eef5f6_100%)] p-4">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h4 className="font-heading text-base font-semibold text-slate-950">{series.label} energy</h4>
                <div className="font-mono-ui mt-1 text-xs text-slate-500">{formatCount(series.count)} final states</div>
              </div>
              <span className="h-3 w-3 rounded-full" style={{ backgroundColor: series.color }} />
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <MetricPill label="P5 eV" value={formatNumber(series.p5, 3)} />
              <MetricPill label="Median eV" value={formatNumber(series.median, 3)} />
              <MetricPill label="P95 eV" value={formatNumber(series.p95, 3)} />
            </div>

            <div className="space-y-2">
              {series.bins.map((bin) => (
                <div key={`${series.label}-${bin.start}`} className="grid grid-cols-[7.25rem_minmax(0,1fr)_4.25rem] items-center gap-3">
                  <div className="font-mono-ui text-[11px] text-slate-500">
                    {formatNumber(bin.start, 1)} to {formatNumber(bin.end, 1)}
                  </div>
                  <div className="h-3 overflow-hidden rounded-full bg-white/90">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${Math.max((bin.value / peak) * 100, 2)}%`,
                        background: `linear-gradient(90deg, ${series.color} 0%, #38bdf8 100%)`
                      }}
                    />
                  </div>
                  <div className="font-mono-ui text-right text-xs text-slate-500">{formatCount(bin.value)}</div>
                </div>
              ))}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <MetricPill label="Min eV" value={formatNumber(series.min, 3)} />
              <MetricPill label="Max eV" value={formatNumber(series.max, 3)} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function BubbleCloud({ data }: { data: RankedItem[] }) {
  if (!data.length) {
    return <ChartEmptyState />;
  }
  const max = Math.max(1, ...data.map((item) => item.value));

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

function CoverageGrid({ data }: { data: typeof formulationData.coverage }) {
  if (!data.length) {
    return <ChartEmptyState message="No field coverage returned from Postgres." />;
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {data.map((item) => (
        <div key={item.label} className="rounded-2xl border border-white/80 bg-white/75 px-4 py-3 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <div className="truncate text-sm font-semibold text-slate-800" title={item.label}>
              {item.label}
            </div>
            <div className="font-mono-ui text-sm font-semibold text-teal-700">{formatPercent(item.pct)}</div>
          </div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full bg-teal-700" style={{ width: `${item.pct}%` }} />
          </div>
          <div className="font-mono-ui mt-2 text-xs text-slate-500">{formatCount(item.count)} records</div>
        </div>
      ))}
    </div>
  );
}

function ComponentDistribution({ data }: { data: RankedItem[] }) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  if (!data.length || total <= 0) {
    return <ChartEmptyState />;
  }
  const peak = data.reduce((current, item) => (item.value > current.value ? item : current), data[0]);

  return (
    <div className="rounded-[24px] bg-[linear-gradient(180deg,#fbfdff_0%,#eef5f6_100%)] p-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label="parsed formulas" value={formatCount(total)} />
        <MetricPill label="peak bucket" value={`${peak.label} components`} />
        <MetricPill label="peak share" value={formatPercent((peak.value / total) * 100)} />
      </div>

      <div className="mt-5 overflow-hidden rounded-full border border-white/80 bg-white/80 p-1 shadow-sm">
        <div className="flex h-5 overflow-hidden rounded-full bg-slate-100">
          {data.map((item, index) => (
            <div
              key={item.label}
              className="h-full"
              style={{
                width: `${(item.value / total) * 100}%`,
                backgroundColor: colors[index % colors.length]
              }}
              title={`${item.label} components: ${formatCount(item.value)}`}
            />
          ))}
        </div>
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {data.map((item, index) => {
          const share = (item.value / total) * 100;

          return (
            <div key={item.label} className="rounded-2xl border border-white/80 bg-white/75 px-3 py-2.5">
              <div className="flex items-center justify-between gap-3">
                <div className="text-sm font-semibold text-slate-800">{item.label} components</div>
                <div className="font-mono-ui text-xs text-slate-500">{formatPercent(share)}</div>
              </div>
              <div className="mt-2 flex items-center gap-3">
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full rounded-full"
                    style={{
                      width: `${Math.max(share, 3)}%`,
                      background: `linear-gradient(90deg, ${colors[index % colors.length]} 0%, #38bdf8 100%)`
                    }}
                  />
                </div>
                <div className="font-mono-ui w-16 text-right text-xs text-slate-500">{formatCount(item.value)}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function FormulaExamples({ data }: { data: typeof formulationData.examples }) {
  if (!data.length) {
    return <ChartEmptyState message="No formulation examples returned from Postgres." />;
  }
  return (
    <div className="grid flex-1 grid-rows-2 gap-3">
      {data.map((item, index) => (
        <article key={item.title} className="flex flex-col rounded-2xl border border-white/80 bg-white/75 px-4 py-4 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <h4 className="truncate text-sm font-semibold text-slate-900" title={item.title}>
              {item.title}
            </h4>
            <span className="font-mono-ui shrink-0 text-[11px] text-teal-700">EX {index + 1}</span>
          </div>
          <p className="mt-2 line-clamp-2 text-xs leading-5 text-slate-500">{item.polymer}</p>
          <div className="font-mono-ui mt-3 rounded-2xl bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-700">
            {item.formula}
          </div>
          <div className="font-mono-ui mt-auto pt-2 text-xs text-slate-500">{item.condition}</div>
        </article>
      ))}
    </div>
  );
}

function RangePlot({ data }: { data: RangeItem[] }) {
  if (!data.length) {
    return <ChartEmptyState />;
  }
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

function SourceMatrix({ data }: { data: typeof structureEffectData }) {
  const max = Math.max(1, ...data.sourceMatrix.flatMap((item) => [item.exp, item.sim, item.na]));
  const columns = [
    { key: "exp" as const, label: "experimental" },
    { key: "sim" as const, label: "simulated" },
    { key: "na" as const, label: "not labeled" }
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
        {data.sourceMatrix.map((row) => (
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

function SourceMatrixWithSummary({ data }: { data: typeof structureEffectData }) {
  const total = data.sources.reduce((sum, item) => sum + item.value, 0);
  if (!data.sources.length || total <= 0) {
    return <ChartEmptyState />;
  }
  const topSource = data.sources.reduce((current, item) => (item.value > current.value ? item : current), data.sources[0]);
  const simulated = data.sources.find((item) => item.label === "simulated");

  return (
    <div className="flex flex-col gap-4">
      <SourceMatrix data={data} />
      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label="classified records" value={formatCount(total)} />
        <MetricPill label="main origin" value={topSource.label} />
        <MetricPill label="simulated share" value={formatPercent(((simulated?.value ?? 0) / total) * 100)} />
      </div>
    </div>
  );
}

function StructureRangeCards({ data }: { data: RangeItem[] }) {
  const widest = data.reduce((current, item) => {
    const currentSpan = current.max - current.min;
    const nextSpan = item.max - item.min;
    return nextSpan > currentSpan ? item : current;
  }, data[0]);
  const topCount = data.reduce((current, item) => (item.count > current.count ? item : current), data[0]);

  return (
    <div className="flex flex-col gap-4">
      <div className="space-y-3">
        {data.map((item, index) => {
          const span = item.max - item.min || 1;
          const medianPosition = Math.min(Math.max(((item.median - item.min) / span) * 100, 0), 100);

          return (
            <article key={item.label} className="rounded-2xl border border-white/80 bg-white/75 p-3 shadow-sm">
              <div className="flex items-center justify-between gap-3">
                <div className="truncate text-sm font-semibold text-slate-900" title={item.label}>
                  {item.label}
                </div>
                <div className="font-mono-ui shrink-0 text-xs text-slate-500">{formatCount(item.count)}</div>
              </div>
              <div className="mt-3 h-2.5 overflow-hidden rounded-full bg-slate-100">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.max(medianPosition, 4)}%`,
                    background: `linear-gradient(90deg, ${colors[index % colors.length]} 0%, #38bdf8 100%)`
                  }}
                />
              </div>
              <div className="mt-3 grid grid-cols-3 gap-2 font-mono-ui text-[11px] text-slate-500">
                <span className="truncate" title={`Min ${formatNumber(item.min)}`}>
                  Min {formatNumber(item.min)}
                </span>
                <span className="truncate text-center" title={`Median ${formatNumber(item.median)}`}>
                  Median {formatNumber(item.median)}
                </span>
                <span className="truncate text-right" title={`Max ${formatNumber(item.max)}`}>
                  Max {formatNumber(item.max)}
                </span>
              </div>
            </article>
          );
        })}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <MetricPill label="ranges shown" value={formatCount(data.length)} />
        <MetricPill label="top count" value={topCount.label} />
        <MetricPill label="widest span" value={widest.label} />
      </div>
    </div>
  );
}

function DftDetailError({ message }: { message: string }) {
  return (
    <div className="flex h-full min-h-[360px] items-center justify-center rounded-[28px] border border-white/80 bg-[linear-gradient(180deg,#fbfdff_0%,#f2f7f9_100%)] p-6 text-center text-sm font-medium leading-6 text-slate-600">
      {message}
    </div>
  );
}

function EnergyTrace({ molecule, detailError }: { molecule: DftMoleculeDetail | null; detailError?: string | null }) {
  const values = (molecule?.trace ?? [])
    .filter((item) => item.scf_energy !== null)
    .map((item) => ({ step: item.step, energy: item.scf_energy as number }));

  if (detailError) {
    return <DftDetailError message={detailError} />;
  }

  if (!molecule || values.length === 0) {
    return (
      <div className="flex h-full min-h-[360px] items-center justify-center rounded-[28px] border border-white/80 bg-[linear-gradient(180deg,#fbfdff_0%,#f2f7f9_100%)] p-6 text-center text-sm font-medium text-slate-600">
        Select a molecule from the map to view its optimization energy trace.
      </div>
    );
  }

  const min = Math.min(...values.map((item) => item.energy));
  const max = Math.max(...values.map((item) => item.energy));
  const span = max - min || 1;
  const minStep = Math.min(...values.map((item) => item.step));
  const maxStep = Math.max(...values.map((item) => item.step));
  const stepSpan = maxStep - minStep || 1;
  const points = values.map((item) => {
    const x = 24 + ((item.step - minStep) / stepSpan) * 452;
    const y = 182 - ((item.energy - min) / span) * 136;
    return { ...item, x, y };
  });

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-[28px] border border-white/80 bg-[linear-gradient(180deg,#fbfdff_0%,#f2f7f9_100%)] p-4">
      <div className="inline-flex w-fit shrink-0 rounded-full border border-white/80 bg-white/80 px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm">
        {molecule.mol_id} · {values.length} energy points
      </div>
      <div className="flex min-h-[360px] flex-1 items-center">
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
          step {minStep}
        </text>
        <text x="414" y="206" className="fill-slate-500 text-[11px]">
          step {maxStep}
        </text>
        </svg>
      </div>
      <div className="grid shrink-0 gap-3 sm:grid-cols-3">
        <MetricPill label="min energy" value={formatNumber(min, 6)} />
        <MetricPill label="max energy" value={formatNumber(max, 6)} />
        <MetricPill label="final step" value={String(molecule.final_step)} />
      </div>
    </div>
  );
}

function atomStyle(atom: number) {
  if (atom === 1) return { label: "H", color: "#cbd5e1", radius: 5 };
  if (atom === 5) return { label: "B", color: "#e11d48", radius: 8 };
  if (atom === 6) return { label: "C", color: "#334155", radius: 8 };
  if (atom === 7) return { label: "N", color: "#7c3aed", radius: 8 };
  if (atom === 8) return { label: "O", color: "#2563eb", radius: 8 };
  if (atom === 9) return { label: "F", color: "#14b8a6", radius: 8 };
  if (atom === 14) return { label: "Si", color: "#38bdf8", radius: 10 };
  if (atom === 15) return { label: "P", color: "#64748b", radius: 10 };
  if (atom === 16) return { label: "S", color: "#f59e0b", radius: 10 };
  if (atom === 17) return { label: "Cl", color: "#0f766e", radius: 10 };
  return { label: String(atom), color: "#64748b", radius: 7 };
}

function distance(a: AtomCoordinate, b: AtomCoordinate) {
  return Math.hypot(a[1] - b[1], a[2] - b[2], a[3] - b[3]);
}

function dftBonds(coordinates: AtomCoordinate[]) {
  const bonds: { from: number; to: number }[] = [];
  coordinates.forEach((a, from) => {
    coordinates.slice(from + 1).forEach((b, offset) => {
      const to = from + offset + 1;
      const threshold = a[0] === 1 || b[0] === 1 ? 1.22 : 1.72;
      if (distance(a, b) <= threshold) {
        bonds.push({ from, to });
      }
    });
  });
  return bonds;
}

function toMolBlock(molId: string, coordinates: AtomCoordinate[], bonds: { from: number; to: number }[]) {
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
    molId,
    "  NexPoly DFT",
    "",
    counts,
    ...atomLines,
    ...bondLines,
    "M  END"
  ].join("\n");
}

function DftMolecule3D({ molecule, detailError }: { molecule: DftMoleculeDetail | null; detailError?: string | null }) {
  const viewerRef = useRef<HTMLDivElement | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const molBlock = useMemo(() => {
    if (!molecule) return null;
    return toMolBlock(molecule.mol_id, molecule.coordinates, dftBonds(molecule.coordinates));
  }, [molecule]);

  useEffect(() => {
    let cancelled = false;

    async function renderMolecule() {
      if (!molBlock) {
        setIsLoading(false);
        return;
      }

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
          setError(nextError instanceof Error ? nextError.message : "Failed to load 3D conformation");
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

  if (detailError) {
    return <DftDetailError message={detailError} />;
  }

  if (!molecule) {
    return (
      <div className="flex h-full min-h-[360px] items-center justify-center rounded-[28px] border border-white/80 bg-[radial-gradient(circle_at_30%_15%,rgba(56,189,248,0.22),transparent_30%),radial-gradient(circle_at_76%_24%,rgba(245,158,11,0.16),transparent_26%),linear-gradient(180deg,#fbfdff_0%,#edf5f8_100%)] p-6 text-center text-sm font-medium text-slate-600">
        Select a molecule from the map to view its final 3D conformation.
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-[28px] border border-white/80 bg-[radial-gradient(circle_at_30%_15%,rgba(56,189,248,0.22),transparent_30%),radial-gradient(circle_at_76%_24%,rgba(245,158,11,0.16),transparent_26%),linear-gradient(180deg,#fbfdff_0%,#edf5f8_100%)] p-4">
      <div className="inline-flex w-fit shrink-0 rounded-full border border-white/80 bg-white/80 px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm">
        {molecule.mol_id} · step {molecule.final_step}
      </div>
      <div className="relative flex min-h-[360px] flex-1 items-center overflow-hidden rounded-[24px]">
        <div ref={viewerRef} className="absolute inset-0 cursor-grab active:cursor-grabbing" />
        {isLoading ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="rounded-full border border-white/80 bg-white/90 px-4 py-2 text-sm font-medium text-slate-700 shadow-sm">
              Loading 3D conformation
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
        <MetricPill label="atoms" value={String(molecule.n_atoms)} />
        <MetricPill label="energy" value={molecule.scf_energy === null ? "-" : formatNumber(molecule.scf_energy, 4)} />
        <MetricPill label="gap eV" value={molecule.gap_ev === null ? "-" : formatNumber(molecule.gap_ev, 3)} />
        <MetricPill label="dipole" value={molecule.dipole_moment === null ? "-" : formatNumber(molecule.dipole_moment, 3)} />
      </div>
    </div>
  );
}

function PcaDistribution3D({
  points,
  selectedMolId,
  isLoading,
  error,
  onSelect
}: {
  points: DftPcaPoint[];
  selectedMolId: string | null;
  isLoading: boolean;
  error: string | null;
  onSelect: (molId: string) => void;
}) {
  const [rotation, setRotation] = useState({ yaw: -0.65, pitch: 0.42 });
  const [zoom, setZoom] = useState(1.35);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{
    startX: number;
    startY: number;
    lastX: number;
    lastY: number;
    moved: boolean;
  } | null>(null);
  const projected = useMemo(() => {
    if (points.length === 0) return [];

    const center = {
      x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
      y: points.reduce((sum, point) => sum + point.y, 0) / points.length,
      z: points.reduce((sum, point) => sum + point.z, 0) / points.length
    };
    const sortedDeviations = points
      .flatMap((point) => [
        Math.abs(point.x - center.x),
        Math.abs(point.y - center.y),
        Math.abs(point.z - center.z)
      ])
      .sort((a, b) => a - b);
    const robustIndex = Math.floor(sortedDeviations.length * 0.92);
    const scaleBase = Math.max(sortedDeviations[robustIndex] ?? 1, 1);
    const gapValues = points.map((point) => point.gap_ev ?? 0);
    const gapMin = Math.min(...gapValues);
    const gapMax = Math.max(...gapValues);
    const gapSpan = gapMax - gapMin || 1;
    const cosYaw = Math.cos(rotation.yaw);
    const sinYaw = Math.sin(rotation.yaw);
    const cosPitch = Math.cos(rotation.pitch);
    const sinPitch = Math.sin(rotation.pitch);

    return points
      .map((point) => {
        const nx = (point.x - center.x) / scaleBase;
        const ny = (point.y - center.y) / scaleBase;
        const nz = (point.z - center.z) / scaleBase;
        const rx = nx * cosYaw + nz * sinYaw;
        const rz = -nx * sinYaw + nz * cosYaw;
        const ry = ny * cosPitch - rz * sinPitch;
        const depth = ny * sinPitch + rz * cosPitch;
        const perspective = 2.45 / (2.8 - depth);
        const gapRatio = ((point.gap_ev ?? gapMin) - gapMin) / gapSpan;

        return {
          ...point,
          sx: 320 + rx * 180 * zoom * perspective,
          sy: 230 - ry * 180 * zoom * perspective,
          depth,
          radius: 4.2 + perspective * 1.9,
          color: `rgb(${Math.round(15 + gapRatio * 37)}, ${Math.round(118 + gapRatio * 71)}, ${Math.round(110 + gapRatio * 128)})`
        };
      })
      .sort((a, b) => a.depth - b.depth);
  }, [points, rotation, zoom]);
  const selectedPoint = points.find((point) => point.mol_id === selectedMolId) ?? null;

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;

    function handleWheel(event: WheelEvent) {
      event.preventDefault();
      event.stopPropagation();
      const direction = event.deltaY > 0 ? -0.08 : 0.08;
      setZoom((current) => Math.max(0.8, Math.min(2.2, current + direction)));
    }

    svg.addEventListener("wheel", handleWheel, { passive: false });
    return () => {
      svg.removeEventListener("wheel", handleWheel);
    };
  }, []);

  function svgPointFromClient(clientX: number, clientY: number) {
    const svg = svgRef.current;
    const matrix = svg?.getScreenCTM();
    if (!svg || !matrix) return null;

    const point = svg.createSVGPoint();
    point.x = clientX;
    point.y = clientY;
    const transformed = point.matrixTransform(matrix.inverse());
    return { x: transformed.x, y: transformed.y };
  }

  function selectNearestPoint(clientX: number, clientY: number) {
    const clickPoint = svgPointFromClient(clientX, clientY);
    if (!clickPoint || projected.length === 0) return;

    let nearest = projected[0];
    let nearestDistance = Infinity;
    projected.forEach((point) => {
      const distance = Math.hypot(point.sx - clickPoint.x, point.sy - clickPoint.y);
      if (distance < nearestDistance) {
        nearest = point;
        nearestDistance = distance;
      }
    });

    if (nearestDistance <= Math.max(34, nearest.radius + 14)) {
      onSelect(nearest.mol_id);
    }
  }

  return (
    <div className="flex h-full flex-col gap-4 rounded-[28px] border border-white/80 bg-[linear-gradient(180deg,#fbfdff_0%,#edf5f8_100%)] p-4">
      <div className="relative min-h-[430px] flex-1 overflow-hidden overscroll-contain rounded-[24px] bg-[radial-gradient(circle_at_50%_40%,rgba(45,212,191,0.18),transparent_38%),linear-gradient(180deg,#f8fafc_0%,#eef5f6_100%)]">
        <svg
          ref={svgRef}
          viewBox="0 0 640 460"
          className="h-full min-h-[430px] w-full cursor-grab touch-none active:cursor-grabbing"
          onPointerDown={(event) => {
            dragRef.current = {
              startX: event.clientX,
              startY: event.clientY,
              lastX: event.clientX,
              lastY: event.clientY,
              moved: false
            };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            if (!dragRef.current) return;
            const dx = event.clientX - dragRef.current.lastX;
            const dy = event.clientY - dragRef.current.lastY;
            const totalDistance = Math.hypot(
              event.clientX - dragRef.current.startX,
              event.clientY - dragRef.current.startY,
            );
            if (totalDistance > 4) {
              dragRef.current.moved = true;
            }
            dragRef.current.lastX = event.clientX;
            dragRef.current.lastY = event.clientY;
            if (!dragRef.current.moved) return;

            setRotation((current) => ({
              yaw: current.yaw + dx * 0.006,
              pitch: Math.max(-1.15, Math.min(1.15, current.pitch + dy * 0.006))
            }));
          }}
          onPointerUp={(event) => {
            const drag = dragRef.current;
            dragRef.current = null;
            if (drag && !drag.moved) {
              selectNearestPoint(event.clientX, event.clientY);
            }
          }}
          onPointerCancel={() => {
            dragRef.current = null;
          }}
        >
          <circle cx="320" cy="230" r="214" fill="rgba(255,255,255,0.34)" />
          <circle cx="320" cy="230" r="150" fill="none" stroke="rgba(203,213,225,0.45)" strokeWidth="1.4" />
          <circle cx="320" cy="230" r="76" fill="none" stroke="rgba(203,213,225,0.34)" strokeWidth="1.2" />
          {projected.map((point) => {
            const selected = point.mol_id === selectedMolId;
            return (
              <circle
                key={point.mol_id}
                cx={point.sx}
                cy={point.sy}
                r={selected ? point.radius + 3 : point.radius}
                fill={selected ? "#e11d48" : point.color}
                fillOpacity={selected ? 0.98 : 0.68}
                className="transition-[r,opacity] duration-150 hover:opacity-100"
                stroke={selected ? "white" : "transparent"}
                strokeWidth={selected ? 3 : 0}
                role="button"
                onPointerUp={(event) => {
                  if (!dragRef.current?.moved) {
                    event.stopPropagation();
                    onSelect(point.mol_id);
                    dragRef.current = null;
                  }
                }}
              >
                <title>{point.mol_id}</title>
              </circle>
            );
          })}
        </svg>

        {isLoading ? (
          <div className="absolute inset-0 flex items-center justify-center bg-white/45">
            <div className="rounded-full border border-white/80 bg-white/95 px-4 py-2 text-sm font-medium text-slate-700 shadow-sm">
              Loading molecule map
            </div>
          </div>
        ) : null}
        {error ? (
          <div className="absolute inset-0 flex items-center justify-center bg-white/65 p-6">
            <div className="max-w-xl rounded-2xl border border-slate-200 bg-white px-5 py-4 text-center text-sm font-medium leading-6 text-slate-700 shadow-sm">
              {error}
            </div>
          </div>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-4">
        <MetricPill label="selected molecule" value={selectedPoint?.mol_id ?? "-"} />
        <MetricPill label="atoms" value={selectedPoint ? String(selectedPoint.n_atoms) : "-"} />
        <MetricPill label="gap eV" value={selectedPoint?.gap_ev === null || !selectedPoint ? "-" : formatNumber(selectedPoint.gap_ev, 3)} />
        <MetricPill label="final step" value={selectedPoint ? String(selectedPoint.final_step) : "-"} />
      </div>
    </div>
  );
}

function DatasetTile({
  dataset,
  onOpen
}: {
  dataset: DisplayDataset;
  onOpen: (key: DatasetKey) => void;
}) {
  const isReserved = dataset.status === "Reserved";

  return (
    <section
      className={[
        "flex min-h-[454px] flex-col rounded-[28px] border p-5 transition-all duration-300",
        isReserved
          ? "border-white/70 bg-white/55 text-slate-500"
          : "border-white/80 bg-white/85 text-slate-950 shadow-sm hover:-translate-y-1 hover:shadow-panel"
      ].join(" ")}
      aria-disabled={isReserved}
    >
      <div className="flex flex-1 flex-col">
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
        <h3 className="font-heading mt-3 flex min-h-[68px] items-start text-[1.4rem] font-semibold tracking-tight text-slate-950">
          {dataset.title}
        </h3>
        <p className="mt-2 flex h-[72px] items-start text-sm leading-6 text-mutedForeground">{dataset.description}</p>
        <div className="mt-5">
          <MetricPill label="Records" value={formatCount(dataset.recordCount)} />
        </div>
        <div className="mt-3 min-h-10 text-xs leading-5 text-mutedForeground">{formatImportMeta(dataset)}</div>
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
        <span>{isReserved ? "Reserved" : "View Charts"}</span>
        <ArrowRight className="h-4 w-4" />
      </button>
    </section>
  );
}

function DatabaseHome({
  onBackHome,
  onOpenDataset,
  displayDatasets,
  summaryLoading,
  summaryError
}: {
  onBackHome: () => void;
  onOpenDataset: (key: DatasetKey) => void;
  displayDatasets: DisplayDataset[];
  summaryLoading: boolean;
  summaryError: string | null;
}) {
  const readyDatasets = displayDatasets.filter((dataset) => dataset.status === "Ready");
  const reservedDatasets = displayDatasets.filter((dataset) => dataset.status === "Reserved");
  const totalRecords = readyDatasets.reduce((sum, dataset) => sum + (dataset.recordCount ?? 0), 0);

  return (
    <>
      <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex items-center gap-3">
          <Button type="button" variant="outline" onClick={onBackHome}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Home
          </Button>
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Current View</div>
            <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">Database Analytics</div>
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
              Polymer Data Platform
            </h1>
            <p className="mt-5 max-w-2xl text-base leading-7 text-slate-300">
              Explore five curated polymer data views through focused charts and summaries.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <MetricPill label="Available views" value={String(readyDatasets.length)} />
            <MetricPill label="Upcoming views" value={String(reservedDatasets.length)} />
            <MetricPill label="Total records" value={formatCount(totalRecords)} />
            <MetricPill label="Chart views" value={String(datasets.length)} />
          </div>
        </div>
      </section>

      {summaryLoading ? (
        <section className="rounded-[28px] border border-white/80 bg-white/75 px-5 py-4 text-sm leading-6 text-slate-600 shadow-sm">
          Checking database source status...
        </section>
      ) : null}

      {summaryError ? (
        <section className="rounded-[28px] border border-rose-200 bg-rose-50 px-5 py-4 text-sm leading-6 text-rose-700 shadow-sm">
          <div className="font-semibold text-rose-800">Database source status unavailable</div>
          <div className="mt-1">{summaryError}</div>
        </section>
      ) : null}

      <section className="grid gap-4 lg:grid-cols-2 xl:grid-cols-5">
        {displayDatasets.map((dataset) => (
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
  hideDescription = false,
  backDatabaseLabel = "Database Analysis",
  homeLabel = "Home",
  recordLabel = "Records",
  viewLabel = "View",
  viewValue = "Charts",
  onViewClick,
  analyticsSource = null,
  analyticsGeneratedAt = null,
  analyticsRefreshing = false,
  analyticsError = null,
  onRefreshAnalytics
}: {
  dataset: DisplayDataset;
  onBackHome: () => void;
  onBackDatabase: () => void;
  children: ReactNode;
  hideDescription?: boolean;
  backDatabaseLabel?: string;
  homeLabel?: string;
  recordLabel?: string;
  viewLabel?: string;
  viewValue?: string;
  onViewClick?: () => void;
  analyticsSource?: string | null;
  analyticsGeneratedAt?: string | null;
  analyticsRefreshing?: boolean;
  analyticsError?: string | null;
  onRefreshAnalytics?: () => void;
}) {
  const analyticsSourceLabel =
    analyticsSource === "live"
      ? "Live statistics"
      : analyticsSource === "snapshot"
        ? "Snapshot statistics"
        : "Statistics";
  const analyticsTitle = analyticsGeneratedAt
    ? `Generated at ${new Date(analyticsGeneratedAt).toLocaleString()}`
    : undefined;

  return (
    <>
      <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            onClick={onBackDatabase}
            className="border-slate-950 bg-slate-950 text-white shadow-sm hover:bg-teal-700"
          >
            <ArrowLeft className="mr-2 h-4 w-4" />
            {backDatabaseLabel}
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={onBackHome}
            className="bg-white/65 text-slate-600 hover:border-teal-200 hover:bg-teal-50 hover:text-teal-800"
          >
            {homeLabel}
          </Button>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {analyticsError ? <Badge className="bg-rose-50 text-rose-700">Refresh failed</Badge> : null}
          {analyticsSource ? (
            <Badge className="bg-slate-100 text-slate-600" title={analyticsTitle}>
              {analyticsSourceLabel}
            </Badge>
          ) : null}
          {onRefreshAnalytics ? (
            <Button
              type="button"
              variant="outline"
              onClick={onRefreshAnalytics}
              disabled={analyticsRefreshing}
              className="bg-white/65 text-slate-600 hover:border-teal-200 hover:bg-teal-50 hover:text-teal-800"
            >
              <RefreshCw className={`mr-2 h-4 w-4 ${analyticsRefreshing ? "animate-spin" : ""}`} />
              {analyticsRefreshing ? "Refreshing" : "Refresh statistics"}
            </Button>
          ) : null}
          <Badge className="bg-teal-50 text-teal-800">{dataset.englishTitle}</Badge>
        </div>
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
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <MetricPill label={recordLabel} value={formatCount(dataset.recordCount)} />
            <MetricPill
              label={viewLabel}
              value={viewValue}
              onClick={onViewClick}
              ariaLabel={onViewClick ? `Open ${dataset.englishTitle} record search` : undefined}
            />
          </div>
        </div>
      </section>

      {children}
    </>
  );
}

function DatasetUnavailableState({ dataset }: { dataset: DisplayDataset }) {
  return (
    <section className="rounded-[28px] border border-dashed border-slate-300 bg-white/70 px-5 py-6 shadow-sm">
      <Badge className="bg-slate-100 text-slate-600">{dataset.sourceStatus ?? "missing"}</Badge>
      <h2 className="font-heading mt-4 text-2xl font-semibold tracking-tight text-slate-950">Data source unavailable</h2>
      <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-600">
        {dataset.sourceMessage ?? "The source file for this dataset is not available in the governed database."}
      </p>
    </section>
  );
}

function DatasetAnalyticsUnavailableState({ loading, error }: { loading: boolean; error: string | null }) {
  return (
    <section className="rounded-[28px] border border-dashed border-slate-300 bg-white/70 px-5 py-6 shadow-sm">
      <Badge className="bg-slate-100 text-slate-600">analytics</Badge>
      <h2 className="font-heading mt-4 text-2xl font-semibold tracking-tight text-slate-950">Analytics data unavailable</h2>
      <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-600">
        {loading
          ? "Checking Postgres-backed analytics data..."
          : error ?? "The analytics endpoint did not return this dataset. Static chart snapshots are disabled for governed runtime views."}
      </p>
    </section>
  );
}

function EmptyCell() {
  return <span className="text-slate-400">-</span>;
}

function StructurePropertyRecordBrowser({
  open,
  onClose,
  expectedTotal
}: {
  open: boolean;
  onClose: () => void;
  expectedTotal: number;
}) {
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [data, setData] = useState<StructurePropertyBrowseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    browseStructurePropertyRecords({ q: query, page, page_size: pageSize })
      .then((response) => {
        if (!cancelled) {
          setData(response);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : "Failed to load records");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [open, page, pageSize, query]);

  useEffect(() => {
    if (!open) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  if (!open) {
    return null;
  }

  const matchedRecords = data?.matched_records ?? 0;
  const totalRecords = data?.total_records ?? expectedTotal;
  const totalPages = Math.max(1, Math.ceil(matchedRecords / pageSize));
  const startRecord = matchedRecords === 0 ? 0 : (page - 1) * pageSize + 1;
  const endRecord = Math.min(page * pageSize, matchedRecords);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
      <section className="flex h-[min(88vh,760px)] w-[min(1180px,calc(100vw-2rem))] flex-col overflow-hidden rounded-[28px] border border-white/80 bg-white/95 shadow-[0_30px_100px_rgba(8,17,31,0.34)]">
        <header className="flex flex-col gap-4 border-b border-slate-200 bg-slate-50/90 px-5 py-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-950 text-white">
                <TableProperties className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-teal-700">
                  Structure-Property
                </div>
                <h2 className="font-heading truncate text-xl font-semibold tracking-tight text-slate-950">
                  Record Search
                </h2>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <MetricPill label="total records" value={formatCount(totalRecords)} />
            <MetricPill label="matched" value={formatCount(matchedRecords)} />
            <button
              type="button"
              aria-label="Close record search"
              onClick={onClose}
              className="flex h-11 w-11 items-center justify-center rounded-2xl border border-slate-200 bg-white text-slate-600 shadow-sm transition hover:border-teal-200 hover:bg-teal-50 hover:text-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="border-b border-slate-200 bg-white px-5 py-4">
          <form
            className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]"
            onSubmit={(event) => {
              event.preventDefault();
              setPage(1);
              setQuery(queryDraft.trim());
            }}
          >
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
              <Input
                value={queryDraft}
                onChange={(event) => setQueryDraft(event.target.value)}
                placeholder="Search SMILES, property, value, unit, source"
                className="border-slate-200 bg-slate-50 pl-10"
              />
            </div>
            <select
              aria-label="Rows per page"
              value={pageSize}
              onChange={(event) => {
                setPage(1);
                setPageSize(Number(event.target.value));
              }}
              className="h-11 rounded-2xl border border-slate-200 bg-slate-50 px-4 text-sm text-slate-700 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value={25}>25 rows</option>
              <option value={50}>50 rows</option>
              <option value={100}>100 rows</option>
            </select>
            <div className="flex gap-2">
              <Button type="submit" className="min-w-24">
                Search
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setQueryDraft("");
                  setQuery("");
                  setPage(1);
                }}
              >
                Clear
              </Button>
            </div>
          </form>
        </div>

        <div className="relative min-h-0 flex-1 overflow-auto bg-white">
          {error ? (
            <div className="m-5 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {error}
            </div>
          ) : null}

          <table className="min-w-[1040px] w-full border-separate border-spacing-0 text-left text-sm">
            <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] uppercase tracking-[0.16em] text-slate-500">
              <tr>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Property ID</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Polymer ID</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">SMILES</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Property</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Value</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Unit</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Source</th>
              </tr>
            </thead>
            <tbody>
              {data?.results.map((row) => (
                <tr key={row.property_id} className="align-top hover:bg-teal-50/35">
                  <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                    {row.property_id}
                  </td>
                  <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                    {row.polymer_id}
                  </td>
                  <td className="max-w-[24rem] border-b border-slate-100 px-4 py-3">
                    <div className="truncate font-mono-ui text-xs text-slate-700" title={row.smiles}>
                      {row.smiles}
                    </div>
                  </td>
                  <td className="max-w-[16rem] border-b border-slate-100 px-4 py-3">
                    <div className="font-medium text-slate-900">{row.property_name}</div>
                  </td>
                  <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                    {row.property_value || <EmptyCell />}
                  </td>
                  <td className="border-b border-slate-100 px-4 py-3 text-slate-700">
                    {row.property_unit || <EmptyCell />}
                  </td>
                  <td className="border-b border-slate-100 px-4 py-3">
                    {row.label_source ? (
                      <Badge className="bg-slate-100 text-slate-700">{row.label_source}</Badge>
                    ) : (
                      <EmptyCell />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {!loading && !error && data?.results.length === 0 ? (
            <div className="px-5 py-10 text-center text-sm text-slate-500">No matching records.</div>
          ) : null}
          {loading ? (
            <div className="absolute inset-x-0 bottom-16 mx-auto w-fit rounded-full border border-white/80 bg-white px-4 py-2 text-sm text-slate-600 shadow-lg">
              Loading records...
            </div>
          ) : null}
        </div>

        <footer className="flex flex-col gap-3 border-t border-slate-200 bg-slate-50/95 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="font-mono-ui text-xs text-slate-500">
            {startRecord}-{endRecord} / {formatCount(matchedRecords)}
            {query ? ` for "${query}"` : ""}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={page <= 1 || loading}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              <ChevronLeft className="mr-2 h-4 w-4" />
              Previous
            </Button>
            <div className="min-w-24 text-center font-mono-ui text-xs text-slate-500">
              {page} / {totalPages}
            </div>
            <Button
              type="button"
              variant="outline"
              disabled={page >= totalPages || loading}
              onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
            >
              Next
              <ChevronRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}

type DftBrowserTab = "molecules" | "steps";

function OptionalNumber({ value, digits = 3 }: { value: number | null; digits?: number }) {
  if (value === null || Number.isNaN(value)) {
    return <EmptyCell />;
  }

  return <>{formatNumber(value, digits)}</>;
}

function DftRecordBrowser({
  open,
  onClose,
  expectedMolecules,
  expectedSteps
}: {
  open: boolean;
  onClose: () => void;
  expectedMolecules: number;
  expectedSteps: number;
}) {
  const [activeTab, setActiveTab] = useState<DftBrowserTab>("molecules");
  const [moleculeQueryDraft, setMoleculeQueryDraft] = useState("");
  const [moleculeQuery, setMoleculeQuery] = useState("");
  const [moleculePage, setMoleculePage] = useState(1);
  const [moleculePageSize, setMoleculePageSize] = useState(25);
  const [moleculeData, setMoleculeData] = useState<DftMoleculeBrowseResponse | null>(null);
  const [moleculeLoading, setMoleculeLoading] = useState(false);
  const [moleculeError, setMoleculeError] = useState<string | null>(null);

  const [stepQueryDraft, setStepQueryDraft] = useState("");
  const [stepQuery, setStepQuery] = useState("");
  const [stepMolId, setStepMolId] = useState<string | null>(null);
  const [stepPage, setStepPage] = useState(1);
  const [stepPageSize, setStepPageSize] = useState(25);
  const [stepData, setStepData] = useState<DftEnergyStepBrowseResponse | null>(null);
  const [stepLoading, setStepLoading] = useState(false);
  const [stepError, setStepError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    let cancelled = false;
    setMoleculeLoading(true);
    setMoleculeError(null);

    browseDftMolecules({ q: moleculeQuery, page: moleculePage, page_size: moleculePageSize })
      .then((response) => {
        if (!cancelled) {
          setMoleculeData(response);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setMoleculeError(nextError instanceof Error ? nextError.message : "Failed to load DFT molecules");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setMoleculeLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [moleculePage, moleculePageSize, moleculeQuery, open]);

  useEffect(() => {
    if (!open || activeTab !== "steps") {
      return;
    }

    let cancelled = false;
    setStepLoading(true);
    setStepError(null);

    browseDftEnergySteps({
      q: stepMolId ? undefined : stepQuery,
      mol_id: stepMolId ?? undefined,
      page: stepPage,
      page_size: stepPageSize
    })
      .then((response) => {
        if (!cancelled) {
          setStepData(response);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setStepError(nextError instanceof Error ? nextError.message : "Failed to load optimization steps");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setStepLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [activeTab, open, stepMolId, stepPage, stepPageSize, stepQuery]);

  useEffect(() => {
    if (!open) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  if (!open) {
    return null;
  }

  const moleculeTotal = moleculeData?.total_records ?? expectedMolecules;
  const stepTotal = moleculeData?.total_step_records ?? stepData?.total_records ?? expectedSteps;
  const averageSteps = moleculeData?.average_steps ?? 0;
  const maxSteps = moleculeData?.max_steps ?? 0;
  const moleculeMatched = moleculeData?.matched_records ?? 0;
  const stepMatched = stepData?.matched_records ?? 0;
  const moleculePages = Math.max(1, Math.ceil(moleculeMatched / moleculePageSize));
  const stepPages = Math.max(1, Math.ceil(stepMatched / stepPageSize));
  const moleculeStart = moleculeMatched === 0 ? 0 : (moleculePage - 1) * moleculePageSize + 1;
  const moleculeEnd = Math.min(moleculePage * moleculePageSize, moleculeMatched);
  const stepStart = stepMatched === 0 ? 0 : (stepPage - 1) * stepPageSize + 1;
  const stepEnd = Math.min(stepPage * stepPageSize, stepMatched);
  const loading = activeTab === "molecules" ? moleculeLoading : stepLoading;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
      <section className="flex h-[min(90vh,790px)] w-[min(1240px,calc(100vw-2rem))] flex-col overflow-hidden rounded-[28px] border border-white/80 bg-white/95 shadow-[0_30px_100px_rgba(8,17,31,0.34)]">
        <header className="flex flex-col gap-4 border-b border-slate-200 bg-slate-50/90 px-5 py-4 xl:flex-row xl:items-center xl:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-950 text-white">
                <Orbit className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-teal-700">
                  DFT Conformation
                </div>
                <h2 className="font-heading truncate text-xl font-semibold tracking-tight text-slate-950">
                  DFT Record Browser
                </h2>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <MetricPill label="molecules" value={formatCount(moleculeTotal)} />
            <MetricPill label="steps" value={formatCount(stepTotal)} />
            <MetricPill label="avg steps" value={formatNumber(averageSteps, 1)} />
            <MetricPill label="max steps" value={formatCount(maxSteps)} />
            <button
              type="button"
              aria-label="Close DFT record browser"
              onClick={onClose}
              className="flex h-11 w-11 items-center justify-center rounded-2xl border border-slate-200 bg-white text-slate-600 shadow-sm transition hover:border-teal-200 hover:bg-teal-50 hover:text-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="border-b border-slate-200 bg-white px-5 py-4">
          <div className="mb-4 flex flex-wrap gap-2">
            {[
              ["molecules", "Molecules"],
              ["steps", "Optimization Steps"]
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setActiveTab(key as DftBrowserTab)}
                className={[
                  "rounded-2xl border px-4 py-2 text-sm font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  activeTab === key
                    ? "border-slate-950 bg-slate-950 text-white shadow-sm"
                    : "border-slate-200 bg-slate-50 text-slate-600 hover:border-teal-200 hover:bg-teal-50 hover:text-teal-800"
                ].join(" ")}
              >
                {label}
              </button>
            ))}
          </div>

          {activeTab === "molecules" ? (
            <form
              className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]"
              onSubmit={(event) => {
                event.preventDefault();
                setMoleculePage(1);
                setMoleculeQuery(moleculeQueryDraft.trim());
              }}
            >
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                <Input
                  value={moleculeQueryDraft}
                  onChange={(event) => setMoleculeQueryDraft(event.target.value)}
                  placeholder="Search mol_id, range group, energy, gap, convergence"
                  className="border-slate-200 bg-slate-50 pl-10"
                />
              </div>
              <select
                aria-label="Molecule rows per page"
                value={moleculePageSize}
                onChange={(event) => {
                  setMoleculePage(1);
                  setMoleculePageSize(Number(event.target.value));
                }}
                className="h-11 rounded-2xl border border-slate-200 bg-slate-50 px-4 text-sm text-slate-700 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value={25}>25 rows</option>
                <option value={50}>50 rows</option>
                <option value={100}>100 rows</option>
              </select>
              <div className="flex gap-2">
                <Button type="submit" className="min-w-24">
                  Search
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setMoleculeQueryDraft("");
                    setMoleculeQuery("");
                    setMoleculePage(1);
                  }}
                >
                  Clear
                </Button>
              </div>
            </form>
          ) : (
            <form
              className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]"
              onSubmit={(event) => {
                event.preventDefault();
                setStepPage(1);
                setStepMolId(null);
                setStepQuery(stepQueryDraft.trim());
              }}
            >
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                <Input
                  value={stepQueryDraft}
                  onChange={(event) => setStepQueryDraft(event.target.value)}
                  placeholder="Search mol_id or numeric step/energy values"
                  className="border-slate-200 bg-slate-50 pl-10"
                />
              </div>
              <select
                aria-label="Step rows per page"
                value={stepPageSize}
                onChange={(event) => {
                  setStepPage(1);
                  setStepPageSize(Number(event.target.value));
                }}
                className="h-11 rounded-2xl border border-slate-200 bg-slate-50 px-4 text-sm text-slate-700 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value={25}>25 rows</option>
                <option value={50}>50 rows</option>
                <option value={100}>100 rows</option>
              </select>
              <div className="flex gap-2">
                <Button type="submit" className="min-w-24">
                  Search
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => {
                    setStepQueryDraft("");
                    setStepQuery("");
                    setStepMolId(null);
                    setStepPage(1);
                  }}
                >
                  Clear
                </Button>
              </div>
            </form>
          )}
        </div>

        <div className="relative min-h-0 flex-1 overflow-auto bg-white">
          {activeTab === "molecules" ? (
            <>
              {moleculeError ? (
                <div className="m-5 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                  {moleculeError}
                </div>
              ) : null}
              <table className="min-w-[1180px] w-full border-separate border-spacing-0 text-left text-sm">
                <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                  <tr>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Mol ID</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Group</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Final Step</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Trace Points</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Atoms</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">SCF Energy</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">HOMO</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">LUMO</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Gap</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Dipole</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Converged</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Steps</th>
                  </tr>
                </thead>
                <tbody>
                  {moleculeData?.results.map((row) => (
                    <tr key={row.mol_id} className="align-top hover:bg-teal-50/35">
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs font-semibold text-slate-800">
                        {row.mol_id}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 text-slate-700">{row.range_group}</td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                        {row.final_step}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                        {row.trace_points}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                        {row.n_atoms}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.scf_energy} digits={6} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.homo_ev} digits={4} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.lumo_ev} digits={4} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.gap_ev} digits={4} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.dipole_moment} digits={4} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3">
                        {row.is_converged ? (
                          <Badge className="bg-slate-100 text-slate-700">{row.is_converged}</Badge>
                        ) : (
                          <EmptyCell />
                        )}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3">
                        <Button
                          type="button"
                          variant="outline"
                          className="h-9 rounded-xl px-3 text-xs"
                          onClick={() => {
                            setStepQueryDraft(row.mol_id);
                            setStepQuery("");
                            setStepMolId(row.mol_id);
                            setStepPage(1);
                            setActiveTab("steps");
                          }}
                        >
                          View Steps
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!moleculeLoading && !moleculeError && moleculeData?.results.length === 0 ? (
                <div className="px-5 py-10 text-center text-sm text-slate-500">No matching molecules.</div>
              ) : null}
            </>
          ) : (
            <>
              {stepError ? (
                <div className="m-5 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                  {stepError}
                </div>
              ) : null}
              <table className="min-w-[820px] w-full border-separate border-spacing-0 text-left text-sm">
                <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                  <tr>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Mol ID</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Step</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">SCF Energy</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">HOMO</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">LUMO</th>
                    <th className="border-b border-slate-200 px-4 py-3 font-semibold">Gap</th>
                  </tr>
                </thead>
                <tbody>
                  {stepData?.results.map((row) => (
                    <tr key={`${row.mol_id}-${row.step}`} className="align-top hover:bg-teal-50/35">
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs font-semibold text-slate-800">
                        {row.mol_id}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                        {row.step}
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.scf_energy} digits={6} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.homo_ev} digits={4} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.lumo_ev} digits={4} />
                      </td>
                      <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                        <OptionalNumber value={row.gap_ev} digits={4} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!stepLoading && !stepError && stepData?.results.length === 0 ? (
                <div className="px-5 py-10 text-center text-sm text-slate-500">No matching optimization steps.</div>
              ) : null}
            </>
          )}

          {loading ? (
            <div className="absolute inset-x-0 bottom-16 mx-auto w-fit rounded-full border border-white/80 bg-white px-4 py-2 text-sm text-slate-600 shadow-lg">
              Loading DFT records...
            </div>
          ) : null}
        </div>

        <footer className="flex flex-col gap-3 border-t border-slate-200 bg-slate-50/95 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          {activeTab === "molecules" ? (
            <>
              <div className="font-mono-ui text-xs text-slate-500">
                {moleculeStart}-{moleculeEnd} / {formatCount(moleculeMatched)}
                {moleculeQuery ? ` for "${moleculeQuery}"` : ""}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  disabled={moleculePage <= 1 || moleculeLoading}
                  onClick={() => setMoleculePage((current) => Math.max(1, current - 1))}
                >
                  <ChevronLeft className="mr-2 h-4 w-4" />
                  Previous
                </Button>
                <div className="min-w-24 text-center font-mono-ui text-xs text-slate-500">
                  {moleculePage} / {moleculePages}
                </div>
                <Button
                  type="button"
                  variant="outline"
                  disabled={moleculePage >= moleculePages || moleculeLoading}
                  onClick={() => setMoleculePage((current) => Math.min(moleculePages, current + 1))}
                >
                  Next
                  <ChevronRight className="ml-2 h-4 w-4" />
                </Button>
              </div>
            </>
          ) : (
            <>
              <div className="font-mono-ui text-xs text-slate-500">
                {stepStart}-{stepEnd} / {formatCount(stepMatched)}
                {stepMolId || stepQuery ? ` for "${stepMolId ?? stepQuery}"` : ""}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  disabled={stepPage <= 1 || stepLoading}
                  onClick={() => setStepPage((current) => Math.max(1, current - 1))}
                >
                  <ChevronLeft className="mr-2 h-4 w-4" />
                  Previous
                </Button>
                <div className="min-w-24 text-center font-mono-ui text-xs text-slate-500">
                  {stepPage} / {stepPages}
                </div>
                <Button
                  type="button"
                  variant="outline"
                  disabled={stepPage >= stepPages || stepLoading}
                  onClick={() => setStepPage((current) => Math.min(stepPages, current + 1))}
                >
                  Next
                  <ChevronRight className="ml-2 h-4 w-4" />
                </Button>
              </div>
            </>
          )}
        </footer>
      </section>
    </div>
  );
}

type ExperimentalCsvKind = "process" | "property";
type ExperimentalCsvResponse = ExperimentalProcessBrowseResponse | ExperimentalPropertyBrowseResponse;

function ExperimentalCsvRecordBrowser({
  kind,
  open,
  onClose,
  expectedTotal
}: {
  kind: ExperimentalCsvKind;
  open: boolean;
  onClose: () => void;
  expectedTotal: number;
}) {
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [data, setData] = useState<ExperimentalCsvResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isProcess = kind === "process";
  const title = isProcess ? "Experimental Process Records" : "Experimental Property Records";
  const eyebrow = isProcess ? "Experimental Process" : "Experimental Properties";
  const placeholder = isProcess
    ? "Search polymer, product, process text, material text"
    : "Search polymer, property name, value";
  const processRows = isProcess ? ((data as ExperimentalProcessBrowseResponse | null)?.results ?? []) : [];
  const propertyRows = !isProcess ? ((data as ExperimentalPropertyBrowseResponse | null)?.results ?? []) : [];

  useEffect(() => {
    if (!open) {
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    const params = { q: query, page, page_size: pageSize };
    const request = isProcess
      ? browseExperimentalProcessRecords(params)
      : browseExperimentalPropertyRecords(params);

    request
      .then((response) => {
        if (!cancelled) {
          setData(response);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : "Failed to load CSV records");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [isProcess, open, page, pageSize, query]);

  useEffect(() => {
    if (!open) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  if (!open) {
    return null;
  }

  const matchedRecords = data?.matched_records ?? 0;
  const totalRecords = data?.total_records ?? expectedTotal;
  const totalPages = Math.max(1, Math.ceil(matchedRecords / pageSize));
  const startRecord = matchedRecords === 0 ? 0 : (page - 1) * pageSize + 1;
  const endRecord = Math.min(page * pageSize, matchedRecords);
  const rowCount = isProcess ? processRows.length : propertyRows.length;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
      <section className="flex h-[min(88vh,760px)] w-[min(1180px,calc(100vw-2rem))] flex-col overflow-hidden rounded-[28px] border border-white/80 bg-white/95 shadow-[0_30px_100px_rgba(8,17,31,0.34)]">
        <header className="flex flex-col gap-4 border-b border-slate-200 bg-slate-50/90 px-5 py-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-950 text-white">
                {isProcess ? <FlaskConical className="h-4 w-4" /> : <Sigma className="h-4 w-4" />}
              </div>
              <div className="min-w-0">
                <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-teal-700">
                  {eyebrow}
                </div>
                <h2 className="font-heading truncate text-xl font-semibold tracking-tight text-slate-950">
                  {title}
                </h2>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <MetricPill label="total records" value={formatCount(totalRecords)} />
            <MetricPill label="matched" value={formatCount(matchedRecords)} />
            <button
              type="button"
              aria-label={`Close ${title}`}
              onClick={onClose}
              className="flex h-11 w-11 items-center justify-center rounded-2xl border border-slate-200 bg-white text-slate-600 shadow-sm transition hover:border-teal-200 hover:bg-teal-50 hover:text-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="border-b border-slate-200 bg-white px-5 py-4">
          <form
            className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]"
            onSubmit={(event) => {
              event.preventDefault();
              setPage(1);
              setQuery(queryDraft.trim());
            }}
          >
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
              <Input
                value={queryDraft}
                onChange={(event) => setQueryDraft(event.target.value)}
                placeholder={placeholder}
                className="border-slate-200 bg-slate-50 pl-10"
              />
            </div>
            <select
              aria-label={`${title} rows per page`}
              value={pageSize}
              onChange={(event) => {
                setPage(1);
                setPageSize(Number(event.target.value));
              }}
              className="h-11 rounded-2xl border border-slate-200 bg-slate-50 px-4 text-sm text-slate-700 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value={25}>25 rows</option>
              <option value={50}>50 rows</option>
              <option value={100}>100 rows</option>
            </select>
            <div className="flex gap-2">
              <Button type="submit" className="min-w-24">
                Search
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setQueryDraft("");
                  setQuery("");
                  setPage(1);
                }}
              >
                Clear
              </Button>
            </div>
          </form>
        </div>

        <div className="relative min-h-0 flex-1 overflow-auto bg-white">
          {error ? (
            <div className="m-5 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {error}
            </div>
          ) : null}

          {isProcess ? (
            <table className="min-w-[1280px] w-full border-separate border-spacing-0 text-left text-sm">
              <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                <tr>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">CSV Row</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Polymer ID</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Polymer</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Product</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Process Text</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Material Text</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Source</th>
                </tr>
              </thead>
              <tbody>
                {processRows.map((row) => (
                  <tr key={`${row.source_file}-${row.source_row_number}`} className="align-top hover:bg-teal-50/35">
                    <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                      {row.source_row_number}
                    </td>
                    <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                      {row.polymer_id}
                    </td>
                    <td className="max-w-[14rem] border-b border-slate-100 px-4 py-3">
                      <div className="truncate font-medium text-slate-900" title={row.polymer_name}>
                        {row.polymer_name || <EmptyCell />}
                      </div>
                    </td>
                    <td className="max-w-[14rem] border-b border-slate-100 px-4 py-3">
                      <div className="truncate text-slate-700" title={row.product_name}>
                        {row.product_name || <EmptyCell />}
                      </div>
                    </td>
                    <td className="max-w-[28rem] border-b border-slate-100 px-4 py-3">
                      <div className="line-clamp-3 text-xs leading-5 text-slate-700" title={row.process_flow_original_text}>
                        {row.process_flow_original_text || <EmptyCell />}
                      </div>
                    </td>
                    <td className="max-w-[22rem] border-b border-slate-100 px-4 py-3">
                      <div className="line-clamp-3 text-xs leading-5 text-slate-700" title={row.material_original_text}>
                        {row.material_original_text || <EmptyCell />}
                      </div>
                    </td>
                    <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-[11px] text-slate-500">
                      {row.source_file}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <table className="min-w-[920px] w-full border-separate border-spacing-0 text-left text-sm">
              <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] uppercase tracking-[0.16em] text-slate-500">
                <tr>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">CSV Row</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Polymer ID</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Polymer</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Property</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Value</th>
                  <th className="border-b border-slate-200 px-4 py-3 font-semibold">Source</th>
                </tr>
              </thead>
              <tbody>
                {propertyRows.map((row) => (
                  <tr key={`${row.source_file}-${row.source_row_number}`} className="align-top hover:bg-teal-50/35">
                    <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                      {row.source_row_number}
                    </td>
                    <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                      {row.polymer_id}
                    </td>
                    <td className="max-w-[16rem] border-b border-slate-100 px-4 py-3">
                      <div className="truncate font-medium text-slate-900" title={row.polymer_name}>
                        {row.polymer_name || <EmptyCell />}
                      </div>
                    </td>
                    <td className="max-w-[16rem] border-b border-slate-100 px-4 py-3">
                      <div className="truncate text-slate-700" title={row.property_name_en}>
                        {row.property_name_en}
                      </div>
                    </td>
                    <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-700">
                      {row.value || <EmptyCell />}
                    </td>
                    <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-[11px] text-slate-500">
                      {row.source_file}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {!loading && !error && rowCount === 0 ? (
            <div className="px-5 py-10 text-center text-sm text-slate-500">No matching records.</div>
          ) : null}
          {loading ? (
            <div className="absolute inset-x-0 bottom-16 mx-auto w-fit rounded-full border border-white/80 bg-white px-4 py-2 text-sm text-slate-600 shadow-lg">
              Loading CSV records...
            </div>
          ) : null}
        </div>

        <footer className="flex flex-col gap-3 border-t border-slate-200 bg-slate-50/95 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="font-mono-ui text-xs text-slate-500">
            {startRecord}-{endRecord} / {formatCount(matchedRecords)}
            {query ? ` for "${query}"` : ""}
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={page <= 1 || loading}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              <ChevronLeft className="mr-2 h-4 w-4" />
              Previous
            </Button>
            <div className="min-w-24 text-center font-mono-ui text-xs text-slate-500">
              {page} / {totalPages}
            </div>
            <Button
              type="button"
              variant="outline"
              disabled={page >= totalPages || loading}
              onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
            >
              Next
              <ChevronRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function ProcessPage({ dataset: summaryDataset, analytics, analyticsLoading, analyticsError, ...props }: DatasetPageProps) {
  const dataset = {
    ...summaryDataset,
    title: "Experimental Process Data",
    description:
      "Summarizes process keywords, product names, material mentions, and extracted condition signals across polymer preparation records."
  };
  const isUnavailable = dataset.status === "Reserved";
  const analyticsUnavailable = !analytics?.process;
  const data = mergeAnalyticsData(processData, analytics?.process);
  const topMaterial = data.topMaterials[0] ?? { label: "-", value: 0 };
  const [recordBrowserOpen, setRecordBrowserOpen] = useState(false);

  return (
    <>
      <DatasetHero
        dataset={dataset}
        backDatabaseLabel="Database Analysis"
        homeLabel="Home"
        viewValue={isUnavailable ? "Unavailable" : analyticsUnavailable ? "Charts unavailable" : "Search Records"}
        onViewClick={isUnavailable || analyticsUnavailable ? undefined : () => setRecordBrowserOpen(true)}
        {...props}
      >
        {isUnavailable ? (
          <DatasetUnavailableState dataset={dataset} />
        ) : analyticsUnavailable ? (
          <DatasetAnalyticsUnavailableState loading={analyticsLoading} error={analyticsError} />
        ) : (
          <>
            <section className="grid gap-5 xl:grid-cols-[minmax(0,1.12fr)_minmax(0,0.88fr)] xl:items-stretch">
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<BarChart3 className="h-4 w-4" />}
                title="Process Flow Keywords"
              >
                <RankedBarsWithSummary data={data.topTerms} />
              </ChartPanel>
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<Atom className="h-4 w-4" />}
                title="Material Entity Cloud"
              >
                <div className="flex h-full flex-1 flex-col justify-between gap-4">
                  <BubbleCloud data={data.topMaterials} />
                  <div className="grid gap-3 sm:grid-cols-3">
                    <MetricPill label="entities" value={formatCount(data.topMaterials.length)} />
                    <MetricPill label="top entity" value={topMaterial.label} />
                    <MetricPill label="top count" value={formatCount(topMaterial.value)} />
                  </div>
                </div>
              </ChartPanel>
            </section>
            <section className="grid gap-5 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] xl:items-stretch">
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<TableProperties className="h-4 w-4" />}
                title="Product Name Ranking"
              >
                <RankedBarsWithSummary data={data.topProducts} />
              </ChartPanel>
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<Layers3 className="h-4 w-4" />}
                title="Process Condition Signals"
              >
                <CoverageSignals data={data.processSignals} summary={data.processSignalSummary} />
              </ChartPanel>
            </section>
          </>
        )}
      </DatasetHero>
      {isUnavailable || analyticsUnavailable ? null : (
        <ExperimentalCsvRecordBrowser
          kind="process"
          open={recordBrowserOpen}
          onClose={() => setRecordBrowserOpen(false)}
          expectedTotal={dataset.recordCount ?? data.rows}
        />
      )}
    </>
  );
}

function PropertyPage({ dataset: summaryDataset, analytics, analyticsLoading, analyticsError, ...props }: DatasetPageProps) {
  const dataset = {
    ...summaryDataset,
    title: "Experimental Property Data",
    description:
      "Summarizes property categories, high-frequency property names, value ranges, and representative properties across polymer records."
  };
  const isUnavailable = dataset.status === "Reserved";
  const analyticsUnavailable = !analytics?.property;
  const data = mergeAnalyticsData(propertyData, analytics?.property);
  const [recordBrowserOpen, setRecordBrowserOpen] = useState(false);

  return (
    <>
      <DatasetHero
        dataset={dataset}
        backDatabaseLabel="Database Analysis"
        homeLabel="Home"
        viewValue={isUnavailable ? "Unavailable" : analyticsUnavailable ? "Charts unavailable" : "Search Records"}
        onViewClick={isUnavailable || analyticsUnavailable ? undefined : () => setRecordBrowserOpen(true)}
        {...props}
      >
        {isUnavailable ? (
          <DatasetUnavailableState dataset={dataset} />
        ) : analyticsUnavailable ? (
          <DatasetAnalyticsUnavailableState loading={analyticsLoading} error={analyticsError} />
        ) : (
          <>
            <section className="grid gap-5 xl:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)] xl:items-stretch">
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<PieChart className="h-4 w-4" />}
                title="Property Category Share"
              >
                <DonutChartWithSummary data={data.categories} topLabel="top category" />
              </ChartPanel>
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<BarChart3 className="h-4 w-4" />}
                title="High-Frequency Property Names"
              >
                <RankedBarsWithSummary data={data.topProperties} />
              </ChartPanel>
            </section>
            <section className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)] xl:items-stretch">
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<Sigma className="h-4 w-4" />}
                title="High-Frequency Property Ranges"
              >
                <RangePlot data={data.ranges.slice(0, 4)} />
              </ChartPanel>
              <ChartPanel
                className="flex h-full flex-col"
                bodyClassName="flex flex-1 flex-col"
                icon={<Search className="h-4 w-4" />}
                title="Representative Properties"
              >
                <CategoryRepresentativeCards data={data.categoryTop} categories={data.categories} />
              </ChartPanel>
            </section>
          </>
        )}
      </DatasetHero>
      {isUnavailable || analyticsUnavailable ? null : (
        <ExperimentalCsvRecordBrowser
          kind="property"
          open={recordBrowserOpen}
          onClose={() => setRecordBrowserOpen(false)}
          expectedTotal={dataset.recordCount ?? data.rows}
        />
      )}
    </>
  );
}

function StructureEffectPage({ dataset: summaryDataset, analytics, analyticsLoading, analyticsError, ...props }: DatasetPageProps) {
  const data = mergeAnalyticsData(structureEffectData, analytics?.structureEffect);
  const dataset = {
    ...summaryDataset,
    title: "Polymer Structure-Property Data",
    description:
      "Links polymer structures with key properties, measurement origins, units, and value ranges to support structure-property comparison."
  };
  const isUnavailable = dataset.status === "Reserved";
  const analyticsUnavailable = !analytics?.structureEffect;
  const [recordBrowserOpen, setRecordBrowserOpen] = useState(false);

  return (
    <>
      <DatasetHero
        dataset={dataset}
        backDatabaseLabel="Database Analysis"
        homeLabel="Home"
        viewValue={isUnavailable ? "Unavailable" : analyticsUnavailable ? "Charts unavailable" : "Search Records"}
        onViewClick={isUnavailable || analyticsUnavailable ? undefined : () => setRecordBrowserOpen(true)}
        {...props}
      >
        {isUnavailable ? (
          <DatasetUnavailableState dataset={dataset} />
        ) : analyticsUnavailable ? (
          <DatasetAnalyticsUnavailableState loading={analyticsLoading} error={analyticsError} />
        ) : (
          <>
        <section className="grid gap-5 xl:grid-cols-[minmax(0,1.02fr)_minmax(0,0.98fr)] xl:items-start">
          <div className="grid gap-5">
            <ChartPanel icon={<Network className="h-4 w-4" />} title="Property Origin Matrix">
              <SourceMatrixWithSummary data={data} />
            </ChartPanel>
            <ChartPanel icon={<PieChart className="h-4 w-4" />} title="Unit Distribution">
              <DonutChartWithSummary data={data.units} topLabel="top unit" />
            </ChartPanel>
          </div>
          <div className="grid gap-5">
            <ChartPanel icon={<BarChart3 className="h-4 w-4" />} title="Property Record Counts">
              <RankedBarsWithSummary data={data.properties} limit={9} />
            </ChartPanel>
            <ChartPanel icon={<Sigma className="h-4 w-4" />} title="Structure-Property Value Ranges">
              <StructureRangeCards data={data.ranges.slice(0, 3)} />
            </ChartPanel>
          </div>
        </section>
          </>
        )}
      </DatasetHero>
      {isUnavailable || analyticsUnavailable ? null : (
        <StructurePropertyRecordBrowser
          open={recordBrowserOpen}
          onClose={() => setRecordBrowserOpen(false)}
          expectedTotal={dataset.recordCount ?? data.rows}
        />
      )}
    </>
  );
}

type PropertyFilterDraft = {
  id: number;
  optionKey: string;
  minValue: string;
  maxValue: string;
};

function formatOptionalFilterNumber(value: number | null, digits = 4) {
  return value === null ? "-" : formatNumber(value, digits);
}

function parseFilterBound(value: string, label: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${label} must be numeric`);
  }
  return parsed;
}

function buildPropertyFilterCondition(
  draft: PropertyFilterDraft,
  option: PropertyFilterOption | undefined
): PropertyFilterCondition {
  if (!option) {
    throw new Error("Select a property before searching");
  }

  const minValue = parseFilterBound(draft.minValue, "Min");
  const maxValue = parseFilterBound(draft.maxValue, "Max");
  if (minValue === null && maxValue === null) {
    throw new Error("Each condition needs at least one threshold");
  }
  if (minValue !== null && maxValue !== null && minValue > maxValue) {
    throw new Error("Min cannot be greater than max");
  }

  if (option.filter_type === "standardized") {
    return {
      filter_type: "standardized",
      property_key: option.property_key,
      canonical_unit: option.canonical_unit,
      min_value: minValue,
      max_value: maxValue
    };
  }

  return {
    filter_type: "raw",
    property_name: option.property_name,
    property_unit_clean: option.property_unit_clean,
    min_value: minValue,
    max_value: maxValue
  };
}

function PropertyFilterPage({ dataset: summaryDataset, onBackHome, onBackDatabase }: DatasetPageProps) {
  const dataset = {
    ...summaryDataset,
    title: "Property Threshold Filter",
    englishTitle: "Property Threshold",
    description:
      "Threshold search over the reliable standardized polymer property table with standardized and raw property options."
  };
  const isUnavailable = dataset.status === "Reserved";
  const [optionsData, setOptionsData] = useState<PropertyFilterOptionsResponse | null>(null);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [conditionDrafts, setConditionDrafts] = useState<PropertyFilterDraft[]>([
    { id: 1, optionKey: "", minValue: "", maxValue: "" }
  ]);
  const [nextConditionId, setNextConditionId] = useState(2);
  const [queryDraft, setQueryDraft] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [activeFilters, setActiveFilters] = useState<PropertyFilterCondition[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [searchData, setSearchData] = useState<PropertyFilterSearchResponse | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  useEffect(() => {
    if (isUnavailable) {
      return;
    }

    let cancelled = false;
    setOptionsLoading(true);
    setOptionsError(null);
    fetchPropertyFilterOptions()
      .then((response) => {
        if (cancelled) {
          return;
        }
        setOptionsData(response);
        const firstKey = response.options[0]?.option_key ?? "";
        if (firstKey) {
          setConditionDrafts((current) =>
            current.map((draft) => (draft.optionKey ? draft : { ...draft, optionKey: firstKey }))
          );
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setOptionsError(nextError instanceof Error ? nextError.message : "Failed to load property options");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setOptionsLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [isUnavailable]);

  useEffect(() => {
    if (activeFilters.length === 0) {
      return;
    }

    let cancelled = false;
    setSearchLoading(true);
    setSearchError(null);
    searchPropertyFilterRecords({
      filters: activeFilters,
      q: activeQuery,
      page,
      page_size: pageSize
    })
      .then((response) => {
        if (!cancelled) {
          setSearchData(response);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setSearchError(nextError instanceof Error ? nextError.message : "Failed to search properties");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setSearchLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [activeFilters, activeQuery, page, pageSize]);

  const options = optionsData?.options ?? [];
  const optionsByKey = useMemo(() => new Map(options.map((option) => [option.option_key, option])), [options]);
  const matchedRecords = searchData?.matched_records ?? 0;
  const totalPages = Math.max(1, Math.ceil(matchedRecords / pageSize));
  const startRecord = matchedRecords === 0 ? 0 : (page - 1) * pageSize + 1;
  const endRecord = Math.min(page * pageSize, matchedRecords);

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const filters = conditionDrafts.map((draft) => buildPropertyFilterCondition(draft, optionsByKey.get(draft.optionKey)));
      setSearchError(null);
      setPage(1);
      setActiveQuery(queryDraft.trim());
      setActiveFilters(filters);
    } catch (nextError) {
      setSearchError(nextError instanceof Error ? nextError.message : "Invalid filter condition");
    }
  }

  return (
    <DatasetHero
      dataset={dataset}
      onBackHome={onBackHome}
      onBackDatabase={onBackDatabase}
      backDatabaseLabel="Database Analysis"
      homeLabel="Home"
      recordLabel="records"
      viewLabel="options"
      viewValue={optionsData ? formatCount(optionsData.options.length) : "Loading"}
    >
      {isUnavailable ? (
        <DatasetUnavailableState dataset={dataset} />
      ) : (
        <section className="grid gap-5">
          <div className="grid gap-3 md:grid-cols-4">
            <MetricPill label="source rows" value={formatCount(optionsData?.total_records ?? dataset.recordCount)} />
            <MetricPill label="mapped rows" value={formatCount(optionsData?.mapped_records ?? null)} />
            <MetricPill label="raw rows" value={formatCount(optionsData?.raw_records ?? null)} />
            <MetricPill label="matched smiles" value={formatCount(searchData?.matched_records ?? null)} />
          </div>

          <section className="rounded-[28px] border border-white/80 bg-white/85 p-5 shadow-sm">
            <form className="grid gap-4" onSubmit={submitSearch}>
              <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-slate-950 text-white">
                    <Filter className="h-4 w-4" />
                  </div>
                  <div>
                    <h2 className="font-heading text-xl font-semibold tracking-tight text-slate-950">Threshold Conditions</h2>
                    <div className="mt-1 text-xs font-medium uppercase tracking-[0.18em] text-teal-700">
                      {optionsLoading ? "Loading options" : `${formatCount(options.length)} properties`}
                    </div>
                  </div>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  disabled={!options.length}
                  onClick={() => {
                    const firstKey = options[0]?.option_key ?? "";
                    setConditionDrafts((current) => [
                      ...current,
                      { id: nextConditionId, optionKey: firstKey, minValue: "", maxValue: "" }
                    ]);
                    setNextConditionId((current) => current + 1);
                  }}
                  className="bg-white"
                >
                  <Plus className="mr-2 h-4 w-4" />
                  Add Condition
                </Button>
              </div>

              {optionsError ? (
                <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                  {optionsError}
                </div>
              ) : null}

              <div className="grid gap-3">
                {conditionDrafts.map((draft, index) => {
                  const selectedOption = optionsByKey.get(draft.optionKey);
                  return (
                    <div
                      key={draft.id}
                      className="grid gap-3 rounded-2xl border border-slate-200 bg-slate-50/80 p-3 lg:grid-cols-[minmax(0,1.8fr)_minmax(120px,0.45fr)_minmax(120px,0.45fr)_auto]"
                    >
                      <div className="grid gap-2">
                        <label className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                          Property {index + 1}
                        </label>
                        <select
                          value={draft.optionKey}
                          onChange={(event) => {
                            const optionKey = event.target.value;
                            setConditionDrafts((current) =>
                              current.map((item) => (item.id === draft.id ? { ...item, optionKey } : item))
                            );
                          }}
                          className="h-11 min-w-0 rounded-2xl border border-slate-200 bg-white px-3 text-sm text-slate-800 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          {options.map((option) => (
                            <option key={option.option_key} value={option.option_key}>
                              {option.filter_type === "standardized" ? "[std]" : "[raw]"} {option.label}
                            </option>
                          ))}
                        </select>
                        {selectedOption ? (
                          <div className="flex flex-wrap gap-2 text-xs text-slate-500">
                            <Badge className="bg-white text-slate-600">{selectedOption.filter_type}</Badge>
                            <span>
                              {formatOptionalFilterNumber(selectedOption.min_value)} -{" "}
                              {formatOptionalFilterNumber(selectedOption.max_value)}
                            </span>
                            <span>{selectedOption.canonical_unit ?? selectedOption.property_unit_clean ?? "unitless"}</span>
                          </div>
                        ) : null}
                      </div>
                      <div className="grid gap-2">
                        <label className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">Min</label>
                        <Input
                          value={draft.minValue}
                          onChange={(event) => {
                            const minValue = event.target.value;
                            setConditionDrafts((current) =>
                              current.map((item) => (item.id === draft.id ? { ...item, minValue } : item))
                            );
                          }}
                          placeholder="optional"
                          className="bg-white"
                        />
                      </div>
                      <div className="grid gap-2">
                        <label className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">Max</label>
                        <Input
                          value={draft.maxValue}
                          onChange={(event) => {
                            const maxValue = event.target.value;
                            setConditionDrafts((current) =>
                              current.map((item) => (item.id === draft.id ? { ...item, maxValue } : item))
                            );
                          }}
                          placeholder="optional"
                          className="bg-white"
                        />
                      </div>
                      <div className="flex items-end">
                        <Button
                          type="button"
                          variant="outline"
                          disabled={conditionDrafts.length <= 1}
                          aria-label={`Remove condition ${index + 1}`}
                          onClick={() => setConditionDrafts((current) => current.filter((item) => item.id !== draft.id))}
                          className="h-11 w-11 bg-white px-0"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>

              <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]">
                <div className="relative">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                  <Input
                    value={queryDraft}
                    onChange={(event) => setQueryDraft(event.target.value)}
                    placeholder="Search polymer name or SMILES"
                    className="bg-slate-50 pl-10"
                  />
                </div>
                <select
                  aria-label="Rows per page"
                  value={pageSize}
                  onChange={(event) => {
                    setPage(1);
                    setPageSize(Number(event.target.value));
                  }}
                  className="h-11 rounded-2xl border border-slate-200 bg-slate-50 px-4 text-sm text-slate-700 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <option value={10}>10 rows</option>
                  <option value={25}>25 rows</option>
                  <option value={50}>50 rows</option>
                  <option value={100}>100 rows</option>
                </select>
                <Button type="submit" disabled={optionsLoading || !options.length || searchLoading}>
                  <Search className="mr-2 h-4 w-4" />
                  Search
                </Button>
              </div>
            </form>
          </section>

          <section className="overflow-hidden rounded-[28px] border border-white/80 bg-white/90 shadow-sm">
            <header className="flex flex-col gap-3 border-b border-slate-200 bg-slate-50/90 px-5 py-4 md:flex-row md:items-center md:justify-between">
              <div>
                <h2 className="font-heading text-xl font-semibold tracking-tight text-slate-950">Matched Polymers</h2>
                <div className="mt-1 font-mono-ui text-xs text-slate-500">
                  {startRecord}-{endRecord} / {formatCount(matchedRecords)}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  disabled={page <= 1 || searchLoading}
                  onClick={() => setPage((current) => Math.max(1, current - 1))}
                  className="bg-white"
                >
                  <ChevronLeft className="mr-2 h-4 w-4" />
                  Previous
                </Button>
                <div className="min-w-20 text-center font-mono-ui text-xs text-slate-500">
                  {page} / {totalPages}
                </div>
                <Button
                  type="button"
                  variant="outline"
                  disabled={page >= totalPages || searchLoading}
                  onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                  className="bg-white"
                >
                  Next
                  <ChevronRight className="ml-2 h-4 w-4" />
                </Button>
              </div>
            </header>

            {searchError ? (
              <div className="m-5 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                {searchError}
              </div>
            ) : null}

            <div className="divide-y divide-slate-100">
              {searchData?.results.map((result) => (
                <article key={result.smiles ?? result.canonical_smiles ?? result.records[0]?.filter_record_id} className="p-5">
                  <div className="grid gap-3 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.4fr)]">
                    <div className="min-w-0">
                      <div className="font-heading text-lg font-semibold text-slate-950">
                        {result.polymer_name || "Unnamed polymer"}
                      </div>
                      <div className="mt-2 truncate font-mono-ui text-xs text-slate-600" title={result.smiles ?? ""}>
                        {result.smiles ?? <EmptyCell />}
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Badge className="bg-teal-50 text-teal-800">{result.matched_filters} filters</Badge>
                        {result.canonical_smiles ? (
                          <Badge className="bg-slate-100 text-slate-600">canonical</Badge>
                        ) : null}
                      </div>
                    </div>
                    <div className="grid gap-3">
                      {result.records.map((record) => {
                        const displayValue =
                          record.canonical_value !== null
                            ? `${formatNumber(record.canonical_value, 4)} ${record.canonical_unit ?? ""}`.trim()
                            : `${record.property_value} ${record.property_unit_clean ?? ""}`.trim();
                        const originalValue = `${record.property_value} ${record.property_unit_raw ?? ""}`.trim();
                        return (
                          <div key={`${record.filter_index}-${record.filter_record_id}`} className="rounded-2xl border border-slate-200 bg-slate-50/80 p-3">
                            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                              <div>
                                <div className="font-medium text-slate-950">
                                  {record.property_label ?? record.property_name}
                                </div>
                                <div className="mt-1 text-sm text-slate-600">
                                  {displayValue || <EmptyCell />}
                                </div>
                                <div className="mt-1 font-mono-ui text-xs text-slate-500">
                                  raw {originalValue || "-"}
                                </div>
                              </div>
                              <div className="flex flex-wrap gap-2 lg:justify-end">
                                {record.value_origin ? (
                                  <Badge className="bg-white text-slate-700">{record.value_origin}</Badge>
                                ) : null}
                                {record.reliable_score !== null ? (
                                  <Badge className="bg-white text-slate-700">
                                    score {formatNumber(record.reliable_score, 2)}
                                  </Badge>
                                ) : null}
                                {record.unit_conversion_status ? (
                                  <Badge className="bg-white text-slate-700">{record.unit_conversion_status}</Badge>
                                ) : null}
                                {record.soft_quality_flags ? (
                                  <Badge className="bg-amber-50 text-amber-800">{record.soft_quality_flags}</Badge>
                                ) : null}
                                {record.duplicate_flag ? (
                                  <Badge className="bg-slate-100 text-slate-700">{record.duplicate_flag}</Badge>
                                ) : null}
                              </div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </article>
              ))}
            </div>

            {searchLoading ? (
              <div className="px-5 py-8 text-center text-sm text-slate-500">Loading matches...</div>
            ) : null}
            {!searchLoading && !searchError && activeFilters.length > 0 && searchData?.results.length === 0 ? (
              <div className="px-5 py-8 text-center text-sm text-slate-500">No matching polymers.</div>
            ) : null}
            {!searchLoading && !searchError && activeFilters.length === 0 ? (
              <div className="px-5 py-8 text-center text-sm text-slate-500">No threshold query submitted.</div>
            ) : null}
          </section>
        </section>
      )}
    </DatasetHero>
  );
}

function DftPage({ dataset: summaryDataset, analytics, analyticsLoading, analyticsError, ...props }: DatasetPageProps) {
  const data = mergeAnalyticsData(dftData, analytics?.dft);
  const dataset = {
    ...summaryDataset,
    title: "DFT Conformation Data",
    englishTitle: "DFT Conformation",
    description:
      "Browse optimized molecular conformations, inspect the selected final 3D structure, compare energy changes during optimization, and review atom and HOMO/LUMO summaries."
  };
  const isUnavailable = dataset.status === "Reserved";
  const analyticsUnavailable = !analytics?.dft;
  const [pcaPoints, setPcaPoints] = useState<DftPcaPoint[]>([]);
  const [pcaLoading, setPcaLoading] = useState(true);
  const [pcaError, setPcaError] = useState<string | null>(null);
  const [selectedMolId, setSelectedMolId] = useState<string | null>(null);
  const [selectedMolecule, setSelectedMolecule] = useState<DftMoleculeDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [recordBrowserOpen, setRecordBrowserOpen] = useState(false);

  useEffect(() => {
    if (isUnavailable) {
      setPcaPoints([]);
      setPcaLoading(false);
      setPcaError(null);
      setSelectedMolId(null);
      return;
    }

    let cancelled = false;

    async function loadPcaSample() {
      setPcaLoading(true);
      setPcaError(null);
      try {
        const response = await fetchDftPcaSample(200);
        if (cancelled) return;
        setPcaPoints(response.results);
        setSelectedMolId(response.results[0]?.mol_id ?? null);
      } catch (nextError) {
        if (!cancelled) {
          setPcaError(nextError instanceof Error ? nextError.message : "Failed to load molecule map");
        }
      } finally {
        if (!cancelled) {
          setPcaLoading(false);
        }
      }
    }

    void loadPcaSample();

    return () => {
      cancelled = true;
    };
  }, [isUnavailable]);

  useEffect(() => {
    if (!selectedMolId) {
      setSelectedMolecule(null);
      return;
    }

    let cancelled = false;
    const molId = selectedMolId;

    async function loadMolecule() {
      setDetailError(null);
      setSelectedMolecule(null);
      try {
        const molecule = await fetchDftMolecule(molId);
        if (!cancelled) {
          setSelectedMolecule(molecule);
        }
      } catch (nextError) {
        if (!cancelled) {
          setSelectedMolecule(null);
          setDetailError(nextError instanceof Error ? nextError.message : "Failed to load conformation trajectory");
        }
      }
    }

    void loadMolecule();

    return () => {
      cancelled = true;
    };
  }, [selectedMolId]);

  return (
    <>
      <DatasetHero
        dataset={dataset}
        backDatabaseLabel="Database Analysis"
        homeLabel="Home"
        recordLabel="Records"
        viewLabel="View"
        viewValue={isUnavailable ? "Unavailable" : analyticsUnavailable ? "Charts unavailable" : "Search Records"}
        onViewClick={isUnavailable || analyticsUnavailable ? undefined : () => setRecordBrowserOpen(true)}
        {...props}
      >
        {isUnavailable ? (
          <DatasetUnavailableState dataset={dataset} />
        ) : analyticsUnavailable ? (
          <DatasetAnalyticsUnavailableState loading={analyticsLoading} error={analyticsError} />
        ) : (
          <>
        <section className="grid gap-5 xl:grid-cols-3 xl:items-stretch">
          <ChartPanel
            className="flex h-full min-w-0 flex-col"
            bodyClassName="flex flex-1 flex-col"
            icon={<Network className="h-4 w-4" />}
            title="Final-State Molecule Map"
          >
            <PcaDistribution3D
              points={pcaPoints}
              selectedMolId={selectedMolId}
              isLoading={pcaLoading}
              error={pcaError}
              onSelect={setSelectedMolId}
            />
          </ChartPanel>
          <ChartPanel
            className="flex h-full min-w-0 flex-col"
            bodyClassName="flex flex-1 flex-col"
            icon={<Orbit className="h-4 w-4" />}
            title="DFT 3D Conformation"
          >
            <DftMolecule3D molecule={selectedMolecule} detailError={detailError} />
          </ChartPanel>
          <ChartPanel
            className="flex h-full min-w-0 flex-col"
            bodyClassName="flex flex-1 flex-col"
            icon={<BarChart3 className="h-4 w-4" />}
            title="Optimization Energy Trace"
          >
            <EnergyTrace molecule={selectedMolecule} detailError={detailError} />
          </ChartPanel>
        </section>
        <section className="grid gap-5">
          <ChartPanel
            className="flex h-full flex-col"
            bodyClassName="flex flex-1 flex-col"
            icon={<Sigma className="h-4 w-4" />}
            title="HOMO / LUMO Energy Distribution"
          >
            <OrbitalDistributionChart data={data.orbitalDistributions} />
          </ChartPanel>
        </section>
        <section className="grid gap-5 xl:grid-cols-2 xl:items-start">
          <ChartPanel
            className="flex flex-col"
            icon={<PieChart className="h-4 w-4" />}
            title="Atom Composition"
          >
            <AtomCompositionCompact data={data.atomTotals} />
          </ChartPanel>
          <ChartPanel
            className="flex h-full flex-col"
            bodyClassName="flex flex-1 flex-col"
            icon={<TableProperties className="h-4 w-4" />}
            title="Final-State Molecule Summary"
          >
            <div className="flex h-full flex-1 flex-col justify-between gap-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <MetricPill label="conformation groups" value={formatCount(data.molCount)} />
                <MetricPill label="optimization steps" value={formatCount(data.rows)} />
                <MetricPill label="median steps" value={String(data.stepRange.median)} />
                <MetricPill label="longest path" value={`${data.stepRange.max} steps`} />
                <MetricPill label="median atoms" value={String(data.atomRange.median)} />
                <MetricPill label="median gap eV" value={formatNumber(data.gapRange.median, 3)} />
              </div>
              <div className="rounded-2xl border border-white/80 bg-white/75 px-4 py-3 text-sm leading-6 text-slate-600 shadow-sm">
                Each conformation group represents one optimization path. The record count covers all optimization steps, and the step metrics help compare path length and final geometry size.
              </div>
            </div>
          </ChartPanel>
        </section>
          </>
        )}
      </DatasetHero>
      {isUnavailable || analyticsUnavailable ? null : (
        <DftRecordBrowser
          open={recordBrowserOpen}
          onClose={() => setRecordBrowserOpen(false)}
          expectedMolecules={data.molCount}
          expectedSteps={dataset.recordCount ?? data.rows}
        />
      )}
    </>
  );
}

function FormulationRecordBrowser({
  open,
  onClose,
  expectedTotal
}: {
  open: boolean;
  onClose: () => void;
  expectedTotal: number;
}) {
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [data, setData] = useState<FormulationBrowseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    browseFormulationRecords({ q: query, page, page_size: pageSize })
      .then((response) => {
        if (!cancelled) {
          setData(response);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : "Failed to load formulation records");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [open, page, pageSize, query]);

  useEffect(() => {
    if (!open) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, open]);

  if (!open) {
    return null;
  }

  const matchedRecords = data?.matched_records ?? 0;
  const totalRecords = data?.total_records ?? expectedTotal;
  const totalPages = Math.max(1, Math.ceil(matchedRecords / pageSize));
  const startRecord = matchedRecords === 0 ? 0 : (page - 1) * pageSize + 1;
  const endRecord = Math.min(page * pageSize, matchedRecords);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4 py-6 backdrop-blur-sm">
      <section className="flex h-[min(88vh,760px)] w-[min(1180px,calc(100vw-2rem))] flex-col overflow-hidden rounded-[28px] border border-white/80 bg-white/95 shadow-[0_30px_100px_rgba(8,17,31,0.34)]">
        <header className="flex flex-col gap-4 border-b border-slate-200 bg-slate-50/90 px-5 py-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-950 text-white">
                <TableProperties className="h-4 w-4" />
              </div>
              <div className="min-w-0">
                <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-teal-700">
                  Formulation Ratios
                </div>
                <h2 className="font-heading truncate text-xl font-semibold tracking-tight text-slate-950">
                  Formulation Record Search
                </h2>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <MetricPill label="total records" value={formatCount(totalRecords)} />
            <MetricPill label="matched" value={formatCount(matchedRecords)} />
            {data?.source_status ? <MetricPill label="source" value={data.source_status} /> : null}
            <button
              type="button"
              aria-label="Close formulation record search"
              onClick={onClose}
              className="flex h-11 w-11 items-center justify-center rounded-2xl border border-slate-200 bg-white text-slate-600 shadow-sm transition hover:border-teal-200 hover:bg-teal-50 hover:text-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="border-b border-slate-200 bg-white px-5 py-4">
          <form
            className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto]"
            onSubmit={(event) => {
              event.preventDefault();
              setPage(1);
              setQuery(queryDraft.trim());
            }}
          >
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
              <Input
                value={queryDraft}
                onChange={(event) => setQueryDraft(event.target.value)}
                placeholder="Search polymer, formulation, catalyst, solvent, temperature"
                className="border-slate-200 bg-slate-50 pl-10"
              />
            </div>
            <select
              aria-label="Formulation rows per page"
              value={pageSize}
              onChange={(event) => {
                setPage(1);
                setPageSize(Number(event.target.value));
              }}
              className="h-11 rounded-2xl border border-slate-200 bg-slate-50 px-4 text-sm text-slate-700 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value={25}>25 rows</option>
              <option value={50}>50 rows</option>
              <option value={100}>100 rows</option>
            </select>
            <div className="flex gap-2">
              <Button type="submit" className="min-w-24">
                Search
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setQueryDraft("");
                  setQuery("");
                  setPage(1);
                }}
              >
                Clear
              </Button>
            </div>
          </form>
          {data?.source_message ? <div className="mt-3 text-xs text-slate-500">{data.source_message}</div> : null}
        </div>

        <div className="relative min-h-0 flex-1 overflow-auto bg-white">
          {error ? (
            <div className="m-5 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
              {error}
            </div>
          ) : null}

          <table className="min-w-[1180px] w-full border-separate border-spacing-0 text-left text-sm">
            <thead className="sticky top-0 z-10 bg-slate-50 text-[11px] uppercase tracking-[0.16em] text-slate-500">
              <tr>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Record</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Polymer</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Formulation</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Catalyst</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Solvent</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Temperature</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Time</th>
                <th className="border-b border-slate-200 px-4 py-3 font-semibold">Source</th>
              </tr>
            </thead>
            <tbody>
              {data?.results.map((row) => (
                <tr key={row.formulation_id} className="align-top hover:bg-teal-50/35">
                  <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-xs text-slate-600">
                    #{row.knowledge_id}
                  </td>
                  <td className="max-w-[14rem] border-b border-slate-100 px-4 py-3">
                    <div className="truncate font-medium text-slate-900" title={row.polymer_iupac ?? undefined}>
                      {row.polymer_iupac || <EmptyCell />}
                    </div>
                  </td>
                  <td className="max-w-[28rem] border-b border-slate-100 px-4 py-3">
                    <div className="line-clamp-3 text-xs leading-5 text-slate-700" title={row.formulation ?? undefined}>
                      {row.formulation || <EmptyCell />}
                    </div>
                  </td>
                  <td className="border-b border-slate-100 px-4 py-3 text-slate-700">{row.catalyst || <EmptyCell />}</td>
                  <td className="border-b border-slate-100 px-4 py-3 text-slate-700">{row.solvent || <EmptyCell />}</td>
                  <td className="border-b border-slate-100 px-4 py-3 text-slate-700">{row.temperature || <EmptyCell />}</td>
                  <td className="border-b border-slate-100 px-4 py-3 text-slate-700">{row.reaction_time || <EmptyCell />}</td>
                  <td className="border-b border-slate-100 px-4 py-3 font-mono-ui text-[11px] text-slate-500">
                    {row.source_file}:{row.source_row_number}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {!loading && !error && data?.results.length === 0 ? (
            <div className="px-5 py-10 text-center text-sm text-slate-500">No matching records.</div>
          ) : null}
          {loading ? (
            <div className="absolute inset-x-0 bottom-16 mx-auto w-fit rounded-full border border-white/80 bg-white px-4 py-2 text-sm text-slate-600 shadow-lg">
              Loading formulation records...
            </div>
          ) : null}
        </div>

        <footer className="flex flex-col gap-3 border-t border-slate-200 bg-slate-50/95 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="font-mono-ui text-xs text-slate-500">
            {startRecord}-{endRecord} / {formatCount(matchedRecords)}
            {query ? ` for "${query}"` : ""}
          </div>
          <div className="flex items-center gap-2">
            <Button type="button" variant="outline" disabled={page <= 1 || loading} onClick={() => setPage((current) => Math.max(1, current - 1))}>
              <ChevronLeft className="mr-2 h-4 w-4" />
              Previous
            </Button>
            <div className="min-w-24 text-center font-mono-ui text-xs text-slate-500">
              {page} / {totalPages}
            </div>
            <Button type="button" variant="outline" disabled={page >= totalPages || loading} onClick={() => setPage((current) => Math.min(totalPages, current + 1))}>
              Next
              <ChevronRight className="ml-2 h-4 w-4" />
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function FormulationPage({ dataset: summaryDataset, analytics, analyticsLoading, analyticsError, ...props }: DatasetPageProps) {
  const [recordBrowserOpen, setRecordBrowserOpen] = useState(false);
  const data = mergeAnalyticsData(formulationData, analytics?.formulation);
  const dataset = {
    ...summaryDataset,
    title: "Formulation Ratio Data",
    description:
      "Summarizes formulation components, ratio expressions, catalysts, solvents, temperature bands, time units, and representative formulation examples."
  };
  const isUnavailable = dataset.status === "Reserved";
  const analyticsUnavailable = !analytics?.formulation;

  return (
    <DatasetHero
      dataset={dataset}
      backDatabaseLabel="Database Analysis"
      homeLabel="Home"
      viewValue={isUnavailable ? "Unavailable" : analyticsUnavailable ? "Charts unavailable" : "Search Records"}
      onViewClick={isUnavailable || analyticsUnavailable ? undefined : () => setRecordBrowserOpen(true)}
      {...props}
    >
      {isUnavailable ? (
        <DatasetUnavailableState dataset={dataset} />
      ) : analyticsUnavailable ? (
        <DatasetAnalyticsUnavailableState loading={analyticsLoading} error={analyticsError} />
      ) : (
        <>
      <section className="grid gap-5 xl:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
        <ChartPanel icon={<Database className="h-4 w-4" />} title="Field Coverage">
          <div className="grid gap-4 sm:grid-cols-2">
            <MetricPill label="document groups" value={formatCount(data.files)} />
            <MetricPill label="records" value={formatCount(dataset.recordCount)} />
            <MetricPill label="avg components" value={averageComponentCount(data.componentCounts)} />
            <MetricPill label="median components" value={medianComponentCount(data.componentCounts)} />
          </div>
          <div className="mt-4">
            <CoverageGrid data={data.coverage} />
          </div>
        </ChartPanel>
        <ChartPanel icon={<Layers3 className="h-4 w-4" />} title="Formula Component Count">
          <ComponentDistribution data={data.componentCounts} />
        </ChartPanel>
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.08fr)_minmax(0,0.92fr)] xl:items-stretch">
        <ChartPanel
          className="flex h-full flex-col"
          bodyClassName="flex flex-1 flex-col"
          icon={<BarChart3 className="h-4 w-4" />}
          title="High-Frequency Formula Components"
        >
          <RankedBarsWithSummary data={data.topComponents} />
        </ChartPanel>
        <ChartPanel
          className="flex h-full flex-col"
          bodyClassName="flex flex-1 flex-col"
          icon={<PieChart className="h-4 w-4" />}
          title="Polymer Family Distribution"
        >
          <DonutChartWithSummary data={data.polymerFamilies} topLabel="top family" />
        </ChartPanel>
      </section>

      <section className="grid gap-5 xl:grid-cols-3">
        <ChartPanel icon={<Sigma className="h-4 w-4" />} title="Formula Expression Matches">
          <HorizontalBars data={data.ratioTypes} />
        </ChartPanel>
        <ChartPanel icon={<FlaskConical className="h-4 w-4" />} title="Process Temperature Bands">
          <HorizontalBars data={data.tempBands} />
        </ChartPanel>
        <ChartPanel icon={<PieChart className="h-4 w-4" />} title="Time Expression Distribution">
          <DonutChart data={data.timeUnits} />
        </ChartPanel>
      </section>

      <section className="grid gap-5 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,0.95fr)_minmax(0,1.1fr)] xl:items-stretch">
        <ChartPanel
          className="flex h-full flex-col"
          bodyClassName="flex flex-1 flex-col"
          icon={<Atom className="h-4 w-4" />}
          title="Common Catalysts"
        >
          <RankedBarsWithSummary data={data.topCatalysts} />
        </ChartPanel>
        <ChartPanel
          className="flex h-full flex-col"
          bodyClassName="flex flex-1 flex-col"
          icon={<Search className="h-4 w-4" />}
          title="Common Solvents"
        >
          <RankedBarsWithSummary data={data.topSolvents} />
        </ChartPanel>
        <ChartPanel
          className="flex h-full flex-col"
          bodyClassName="flex flex-1 flex-col"
          icon={<TableProperties className="h-4 w-4" />}
          title="Formulation Examples"
        >
          <FormulaExamples data={data.examples} />
        </ChartPanel>
      </section>
        </>
      )}
      {isUnavailable || analyticsUnavailable ? null : (
        <FormulationRecordBrowser
          open={recordBrowserOpen}
          onClose={() => setRecordBrowserOpen(false)}
          expectedTotal={dataset.recordCount ?? data.rows}
        />
      )}
    </DatasetHero>
  );
}

export function DatabaseAnalysis({
  onBackHome,
  onBackDatabase,
  onOpenDataset,
  selectedKey
}: {
  onBackHome: () => void;
  onBackDatabase: () => void;
  onOpenDataset: (key: DatasetKey) => void;
  selectedKey: DatasetKey | null;
}) {
  const { summary, loading: summaryLoading, error: summaryError } = useDatabaseDatasetSummary();
  const {
    analytics,
    loading: analyticsLoading,
    refreshing: analyticsRefreshing,
    error: analyticsError,
    source: analyticsSource,
    generatedAt: analyticsGeneratedAt,
    refresh: refreshAnalytics
  } = useDatabaseAnalytics();
  const displayDatasets = useMemo(() => resolveDatasets(summary, summaryError), [summary, summaryError]);
  const commonProps = {
    onBackHome,
    onBackDatabase,
    analytics,
    analyticsLoading,
    analyticsRefreshing,
    analyticsError,
    analyticsSource,
    analyticsGeneratedAt,
    onRefreshAnalytics: refreshAnalytics
  };

  if (selectedKey === "process") {
    return <ProcessPage {...commonProps} dataset={getDisplayDataset(displayDatasets, "process")} />;
  }
  if (selectedKey === "property") {
    return <PropertyPage {...commonProps} dataset={getDisplayDataset(displayDatasets, "property")} />;
  }
  if (selectedKey === "structureEffect") {
    return <StructureEffectPage {...commonProps} dataset={getDisplayDataset(displayDatasets, "structureEffect")} />;
  }
  if (selectedKey === "propertyFilter") {
    return <PropertyFilterPage {...commonProps} dataset={getDisplayDataset(displayDatasets, "propertyFilter")} />;
  }
  if (selectedKey === "dft") {
    return <DftPage {...commonProps} dataset={getDisplayDataset(displayDatasets, "dft")} />;
  }
  if (selectedKey === "formulation") {
    return <FormulationPage {...commonProps} dataset={getDisplayDataset(displayDatasets, "formulation")} />;
  }

  return (
    <DatabaseHome
      onBackHome={onBackHome}
      onOpenDataset={onOpenDataset}
      displayDatasets={displayDatasets}
      summaryLoading={summaryLoading}
      summaryError={summaryError}
    />
  );
}
