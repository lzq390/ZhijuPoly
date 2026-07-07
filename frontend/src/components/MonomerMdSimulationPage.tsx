import {
  Activity,
  ArrowLeft,
  Atom,
  CheckCircle2,
  Database,
  Gauge,
  Loader2,
  Play,
  RotateCw,
  Timer,
  TriangleAlert,
  XCircle
} from "lucide-react";
import { type FormEvent } from "react";
import {
  getMonomerMdSmilesValidationError,
  useMonomerMdSimulation
} from "../hooks/useMonomerMdSimulation";
import { cn } from "../lib/utils";
import type {
  MonomerMdArtifact,
  MonomerMdJobStatus,
  MonomerMdSeries,
  MonomerMdSimulationResult,
  MonomerMdTrajectoryPoint,
  MonomerMdTrajectoryPreview
} from "../types";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";

type MonomerMdSimulationPageProps = {
  onBackHome: () => void;
};

type PlotPoint = {
  x: number;
  y: number;
};

const STATUS_LABELS: Record<MonomerMdJobStatus, string> = {
  pending: "Pending",
  submitted: "Submitted",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled"
};

const STATUS_STEPS: MonomerMdJobStatus[] = ["pending", "submitted", "running", "completed"];
const SUMMARY_LABELS: Record<string, string> = {
  final_density_g_cm3: "Final density",
  mean_density_g_cm3: "Mean density",
  mean_temperature_k: "Mean temperature",
  final_temperature_k: "Final temperature",
  mean_total_energy_kcal_mol: "Mean total energy",
  final_total_energy_kcal_mol: "Final total energy",
  elapsed_seconds: "Elapsed time",
  n_atoms: "Atoms",
  n_frames: "Frames",
  n_steps: "Steps"
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function numericValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function formatNumber(value: number, digits = 2) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);
}

function formatValue(value: unknown) {
  const numeric = numericValue(value);
  if (numeric != null) {
    return formatNumber(numeric, Math.abs(numeric) >= 100 ? 1 : 3);
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return "--";
}

function normalizeSeries(series: MonomerMdSeries | undefined, valueKeys: string[]): PlotPoint[] {
  if (!series) {
    return [];
  }
  const rawPoints = Array.isArray(series) ? series : series.points;
  return rawPoints
    .map((point, index) => {
      const record = point as Record<string, unknown>;
      const timeNs = numericValue(record.time_ns);
      const x = numericValue(record.time_ps) ?? (timeNs == null ? null : timeNs * 1000) ?? numericValue(record.step) ?? numericValue(record.frame) ?? index;
      const y =
        numericValue(record.value) ??
        valueKeys.map((key) => numericValue(record[key])).find((value) => value != null) ??
        Object.entries(record)
          .filter(([key]) => !["time_ps", "time_ns", "step", "frame"].includes(key))
          .map(([, value]) => numericValue(value))
          .find((value) => value != null);
      return y == null || !Number.isFinite(x) ? null : { x, y };
    })
    .filter((point): point is PlotPoint => point !== null);
}

function polyline(points: PlotPoint[]) {
  const width = 320;
  const height = 120;
  const pad = 14;
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const xSpan = maxX - minX || 1;
  const ySpan = maxY - minY || 1;
  return points
    .map((point) => {
      const x = pad + ((point.x - minX) / xSpan) * (width - pad * 2);
      const y = height - pad - ((point.y - minY) / ySpan) * (height - pad * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function progressValue(status: MonomerMdJobStatus | null, progress: number | null | undefined) {
  const explicit = numericValue(progress);
  if (explicit != null) {
    return Math.max(0, Math.min(100, explicit <= 1 ? explicit * 100 : explicit));
  }
  if (status === "completed") return 100;
  if (status === "running") return 62;
  if (status === "submitted") return 24;
  if (status === "pending") return 10;
  if (status === "failed" || status === "cancelled") return 100;
  return 0;
}

function statusTone(status: MonomerMdJobStatus | null) {
  if (status === "completed") return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (status === "failed" || status === "cancelled") return "border-red-200 bg-red-50 text-red-700";
  if (status) return "border-sky-200 bg-sky-50 text-sky-700";
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function summaryEntries(result: MonomerMdSimulationResult | null) {
  if (!result) return [];
  const seen = new Set<string>();
  const preferred = Object.keys(SUMMARY_LABELS)
    .filter((key) => key in result.summary)
    .map((key) => {
      seen.add(key);
      return [key, result.summary[key]] as const;
    });
  return [...preferred, ...Object.entries(result.summary).filter(([key]) => !seen.has(key))].slice(0, 8);
}

function normalizeArtifacts(artifacts: MonomerMdSimulationResult["artifacts"] | undefined): MonomerMdArtifact[] {
  if (!artifacts) return [];
  if (Array.isArray(artifacts)) return artifacts;
  return Object.entries(artifacts).map(([name, value]) => {
    if (isRecord(value)) {
      return { name, ...(value as MonomerMdArtifact) };
    }
    return { name, path: value == null ? null : String(value) };
  });
}

function trajectoryPoints(trajectory: MonomerMdTrajectoryPreview | null | undefined): MonomerMdTrajectoryPoint[] {
  return trajectory?.points ?? trajectory?.atoms ?? [];
}

function atomColor(point: MonomerMdTrajectoryPoint) {
  const label = (point.element ?? point.atom_type ?? "").toUpperCase();
  if (label.startsWith("O")) return "#ef4444";
  if (label.startsWith("N")) return "#2563eb";
  if (label.startsWith("H")) return "#94a3b8";
  if (label.startsWith("F") || label.startsWith("CL")) return "#10b981";
  return "#0f172a";
}

function projectAtom(point: MonomerMdTrajectoryPoint, points: MonomerMdTrajectoryPoint[]) {
  const xs = points.map((item) => item.x);
  const ys = points.map((item) => item.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const xSpan = maxX - minX || 1;
  const ySpan = maxY - minY || 1;
  return {
    x: 16 + ((point.x - minX) / xSpan) * 288,
    y: 164 - ((point.y - minY) / ySpan) * 148
  };
}

function StatusBadge({ status }: { status: MonomerMdJobStatus | null }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-semibold", statusTone(status))}>
      {status === "completed" ? <CheckCircle2 className="h-3.5 w-3.5" /> : null}
      {status === "failed" || status === "cancelled" ? <XCircle className="h-3.5 w-3.5" /> : null}
      {status === "pending" || status === "submitted" || status === "running" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
      {status ? STATUS_LABELS[status] : "Not submitted"}
    </span>
  );
}

function SeriesCard({ title, series, unit, color, valueKeys, isLoading }: { title: string; series: MonomerMdSeries | undefined; unit: string; color: string; valueKeys: string[]; isLoading: boolean }) {
  const points = normalizeSeries(series, valueKeys);
  const latest = points[points.length - 1];
  return (
    <section className="flex min-h-[230px] flex-col rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase text-slate-400">{title}</div>
          <div className="mt-1 text-sm font-semibold text-slate-950">{title} curve</div>
        </div>
        <div className="text-right">
          <div className="text-lg font-semibold tabular-nums text-slate-950">{latest ? formatNumber(latest.y, Math.abs(latest.y) >= 100 ? 1 : 3) : "--"}</div>
          <div className="text-[11px] text-slate-500">{unit}</div>
        </div>
      </div>
      <div className="mt-4 flex min-h-[126px] flex-1 items-center justify-center rounded-lg border border-slate-100 bg-slate-50/80">
        {points.length > 1 ? (
          <svg viewBox="0 0 320 120" role="img" aria-label={`${title} curve`} className="h-full w-full">
            <line x1="14" y1="28" x2="306" y2="28" stroke="#e2e8f0" strokeDasharray="4 5" />
            <line x1="14" y1="60" x2="306" y2="60" stroke="#e2e8f0" strokeDasharray="4 5" />
            <line x1="14" y1="92" x2="306" y2="92" stroke="#e2e8f0" strokeDasharray="4 5" />
            <polyline points={polyline(points)} fill="none" stroke={color} strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" />
          </svg>
        ) : (
          <div className="px-4 text-center text-xs leading-5 text-slate-500">{isLoading ? "Waiting for series data from the backend." : "Submit a SMILES to show this curve."}</div>
        )}
      </div>
      <div className="mt-3 flex items-center justify-between gap-3 text-[11px] text-slate-500">
        <span>{points.length ? `${points.length} points` : "No data"}</span>
        <span>{latest ? formatNumber(latest.x, 1) : "--"}</span>
      </div>
    </section>
  );
}

function SummaryPanel({ result }: { result: MonomerMdSimulationResult | null }) {
  const entries = summaryEntries(result);
  return (
    <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Gauge className="h-4 w-4 text-sky-600" />Summary</div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {entries.length ? entries.map(([key, value]) => (
          <div key={key} className="min-h-[72px] rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
            <div className="truncate text-[11px] text-slate-500">{SUMMARY_LABELS[key] ?? key}</div>
            <div className="mt-1 break-words text-sm font-semibold text-slate-950">{formatValue(value)}</div>
          </div>
        )) : (
          <div className="col-span-full min-h-[72px] rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-5 text-sm text-slate-500">Density, temperature, energy, and sampling summary appear after completion.</div>
        )}
      </div>
    </section>
  );
}

function TrajectoryCard({ trajectory }: { trajectory: MonomerMdTrajectoryPreview | null | undefined }) {
  const points = trajectoryPoints(trajectory);
  const visiblePoints = points.slice(0, 180);
  return (
    <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Atom className="h-4 w-4 text-teal-600" />Trajectory preview</div>
        <span className="rounded-md bg-slate-100 px-2 py-1 text-[11px] text-slate-600">{trajectory?.stage_id ?? "preview"}</span>
      </div>
      <div className="mt-4 min-h-[220px] rounded-lg border border-slate-100 bg-slate-50">
        {visiblePoints.length ? (
          <svg viewBox="0 0 320 180" role="img" aria-label="Trajectory projection" className="h-full min-h-[220px] w-full">
            <rect x="12" y="12" width="296" height="156" rx="10" fill="#ffffff" stroke="#e2e8f0" />
            {visiblePoints.map((point, index) => {
              const projected = projectAtom(point, visiblePoints);
              return <circle key={`${point.atom_id ?? index}-${index}`} cx={projected.x} cy={projected.y} r={point.element === "H" || point.atom_type === "H" ? 2 : 3} fill={atomColor(point)} opacity="0.76" />;
            })}
          </svg>
        ) : trajectory?.preview_url ? (
          <div className="flex min-h-[220px] flex-col items-center justify-center gap-2 px-4 text-center text-sm text-slate-600"><Database className="h-5 w-5 text-slate-400" /><a className="font-medium text-sky-700 hover:text-sky-800" href={trajectory.preview_url} target="_blank" rel="noreferrer">Open trajectory preview</a></div>
        ) : (
          <div className="flex min-h-[220px] items-center justify-center px-4 text-center text-sm text-slate-500">Trajectory samples or a preview file appear after completion.</div>
        )}
      </div>
      <div className="mt-3 grid gap-2 text-xs text-slate-600 sm:grid-cols-3">
        <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">Frame</span><span className="font-semibold text-slate-900">{trajectory?.frame_index ?? "--"}</span></div>
        <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">Time</span><span className="font-semibold text-slate-900">{trajectory?.time_ps != null ? `${formatNumber(trajectory.time_ps, 1)} ps` : "--"}</span></div>
        <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">Sampled</span><span className="font-semibold text-slate-900">{trajectory?.sampled_points ?? (points.length || "--")}</span></div>
      </div>
    </section>
  );
}

function ArtifactsPanel({ result }: { result: MonomerMdSimulationResult | null }) {
  const artifacts = normalizeArtifacts(result?.artifacts);
  return (
    <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Database className="h-4 w-4 text-slate-600" />Artifacts</div>
      <div className="mt-3 space-y-2">
        {artifacts.length ? artifacts.map((artifact, index) => {
          const label = artifact.label ?? artifact.name ?? `artifact-${index + 1}`;
          const detail = artifact.path ?? artifact.kind ?? "artifact";
          return (
            <div key={`${label}-${index}`} className="flex min-h-[54px] items-center justify-between gap-3 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
              <div className="min-w-0"><div className="truncate text-sm font-medium text-slate-950">{label}</div><div className="truncate text-xs text-slate-500">{detail}</div></div>
              <div className="shrink-0 text-right text-xs text-slate-500">{artifact.url ? <a href={artifact.url} target="_blank" rel="noreferrer" className="font-medium text-sky-700 hover:text-sky-800">Open</a> : "--"}</div>
            </div>
          );
        }) : <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-5 text-sm text-slate-500">Trajectory, log, and series artifacts appear after completion.</div>}
      </div>
    </section>
  );
}

function ResultNotice({ result, completedWithoutResult }: { result: MonomerMdSimulationResult | null; completedWithoutResult: boolean }) {
  if (completedWithoutResult) {
    return (
      <section className="rounded-[14px] border border-red-100 bg-red-50 px-4 py-3 text-sm leading-6 text-red-700">
        <span className="inline-flex items-start gap-2">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          The job completed, but the backend did not return a result payload. Check the worker logs and artifact path before using this run.
        </span>
      </section>
    );
  }
  if (!result) {
    return null;
  }

  const messages = new Set<string>();
  if (result.not_equilibrated || result.physical_density_estimate === false) {
    messages.add("1000-step demo output is not equilibrated and is not a physical density estimate.");
  }
  for (const warning of result.warnings ?? []) {
    if (warning.trim()) {
      messages.add(warning.trim());
    }
  }
  if (!messages.size) {
    return null;
  }

  return (
    <section className="rounded-[14px] border border-amber-100 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
      <div className="inline-flex items-start gap-2">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
        <div className="space-y-1">
          {[...messages].map((message) => (
            <div key={message}>{message}</div>
          ))}
        </div>
      </div>
    </section>
  );
}

export function MonomerMdSimulationPage({ onBackHome }: MonomerMdSimulationPageProps) {
  const simulation = useMonomerMdSimulation();
  const validationError = getMonomerMdSmilesValidationError(simulation.smiles);
  const serviceUnavailable = simulation.serviceStatus?.enabled === false || simulation.serviceStatus?.available === false;
  const canSubmit = !simulation.isLoading && !validationError && !serviceUnavailable;
  const currentStatus = simulation.job?.status ?? null;
  const progress = progressValue(currentStatus, simulation.job?.progress);
  const result = simulation.data;
  const completedWithoutResult = currentStatus === "completed" && !result;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (canSubmit) {
      void simulation.submit(simulation.smiles);
    }
  }

  return (
    <div className="min-h-full bg-[#f1f5f9] text-slate-950">
      <div className="mx-auto flex w-full max-w-[1480px] flex-col gap-4">
        <nav className="flex flex-col gap-3 rounded-[14px] border border-slate-200 bg-white px-4 py-3 shadow-sm md:flex-row md:items-center md:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <Button type="button" variant="outline" onClick={onBackHome} className="h-9 shrink-0 rounded-md border-slate-200 bg-white px-3 text-slate-700 shadow-none hover:border-slate-300 hover:bg-slate-50"><ArrowLeft className="mr-2 h-4 w-4" />Back</Button>
            <div className="min-w-0"><div className="text-[11px] font-semibold uppercase text-slate-400">Monomer MD Workbench</div><div className="truncate text-base font-semibold text-slate-950">{"\u5355\u4f53 MD \u6a21\u62df"}</div></div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={cn("inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 font-medium", serviceUnavailable ? "border-red-200 bg-red-50 text-red-700" : "border-slate-200 bg-slate-50 text-slate-600")}>
              {simulation.isStatusLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Activity className="h-3.5 w-3.5" />}
              {simulation.isStatusLoading ? "Checking worker" : simulation.statusError ? "Status check failed" : serviceUnavailable ? "Worker unavailable" : "Worker available"}
            </span>
            <Button type="button" variant="outline" onClick={() => void simulation.refreshStatus()} className="h-8 rounded-md border-slate-200 bg-white px-2.5 text-xs text-slate-600 shadow-none hover:bg-slate-50"><RotateCw className="mr-1.5 h-3.5 w-3.5" />Refresh</Button>
          </div>
        </nav>

        <section className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)]">
          <div className="flex min-w-0 flex-col gap-4">
            <form onSubmit={handleSubmit} className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between gap-3"><div><div className="text-[11px] font-semibold uppercase text-slate-400">Input</div><h1 className="mt-1 text-base font-semibold text-slate-950">Monomer SMILES</h1></div><span className="rounded-md bg-sky-50 px-2 py-1 text-xs font-medium text-sky-700">Molecule</span></div>
              <label className="mt-4 block space-y-2"><span className="text-xs font-medium text-slate-600">SMILES</span><Textarea value={simulation.smiles} onChange={(event) => simulation.setSmiles(event.target.value)} placeholder="Example: CCOC(=O)c1ccc(N)cc1" spellCheck={false} className="min-h-[96px] rounded-lg border-slate-200 bg-white font-mono text-[13px] leading-5 text-slate-900 shadow-none placeholder:text-slate-400 focus-visible:ring-sky-200" disabled={simulation.isLoading} /></label>
              <div className="mt-2 min-h-[20px] text-xs text-slate-500">{validationError ? <span className="text-red-600">{validationError}</span> : "Use a monomer structure without *. Repeat-unit inputs belong on the MD demo page."}</div>
              <div className="mt-2 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">This first-phase run is a 1000-step demo. It is not equilibrated and is not a physical density estimate.</div>
              <div className="mt-4 flex flex-wrap items-center gap-2"><Button type="submit" disabled={!canSubmit} className="h-10 rounded-md px-4 shadow-none disabled:opacity-[0.45]">{simulation.isLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}{simulation.isLoading ? "Running" : "Submit simulation"}</Button><Button type="button" variant="outline" onClick={simulation.reset} disabled={simulation.isLoading} className="h-10 rounded-md border-slate-200 bg-white px-4 text-slate-700 shadow-none hover:bg-slate-50">Clear results</Button></div>
              {serviceUnavailable ? <div className="mt-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">{simulation.serviceStatus?.message ?? "Backend reports that monomer MD service is unavailable."}</div> : null}
              {simulation.statusError ? <div className="mt-3 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">Status endpoint unavailable: {simulation.statusError}</div> : null}
            </form>

            <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between gap-3"><div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Timer className="h-4 w-4 text-slate-600" />Job status</div><StatusBadge status={currentStatus} /></div>
              <div className="mt-4 space-y-3">
                <label className="block space-y-1.5"><span className="text-xs font-medium text-slate-500">Job ID</span><Input value={simulation.job?.job_id ?? ""} readOnly placeholder="No job yet" className="h-9 rounded-md border-slate-200 bg-slate-50 font-mono text-xs shadow-none" /></label>
                <div><div className="mb-1 flex items-center justify-between text-xs text-slate-500"><span>{currentStatus ? STATUS_LABELS[currentStatus] : "Submit to start polling the backend job."}</span><span className="tabular-nums">{formatNumber(progress, 0)}%</span></div><div className="h-2 overflow-hidden rounded-full bg-slate-100"><div className={cn("h-full rounded-full transition-all", currentStatus === "failed" || currentStatus === "cancelled" ? "bg-red-500" : "bg-sky-500")} style={{ width: `${progress}%` }} /></div></div>
                <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-1">{STATUS_STEPS.map((status) => { const active = currentStatus === status; const complete = STATUS_STEPS.indexOf(currentStatus ?? "pending") > STATUS_STEPS.indexOf(status) || currentStatus === "completed"; return <div key={status} className={cn("flex items-center justify-between rounded-lg border px-3 py-2", active ? "border-sky-200 bg-sky-50 text-sky-800" : complete ? "border-emerald-100 bg-emerald-50 text-emerald-800" : "border-slate-100 bg-slate-50 text-slate-500")}><span>{STATUS_LABELS[status]}</span>{complete ? <CheckCircle2 className="h-3.5 w-3.5" /> : active ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <span className="h-3.5 w-3.5 rounded-full border border-slate-200" />}</div>; })}</div>
                {simulation.error ? <div className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700"><span className="inline-flex items-start gap-2"><TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />{simulation.error}</span></div> : null}
              </div>
            </section>
          </div>

          <div className="grid min-w-0 gap-4">
            <ResultNotice result={result} completedWithoutResult={completedWithoutResult} />
            <SummaryPanel result={result} />
            <div className="grid gap-4 lg:grid-cols-3">
              <SeriesCard title="Density" series={result?.density_series} unit="g/cm3" color="#0ea5e9" valueKeys={["density", "density_g_cm3", "rho"]} isLoading={simulation.isLoading} />
              <SeriesCard title="Temperature" series={result?.temperature_series} unit="K" color="#10b981" valueKeys={["temperature", "temperature_k", "temp"]} isLoading={simulation.isLoading} />
              <SeriesCard title="Energy" series={result?.energy_series} unit="kcal/mol" color="#6366f1" valueKeys={["energy", "total_energy", "total_energy_kcal_mol", "potential_energy"]} isLoading={simulation.isLoading} />
            </div>
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]"><TrajectoryCard trajectory={result?.trajectory_preview} /><ArtifactsPanel result={result} /></div>
          </div>
        </section>
      </div>
    </div>
  );
}
