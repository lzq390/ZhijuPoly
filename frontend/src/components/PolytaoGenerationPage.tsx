import {
  ArrowLeft,
  Atom,
  CheckCircle2,
  CircleAlert,
  Copy,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Wand2
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { StructurePreview3D } from "./StructurePreview3D";
import { StructureSvg } from "./StructureSvg";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Select } from "./ui/select";
import {
  DEFAULT_POLYTAO_DESCRIPTORS,
  EMPTY_POLYTAO_DESCRIPTORS,
  getPolytaoRuntimeDisplayState,
  polytaoDescriptorMapFromEntries,
  type PolytaoRuntimeDisplayState,
  usePolytaoGeneration
} from "../hooks/usePolytaoGeneration";
import { cn } from "../lib/utils";
import { calculatePolytaoDescriptors } from "../services/api";
import type {
  PolytaoCandidate,
  PolytaoDescriptorMap,
  PolytaoDescriptorName,
  PolytaoGenerationRequest,
  PolytaoGenerationResponse,
  PolytaoJobStatusResponse,
  PolytaoStatusResponse,
  StructureWorkspaceContext
} from "../types";
import { POLYTAO_DESCRIPTOR_NAMES } from "../types";

type PolytaoGenerationPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  onBackHome: () => void;
};

type DescriptorGroup = {
  title: string;
  detail: string;
  descriptors: [PolytaoDescriptorName, ...PolytaoDescriptorName[]];
  accentClassName: string;
};

const DESCRIPTOR_GROUPS: DescriptorGroup[] = [
  {
    title: "Size / Composition",
    detail: "Molecular size and atom composition.",
    descriptors: ["MolWt", "HeavyAtomCount", "NumHeteroatoms"],
    accentClassName: "border-sky-100 bg-sky-50/80 text-sky-700"
  },
  {
    title: "Donor / Acceptor",
    detail: "Hydrogen-bond and hetero atom counts.",
    descriptors: ["NHOHCount", "NOCount", "NumHAcceptors", "NumHDonors"],
    accentClassName: "border-cyan-100 bg-cyan-50/80 text-cyan-700"
  },
  {
    title: "Ring System",
    detail: "Aliphatic, aromatic, and total ring counts.",
    descriptors: [
      "NumAliphaticCarbocycles",
      "NumAliphaticHeterocycles",
      "NumAliphaticRings",
      "NumAromaticCarbocycles",
      "NumAromaticHeterocycles",
      "NumAromaticRings",
      "RingCount"
    ],
    accentClassName: "border-indigo-100 bg-indigo-50/80 text-indigo-700"
  },
  {
    title: "Flexibility",
    detail: "Rotatable-bond count.",
    descriptors: ["NumRotatableBonds"],
    accentClassName: "border-emerald-100 bg-emerald-50/80 text-emerald-700"
  }
];

function parseNumber(value: string, fallback: number) {
  if (!value.trim()) {
    return Number.NaN;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatOptionalNumber(value: number | null | undefined, digits = 2) {
  return value == null ? "Pending" : value.toFixed(digits);
}

function formatDescriptorValue(value: number) {
  if (!Number.isFinite(value)) {
    return "";
  }
  return Math.abs(value - Math.round(value)) < 1e-9 ? String(Math.round(value)) : value.toPrecision(6);
}

function descriptorPrompt(descriptors: PolytaoDescriptorMap) {
  return POLYTAO_DESCRIPTOR_NAMES.map((name) => formatDescriptorValue(descriptors[name])).join(",");
}

function copyText(value: string) {
  void navigator.clipboard?.writeText(value);
}

export function PolytaoGenerationPage({ structure, onEditStructure, onBackHome }: PolytaoGenerationPageProps) {
  const polytao = usePolytaoGeneration();
  const [descriptorError, setDescriptorError] = useState<string | null>(null);
  const [isDescriptorLoading, setIsDescriptorLoading] = useState(false);
  const currentPrompt = useMemo(() => descriptorPrompt(polytao.request.descriptors), [polytao.request.descriptors]);
  const hasStructure = structure.smiles.trim().length > 0;
  const runtimeDisplayState = getPolytaoRuntimeDisplayState(
    polytao.serviceStatus,
    polytao.statusError,
    polytao.isStatusLoading
  );

  function updateRequest(partial: Partial<PolytaoGenerationRequest>) {
    polytao.setRequest({
      ...polytao.request,
      ...partial,
      descriptors: partial.descriptors ?? polytao.request.descriptors
    });
  }

  function updateDescriptor(name: PolytaoDescriptorName, value: number) {
    updateRequest({
      descriptors: {
        ...polytao.request.descriptors,
        [name]: value
      },
      input_smiles: null
    });
  }

  async function handleDescriptorPrefill() {
    setDescriptorError(null);
    setIsDescriptorLoading(true);
    try {
      const currentSmiles = (await structure.getCurrentSmiles()).trim();
      if (!currentSmiles) {
        setDescriptorError("Set a structure before descriptor prefill.");
        return;
      }
      const response = await calculatePolytaoDescriptors({ smiles: currentSmiles });
      const descriptors = polytaoDescriptorMapFromEntries(response.descriptors);
      polytao.setRequest({
        ...polytao.request,
        descriptors,
        input_smiles: response.canonical_smiles
      });
    } catch (error) {
      setDescriptorError(error instanceof Error ? error.message : "Failed to calculate PolyTAO descriptors.");
    } finally {
      setIsDescriptorLoading(false);
    }
  }

  async function handleSubmit() {
    setDescriptorError(null);
    if (polytao.serviceStatus && !polytao.serviceStatus.available) {
      setDescriptorError(polytao.serviceStatus.message);
      return;
    }

    const currentSmiles = polytao.request.input_smiles?.trim() || "";

    await polytao.submit({
      ...polytao.request,
      input_smiles: currentSmiles || null
    });
  }

  const descriptorReady = POLYTAO_DESCRIPTOR_NAMES.every((name) => Number.isFinite(polytao.request.descriptors[name]));
  const canSubmit =
    !polytao.isLoading &&
    polytao.serviceStatus?.available === true &&
    descriptorReady &&
    polytao.request.candidate_count >= 1 &&
    polytao.request.candidate_count <= 50 &&
    polytao.request.top_k >= 1 &&
    polytao.request.top_k <= 500 &&
    polytao.request.top_p > 0 &&
    polytao.request.top_p <= 1 &&
    polytao.request.temperature >= 0.1 &&
    polytao.request.temperature <= 2.0 &&
    polytao.request.max_length >= 16 &&
    polytao.request.max_length <= 512;

  return (
    <div className="relative -mx-4 -my-5 min-h-[calc(100vh-2.5rem)] bg-[#f6f8fb] text-slate-900 md:-mx-8 md:-my-8">
      <header className="border-b border-slate-200 bg-white px-4 py-3 shadow-[0_10px_30px_rgba(15,23,42,0.04)] md:px-6">
        <div className="mx-auto flex max-w-[1480px] flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              type="button"
              variant="outline"
              onClick={onBackHome}
              className="h-9 rounded-[8px] border-slate-200 bg-white px-3 shadow-none"
            >
              <ArrowLeft className="mr-2 h-4 w-4" />
              Home
            </Button>
            <div className="min-w-0">
              <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Current Module</div>
              <h1 className="truncate font-heading text-[18px] font-semibold text-slate-950">PolyTAO Generation</h1>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ServicePill
              status={polytao.serviceStatus}
              statusError={polytao.statusError}
              isLoading={polytao.isStatusLoading}
            />
            <Button
              type="button"
              variant="outline"
              onClick={() => void polytao.refreshStatus()}
              disabled={polytao.isStatusLoading}
              className="h-9 rounded-[8px] border-slate-200 bg-white px-3 shadow-none"
            >
              {polytao.isStatusLoading ? (
                <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="mr-2 h-4 w-4" />
              )}
              Refresh
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1480px] px-4 py-4 md:px-6 md:py-5">
        <section className="grid min-w-0 gap-4 xl:grid-cols-[minmax(270px,0.72fr)_minmax(520px,1fr)_minmax(310px,0.72fr)]">
          <StructureSourcePanel
            structure={structure}
            hasStructure={hasStructure}
            onEditStructure={onEditStructure}
            onPrefill={() => void handleDescriptorPrefill()}
            isPrefilling={isDescriptorLoading}
            descriptorError={descriptorError}
          />

          <DescriptorEditorPanel
            descriptors={polytao.request.descriptors}
            prompt={currentPrompt}
            onDescriptorChange={updateDescriptor}
            onLoadSample={() => {
              updateRequest({ descriptors: { ...DEFAULT_POLYTAO_DESCRIPTORS }, input_smiles: null });
              setDescriptorError(null);
            }}
            onClear={() => {
              updateRequest({ descriptors: { ...EMPTY_POLYTAO_DESCRIPTORS }, input_smiles: null });
              setDescriptorError(null);
            }}
          />

          <RunControlPanel
            request={polytao.request}
            status={polytao.serviceStatus}
            statusError={polytao.statusError}
            runtimeDisplayState={runtimeDisplayState}
            job={polytao.job}
            canSubmit={canSubmit}
            descriptorReady={descriptorReady}
            isLoading={polytao.isLoading}
            prompt={currentPrompt}
            onRequestChange={updateRequest}
            onSubmit={() => void handleSubmit()}
          />
        </section>

        <ResultsPanel
          data={polytao.data}
          job={polytao.job}
          error={polytao.error}
          isLoading={polytao.isLoading}
        />
      </main>
    </div>
  );
}

function ServicePill({
  status,
  statusError,
  isLoading
}: {
  status: PolytaoStatusResponse | null;
  statusError: string | null;
  isLoading: boolean;
}) {
  const displayState = getPolytaoRuntimeDisplayState(status, statusError, isLoading);
  if (displayState === "checking") {
    return (
      <Badge className="border-slate-200 bg-slate-50 text-slate-700">
        <LoaderCircle className="mr-2 h-3.5 w-3.5 animate-spin" />
        Checking runtime
      </Badge>
    );
  }
  if (displayState === "ready") {
    return (
      <Badge className="border-emerald-200 bg-emerald-50 text-emerald-700">
        <CheckCircle2 className="mr-2 h-3.5 w-3.5" />
        Runtime ready
      </Badge>
    );
  }
  if (displayState === "cold") {
    return (
      <Badge className="border-amber-200 bg-amber-50 text-amber-700">
        <CircleAlert className="mr-2 h-3.5 w-3.5" />
        Runtime cold
      </Badge>
    );
  }
  if (displayState === "loading") {
    return (
      <Badge className="border-sky-200 bg-sky-50 text-sky-700">
        <LoaderCircle className="mr-2 h-3.5 w-3.5 animate-spin" />
        Runtime loading
      </Badge>
    );
  }
  const label =
    displayState === "disabled"
      ? "Disabled"
      : displayState === "db_unavailable"
        ? "DB unavailable"
        : "Runtime error";
  return (
    <Badge className="border-rose-200 bg-rose-50 text-rose-700">
      <CircleAlert className="mr-2 h-3.5 w-3.5" />
      {label}
    </Badge>
  );
}

function Panel({
  title,
  eyebrow,
  icon,
  children,
  className,
  actions
}: {
  title: string;
  eyebrow: string;
  icon: ReactNode;
  children: ReactNode;
  className?: string;
  actions?: ReactNode;
}) {
  return (
    <section
      className={cn(
        "min-w-0 overflow-hidden rounded-[14px] border border-slate-200 bg-white shadow-[0_12px_32px_rgba(15,23,42,0.045)]",
        className
      )}
    >
      <div className="flex min-h-[68px] items-center justify-between gap-4 border-b border-slate-100 px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex h-10 w-10 flex-none items-center justify-center rounded-[10px] border border-slate-200 bg-slate-50 text-slate-700">
            {icon}
          </span>
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">{eyebrow}</div>
            <h2 className="truncate font-heading text-[16px] font-semibold text-slate-950">{title}</h2>
          </div>
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

function StructureSourcePanel({
  structure,
  hasStructure,
  onEditStructure,
  onPrefill,
  isPrefilling,
  descriptorError
}: {
  structure: StructureWorkspaceContext;
  hasStructure: boolean;
  onEditStructure: () => void;
  onPrefill: () => void;
  isPrefilling: boolean;
  descriptorError: string | null;
}) {
  return (
    <Panel title="Structure Source" eyebrow="Input" icon={<Atom className="h-4 w-4" />}>
      <div className="space-y-4 p-4">
        <div className="rounded-[10px] border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">Current SMILES</div>
          <div className="mt-2 min-h-[48px] break-all font-mono-ui text-sm leading-6 text-slate-900">
            {hasStructure ? structure.smiles : "No shared structure. Manual descriptor generation is still available."}
          </div>
        </div>

        {hasStructure ? (
          <div className="overflow-hidden rounded-[10px] border border-slate-200 bg-white">
            <StructurePreview3D
              smiles={structure.smiles}
              variant="bare"
              previewClassName="min-h-[220px]"
              visualStyle="polished-atoms"
            />
          </div>
        ) : (
          <div className="flex min-h-[220px] items-center justify-center rounded-[10px] border border-dashed border-slate-200 bg-slate-50 px-4 text-center text-sm leading-6 text-slate-500">
            Open the shared structure workbench to draw or import a repeat unit.
          </div>
        )}

        <div className="grid gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={onEditStructure}
            className="h-10 rounded-[8px] border-slate-200 bg-white shadow-none"
          >
            <Atom className="mr-2 h-4 w-4" />
            Edit Structure
          </Button>
          <Button
            type="button"
            onClick={onPrefill}
            disabled={isPrefilling}
            className="h-10 rounded-[8px] shadow-none"
          >
            {isPrefilling ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <Wand2 className="mr-2 h-4 w-4" />}
            Prefill Descriptors
          </Button>
        </div>

        {descriptorError ? <AlertBox tone="danger" title={descriptorError} /> : null}
      </div>
    </Panel>
  );
}

function DescriptorEditorPanel({
  descriptors,
  prompt,
  onDescriptorChange,
  onLoadSample,
  onClear
}: {
  descriptors: PolytaoDescriptorMap;
  prompt: string;
  onDescriptorChange: (name: PolytaoDescriptorName, value: number) => void;
  onLoadSample: () => void;
  onClear: () => void;
}) {
  return (
    <Panel
      title="Descriptor Editor"
      eyebrow="15 RDKit fields"
      icon={<Sparkles className="h-4 w-4" />}
      actions={
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={onLoadSample}
            className="h-8 rounded-[8px] border-slate-200 bg-white px-3 text-xs shadow-none"
          >
            <Wand2 className="mr-2 h-3.5 w-3.5" />
            Load Sample
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={onClear}
            className="h-8 rounded-[8px] border-slate-200 bg-white px-3 text-xs shadow-none"
          >
            <RotateCcw className="mr-2 h-3.5 w-3.5" />
            Clear
          </Button>
        </div>
      }
    >
      <div className="space-y-4 p-4">
        <div className="flex items-start justify-between gap-3 rounded-[10px] border border-slate-200 bg-slate-50 px-3 py-2">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">Prompt Preview</div>
            <div className="mt-1 break-all font-mono-ui text-xs leading-5 text-slate-800">{prompt}</div>
          </div>
          <button
            type="button"
            onClick={() => copyText(prompt)}
            className="flex h-8 w-8 flex-none items-center justify-center rounded-[8px] text-slate-500 transition hover:bg-white hover:text-slate-950"
            aria-label="Copy PolyTAO descriptor prompt"
          >
            <Copy className="h-4 w-4" />
          </button>
        </div>

        <div className="grid gap-x-5 gap-y-6 lg:grid-cols-2">
          {DESCRIPTOR_GROUPS.map((group) => (
            <DescriptorGroupEditor
              key={group.title}
              group={group}
              descriptors={descriptors}
              onDescriptorChange={onDescriptorChange}
            />
          ))}
        </div>
      </div>
    </Panel>
  );
}

function DescriptorGroupEditor({
  group,
  descriptors,
  onDescriptorChange
}: {
  group: DescriptorGroup;
  descriptors: PolytaoDescriptorMap;
  onDescriptorChange: (name: PolytaoDescriptorName, value: number) => void;
}) {
  const [selectedDescriptor, setSelectedDescriptor] = useState<PolytaoDescriptorName>(group.descriptors[0]);
  const filledCount = group.descriptors.reduce(
    (count, name) => count + (Number.isFinite(descriptors[name]) ? 1 : 0),
    0
  );

  return (
    <div className="min-w-0 border-l-2 border-slate-200 pl-3">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge
            className={cn(
              "rounded-[8px] px-2 py-1 text-[10px] tracking-[0.08em] shadow-none",
              group.accentClassName
            )}
          >
            {group.title}
          </Badge>
          <span className="rounded-[8px] border border-slate-200 bg-slate-50 px-2 py-1 text-[10px] font-semibold tracking-[0.08em] text-slate-600">
            Filled {filledCount}/{group.descriptors.length}
          </span>
        </div>
        <div className="mt-2 text-xs leading-5 text-slate-500">{group.detail}</div>
      </div>
      <div className="mt-3 grid gap-2">
        <label className="grid gap-1.5">
          <span className="truncate text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-500">
            Descriptor
          </span>
          <Select
            aria-label={`${group.title} descriptor`}
            value={selectedDescriptor}
            onChange={(event) => setSelectedDescriptor(event.target.value as PolytaoDescriptorName)}
            className="h-9 rounded-[8px] border-slate-200 bg-white px-3 shadow-none"
          >
            {group.descriptors.map((name) => (
              <option key={name} value={name}>
                {name} — {Number.isFinite(descriptors[name]) ? "Filled" : "Empty"}
              </option>
            ))}
          </Select>
        </label>
        <NumberField
          label={selectedDescriptor}
          value={descriptors[selectedDescriptor]}
          step={selectedDescriptor === "MolWt" ? 0.1 : 1}
          onChange={(value) => onDescriptorChange(selectedDescriptor, value)}
        />
      </div>
    </div>
  );
}

function RunControlPanel({
  request,
  status,
  statusError,
  runtimeDisplayState,
  job,
  canSubmit,
  descriptorReady,
  isLoading,
  prompt,
  onRequestChange,
  onSubmit
}: {
  request: PolytaoGenerationRequest;
  status: PolytaoStatusResponse | null;
  statusError: string | null;
  runtimeDisplayState: PolytaoRuntimeDisplayState;
  job: PolytaoJobStatusResponse | null;
  canSubmit: boolean;
  descriptorReady: boolean;
  isLoading: boolean;
  prompt: string;
  onRequestChange: (partial: Partial<PolytaoGenerationRequest>) => void;
  onSubmit: () => void;
}) {
  let runtimeMessage: string | null = null;
  if (runtimeDisplayState === "cold") {
    runtimeMessage = "The PolyTAO runtime is cold. Your first job will load the model before generation starts.";
  } else if (runtimeDisplayState === "loading") {
    runtimeMessage = "The PolyTAO model is loading. Accepted jobs will run when the runtime is ready.";
  } else if (runtimeDisplayState === "runtime_error") {
    runtimeMessage = statusError ?? status?.runtime_error ?? status?.message ?? "PolyTAO runtime status is unavailable.";
    if (status?.available) {
      runtimeMessage = `${runtimeMessage} Submitting another job will retry model loading.`;
    }
  } else if (runtimeDisplayState === "disabled" || runtimeDisplayState === "db_unavailable") {
    runtimeMessage = statusError ?? status?.message ?? "PolyTAO is unavailable.";
  }

  return (
    <Panel title="Run Controls" eyebrow="Sampling" icon={<Play className="h-4 w-4" />}>
      <div className="space-y-4 p-4">
        <div className="grid gap-2">
          <NumberField
            label="Candidate Count"
            min={1}
            max={50}
            step={1}
            value={request.candidate_count}
            onChange={(value) => onRequestChange({ candidate_count: value })}
          />
          <NumberField
            label="Temperature"
            min={0.1}
            max={2}
            step={0.05}
            value={request.temperature}
            onChange={(value) => onRequestChange({ temperature: value })}
          />
          <NumberField
            label="Top-K"
            min={1}
            max={500}
            step={1}
            value={request.top_k}
            onChange={(value) => onRequestChange({ top_k: value })}
          />
          <NumberField
            label="Top-P"
            min={0.001}
            max={1}
            step={0.001}
            value={request.top_p}
            onChange={(value) => onRequestChange({ top_p: value })}
          />
          <NumberField
            label="Max Length"
            min={16}
            max={512}
            step={1}
            value={request.max_length}
            onChange={(value) => onRequestChange({ max_length: value })}
          />
        </div>

        <div className="rounded-[10px] border border-slate-200 bg-slate-50 px-3 py-3">
          <MetricRow label="Prompt fields" value={String(prompt.split(",").length)} />
          <MetricRow label="Runtime version" value={status?.worker_version ?? "Backend"} />
          <MetricRow label="Active jobs" value={String(status?.active_jobs ?? 0)} />
        </div>

        {job ? <JobProgress job={job} /> : null}
        {runtimeMessage ? <AlertBox tone="warning" title={runtimeMessage} /> : null}
        {!descriptorReady ? <AlertBox tone="warning" title="Fill all 15 descriptors, prefill from a structure, or load the sample vector." /> : null}

        <Button
          type="button"
          onClick={onSubmit}
          disabled={!canSubmit}
          className="h-11 w-full rounded-[8px] shadow-none"
        >
          {isLoading ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
          {isLoading ? "Generating" : "Submit PolyTAO Job"}
        </Button>
      </div>
    </Panel>
  );
}

function JobProgress({ job }: { job: PolytaoJobStatusResponse }) {
  const progress = Math.max(0, Math.min(100, job.progress_percent ?? 0));
  return (
    <div className="rounded-[10px] border border-slate-200 bg-white px-3 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-500">{job.status}</div>
        <div className="text-xs font-semibold text-slate-700">{progress}%</div>
      </div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-100">
        <div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${progress}%` }} />
      </div>
      <div className="mt-2 min-w-0 break-words text-xs leading-5 text-slate-500 [overflow-wrap:anywhere]">
        {job.progress_message}
      </div>
      <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
        <MetricRow label="Returned" value={String(job.returned_count)} />
        <MetricRow label="Attempts" value={String(job.attempts)} />
      </div>
    </div>
  );
}

function ResultsPanel({
  data,
  job,
  error,
  isLoading
}: {
  data: PolytaoGenerationResponse | null;
  job: PolytaoJobStatusResponse | null;
  error: string | null;
  isLoading: boolean;
}) {
  return (
    <section className="mt-4 overflow-hidden rounded-[14px] border border-slate-200 bg-white shadow-[0_12px_32px_rgba(15,23,42,0.045)]">
      <div className="flex flex-col gap-3 border-b border-slate-100 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">Results</div>
          <h2 className="font-heading text-[16px] font-semibold text-slate-950">Candidate Structures</h2>
        </div>
        <div className="flex flex-wrap gap-2">
          <Badge className="border-slate-200 bg-slate-50 text-slate-700">{data?.returned_count ?? job?.returned_count ?? 0} returned</Badge>
          <Badge className="border-slate-200 bg-slate-50 text-slate-700">{data?.query_time_ms ? `${data.query_time_ms.toFixed(1)} ms` : "Idle"}</Badge>
          {data?.attempts != null ? (
            <Badge className="border-slate-200 bg-slate-50 text-slate-700">{data.attempts} attempts</Badge>
          ) : null}
        </div>
      </div>

      <div className="p-4">
        {error ? <AlertBox tone="danger" title={error} /> : null}
        {isLoading ? (
          <div className="flex min-h-[240px] items-center justify-center rounded-[12px] border border-dashed border-slate-200 bg-slate-50 text-sm font-semibold text-slate-600">
            <LoaderCircle className="mr-2 h-5 w-5 animate-spin text-sky-600" />
            Waiting for PolyTAO backend runtime
          </div>
        ) : null}
        {!isLoading && !data && !error ? (
          <div className="flex min-h-[240px] flex-col items-center justify-center rounded-[12px] border border-dashed border-slate-200 bg-slate-50 px-6 text-center">
            <Sparkles className="h-6 w-6 text-slate-400" />
            <div className="mt-3 font-heading text-base font-semibold text-slate-900">No job result yet</div>
            <div className="mt-1 text-sm leading-6 text-slate-500">Submit a descriptor prompt to populate this table.</div>
          </div>
        ) : null}
        {!isLoading && data ? (
          <div className="space-y-4">
            <FilterCounter counter={data.filter_counter} />
            {data.results.length ? (
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
                {data.results.map((candidate) => (
                  <CandidateCard key={`${candidate.rank}-${candidate.generated_smiles}`} candidate={candidate} />
                ))}
              </div>
            ) : (
              <div className="rounded-[12px] border border-dashed border-slate-200 bg-slate-50 px-4 py-10 text-center text-sm text-slate-500">
                Backend runtime completed but no candidate passed filtering.
              </div>
            )}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function FilterCounter({ counter }: { counter: Record<string, number> }) {
  const entries = Object.entries(counter);
  if (!entries.length) {
    return null;
  }
  return (
    <div className="flex flex-wrap gap-2">
      {entries.map(([key, value]) => (
        <Badge key={key} className="border-slate-200 bg-slate-50 text-slate-700">
          {key}: {value}
        </Badge>
      ))}
    </div>
  );
}

function CandidateCard({ candidate }: { candidate: PolytaoCandidate }) {
  return (
    <article className="min-w-0 overflow-hidden rounded-[12px] border border-slate-200 bg-white shadow-[0_10px_24px_rgba(15,23,42,0.04)]">
      <div className="border-b border-slate-100 bg-slate-50 px-3 py-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <Badge className="rounded-[8px] border-sky-100 bg-sky-50 px-2 py-1 text-[10px] text-sky-700 shadow-none">
            Rank {candidate.rank}
          </Badge>
          <Badge
            className={cn(
              "rounded-[8px] px-2 py-1 text-[10px] shadow-none",
              candidate.valid_smiles
                ? "border-emerald-100 bg-emerald-50 text-emerald-700"
                : "border-rose-100 bg-rose-50 text-rose-700"
            )}
          >
            {candidate.valid_smiles ? "Valid" : "Invalid"}
          </Badge>
        </div>
        {candidate.structure_svg ? (
          <StructureSvg
            svg={candidate.structure_svg}
            alt={`PolyTAO structure rank ${candidate.rank}`}
            imageClassName="max-h-[168px]"
          />
        ) : (
          <div className="flex min-h-[150px] items-center justify-center rounded-[10px] bg-white px-3 text-center font-mono-ui text-xs leading-5 text-slate-500">
            {candidate.generated_smiles}
          </div>
        )}
      </div>
      <div className="space-y-3 p-3">
        <div className="group flex items-start gap-2 rounded-[10px] border border-slate-200 bg-slate-50 px-3 py-2">
          <div className="min-w-0 flex-1 break-all font-mono-ui text-xs leading-5 text-slate-900">{candidate.generated_smiles}</div>
          <button
            type="button"
            onClick={() => copyText(candidate.generated_smiles)}
            className="flex h-7 w-7 flex-none items-center justify-center rounded-[8px] text-slate-500 transition hover:bg-white hover:text-slate-950"
            aria-label="Copy generated SMILES"
          >
            <Copy className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="grid gap-2 text-xs">
          <MetricRow label="SA score" value={formatOptionalNumber(candidate.sa_score)} />
          <MetricRow label="Raw output" value={candidate.raw_smiles} mono />
        </div>
        {candidate.warnings.length ? (
          <div className="min-w-0 break-words rounded-[10px] border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 [overflow-wrap:anywhere]">
            {candidate.warnings.join(", ")}
          </div>
        ) : null}
      </div>
    </article>
  );
}

function NumberField({
  label,
  value,
  min,
  max,
  step,
  onChange
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="grid gap-1.5">
      <span className="truncate text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-500">{label}</span>
      <Input
        type="number"
        min={min}
        max={max}
        step={step}
        value={Number.isFinite(value) ? value : ""}
        onChange={(event) => onChange(parseNumber(event.target.value, value))}
        className="h-9 rounded-[8px] border-slate-200 bg-white px-3 shadow-none"
      />
    </label>
  );
}

function MetricRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span className="text-slate-500">{label}</span>
      <span className={cn("min-w-0 text-right font-semibold text-slate-800 [overflow-wrap:anywhere]", mono ? "break-all font-mono-ui" : "break-words")}>
        {value}
      </span>
    </div>
  );
}

function AlertBox({
  title,
  children,
  tone
}: {
  title: string;
  children?: ReactNode;
  tone: "warning" | "danger";
}) {
  const className =
    tone === "danger"
      ? "border-rose-100 bg-rose-50 text-rose-700"
      : "border-amber-100 bg-amber-50 text-amber-800";
  return (
    <div className={cn("flex gap-2 rounded-[10px] border px-3 py-2 text-xs leading-5", className)}>
      <CircleAlert className="mt-0.5 h-4 w-4 flex-none" />
      <div className="min-w-0">
        <div className="min-w-0 break-words font-semibold [overflow-wrap:anywhere]">{title}</div>
        {children}
      </div>
    </div>
  );
}
