import { type FormEvent, type ReactNode, useEffect, useMemo, useState } from "react";
import {
  BarChart3,
  Clock3,
  Download,
  Eraser,
  Expand,
  FileClock,
  FlaskConical,
  Globe2,
  KeyRound,
  Loader2,
  RefreshCcw,
  Search,
  Trash2,
  X
} from "lucide-react";
import { useOnlineKnowledgeSearch } from "../../hooks/useOnlineKnowledgeSearch";
import { exportOnlineKnowledgeCsv, fetchOnlineKnowledgeDefaultConfig } from "../../services/api";
import {
  ONLINE_KNOWLEDGE_DEFAULT_BASE_URL,
  ONLINE_KNOWLEDGE_DEFAULT_MAX_PAPERS,
  ONLINE_KNOWLEDGE_DEFAULT_MODEL
} from "../../constants/onlineKnowledgeDefaults";
import type {
  OnlineKnowledgeCountItem,
  OnlineKnowledgeMode,
  OnlineKnowledgePropertyPoint,
  OnlineKnowledgeSearchResponse,
  OnlineKnowledgeSynthesis
} from "../../types";
import { cn } from "../../lib/utils";
import { Alert } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Select } from "../ui/select";

type DetailState = {
  label: string;
  value: string;
} | null;

type OnlineKnowledgeSearchPanelProps = {
  initialMaterial?: string;
};

export function OnlineKnowledgeSearchPanel({ initialMaterial = "" }: OnlineKnowledgeSearchPanelProps) {
  const [material, setMaterial] = useState(initialMaterial.trim());
  const [baseUrl, setBaseUrl] = useState(ONLINE_KNOWLEDGE_DEFAULT_BASE_URL);
  const [model, setModel] = useState(ONLINE_KNOWLEDGE_DEFAULT_MODEL);
  const [apiKey, setApiKey] = useState("");
  const [useServerDefault, setUseServerDefault] = useState(false);
  const [hasServerApiKey, setHasServerApiKey] = useState(false);
  const [mode, setMode] = useState<OnlineKnowledgeMode>("synthesis");
  const [maxPapers, setMaxPapers] = useState(ONLINE_KNOWLEDGE_DEFAULT_MAX_PAPERS);
  const [detail, setDetail] = useState<DetailState>(null);
  const [csvError, setCsvError] = useState<string | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [isLoadingConfig, setIsLoadingConfig] = useState(false);
  const searchState = useOnlineKnowledgeSearch();

  useEffect(() => {
    const nextMaterial = initialMaterial.trim();
    if (nextMaterial) {
      setMaterial(nextMaterial);
    }
  }, [initialMaterial]);

  useEffect(() => {
    void searchState.loadHistory();
  }, [searchState.loadHistory]);

  const hasModelAccess =
    ((useServerDefault && hasServerApiKey) || apiKey.trim().length > 0) &&
    baseUrl.trim().length > 0 &&
    model.trim().length > 0;
  const canSearch =
    material.trim().length > 0 &&
    hasModelAccess &&
    maxPapers >= 1 &&
    maxPapers <= 2000 &&
    !searchState.isLoading;

  const activeData = searchState.data;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSearch) {
      return;
    }

    await submitSearch(material.trim(), mode, maxPapers);
  }

  async function submitSearch(nextMaterial: string, nextMode: OnlineKnowledgeMode, nextMaxPapers: number) {
    if (!nextMaterial || !hasModelAccess || searchState.isLoading) {
      return;
    }

    const payload = {
      material: nextMaterial,
      api_key: useServerDefault ? null : apiKey.trim(),
      base_url: baseUrl.trim(),
      model: model.trim(),
      mode: nextMode,
      max_papers: nextMaxPapers,
      extraction_delay_seconds: 0.5,
      use_server_default: useServerDefault
    };

    await searchState.submit(payload);
  }

  async function handleLoadDefaultConfig() {
    setConfigError(null);
    setIsLoadingConfig(true);
    try {
      const config = await fetchOnlineKnowledgeDefaultConfig();
      setBaseUrl(config.base_url);
      setModel(config.model);
      setMaxPapers(config.max_papers);
      setHasServerApiKey(config.has_server_api_key);
      setUseServerDefault(config.has_server_api_key);
      if (config.has_server_api_key) {
        setApiKey("");
      } else {
        setConfigError("Server API key is not configured. Fill an API key manually or update backend .env.");
      }
    } catch (error) {
      setConfigError(error instanceof Error ? error.message : "Failed to load API config");
    } finally {
      setIsLoadingConfig(false);
    }
  }

  async function handleExportCsv() {
    if (!activeData || activeData.dataframe.length === 0) {
      return;
    }

    setCsvError(null);
    try {
      const response = await exportOnlineKnowledgeCsv(
        activeData.dataframe,
        `${activeData.material.replace(/\s+/g, "_")}_${activeData.mode}_results.csv`
      );
      const blob = new Blob([response.csv_content], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = response.filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setCsvError(error instanceof Error ? error.message : "CSV export failed");
    }
  }

  return (
    <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="space-y-4">
        <form onSubmit={handleSubmit} autoComplete="off" className="rounded-[32px] border border-white/70 bg-white/75 p-4 shadow-soft md:p-5">
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1.2fr)_180px]">
            <Field label="Material">
              <Input
                value={material}
                onChange={(event) => setMaterial(event.target.value)}
                placeholder="polyimide, PLA, PET..."
                aria-label="Material"
              />
            </Field>
            <Field label="Search Mode">
              <Select value={mode} onChange={(event) => setMode(event.target.value as OnlineKnowledgeMode)}>
                <option value="synthesis">Synthesis</option>
                <option value="property">Property</option>
              </Select>
            </Field>
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_220px_180px]">
            <Field label="API Key">
              <Input
                type={useServerDefault ? "text" : "password"}
                value={useServerDefault ? "Configured on server" : apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder={useServerDefault ? "Configured on server" : "API key"}
                autoComplete="new-password"
                name="online-api-key-current"
                aria-label="API key"
                disabled={useServerDefault}
              />
            </Field>
            <Field label="Base URL">
              <Input
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
                placeholder="Provider API base URL"
                autoComplete="off"
                name="online-base-url-current"
                aria-label="Base URL"
              />
            </Field>
            <Field label="Model">
              <Input
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder="Model name"
                autoComplete="off"
                name="online-model-current"
                aria-label="Model"
              />
            </Field>
            <div className="flex items-end">
              <Button type="button" variant="outline" onClick={handleLoadDefaultConfig} disabled={isLoadingConfig} className="w-full">
                {isLoadingConfig ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <KeyRound className="mr-2 h-4 w-4" />}
                Load API Config
              </Button>
            </div>
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-[160px_140px]">
            <Field label="Max Papers">
              <Input
                type="number"
                min={1}
                max={2000}
                value={maxPapers}
                onChange={(event) => setMaxPapers(Number(event.target.value))}
                aria-label="Max papers"
              />
            </Field>
            <div className="flex items-end">
              <Button type="submit" disabled={!canSearch} className="w-full">
                {searchState.isLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
                Search
              </Button>
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <Badge className="bg-teal-50 text-teal-800">
              <KeyRound className="mr-1.5 h-3.5 w-3.5" />
              Model access is used only for this search
            </Badge>
            <Badge className="bg-white text-slate-700">
              <Globe2 className="mr-1.5 h-3.5 w-3.5" />
              Crossref, Semantic Scholar, OpenAlex, PubMed, arXiv
            </Badge>
          </div>
        </form>

        {searchState.error ? <Alert variant="destructive">{searchState.error}</Alert> : null}
        {csvError ? <Alert variant="destructive">{csvError}</Alert> : null}
        {configError ? <Alert variant="destructive">{configError}</Alert> : null}

        {searchState.isLoading ? (
          <div className="flex min-h-[280px] flex-col items-center justify-center rounded-[32px] border border-white/70 bg-white/75 p-5 text-center shadow-soft">
            <Loader2 className="h-8 w-8 animate-spin text-teal-700" />
            <div className="mt-4 font-heading text-xl font-semibold tracking-tight text-slate-950">Retrieving literature</div>
            <p className="mt-2 max-w-xl text-sm leading-6 text-slate-600">
              The system is collecting abstracts and extracting polymer information. Current status: {searchState.jobStatus ?? "pending"}.
            </p>
          </div>
        ) : activeData ? (
          <OnlineResults data={activeData} onOpenDetail={(label, value) => setDetail({ label, value })} onExportCsv={handleExportCsv} />
        ) : (
          <div className="flex min-h-[280px] items-center justify-center rounded-[32px] border border-white/70 bg-white/75 p-5 text-center text-sm text-mutedForeground shadow-soft">
            Enter a material and temporary model access to retrieve online polymer knowledge.
          </div>
        )}
      </div>

      <aside className="space-y-4">
        <HistoryPanel
          state={searchState}
          onRestore={(item) => {
            setMaterial(item.material);
            setMode(item.mode);
            setMaxPapers(item.max_papers || 100);
            searchState.restoreFromHistory(item);
          }}
          onReplay={(item) => {
            setMaterial(item.material);
            setMode(item.mode);
            setMaxPapers(item.max_papers || 100);
            void submitSearch(item.material, item.mode, item.max_papers || 100);
          }}
          canReplay={hasModelAccess && !searchState.isLoading}
        />
      </aside>

      <DetailDialog detail={detail} onClose={() => setDetail(null)} />
    </section>
  );
}

function OnlineResults({
  data,
  onOpenDetail,
  onExportCsv
}: {
  data: OnlineKnowledgeSearchResponse;
  onOpenDetail: (label: string, value: string) => void;
  onExportCsv: () => void;
}) {
  const isPropertyMode = data.mode === "property";
  const resultCount = isPropertyMode ? data.propertyPoints.length : data.syntheses.length;

  return (
    <div className="space-y-4">
      <div className="rounded-[32px] border border-white/70 bg-white/75 p-4 shadow-soft md:p-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-teal-700">Search Result</div>
            <h2 className="font-heading mt-1 text-2xl font-semibold tracking-tight text-slate-950">{data.material}</h2>
          </div>
          <div className="flex flex-wrap gap-2">
            {data.exampleUsed ? <Badge className="bg-amber-50 text-amber-800">Example data used</Badge> : null}
            <Button type="button" variant="outline" onClick={onExportCsv} disabled={data.dataframe.length === 0}>
              <Download className="mr-2 h-4 w-4" />
              CSV
            </Button>
          </div>
        </div>

        <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric icon={<Globe2 className="h-4 w-4" />} label="Papers" value={String(data.totalPapers)} />
          <Metric icon={<FlaskConical className="h-4 w-4" />} label={isPropertyMode ? "Data Points" : "Reactions"} value={String(resultCount)} />
          <Metric icon={<Clock3 className="h-4 w-4" />} label="Search Time" value={`${(data.query_time_ms / 1000).toFixed(1)} s`} />
          <Metric icon={<FileClock className="h-4 w-4" />} label="Limit" value={String(data.max_papers)} />
        </div>
      </div>

      {isPropertyMode ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <DistributionPanel title="Property Names" items={data.propertyNameDistribution} />
          <DistributionPanel title="Conditions" items={data.conditionDistribution} />
          <DistributionPanel title="Polymer Types" items={data.polymerTypeDistribution} />
          <DistributionPanel title="Relationships" items={data.relationshipDistribution} />
        </div>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          <DistributionPanel title="Temperature Distribution" items={data.temperatureDistribution} />
          <DistributionPanel title="Solvent Distribution" items={data.solventDistribution} />
          <DistributionPanel title="Reaction Types" items={data.reactionTypeTable} />
          <DistributionPanel title="Catalysts" items={data.catalystTable} />
          <DistributionPanel title="Temperature Expressions" items={data.tempLabels} />
        </div>
      )}

      <div className="rounded-[32px] border border-white/70 bg-white/75 p-4 shadow-soft md:p-5">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-4 w-4 text-teal-700" />
          <h3 className="font-heading text-lg font-semibold tracking-tight text-slate-950">Condition Summary</h3>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          {data.conditionSummary.map((item) => (
            <Badge key={item} className="bg-white text-slate-700">
              {item}
            </Badge>
          ))}
        </div>
      </div>

      <div className="space-y-3">
        {isPropertyMode ? (
          data.propertyPoints.length > 0 ? (
            data.propertyPoints.map((item, index) => (
              <PropertyPointCard key={`${item.polymer_name}-${item.property_name}-${index}`} index={index + 1} item={item} onOpenDetail={onOpenDetail} />
            ))
          ) : (
            <EmptyResult>No property-condition relationships were extracted from the available abstracts.</EmptyResult>
          )
        ) : data.syntheses.length > 0 ? (
          data.syntheses.map((item, index) => (
            <SynthesisCard key={`${item.product_name}-${index}`} index={index + 1} item={item} onOpenDetail={onOpenDetail} />
          ))
        ) : (
          <div className="flex min-h-[180px] items-center justify-center rounded-[32px] border border-white/70 bg-white/75 p-5 text-center text-sm text-mutedForeground shadow-soft">
            No synthesis reactions were extracted from the available abstracts.
          </div>
        )}
      </div>
    </div>
  );
}

function HistoryPanel({
  state,
  onRestore,
  onReplay,
  canReplay
}: {
  state: ReturnType<typeof useOnlineKnowledgeSearch>;
  onRestore: (item: ReturnType<typeof useOnlineKnowledgeSearch>["history"][number]) => void;
  onReplay: (item: ReturnType<typeof useOnlineKnowledgeSearch>["history"][number]) => void;
  canReplay: boolean;
}) {
  return (
    <div className="rounded-[32px] border border-white/70 bg-white/75 p-4 shadow-soft">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-teal-700">Search History</div>
          <div className="font-heading mt-1 text-lg font-semibold tracking-tight text-slate-950">{state.history.length} records</div>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => void state.loadHistory()}
            className="flex h-9 w-9 items-center justify-center rounded-xl text-slate-500 transition-colors hover:bg-white hover:text-slate-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="Refresh history"
            title="Refresh history"
          >
            <RefreshCcw className={cn("h-4 w-4", state.isHistoryLoading && "animate-spin")} />
          </button>
          <button
            type="button"
            onClick={() => void state.clearHistory()}
            disabled={state.history.length === 0}
            className="flex h-9 w-9 items-center justify-center rounded-xl text-slate-500 transition-colors hover:bg-white hover:text-slate-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40"
            aria-label="Clear history"
            title="Clear history"
          >
            <Eraser className="h-4 w-4" />
          </button>
        </div>
      </div>

      {state.historyError ? <Alert variant="destructive" className="mt-3">{state.historyError}</Alert> : null}

      <div className="mt-4 space-y-2">
        {state.history.length > 0 ? (
          state.history.map((item) => (
            <div key={item.history_id} className="rounded-2xl border border-slate-200/80 bg-white/75 p-3">
              <div className="flex items-start justify-between gap-3">
                <button type="button" onClick={() => onRestore(item)} className="min-w-0 text-left">
                  <div className="truncate text-sm font-semibold text-slate-950">{item.material}</div>
                  <div className="mt-1 text-xs text-slate-500">
                    {item.timestamp} · {item.reactions_extracted} records
                  </div>
                </button>
                <div className="flex shrink-0 gap-1">
                  <button
                    type="button"
                    onClick={() => onReplay(item)}
                    disabled={!canReplay}
                    className="flex h-8 w-8 items-center justify-center rounded-xl text-slate-400 transition-colors hover:bg-teal-50 hover:text-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40"
                    aria-label="Run this search again"
                    title="Run again"
                  >
                    <Search className="h-4 w-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => void state.deleteHistoryItem(item.history_id)}
                    className="flex h-8 w-8 items-center justify-center rounded-xl text-slate-400 transition-colors hover:bg-rose-50 hover:text-rose-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    aria-label="Delete history item"
                    title="Delete"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>
          ))
        ) : (
          <div className="flex min-h-[160px] items-center justify-center text-center text-sm text-mutedForeground">
            Completed online searches will appear here.
          </div>
        )}
      </div>
    </div>
  );
}

function SynthesisCard({
  index,
  item,
  onOpenDetail
}: {
  index: number;
  item: OnlineKnowledgeSynthesis;
  onOpenDetail: (label: string, value: string) => void;
}) {
  return (
    <article className="rounded-[28px] border border-white/70 bg-white/85 p-4 shadow-sm md:p-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <Badge className="bg-teal-50 text-teal-800">#{index}</Badge>
          <h3 className="font-heading mt-3 text-xl font-semibold tracking-tight text-slate-950">{item.method}</h3>
          <p className="mt-1 text-sm text-slate-600">{item.product_name}</p>
        </div>
        <Badge className="bg-white text-slate-700">{item.reaction_type}</Badge>
      </div>

      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        <CompactField label="Reactants" value={item.reactants} onOpenDetail={onOpenDetail} />
        <CompactField label="Properties" value={item.properties} onOpenDetail={onOpenDetail} />
        <CompactField label="Temperature" value={item.temperature} onOpenDetail={onOpenDetail} />
        <CompactField label="Catalyst" value={item.catalyst} onOpenDetail={onOpenDetail} />
        <CompactField label="Solvent" value={item.solvent} onOpenDetail={onOpenDetail} />
        <CompactField label="Time" value={item.time} onOpenDetail={onOpenDetail} />
        <CompactField label="Atmosphere" value={item.atmosphere} onOpenDetail={onOpenDetail} />
        <CompactField label="Pressure" value={item.pressure} onOpenDetail={onOpenDetail} />
        <CompactField label="Initiator" value={item.initiator} onOpenDetail={onOpenDetail} />
      </div>
    </article>
  );
}

function PropertyPointCard({
  index,
  item,
  onOpenDetail
}: {
  index: number;
  item: OnlineKnowledgePropertyPoint;
  onOpenDetail: (label: string, value: string) => void;
}) {
  return (
    <article className="rounded-[28px] border border-white/70 bg-white/85 p-4 shadow-sm md:p-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <Badge className="bg-teal-50 text-teal-800">#{index}</Badge>
          <h3 className="font-heading mt-3 text-xl font-semibold tracking-tight text-slate-950">{item.property_name}</h3>
          <p className="mt-1 text-sm text-slate-600">{item.polymer_name}</p>
        </div>
        <Badge className="bg-white text-slate-700">{item.relationship}</Badge>
      </div>

      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        <CompactField label="Property Value" value={item.property_value} onOpenDetail={onOpenDetail} />
        <CompactField label="Condition" value={item.condition_name} onOpenDetail={onOpenDetail} />
        <CompactField label="Condition Value" value={item.condition_value} onOpenDetail={onOpenDetail} />
        <CompactField label="Polymer Type" value={item.polymer_type} onOpenDetail={onOpenDetail} />
        <CompactField label="Paper" value={item.paper_title} onOpenDetail={onOpenDetail} />
      </div>
    </article>
  );
}

function CompactField({
  label,
  value,
  onOpenDetail
}: {
  label: string;
  value: string;
  onOpenDetail: (label: string, value: string) => void;
}) {
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_2rem] items-center gap-2 rounded-2xl border border-slate-200/80 bg-white/70 px-3 py-2">
      <div className="min-w-0">
        <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">{label}</div>
        <div className="mt-1 truncate text-sm font-medium text-slate-900">{value}</div>
      </div>
      <button
        type="button"
        onClick={() => onOpenDetail(label, value)}
        className="flex h-8 w-8 items-center justify-center rounded-xl text-slate-500 transition-colors hover:bg-teal-50 hover:text-teal-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={`View full ${label}`}
        title="View full content"
      >
        <Expand className="h-4 w-4" />
      </button>
    </div>
  );
}

function EmptyResult({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-[180px] items-center justify-center rounded-[32px] border border-white/70 bg-white/75 p-5 text-center text-sm text-mutedForeground shadow-soft">
      {children}
    </div>
  );
}

function DistributionPanel({ title, items }: { title: string; items: OnlineKnowledgeCountItem[] }) {
  const maxCount = useMemo(() => Math.max(1, ...items.map((item) => item.count)), [items]);

  return (
    <div className="rounded-[28px] border border-white/70 bg-white/75 p-4 shadow-sm">
      <h3 className="font-heading text-lg font-semibold tracking-tight text-slate-950">{title}</h3>
      <div className="mt-4 space-y-3">
        {items.length > 0 ? (
          items.map((item) => (
            <div key={item.label}>
              <div className="flex items-center justify-between gap-3 text-xs">
                <span className="truncate font-medium text-slate-700">{item.label}</span>
                <span className="shrink-0 text-slate-500">
                  {item.count} · {item.percentage}%
                </span>
              </div>
              <div className="mt-1 h-2 overflow-hidden rounded-full bg-slate-100">
                <div className="h-full rounded-full bg-teal-600" style={{ width: `${Math.max(6, (item.count / maxCount) * 100)}%` }} />
              </div>
            </div>
          ))
        ) : (
          <div className="flex min-h-[120px] items-center justify-center text-center text-sm text-mutedForeground">No data available.</div>
        )}
      </div>
    </div>
  );
}

function Metric({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-200/80 bg-white/75 p-4">
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
        <span className="text-teal-700">{icon}</span>
        {label}
      </div>
      <div className="font-heading mt-2 text-2xl font-semibold tracking-tight text-slate-950">{value}</div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">{label}</span>
      {children}
    </label>
  );
}

function DetailDialog({ detail, onClose }: { detail: DetailState; onClose: () => void }) {
  useEffect(() => {
    if (!detail) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [detail, onClose]);

  if (!detail) {
    return null;
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="online-knowledge-detail-title"
      onClick={onClose}
    >
      <div
        className="max-h-[78vh] w-full max-w-3xl overflow-hidden rounded-[24px] border border-white/80 bg-white shadow-[0_30px_90px_rgba(8,17,31,0.28)]"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div className="min-w-0">
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-teal-700">{detail.label}</div>
            <h2 id="online-knowledge-detail-title" className="font-heading mt-1 text-xl font-semibold tracking-tight text-slate-950">
              Full Content
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-950 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="max-h-[60vh] overflow-auto px-5 py-5">
          <p className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">{detail.value}</p>
        </div>
      </div>
    </div>
  );
}
