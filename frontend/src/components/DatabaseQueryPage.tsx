import { ArrowLeft, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { lookupSmilesInDatabase } from "../services/api";
import type { SmilesLookupResponse, SmilesLookupResult, SmilesLookupTable, StructureWorkspaceContext } from "../types";
import { CurrentStructurePanel, MissingStructurePanel } from "./StructureWorkbenchPage";
import { StructurePreview3D } from "./StructurePreview3D";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { Select } from "./ui/select";

type DatabaseQueryPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  onBackHome: () => void;
};

type TableOption = {
  value: SmilesLookupTable;
  label: string;
  detail: string;
  fields: string;
};

const tableOptions: TableOption[] = [
  {
    value: "polymers",
    label: "Polymer Structure-Property Data (Polymers)",
    detail: "Polymer-level records from the structure-property dataset.",
    fields: "smiles, canonical_smiles"
  },
  {
    value: "properties",
    label: "Polymer Structure-Property Data (Properties)",
    detail: "Property-level records joined through polymer structures.",
    fields: "polymers.smiles, polymers.canonical_smiles"
  },
  {
    value: "pi_candidates",
    label: "PI Polymer",
    detail: "PI polymer structures and monomers from the reverse-design database.",
    fields: "polym, canonical_polym, mon1, mon2"
  }
];

const tableOptionByValue = Object.fromEntries(
  tableOptions.map((option) => [option.value, option])
) as Record<SmilesLookupTable, TableOption>;

const fieldLabels: Record<string, string> = {
  polymer_id: "Polymer ID",
  property_id: "Property ID",
  property_count: "Properties",
  rdkit_parse_ok: "RDKit OK",
  property_name: "Property",
  property_value: "Value",
  property_value_num: "Numeric",
  property_unit: "Unit",
  label_source: "Source",
  pi_id: "PI ID",
  mon1: "Monomer A",
  mon2: "Monomer B",
  polym: "Polymer SMILES",
  tg_celsius: "Tg (C)",
  dielectric_const_dc: "Dielectric DC",
  static_dielectric_const: "Static Dielectric",
  dipole_debye: "Dipole",
  electrophilicity_index: "Electrophilicity",
  homo_lumo_gap_ev: "HOMO-LUMO Gap",
  hardness: "Hardness",
  mulliken_electronegativity: "Electronegativity",
  redox_window_v: "Redox Window",
  linear_expansion: "Linear Expansion",
  refractive_index: "Refractive Index"
};

function formatLookupValue(value: string | number | boolean | null) {
  if (value === null || value === "") {
    return "-";
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  }
  return value;
}

function LookupResultCard({ result }: { result: SmilesLookupResult }) {
  const fields = Object.entries(result.fields).filter(([, value]) => value !== null && value !== "");

  return (
    <article className="rounded-[24px] border border-white/80 bg-white/85 p-4 shadow-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className="bg-slate-100 text-slate-700">ID {result.record_id}</Badge>
            <Badge className="bg-teal-50 text-teal-800">{result.source_column}</Badge>
          </div>
          <h3 className="font-heading mt-3 text-lg font-semibold tracking-tight text-slate-950">
            {result.summary}
          </h3>
        </div>
        <div className="min-w-0 rounded-2xl border border-slate-200/80 bg-slate-50 px-3 py-2 lg:max-w-[460px]">
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-mutedForeground">Matched SMILES</div>
          <div className="mt-1 break-all font-mono-ui text-xs text-slate-800">{result.smiles}</div>
        </div>
      </div>

      {result.canonical_smiles ? (
        <div className="mt-3 rounded-2xl border border-slate-200/80 bg-white/80 px-3 py-2">
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-mutedForeground">
            Canonical SMILES
          </div>
          <div className="mt-1 break-all font-mono-ui text-xs text-slate-800">{result.canonical_smiles}</div>
        </div>
      ) : null}

      {fields.length > 0 ? (
        <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {fields.map(([key, value]) => (
            <div key={key} className="min-w-0 rounded-2xl border border-slate-200/80 bg-slate-50 px-3 py-2">
              <div className="truncate text-[10px] font-semibold uppercase tracking-[0.12em] text-mutedForeground">
                {fieldLabels[key] ?? key}
              </div>
              <div className="mt-1 truncate text-sm font-medium text-slate-900" title={formatLookupValue(value)}>
                {formatLookupValue(value)}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </article>
  );
}

function LookupResults({
  data,
  error,
  isLoading
}: {
  data: SmilesLookupResponse | null;
  error: string | null;
  isLoading: boolean;
}) {
  if (isLoading) {
    return (
      <div className="rounded-[30px] border border-white/70 bg-white/85 p-8 text-center text-sm text-mutedForeground shadow-sm">
        Searching selected table...
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-[30px] border border-red-200 bg-red-50 p-6 text-sm text-red-700 shadow-sm">
        {error}
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-[30px] border border-white/70 bg-white/85 p-8 text-center text-sm text-mutedForeground shadow-sm">
        Run a table lookup to see exact SMILES matches here.
      </div>
    );
  }

  return (
    <section className="overflow-hidden rounded-[32px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(243,248,250,0.92)_100%)] shadow-soft">
      <div className="border-b border-slate-200/80 px-6 py-5 md:px-8">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/70">Lookup Results</div>
            <h2 className="font-heading mt-2 text-[1.8rem] font-semibold tracking-tight text-slate-950">
              {data.exists ? "SMILES Found" : "No Exact Match"}
            </h2>
            <p className="mt-1 text-sm leading-6 text-mutedForeground">
              Canonical query: <span className="font-mono-ui text-slate-800">{data.canonical_smiles}</span>
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge className={data.exists ? "bg-teal-50 text-teal-800" : "bg-slate-100 text-slate-700"}>
              {data.total} matches
            </Badge>
            <Badge className="bg-slate-100 text-slate-700">{data.query_time_ms.toFixed(1)} ms</Badge>
          </div>
        </div>
      </div>

      <div className="space-y-3 px-4 py-4 md:px-5 md:py-5">
        {data.results.length > 0 ? (
          data.results.map((result) => <LookupResultCard key={`${result.source_column}-${result.record_id}`} result={result} />)
        ) : (
          <div className="rounded-[24px] border border-white/80 bg-white/85 p-6 text-sm text-mutedForeground">
            The selected table does not contain this SMILES after canonicalization.
          </div>
        )}
      </div>
    </section>
  );
}

function LookupProgressPanel({
  canSubmit,
  data,
  error,
  isLoading
}: {
  canSubmit: boolean;
  data: SmilesLookupResponse | null;
  error: string | null;
  isLoading: boolean;
}) {
  const progress = error ? 100 : data ? 100 : isLoading ? 78 : canSubmit ? 66 : 28;
  const statusLabel = error
    ? "Lookup failed"
    : isLoading
      ? "Searching"
      : data
        ? data.exists
          ? "Match found"
          : "No exact match"
        : canSubmit
          ? "Ready"
          : "Waiting for structure";
  const summary = error
    ? "Check the selected table or backend connection."
    : data
      ? `${data.total} matches returned from selected table.`
      : isLoading
        ? "Canonicalizing SMILES and scanning records."
        : canSubmit
          ? "Current structure and table are available."
          : "Set a shared structure before running lookup.";
  const steps = [
    { label: "Structure", isActive: canSubmit || isLoading || Boolean(data) || Boolean(error) },
    { label: "Table", isActive: canSubmit || isLoading || Boolean(data) || Boolean(error) },
    { label: "Result", isActive: isLoading || Boolean(data) || Boolean(error) }
  ];

  return (
    <div className="mt-auto rounded-[22px] border border-sky-100 bg-sky-50/55 px-4 py-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.78)]">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-700/80">Lookup Progress</div>
          <div className="mt-1 text-sm font-semibold text-slate-950">{statusLabel}</div>
        </div>
        <div className="rounded-full border border-sky-100 bg-white px-2.5 py-1 text-xs font-semibold text-sky-700 shadow-sm">
          {progress}%
        </div>
      </div>

      <div className="mt-4 h-2 overflow-hidden rounded-full bg-white shadow-inner">
        <div
          className="h-full rounded-full bg-[linear-gradient(90deg,#0f766e_0%,#2563eb_100%)] transition-all duration-500"
          style={{ width: `${progress}%` }}
        />
      </div>

      <div className="mt-4 grid grid-cols-3 gap-2">
        {steps.map((step) => (
          <div key={step.label} className="flex min-w-0 items-center gap-2">
            <span
              className={[
                "h-2.5 w-2.5 flex-none rounded-full border",
                step.isActive ? "border-teal-500 bg-teal-500" : "border-slate-300 bg-white"
              ].join(" ")}
            />
            <span className="truncate text-xs font-medium text-slate-600">{step.label}</span>
          </div>
        ))}
      </div>

      <div className="mt-4 rounded-2xl border border-white/80 bg-white/78 px-3 py-2 text-xs leading-5 text-slate-600">
        {summary}
      </div>
    </div>
  );
}

export function DatabaseQueryPage({ structure, onEditStructure, onBackHome }: DatabaseQueryPageProps) {
  const [selectedTable, setSelectedTable] = useState<SmilesLookupTable>("polymers");
  const [data, setData] = useState<SmilesLookupResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const lookupRequestId = useRef(0);
  const previousSmilesRef = useRef(structure.smiles);
  const smiles = structure.smiles;
  const canSubmit = smiles.trim().length > 0 && !isLoading;
  const selectedOption = tableOptionByValue[selectedTable];

  function clearLookupFeedback() {
    lookupRequestId.current += 1;
    setData(null);
    setError(null);
    setIsLoading(false);
  }

  useEffect(() => {
    if (previousSmilesRef.current !== smiles) {
      previousSmilesRef.current = smiles;
      clearLookupFeedback();
    }
  }, [smiles]);

  function handleTableChange(nextTable: SmilesLookupTable) {
    if (nextTable !== selectedTable) {
      clearLookupFeedback();
    }
    setSelectedTable(nextTable);
  }

  async function handleSubmit() {
    const querySmiles = (await structure.getCurrentSmiles()).trim();
    if (!querySmiles || isLoading) {
      setError("请先在结构工作台绘制或输入 SMILES。");
      return;
    }

    const requestId = lookupRequestId.current + 1;
    lookupRequestId.current = requestId;
    setIsLoading(true);
    setError(null);
    setData(null);
    try {
      const result = await lookupSmilesInDatabase({
        smiles: querySmiles,
        table: selectedTable
      });
      if (lookupRequestId.current !== requestId) {
        return;
      }
      setData(result);
    } catch (requestError) {
      if (lookupRequestId.current !== requestId) {
        return;
      }
      setData(null);
      setError(requestError instanceof Error ? requestError.message : "Database lookup failed");
    } finally {
      if (lookupRequestId.current === requestId) {
        setIsLoading(false);
      }
    }
  }

  return (
    <div className="space-y-6">
      <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex items-center gap-3">
          <Button type="button" variant="outline" onClick={onBackHome}>
            <ArrowLeft className="mr-2 h-4 w-4" />
            Home
          </Button>
          <div>
            <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Current Module</div>
            <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">Database Query</div>
          </div>
        </div>
        <Badge className="bg-teal-50 text-teal-800">Exact SMILES Lookup</Badge>
      </nav>

      <CurrentStructurePanel structure={structure} onEditStructure={onEditStructure} />

      {!smiles.trim() ? (
        <MissingStructurePanel
          title="请先设置查询结构"
          description="数据库查询会使用结构工作台中的共享 SMILES。先绘制、导入或输入结构后，再回到这里选择表并运行精确查询。"
          onEditStructure={onEditStructure}
        />
      ) : (
        <>
          <section className="grid items-stretch gap-6 xl:grid-cols-[minmax(0,0.92fr)_minmax(360px,0.56fr)]">
            <StructurePreview3D
              smiles={smiles}
              className="xl:flex xl:flex-1 xl:flex-col"
              contentClassName="xl:flex xl:flex-1 xl:flex-col"
              previewClassName="h-[340px] xl:h-auto xl:min-h-[420px] xl:flex-1"
              visualStyle="polished-atoms"
            />

            <Card className="flex flex-col overflow-hidden rounded-[24px] border-sky-100 bg-white shadow-[0_22px_58px_rgba(37,99,235,0.10)]">
              <CardHeader className="gap-3 border-b border-sky-100 bg-white">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-[11px] font-medium uppercase tracking-[0.22em] text-teal-700/80">Table Lookup</div>
                    <CardTitle className="mt-2 text-[1.35rem] tracking-tight">Lookup Settings</CardTitle>
                    <CardDescription>Select the database table to check against the current SMILES.</CardDescription>
                  </div>
                  <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-[0_12px_30px_rgba(8,17,31,0.18)]">
                    <Search className="h-4 w-4" />
                  </div>
                </div>
              </CardHeader>
              <CardContent className="flex flex-1 flex-col space-y-4 pt-5">
                <label className="space-y-1.5">
                  <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-mutedForeground">
                    Table
                  </span>
                  <Select
                    value={selectedTable}
                    onChange={(event) => handleTableChange(event.target.value as SmilesLookupTable)}
                  >
                    {tableOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                </label>

                <div className="rounded-2xl border border-slate-200/80 bg-slate-50 px-4 py-3">
                  <div className="text-sm font-semibold text-slate-950">{selectedOption.detail}</div>
                  <div className="mt-1 break-all font-mono-ui text-xs text-slate-600">{selectedOption.fields}</div>
                </div>

                <div className="flex flex-col gap-3 border-t border-slate-200/70 pt-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="text-sm leading-6 text-mutedForeground">
                    {canSubmit ? "Structure and table are ready." : "Enter a structure before running lookup."}
                  </div>
                  <Button type="button" className="min-h-[44px] min-w-[172px]" onClick={handleSubmit} disabled={!canSubmit}>
                    <Search className="mr-2 h-4 w-4" />
                    {isLoading ? "Searching..." : "Run Lookup"}
                  </Button>
                </div>

                <LookupProgressPanel canSubmit={canSubmit} data={data} error={error} isLoading={isLoading} />
              </CardContent>
            </Card>
          </section>

          <LookupResults data={data} error={error} isLoading={isLoading} />
        </>
      )}
    </div>
  );
}
