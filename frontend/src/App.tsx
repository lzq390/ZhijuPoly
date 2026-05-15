import { type ReactNode, useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, Atom, BarChart3, BookOpen, Database, Microscope, Search, Sparkles } from "lucide-react";
import { DatabaseAnalysis, type DatasetKey } from "./components/DatabaseAnalysis";
import { DatabaseQueryPage } from "./components/DatabaseQueryPage";
import { KetcherEditor } from "./components/KetcherEditor";
import { KnowledgeSearch } from "./components/KnowledgeSearch";
import { Layout } from "./components/Layout";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { ReverseDesignPage } from "./components/ReverseDesignPage";
import { StructurePreview3D } from "./components/StructurePreview3D";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { useKetcher } from "./hooks/useKetcher";
import { usePredict } from "./hooks/usePredict";
import { useQuery } from "./hooks/useQuery";
import {
  type KnowledgeNavigationRequest,
  type PredictableProperty,
  type ResultsTab,
  type WorkspaceMode
} from "./types";

type ActiveModule = "home" | "explorer" | "reverseDesign" | "databaseQuery" | "database" | "knowledge";

type AppRoute = {
  module: ActiveModule;
  datasetKey: DatasetKey | null;
};

type KnowledgeNavigationInput = string | KnowledgeNavigationRequest;

const datasetPathByKey: Record<DatasetKey, string> = {
  process: "/database/process",
  property: "/database/property",
  structureEffect: "/database/structure-effect",
  dft: "/database/dft",
  formulation: "/database/formulation"
};

const datasetKeyByPath = Object.fromEntries(
  Object.entries(datasetPathByKey).map(([key, path]) => [path, key as DatasetKey])
) as Record<string, DatasetKey>;

function normalizePath(pathname: string) {
  const normalized = pathname.replace(/\/+$/, "");
  return normalized.length > 0 ? normalized : "/";
}

function routeFromPath(pathname: string): AppRoute {
  const path = normalizePath(pathname);

  if (path === "/explorer") {
    return { module: "explorer", datasetKey: null };
  }

  if (path === "/reverse-design") {
    return { module: "reverseDesign", datasetKey: null };
  }

  if (path === "/database-query") {
    return { module: "databaseQuery", datasetKey: null };
  }

  if (path === "/knowledge") {
    return { module: "knowledge", datasetKey: null };
  }

  if (path === "/database") {
    return { module: "database", datasetKey: null };
  }

  const datasetKey = datasetKeyByPath[path];
  if (datasetKey) {
    return { module: "database", datasetKey };
  }

  return { module: "home", datasetKey: null };
}

function pathFromRoute(route: AppRoute) {
  if (route.module === "explorer") {
    return "/explorer";
  }

  if (route.module === "reverseDesign") {
    return "/reverse-design";
  }

  if (route.module === "databaseQuery") {
    return "/database-query";
  }

  if (route.module === "knowledge") {
    return "/knowledge";
  }

  if (route.module === "database") {
    return route.datasetKey ? datasetPathByKey[route.datasetKey] : "/database";
  }

  return "/";
}

function getInitialRoute() {
  if (typeof window === "undefined") {
    return { module: "home", datasetKey: null } satisfies AppRoute;
  }

  return routeFromPath(window.location.pathname);
}

function normalizeKnowledgeTerms(terms: string[] | undefined) {
  const normalized: string[] = [];
  const seen = new Set<string>();

  for (const term of terms ?? []) {
    const value = term.trim();
    if (!value) {
      continue;
    }

    const key = value.toLocaleLowerCase();
    if (seen.has(key)) {
      continue;
    }

    seen.add(key);
    normalized.push(value);
  }

  return normalized;
}

function getKnowledgeTermsFromSearch(search: string) {
  return normalizeKnowledgeTerms(new URLSearchParams(search).getAll("term"));
}

type ModuleTileProps = {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  description: string;
  status: string;
  actionLabel: string;
  disabled?: boolean;
  onClick?: () => void;
};

function ModuleTile({
  icon,
  eyebrow,
  title,
  description,
  status,
  actionLabel,
  disabled = false,
  onClick
}: ModuleTileProps) {
  return (
    <section
      className={[
        "group flex min-h-[260px] flex-col justify-between rounded-[30px] border p-5 transition-all duration-300 md:p-6",
        disabled
          ? "border-white/70 bg-white/55 text-slate-500"
          : "border-white/80 bg-white/85 text-slate-950 shadow-sm hover:-translate-y-1 hover:border-white hover:shadow-panel"
      ].join(" ")}
      aria-disabled={disabled}
    >
      <div>
        <div className="flex items-start justify-between gap-4">
          <div
            className={[
              "flex h-12 w-12 items-center justify-center rounded-2xl border shadow-sm",
              disabled ? "border-slate-200 bg-slate-100 text-slate-400" : "border-white bg-slate-950 text-white"
            ].join(" ")}
          >
            {icon}
          </div>
          <Badge className={disabled ? "bg-slate-100 text-slate-500" : "bg-teal-50 text-teal-800"}>{status}</Badge>
        </div>
        <div className="mt-7 text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">{eyebrow}</div>
        <h2 className="font-heading mt-3 text-[1.55rem] font-semibold tracking-tight text-slate-950">{title}</h2>
        <p className="mt-3 text-sm leading-6 text-mutedForeground">{description}</p>
      </div>

      <button
        type="button"
        disabled={disabled}
        onClick={onClick}
        className={[
          "mt-8 inline-flex min-h-11 items-center justify-between rounded-2xl px-4 text-sm font-semibold transition-all duration-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          disabled
            ? "cursor-not-allowed bg-slate-100 text-slate-400"
            : "bg-slate-950 text-white hover:bg-teal-700"
        ].join(" ")}
      >
        <span>{actionLabel}</span>
        <ArrowRight className="h-4 w-4" />
      </button>
    </section>
  );
}

function HomePage({
  onOpenExplorer,
  onOpenReverseDesign,
  onOpenDatabaseQuery,
  onOpenDatabase,
  onOpenKnowledge
}: {
  onOpenExplorer: () => void;
  onOpenReverseDesign: () => void;
  onOpenDatabaseQuery: () => void;
  onOpenDatabase: () => void;
  onOpenKnowledge: () => void;
}) {
  const moduleRows = [
    { name: "Polymer Property Explorer", status: "Ready" },
    { name: "Tg Reverse Design", status: "Ready" },
    { name: "Database Query", status: "Ready" },
    { name: "Database Analytics", status: "Ready" },
    { name: "Local Knowledge Search", status: "Ready" }
  ];

  return (
    <>
      <section className="hero-glow mesh-surface relative overflow-hidden rounded-[36px] border border-white/70 px-6 py-7 md:px-8 md:py-9">
        <div className="animate-fade-up grid gap-8 lg:grid-cols-[minmax(0,1fr)_420px] lg:items-center">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <div className="rounded-full border border-white/80 bg-white/80 px-4 py-2 text-sm font-semibold tracking-[0.16em] text-slate-950 shadow-sm">
                NEXPOLY
              </div>
              <Badge>Workspace Home</Badge>
            </div>

            <div className="mt-7 max-w-4xl">
              <h1 className="font-heading text-[2.65rem] font-semibold leading-[0.98] tracking-[-0.03em] text-slate-950 md:text-[4.4rem]">
                NexPoly Workspace
              </h1>
              <p className="mt-5 max-w-2xl text-base leading-7 text-slate-600 md:text-lg">
                A unified entry point for polymer property exploration, reverse design, database analytics, and local knowledge search.
              </p>
            </div>

          </div>

          <div className="rounded-[30px] border border-white/15 bg-slate-950 p-5 text-slate-50 shadow-[0_28px_70px_rgba(8,17,31,0.24)]">
            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">Module Index</div>
                <div className="font-heading mt-2 text-xl font-semibold tracking-tight">Workspace Structure</div>
              </div>
              <Database className="h-5 w-5 text-teal-300" />
            </div>

            <div className="mt-6 space-y-3">
              {moduleRows.map((item, index) => (
                <div
                  key={item.name}
                  className="grid grid-cols-[2.5rem_minmax(0,1fr)_auto] items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.06] px-3 py-3"
                >
                  <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-white/10 font-mono-ui text-sm text-teal-200">
                    {String(index + 1).padStart(2, "0")}
                  </div>
                  <div className="min-w-0">
                    <div className="truncate text-sm font-semibold">{item.name}</div>
                    <div className="mt-0.5 text-xs text-slate-400">{item.status}</div>
                  </div>
                  <div className={item.status === "Ready" ? "h-2.5 w-2.5 rounded-full bg-teal-300" : "h-2.5 w-2.5 rounded-full bg-slate-600"} />
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <ModuleTile
          icon={<Atom className="h-5 w-5" />}
          eyebrow="Available"
          title="Polymer Property Explorer"
          description="Edit structures, run similarity matching, preview 3D geometry, and request property predictions in one workspace."
          status="Ready"
          actionLabel="Enter"
          onClick={onOpenExplorer}
        />
        <ModuleTile
          icon={<Sparkles className="h-5 w-5" />}
          eyebrow="Available"
          title="Tg Reverse Design"
          description="Draw a polymer structure, enter a target Tg, and search the local PI candidate database."
          status="Ready"
          actionLabel="Enter"
          onClick={onOpenReverseDesign}
        />
        <ModuleTile
          icon={<Search className="h-5 w-5" />}
          eyebrow="Available"
          title="Database Query"
          description="Draw or paste a SMILES string, select a local table, and check whether that exact structure is already stored."
          status="Ready"
          actionLabel="Enter"
          onClick={onOpenDatabaseQuery}
        />
        <ModuleTile
          icon={<BarChart3 className="h-5 w-5" />}
          eyebrow="Available"
          title="Database Analytics"
          description="Explore five curated data views covering conformations, shares, distributions, and text-derived summaries."
          status="Ready"
          actionLabel="Enter"
          onClick={onOpenDatabase}
        />
        <ModuleTile
          icon={<BookOpen className="h-5 w-5" />}
          eyebrow="Available"
          title="Local Knowledge Search"
          description="Search local patent knowledge records by abstract keywords and inspect formulation-related details."
          status="Ready"
          actionLabel="Enter"
          onClick={onOpenKnowledge}
        />
      </section>
    </>
  );
}

export default function App() {
  const [activeModule, setActiveModule] = useState<ActiveModule>(() => getInitialRoute().module);
  const [selectedDatasetKey, setSelectedDatasetKey] = useState<DatasetKey | null>(() => getInitialRoute().datasetKey);
  const [knowledgeInitialQuery, setKnowledgeInitialQuery] = useState(() => {
    if (typeof window === "undefined") {
      return "";
    }
    return new URLSearchParams(window.location.search).get("q") ?? "";
  });
  const [knowledgeInitialTerms, setKnowledgeInitialTerms] = useState(() => {
    if (typeof window === "undefined") {
      return [] as string[];
    }
    return getKnowledgeTermsFromSearch(window.location.search);
  });
  const [hasOpenedExplorer, setHasOpenedExplorer] = useState(() => getInitialRoute().module === "explorer");
  const [preserveReverseDesignForKnowledge, setPreserveReverseDesignForKnowledge] = useState(false);
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher("*CC*");
  const { request, setRequest, isLoading, error, data, submit } = useQuery();
  const predict = usePredict();
  const [panelMode, setPanelMode] = useState<WorkspaceMode>("query");
  const [activeResultsTab, setActiveResultsTab] = useState<ResultsTab>("query");
  const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>([]);

  const canQuery =
    !isLoading &&
    smiles.trim().length > 0 &&
    (request.match_mode === "structure" || request.property_name !== null);
  const canPredict = !predict.isLoading && smiles.trim().length > 0 && selectedProperties.length > 0;

  const activeMode =
    panelMode === "predict"
      ? "Property prediction"
      : request.match_mode === "property"
        ? "Property similarity"
        : "Structural similarity";
  const activeModeLabel =
    panelMode === "predict"
      ? "Property Prediction"
      : request.match_mode === "property"
        ? "Property Similarity"
        : "Structural Similarity";

  const resultCount =
    activeResultsTab === "predict" ? Object.keys(predict.data?.predictions ?? {}).length : data?.total ?? 0;
  const resultTiming =
    activeResultsTab === "predict" ? predict.data?.query_time_ms : data?.query_time_ms;

  async function handleQuerySubmit() {
    setActiveResultsTab("query");
    await submit({ ...request, smiles });
  }

  async function handlePredictSubmit() {
    setActiveResultsTab("predict");
    try {
      await predict.submit({
        smiles,
        properties: selectedProperties
      });
    } catch {
      // Error state is already captured by the hook and shown in the results panel.
    }
  }

  const resultPanelTitle = activeResultsTab === "predict" ? "Prediction Results" : "Similarity Matching Results";
  const resultPanelDescription =
    activeResultsTab === "predict"
      ? "After prediction finishes, selected property values and calculation time appear here."
      : "After similarity matching runs, summaries, 2D structures, SMILES, and similarity scores appear here.";
  const resultPrimaryBadge =
    activeResultsTab === "predict"
      ? predict.data
        ? `${Object.keys(predict.data.predictions).length} predictions`
        : "No predictions"
      : data
        ? `${data.total} records`
        : "No results";
  const resultSecondaryBadge =
    activeResultsTab === "predict"
      ? predict.isLoading
        ? "Predicting"
        : "Prediction mode"
      : request.match_mode === "property"
        ? "Property similarity"
        : "Structural similarity";

  useEffect(() => {
    if (typeof window === "undefined" || !("scrollRestoration" in window.history)) {
      return;
    }

    const previousScrollRestoration = window.history.scrollRestoration;
    window.history.scrollRestoration = "manual";

    return () => {
      window.history.scrollRestoration = previousScrollRestoration;
    };
  }, []);

  function applyRoute(route: AppRoute) {
    setActiveModule(route.module);
    setSelectedDatasetKey(route.module === "database" ? route.datasetKey : null);

    if (route.module === "explorer") {
      setHasOpenedExplorer(true);
    }
    if (route.module !== "knowledge") {
      setPreserveReverseDesignForKnowledge(false);
    }
  }

  function navigate(route: AppRoute) {
    const path = pathFromRoute(route);

    if (typeof window !== "undefined") {
      if (normalizePath(window.location.pathname) !== path) {
        window.history.pushState(route, "", path);
      }
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }

    applyRoute(route);
  }

  useEffect(() => {
    function handlePopState() {
      const route = routeFromPath(window.location.pathname);
      if (route.module === "knowledge") {
        setKnowledgeInitialQuery(new URLSearchParams(window.location.search).get("q") ?? "");
        setKnowledgeInitialTerms(getKnowledgeTermsFromSearch(window.location.search));
      }
      applyRoute(route);
    }

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  function openExplorer() {
    navigate({ module: "explorer", datasetKey: null });
  }

  function openReverseDesign() {
    navigate({ module: "reverseDesign", datasetKey: null });
  }

  function openDatabaseQuery() {
    navigate({ module: "databaseQuery", datasetKey: null });
  }

  function openDatabase() {
    navigate({ module: "database", datasetKey: null });
  }

  function openKnowledge(input?: KnowledgeNavigationInput) {
    const rawQuery = typeof input === "string" ? input : input?.query;
    const trimmedQuery = rawQuery?.trim() ?? "";
    const terms = typeof input === "string" ? [] : normalizeKnowledgeTerms(input?.terms);
    const route = { module: "knowledge", datasetKey: null } satisfies AppRoute;
    const searchParams = new URLSearchParams();

    if (trimmedQuery) {
      searchParams.set("q", trimmedQuery);
    }
    for (const term of terms) {
      searchParams.append("term", term);
    }

    const queryString = searchParams.toString();
    const path = queryString ? `/knowledge?${queryString}` : "/knowledge";

    setPreserveReverseDesignForKnowledge(activeModule === "reverseDesign");
    setKnowledgeInitialQuery(trimmedQuery);
    setKnowledgeInitialTerms(terms);

    if (typeof window !== "undefined") {
      if (`${normalizePath(window.location.pathname)}${window.location.search}` !== path) {
        window.history.pushState(route, "", path);
      }
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }

    applyRoute(route);
  }

  return (
    <Layout>
      <div className={activeModule === "home" ? "contents" : "hidden"}>
        <HomePage
          onOpenExplorer={openExplorer}
          onOpenReverseDesign={openReverseDesign}
          onOpenDatabaseQuery={openDatabaseQuery}
          onOpenDatabase={openDatabase}
          onOpenKnowledge={openKnowledge}
        />
      </div>

      {activeModule === "databaseQuery" ? (
        <DatabaseQueryPage onBackHome={() => navigate({ module: "home", datasetKey: null })} />
      ) : null}

      {activeModule === "database" ? (
        <DatabaseAnalysis
          selectedKey={selectedDatasetKey}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
          onBackDatabase={() => navigate({ module: "database", datasetKey: null })}
          onOpenDataset={(datasetKey) => navigate({ module: "database", datasetKey })}
        />
      ) : null}

      {activeModule === "knowledge" ? (
        <KnowledgeSearch
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
          initialQuery={knowledgeInitialQuery}
          initialTerms={knowledgeInitialTerms}
        />
      ) : null}

      {activeModule === "reverseDesign" || preserveReverseDesignForKnowledge ? (
        <div
          className={activeModule === "reverseDesign" ? "contents" : "hidden"}
          aria-hidden={activeModule !== "reverseDesign"}
        >
          <ReverseDesignPage
            onBackHome={() => navigate({ module: "home", datasetKey: null })}
            onOpenKnowledge={openKnowledge}
          />
        </div>
      ) : null}

      {hasOpenedExplorer ? (
        <div className={activeModule === "explorer" ? "contents" : "hidden"} aria-hidden={activeModule !== "explorer"}>
          <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
            <div className="flex items-center gap-3">
              <Button type="button" variant="outline" onClick={() => navigate({ module: "home", datasetKey: null })}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                Home
              </Button>
              <div>
                <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">Current Module</div>
                <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">
                  Polymer Property Explorer
                </div>
              </div>
            </div>
            <Badge className="bg-teal-50 text-teal-800">Explorer</Badge>
          </nav>

          <section className="hero-glow mesh-surface relative overflow-hidden rounded-[36px] border border-white/70 px-6 py-6 md:px-8 md:py-8">
            <div className="pointer-events-none absolute inset-y-0 right-0 hidden w-[36%] bg-[radial-gradient(circle_at_center,rgba(15,118,110,0.14),transparent_58%)] lg:block" />
            <div className="pointer-events-none absolute -right-10 top-12 h-40 w-40 rounded-full border border-white/40 bg-white/20 blur-2xl" />
            <div className="pointer-events-none absolute left-8 top-24 h-24 w-24 rounded-full bg-teal-300/20 blur-3xl" />

            <div className="animate-fade-up">
              <div className="flex flex-wrap items-center gap-3">
                <div className="rounded-full border border-white/80 bg-white/80 px-4 py-2 text-sm font-semibold tracking-[0.16em] text-slate-950 shadow-sm">
                  NEXPOLY
                </div>
                <Badge>Polymer Similarity Matching & Property Prediction</Badge>
              </div>

              <div className="mt-6 overflow-x-auto">
                <h1 className="font-heading whitespace-nowrap text-[2.5rem] font-semibold tracking-[-0.04em] text-slate-950 md:text-[4rem] md:leading-[0.95]">
                  Polymer Property Explorer
                </h1>
                <p className="mt-4 whitespace-nowrap text-base leading-7 text-slate-600 md:text-lg">
                  Bring structure editing, similarity matching, 3D review, and property prediction into one focused research workspace.
                </p>
              </div>

              <div className="mt-8 grid gap-3 md:grid-cols-3">
                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                    {panelMode === "predict" ? <Sparkles className="h-4 w-4 text-teal-600" /> : <Atom className="h-4 w-4 text-teal-600" />}
                    Current Mode
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                    {activeModeLabel}
                  </div>
                  <div className="mt-2 text-sm leading-6 text-mutedForeground">
                    {panelMode === "predict"
                      ? "Select target properties in the control card and run prediction for the current structure."
                      : "Switch between structural similarity and property similarity matching in the control card."}
                  </div>
                </div>

                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                    <Microscope className="h-4 w-4 text-sky-600" />
                    Structure Input
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                    {smiles.trim().length > 0 ? "Ready" : "Waiting"}
                  </div>
                  <div className="mt-2 text-sm leading-6 text-mutedForeground">
                    The structure editor keeps the SMILES input updated for matching or prediction.
                  </div>
                </div>

                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-slate-950 p-5 text-center text-slate-50 shadow-[0_22px_50px_rgba(8,17,31,0.2)]">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">
                    <Database className="h-4 w-4 text-teal-300" />
                    Latest Results
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight">{resultCount}</div>
                  <div className="mt-2 text-sm leading-6 text-slate-300">
                    {resultTiming ? `${resultTiming.toFixed(1)} ms returned` : "Result count and latency appear after execution."}
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section>
            <div className="grid items-stretch gap-6 xl:grid-cols-[minmax(0,1.22fr)_minmax(0,0.92fr)]">
              <div className="min-w-0">
                <KetcherEditor
                  smiles={smiles}
                  iframeRef={iframeRef}
                  onReadyChange={setIsReady}
                  onChange={(value) => {
                    setSmiles(value);
                    setRequest({ ...request, smiles: value });
                  }}
                />
              </div>

              <div className="flex min-w-0 flex-col gap-6">
                <StructurePreview3D smiles={smiles} />
                <QueryPanel
                  className="w-full self-start"
                  mode={panelMode}
                  onModeChange={setPanelMode}
                  request={{ ...request, smiles }}
                  onChange={setRequest}
                  onQuerySubmit={handleQuerySubmit}
                  onPredictSubmit={handlePredictSubmit}
                  selectedProperties={selectedProperties}
                  onSelectedPropertiesChange={setSelectedProperties}
                  queryDisabled={!canQuery}
                  predictDisabled={!canPredict}
                  isQueryLoading={isLoading}
                  isPredicting={predict.isLoading}
                />
              </div>
            </div>
          </section>

          <section className="relative pt-2">
            <div className="absolute left-0 right-0 top-0 h-px bg-gradient-to-r from-transparent via-slate-400/40 to-transparent" />
            <div className="pt-6">
              <div className="overflow-hidden rounded-[32px] border border-white/70 bg-[linear-gradient(180deg,rgba(255,255,255,0.98)_0%,rgba(243,248,250,0.92)_100%)] shadow-soft">
                <div className="border-b border-slate-200/80 px-6 py-5 md:px-8">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
                    <div>
                      <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/70">Results</div>
                      <h2 className="font-heading mt-2 text-[1.8rem] font-semibold tracking-tight text-slate-950">
                        {resultPanelTitle}
                      </h2>
                      <p className="mt-1 text-sm leading-6 text-mutedForeground">{resultPanelDescription}</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Badge className="bg-slate-100 text-slate-700">{resultPrimaryBadge}</Badge>
                      <Badge className="bg-slate-100 text-slate-700">{resultSecondaryBadge}</Badge>
                    </div>
                  </div>
                </div>
                <div className="px-4 py-4 md:px-5 md:py-5">
                  <ResultsDisplay
                    data={data}
                    error={error}
                    isLoading={isLoading}
                    request={{ ...request, smiles }}
                    predictData={predict.data}
                    isPredicting={predict.isLoading}
                    predictError={predict.error}
                    activeTab={activeResultsTab}
                    onTabChange={setActiveResultsTab}
                  />
                </div>
              </div>
            </div>
          </section>
        </div>
      ) : null}
    </Layout>
  );
}
