import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Atom,
  BarChart3,
  BookOpen,
  Database,
  Filter,
  FlaskConical,
  Grid2X2,
  Microscope,
  Search,
  Sparkles
} from "lucide-react";
import {
  AppShell,
  type AppShellModuleGroup,
  type AppShellModuleItem
} from "./components/AppShell";
import { AgentWorkspaceHomePage, agentWorkspaceUrl } from "./components/AgentWorkspaceHomePage";
import { ConditionalGenerationPage } from "./components/ConditionalGenerationPage";
import { DatabaseAnalysis, type DatasetKey } from "./components/DatabaseAnalysis";
import { DatabaseFilterPage } from "./components/DatabaseFilterPage";
import { DatabaseQueryPage } from "./components/DatabaseQueryPage";
import { ExperimentWorkflowDemoPage } from "./components/ExperimentWorkflowDemoPage";
import { HighThroughputWorkflowDemoPage } from "./components/HighThroughputWorkflowDemoPage";
import { HomopolymerPropertyPredictionPage } from "./components/HomopolymerPropertyPredictionPage";
import { KnowledgeSearch } from "./components/KnowledgeSearch";
import { LabDataPage, type LabDataView } from "./components/LabDataPage";
import { MdSimulationDemoPage } from "./components/MdSimulationDemoPage";
import { MonomerMdSimulationPage } from "./components/MonomerMdSimulationPage";
import { MonomerDftPage } from "./components/MonomerDftPage";
import { MonomerPolymerizationPage } from "./components/MonomerPolymerizationPage";
import { PolytaoGenerationPage } from "./components/PolytaoGenerationPage";
import { ReverseDesignPage } from "./components/ReverseDesignPage";
import { PolymerExplorerDesktopPage } from "./components/PolymerExplorerDesktopPage";
import {
  StructureWorkbenchPage,
  type StructureCanvasOwnerHandle
} from "./components/StructureWorkbenchPage";
import { useKetcher } from "./hooks/useKetcher";
import { useQuery } from "./hooks/useQuery";
import { useTgAssistant } from "./hooks/useTgAssistant";
import { standardizeSmiles } from "./services/api";
import { getMonomerDftJobIdFromSearch, getMonomerDftPath } from "./lib/monomerDftRouting";
import {
  normalizeKnowledgeSearchGroups,
  serializeKnowledgeSearchGroups
} from "./lib/knowledgeSearchExpression";
import {
  createOpenScienceProjectBridge,
  type OpenScienceProjectsSnapshot
} from "./lib/openScienceProjectBridge";
import {
  createOpenScienceGeneralSessionBridge,
  type OpenScienceGeneralSessionsSnapshot
} from "./lib/openScienceGeneralSessionBridge";
import {
  type KnowledgeNavigationRequest,
  type StructureWorkspaceContext
} from "./types";

type ActiveModule =
  | "home"
  | "structureWorkbench"
  | "homopolymerPrediction"
  | "explorer"
  | "mdSimulationDemo"
  | "monomerMdSimulation"
  | "monomerDft"
  | "monomerPolymerization"
  | "reverseDesign"
  | "conditionalGeneration"
  | "polytaoGeneration"
  | "databaseQuery"
  | "databaseFilter"
  | "database"
  | "knowledge"
  | "labData"
  | "experimentWorkflowDemo"
  | "highThroughputWorkflowDemo";

type AppRoute = {
  module: ActiveModule;
  datasetKey: DatasetKey | null;
  labDataView?: LabDataView;
};

type KnowledgeNavigationInput = string | KnowledgeNavigationRequest;
type AgentWorkspaceView = "general" | "projects" | "project";
const POLYTAO_ROUTE = "/polytao-generation";
const HOMOPOLYMER_PREDICTION_ROUTE = "/homopolymer-property-prediction";
const LEGACY_POLYTAO_ROUTE = "/conditional-generation/polytao";
const DATABASE_FILTER_ROUTE = "/database-filter";
const LEGACY_DATABASE_FILTER_ROUTE = "/database/property-filter";
const STRUCTURE_NAVIGATION_SYNC_TIMEOUT_MS = 1500;

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

  if (path === "/structure-workbench") {
    return { module: "structureWorkbench", datasetKey: null };
  }

  if (path === HOMOPOLYMER_PREDICTION_ROUTE) {
    return { module: "homopolymerPrediction", datasetKey: null };
  }

  if (path === "/explorer") {
    return { module: "explorer", datasetKey: null };
  }

  if (path === "/md-simulation") {
    return { module: "mdSimulationDemo", datasetKey: null };
  }

  if (path === "/monomer-md-simulation") {
    return { module: "monomerMdSimulation", datasetKey: null };
  }

  if (path === "/monomer-dft") {
    return { module: "monomerDft", datasetKey: null };
  }

  if (path === "/monomer-polymerization") {
    return { module: "monomerPolymerization", datasetKey: null };
  }

  if (path === "/reverse-design") {
    return { module: "reverseDesign", datasetKey: null };
  }

  if (path === "/conditional-generation") {
    return { module: "conditionalGeneration", datasetKey: null };
  }

  if (path === POLYTAO_ROUTE || path === LEGACY_POLYTAO_ROUTE) {
    return { module: "polytaoGeneration", datasetKey: null };
  }

  if (path === "/database-query") {
    return { module: "databaseQuery", datasetKey: null };
  }

  if (path === DATABASE_FILTER_ROUTE || path === LEGACY_DATABASE_FILTER_ROUTE) {
    return { module: "databaseFilter", datasetKey: null };
  }

  if (path === "/knowledge") {
    return { module: "knowledge", datasetKey: null };
  }

  if (path === "/experiment-workflow-demo") {
    return { module: "experimentWorkflowDemo", datasetKey: null };
  }

  if (path === "/high-throughput-workflow-demo") {
    return { module: "highThroughputWorkflowDemo", datasetKey: null };
  }

  if (path === "/lab-data" || path === "/lab-data/collect") {
    return { module: "labData", datasetKey: null, labDataView: "collect" };
  }

  if (path === "/lab-data/dashboard") {
    return { module: "labData", datasetKey: null, labDataView: "dashboard" };
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
  if (route.module === "structureWorkbench") {
    return "/structure-workbench";
  }

  if (route.module === "homopolymerPrediction") {
    return HOMOPOLYMER_PREDICTION_ROUTE;
  }

  if (route.module === "explorer") {
    return "/explorer";
  }

  if (route.module === "mdSimulationDemo") {
    return "/md-simulation";
  }

  if (route.module === "monomerMdSimulation") {
    return "/monomer-md-simulation";
  }

  if (route.module === "monomerDft") {
    return "/monomer-dft";
  }

  if (route.module === "monomerPolymerization") {
    return "/monomer-polymerization";
  }

  if (route.module === "reverseDesign") {
    return "/reverse-design";
  }

  if (route.module === "conditionalGeneration") {
    return "/conditional-generation";
  }

  if (route.module === "polytaoGeneration") {
    return POLYTAO_ROUTE;
  }

  if (route.module === "databaseQuery") {
    return "/database-query";
  }

  if (route.module === "databaseFilter") {
    return DATABASE_FILTER_ROUTE;
  }

  if (route.module === "knowledge") {
    return "/knowledge";
  }

  if (route.module === "experimentWorkflowDemo") {
    return "/experiment-workflow-demo";
  }

  if (route.module === "highThroughputWorkflowDemo") {
    return "/high-throughput-workflow-demo";
  }

  if (route.module === "labData") {
    return route.labDataView === "dashboard" ? "/lab-data/dashboard" : "/lab-data/collect";
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

  const route = routeFromPath(window.location.pathname);
  if (normalizePath(window.location.pathname) === LEGACY_POLYTAO_ROUTE) {
    window.history.replaceState(route, "", POLYTAO_ROUTE);
  } else if (normalizePath(window.location.pathname) === LEGACY_DATABASE_FILTER_ROUTE) {
    window.history.replaceState(route, "", DATABASE_FILTER_ROUTE);
  }
  return route;
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

function getKnowledgeQueryFromSearch(search: string) {
  const terms = getKnowledgeTermsFromSearch(search);
  if (terms.length) return terms.join("；");
  return new URLSearchParams(search).get("q") ?? "";
}

export default function App() {
  const [activeModule, setActiveModule] = useState<ActiveModule>(() => getInitialRoute().module);
  const [selectedDatasetKey, setSelectedDatasetKey] = useState<DatasetKey | null>(() => getInitialRoute().datasetKey);
  const [labDataView, setLabDataView] = useState<LabDataView>(() => getInitialRoute().labDataView ?? "collect");
  const [monomerDftJobId, setMonomerDftJobId] = useState<string | null>(() => {
    if (typeof window === "undefined" || getInitialRoute().module !== "monomerDft") {
      return null;
    }
    return getMonomerDftJobIdFromSearch(window.location.search);
  });
  const [knowledgeInitialQuery, setKnowledgeInitialQuery] = useState(() => {
    if (typeof window === "undefined") {
      return "";
    }
    return getKnowledgeQueryFromSearch(window.location.search);
  });
  const [knowledgeInitialTerms, setKnowledgeInitialTerms] = useState(() => {
    if (typeof window === "undefined") {
      return [] as string[];
    }
    return getKnowledgeTermsFromSearch(window.location.search);
  });
  const [preserveReverseDesignForKnowledge, setPreserveReverseDesignForKnowledge] = useState(false);
  const [hasMountedStructureWorkbench, setHasMountedStructureWorkbench] = useState(
    () => getInitialRoute().module === "structureWorkbench"
  );
  const activeModuleRef = useRef(activeModule);
  activeModuleRef.current = activeModule;
  const structureCanvasOwnerRef = useRef<StructureCanvasOwnerHandle | null>(null);
  const structureNavigationSyncRef = useRef<Promise<void> | null>(null);
  const structureNavigationIntentRef = useRef(0);
  const popstateRevisionRef = useRef(0);
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher();
  const { request, setRequest, isLoading, error, data, submit } = useQuery();
  const tgAssistant = useTgAssistant();
  const agentWorkspaceIframeRef = useRef<HTMLIFrameElement | null>(null);
  const [agentWorkspaceFrameUrl, setAgentWorkspaceFrameUrl] = useState(agentWorkspaceUrl);
  const [agentWorkspaceReloadKey, setAgentWorkspaceReloadKey] = useState(0);
  const [agentWorkspaceView, setAgentWorkspaceView] = useState<AgentWorkspaceView>("general");
  const [projectSnapshot, setProjectSnapshot] = useState<OpenScienceProjectsSnapshot | null>(null);
  const [generalSessionSnapshot, setGeneralSessionSnapshot] =
    useState<OpenScienceGeneralSessionsSnapshot | null>(null);
  const projectBridge = useMemo(
    () =>
      createOpenScienceProjectBridge({
        workspaceUrl: agentWorkspaceUrl(),
        getFrameWindow: () => agentWorkspaceIframeRef.current?.contentWindow ?? null,
        onSnapshot: (snapshot) => {
          setProjectSnapshot(snapshot);
          if (snapshot.activeDirectory) {
            setAgentWorkspaceView("project");
          }
        }
      }),
    []
  );
  const generalSessionBridge = useMemo(
    () =>
      createOpenScienceGeneralSessionBridge({
        workspaceUrl: agentWorkspaceUrl(),
        getFrameWindow: () => agentWorkspaceIframeRef.current?.contentWindow ?? null,
        onSnapshot: setGeneralSessionSnapshot
      }),
    []
  );

  async function getCurrentSmiles() {
    const fallbackSmiles = smiles.trim();
    let currentSmiles = fallbackSmiles;
    const ketcher = iframeRef.current?.contentWindow?.ketcher;
    if (ketcher && typeof ketcher.getSmiles === "function") {
      try {
        const editorSmiles = (await ketcher.getSmiles()).trim();
        if (editorSmiles) {
          if (editorSmiles !== fallbackSmiles) {
            setSmiles(editorSmiles);
          }
          currentSmiles = editorSmiles;
        } else if (fallbackSmiles) {
          currentSmiles = fallbackSmiles;
        } else {
          setSmiles(editorSmiles);
          currentSmiles = editorSmiles;
        }
      } catch (syncError) {
        console.error("Failed to read SMILES from Ketcher", syncError);
      }
    }

    if (!currentSmiles) {
      return currentSmiles;
    }

    try {
      const result = await standardizeSmiles({ smiles: currentSmiles });
      if (result.standardized_smiles !== smiles.trim()) {
        setSmiles(result.standardized_smiles);
      }
      return result.standardized_smiles;
    } catch (standardizeError) {
      console.error("Failed to standardize current SMILES", standardizeError);
      return currentSmiles;
    }
  }

  const structureWorkspace: StructureWorkspaceContext = {
    smiles,
    setSmiles,
    iframeRef,
    setIsReady,
    getCurrentSmiles
  };

  const syncStructureBeforeNavigation = useCallback(() => {
    if (structureNavigationSyncRef.current) {
      return structureNavigationSyncRef.current;
    }

    const task = structureCanvasOwnerRef.current?.syncBeforeLeave() ?? Promise.resolve();
    const guarded = new Promise<void>((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        resolve();
      };
      const timeout = window.setTimeout(finish, STRUCTURE_NAVIGATION_SYNC_TIMEOUT_MS);
      task.then(finish, finish);
    });
    const tracked = guarded.finally(() => {
      if (structureNavigationSyncRef.current === tracked) {
        structureNavigationSyncRef.current = null;
      }
    });
    structureNavigationSyncRef.current = tracked;
    return tracked;
  }, []);

  const beforeStructureCanvasNavigation = useCallback(() => {
    const intent = structureNavigationIntentRef.current + 1;
    structureNavigationIntentRef.current = intent;
    return syncStructureBeforeNavigation().then(
      () => structureNavigationIntentRef.current === intent
    );
  }, [syncStructureBeforeNavigation]);

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

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      projectBridge.handleMessage(event);
      generalSessionBridge.handleMessage(event);
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [generalSessionBridge, projectBridge]);

  function applyRoute(route: AppRoute) {
    setActiveModule(route.module);
    setSelectedDatasetKey(route.module === "database" ? route.datasetKey : null);
    setLabDataView(route.module === "labData" ? route.labDataView ?? "collect" : "collect");

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
    if (activeModule === "structureWorkbench") {
      setHasMountedStructureWorkbench(true);
    }
  }, [activeModule]);

  useEffect(() => {
    function handlePopState() {
      structureNavigationIntentRef.current += 1;
      const route = routeFromPath(window.location.pathname);
      const pathname = window.location.pathname;
      const search = window.location.search;
      const revision = popstateRevisionRef.current + 1;
      popstateRevisionRef.current = revision;

      const applyLatestRoute = () => {
        if (popstateRevisionRef.current !== revision) return;
        if (normalizePath(pathname) === LEGACY_POLYTAO_ROUTE) {
          window.history.replaceState(route, "", POLYTAO_ROUTE);
        } else if (normalizePath(pathname) === LEGACY_DATABASE_FILTER_ROUTE) {
          window.history.replaceState(route, "", DATABASE_FILTER_ROUTE);
        }
        if (route.module === "knowledge") {
          setKnowledgeInitialQuery(getKnowledgeQueryFromSearch(search));
          setKnowledgeInitialTerms(getKnowledgeTermsFromSearch(search));
        }
        if (route.module === "monomerDft") {
          setMonomerDftJobId(getMonomerDftJobIdFromSearch(search));
        }
        applyRoute(route);
      };

      if (
        activeModuleRef.current === "structureWorkbench" ||
        activeModuleRef.current === "homopolymerPrediction"
      ) {
        void syncStructureBeforeNavigation().then(applyLatestRoute);
      } else {
        applyLatestRoute();
      }
    }

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [syncStructureBeforeNavigation]);

  useEffect(() => {
    if (activeModule !== "knowledge" || knowledgeInitialTerms.length === 0) return;
    const canonicalQuery = knowledgeInitialTerms.join("；");
    const searchParams = new URLSearchParams(window.location.search);
    if (searchParams.get("q") === canonicalQuery) return;
    searchParams.set("q", canonicalQuery);
    window.history.replaceState(
      { module: "knowledge", datasetKey: null } satisfies AppRoute,
      "",
      `/knowledge?${searchParams.toString()}`
    );
    setKnowledgeInitialQuery(canonicalQuery);
  }, [activeModule, knowledgeInitialTerms]);

  function openExplorer() {
    navigate({ module: "explorer", datasetKey: null });
  }

  function openStructureWorkbench() {
    navigate({ module: "structureWorkbench", datasetKey: null });
  }

  function openHomopolymerPrediction() {
    navigate({ module: "homopolymerPrediction", datasetKey: null });
  }

  function openMdSimulationDemo() {
    navigate({ module: "mdSimulationDemo", datasetKey: null });
  }

  function openMonomerMdSimulation() {
    navigate({ module: "monomerMdSimulation", datasetKey: null });
  }

  function openMonomerDft(jobId: string | null = null) {
    const route = { module: "monomerDft", datasetKey: null } satisfies AppRoute;
    const path = getMonomerDftPath(jobId);
    if (typeof window !== "undefined") {
      if (`${normalizePath(window.location.pathname)}${window.location.search}` !== path) {
        window.history.pushState(route, "", path);
      }
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    }
    setMonomerDftJobId(jobId);
    applyRoute(route);
  }

  function openMonomerPolymerization() {
    navigate({ module: "monomerPolymerization", datasetKey: null });
  }

  function openReverseDesign() {
    navigate({ module: "reverseDesign", datasetKey: null });
  }

  function openConditionalGeneration() {
    navigate({ module: "conditionalGeneration", datasetKey: null });
  }

  function openPolytaoGeneration() {
    navigate({ module: "polytaoGeneration", datasetKey: null });
  }

  function openExperimentWorkflowDemo() {
    navigate({ module: "experimentWorkflowDemo", datasetKey: null });
  }

  function openHighThroughputWorkflowDemo() {
    navigate({ module: "highThroughputWorkflowDemo", datasetKey: null });
  }

  function openDatabaseQuery() {
    navigate({ module: "databaseQuery", datasetKey: null });
  }

  function openDatabaseFilter() {
    navigate({ module: "databaseFilter", datasetKey: null });
  }

  function openDatabase() {
    navigate({ module: "database", datasetKey: null });
  }

  function openKnowledge(input?: KnowledgeNavigationInput) {
    const rawQuery = typeof input === "string" ? input : input?.query;
    const groups = typeof input === "string" ? [] : normalizeKnowledgeSearchGroups(input?.groups ?? []);
    const terms = typeof input === "string" || groups.length ? [] : normalizeKnowledgeTerms(input?.terms);
    const trimmedQuery = groups.length
      ? serializeKnowledgeSearchGroups(groups)
      : terms.length
        ? terms.join("；")
        : (rawQuery?.trim() ?? "");
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

  function openLabData(view: LabDataView = "collect") {
    navigate({ module: "labData", datasetKey: null, labDataView: view });
  }

  function openModuleById(moduleId: string) {
    switch (moduleId) {
      case "structureWorkbench":
        openStructureWorkbench();
        break;
      case "homopolymerPrediction":
        openHomopolymerPrediction();
        break;
      case "labData":
        openLabData("collect");
        break;
      case "databaseQuery":
        openDatabaseQuery();
        break;
      case "databaseFilter":
        openDatabaseFilter();
        break;
      case "database":
        openDatabase();
        break;
      case "knowledge":
        openKnowledge();
        break;
      case "explorer":
        openExplorer();
        break;
      case "mdSimulationDemo":
        openMdSimulationDemo();
        break;
      case "monomerMdSimulation":
        openMonomerMdSimulation();
        break;
      case "monomerDft":
        openMonomerDft();
        break;
      case "monomerPolymerization":
        openMonomerPolymerization();
        break;
      case "reverseDesign":
        openReverseDesign();
        break;
      case "conditionalGeneration":
        openConditionalGeneration();
        break;
      case "polytaoGeneration":
        openPolytaoGeneration();
        break;
      case "experimentWorkflowDemo":
        openExperimentWorkflowDemo();
        break;
      case "highThroughputWorkflowDemo":
        openHighThroughputWorkflowDemo();
        break;
    }
  }

  function openAgentProject(directory: string) {
    navigate({ module: "home", datasetKey: null });
    setAgentWorkspaceView("project");
    projectBridge.openProject(directory);
  }

  function browseAgentProjects() {
    navigate({ module: "home", datasetKey: null });
    setAgentWorkspaceView("projects");
    projectBridge.browseProjects();
  }

  function createAgentProject() {
    navigate({ module: "home", datasetKey: null });
    projectBridge.newProject();
  }

  function setAgentProjectFavorite(directory: string, favorite: boolean) {
    projectBridge.setProjectFavorite(directory, favorite);
  }

  function archiveAgentProject(directory: string) {
    if (projectSnapshot?.activeDirectory === directory) {
      setAgentWorkspaceView("general");
      setGeneralSessionSnapshot(null);
    }
    projectBridge.archiveProject(directory);
  }

  function agentWorkspaceRouteUrl(pathname: string) {
    try {
      const url = new URL(agentWorkspaceUrl());
      url.pathname = pathname;
      url.search = "";
      url.hash = "";
      return url.toString();
    } catch {
      return agentWorkspaceUrl();
    }
  }

  function openGeneralWorkspace() {
    navigate({ module: "home", datasetKey: null });
    setAgentWorkspaceView("general");
    setGeneralSessionSnapshot(null);
    setAgentWorkspaceFrameUrl(agentWorkspaceRouteUrl("/"));
    setAgentWorkspaceReloadKey((current) => current + 1);
  }

  function createGeneralSession() {
    navigate({ module: "home", datasetKey: null });
    setAgentWorkspaceView("general");
    generalSessionBridge.newSession();
  }

  function openGeneralSession(sessionID: string) {
    navigate({ module: "home", datasetKey: null });
    setAgentWorkspaceView("general");
    generalSessionBridge.openSession(sessionID);
  }

  const standaloneModules: AppShellModuleItem[] = [
    {
      id: "structureWorkbench",
      label: "结构工作台",
      description: "统一绘制、输入和预览当前共享结构。",
      route: "/structure-workbench",
      icon: <Grid2X2 className="h-4 w-4" />,
      isActive: activeModule === "structureWorkbench",
      onClick: openStructureWorkbench
    }
  ];
  const moduleGroups: AppShellModuleGroup[] = [
    {
      id: "discover",
      label: "材料发现",
      secondaryLabel: "Discover",
      items: [
        {
          id: "knowledge",
          label: "知识检索",
          description: "检索聚合物文献、摘要和合成知识。",
          route: "/knowledge",
          icon: <BookOpen className="h-4 w-4" />,
          isActive: activeModule === "knowledge",
          onClick: () => openKnowledge()
        },
        {
          id: "polytaoGeneration",
          label: "聚合物生成",
          description: "按 15 个 RDKit 描述符调用 PolyTAO 生成候选重复单元。",
          route: POLYTAO_ROUTE,
          icon: <Sparkles className="h-4 w-4" />,
          isActive: activeModule === "polytaoGeneration",
          onClick: openPolytaoGeneration
        },
        {
          id: "explorer",
          label: "聚合物性能探索",
          description: "编辑结构、运行结构或性能相似匹配并预览 3D 构象。",
          route: "/explorer",
          icon: <Atom className="h-4 w-4" />,
          isActive: activeModule === "explorer",
          onClick: openExplorer
        },
        {
          id: "databaseQuery",
          label: "数据库查询",
          description: "用结构或 SMILES 检查数据库记录。",
          route: "/database-query",
          icon: <Search className="h-4 w-4" />,
          isActive: activeModule === "databaseQuery",
          onClick: openDatabaseQuery
        },
        {
          id: "databaseFilter",
          label: "数据库筛选",
          description: "按多个性质阈值组合筛选聚合物记录。",
          route: DATABASE_FILTER_ROUTE,
          icon: <Filter className="h-4 w-4" />,
          isActive: activeModule === "databaseFilter",
          onClick: openDatabaseFilter
        },
        {
          id: "database",
          label: "数据库分析",
          description: "浏览过程、性能、DFT 与结构数据集。",
          route: "/database",
          icon: <Database className="h-4 w-4" />,
          isActive: activeModule === "database",
          onClick: openDatabase
        }
      ]
    },
    {
      id: "build",
      label: "材料设计",
      secondaryLabel: "Build",
      items: [
        {
          id: "homopolymerPrediction",
          label: "均聚物性质预测",
          description: "在共享结构画板中预测九项热学、力学与气体渗透性质。",
          route: HOMOPOLYMER_PREDICTION_ROUTE,
          icon: <BarChart3 className="h-4 w-4" />,
          isActive: activeModule === "homopolymerPrediction",
          onClick: openHomopolymerPrediction
        },
        {
          id: "monomerPolymerization",
          label: "单体正向聚合",
          description: "用 SMiPoly 规则对一个或两个单体生成少量聚合物候选。",
          route: "/monomer-polymerization",
          icon: <FlaskConical className="h-4 w-4" />,
          isActive: activeModule === "monomerPolymerization",
          onClick: openMonomerPolymerization
        },
        {
          id: "mdSimulationDemo",
          label: "MD 模拟",
          description: "输入 SMILES 和默认参数，演示分子动力学流程与轨迹结果。",
          route: "/md-simulation",
          icon: <Activity className="h-4 w-4" />,
          isActive: activeModule === "mdSimulationDemo",
          onClick: openMdSimulationDemo
        },
        {
          id: "monomerMdSimulation",
          label: "单体 MD 模拟",
          description: "Submit ordinary monomer SMILES and track MD worker job results.",
          route: "/monomer-md-simulation",
          icon: <Microscope className="h-4 w-4" />,
          isActive: activeModule === "monomerMdSimulation",
          onClick: openMonomerMdSimulation
        },
        {
          id: "monomerDft",
          label: "单体 DFT（AIMNet2）",
          description: "用独立 GPU Worker 计算单点性质、Hessian、频率和几何优化。",
          route: "/monomer-dft",
          icon: <FlaskConical className="h-4 w-4" />,
          isActive: activeModule === "monomerDft",
          onClick: () => openMonomerDft()
        },
        {
          id: "conditionalGeneration",
          label: "条件聚合物生成",
          description: "基于目标条件生成候选聚合物。",
          route: "/conditional-generation",
          icon: <Microscope className="h-4 w-4" />,
          isActive: activeModule === "conditionalGeneration",
          onClick: openConditionalGeneration
        }
      ]
    },
    {
      id: "optimize",
      label: "实验优化",
      secondaryLabel: "Optimize",
      items: [
        {
          id: "reverseDesign",
          label: "Tg 逆向设计",
          description: "按目标玻璃化转变温度筛选候选结构。",
          route: "/reverse-design",
          icon: <Sparkles className="h-4 w-4" />,
          isActive: activeModule === "reverseDesign",
          onClick: openReverseDesign
        },
        {
          id: "highThroughputWorkflowDemo",
          label: "高通量优化演示",
          description: "用模拟数据展示 PI 候选空间、单目标 Agent 和配方混合优化闭环。",
          route: "/high-throughput-workflow-demo",
          icon: <BarChart3 className="h-4 w-4" />,
          isActive: activeModule === "highThroughputWorkflowDemo",
          onClick: openHighThroughputWorkflowDemo
        }
      ]
    },
    {
      id: "data",
      label: "数据管理",
      secondaryLabel: "Data",
      items: [],
      emptyLabel: "暂无模块"
    }
  ];
  const isFullBleedModule =
    activeModule === "explorer" ||
    activeModule === "homopolymerPrediction" ||
    activeModule === "databaseQuery" ||
    activeModule === "databaseFilter" ||
    activeModule === "database" ||
    activeModule === "knowledge" ||
    activeModule === "structureWorkbench" ||
    activeModule === "monomerPolymerization" ||
    activeModule === "polytaoGeneration" ||
    activeModule === "reverseDesign" ||
    activeModule === "conditionalGeneration" ||
    activeModule === "experimentWorkflowDemo" ||
    activeModule === "highThroughputWorkflowDemo" ||
    activeModule === "mdSimulationDemo" ||
    activeModule === "monomerMdSimulation" ||
    activeModule === "monomerDft";
  const isTgKetcherOwner =
    activeModule === "reverseDesign" ||
    activeModule === "conditionalGeneration" ||
    (activeModule === "knowledge" && preserveReverseDesignForKnowledge);
  const shouldKeepStructureWorkbenchMounted =
    hasMountedStructureWorkbench &&
    activeModule !== "homopolymerPrediction" &&
    activeModule !== "explorer" &&
    activeModule !== "databaseQuery" &&
    !isTgKetcherOwner;

  return (
    <AppShell
      activeModule={activeModule}
      fullBleed={isFullBleedModule}
      standaloneModules={standaloneModules}
      moduleGroups={moduleGroups}
      onOpenHome={openGeneralWorkspace}
      projects={projectSnapshot?.projects ?? []}
      activeProjectDirectory={
        activeModule === "home" ? projectSnapshot?.activeDirectory ?? null : null
      }
      isProjectBridgeReady={projectSnapshot !== null}
      onOpenProject={openAgentProject}
      onBrowseProjects={browseAgentProjects}
      onNewProject={createAgentProject}
      onSetProjectFavorite={setAgentProjectFavorite}
      onArchiveProject={archiveAgentProject}
      isGeneralWorkspaceActive={
        activeModule === "home" && agentWorkspaceView === "general"
      }
      generalSessions={generalSessionSnapshot?.sessions ?? []}
      activeGeneralSessionID={generalSessionSnapshot?.activeSessionID ?? null}
      isGeneralSessionBridgeReady={generalSessionSnapshot !== null}
      onOpenGeneralWorkspace={openGeneralWorkspace}
      onNewGeneralSession={createGeneralSession}
      onOpenGeneralSession={openGeneralSession}
      onRenameGeneralSession={(sessionID, title) => generalSessionBridge.renameSession(sessionID, title)}
      onDeleteGeneralSession={(sessionID) => generalSessionBridge.deleteSession(sessionID)}
      beforeNavigate={
        activeModule === "structureWorkbench" || activeModule === "homopolymerPrediction"
          ? beforeStructureCanvasNavigation
          : undefined
      }
    >
      <div className={activeModule === "home" ? "h-full" : "hidden"}>
        <AgentWorkspaceHomePage
          iframeRef={agentWorkspaceIframeRef}
          src={agentWorkspaceFrameUrl}
          reloadKey={agentWorkspaceReloadKey}
          onLoad={() => {
            setProjectSnapshot(null);
            projectBridge.requestProjects();
            if (agentWorkspaceView === "general") {
              setGeneralSessionSnapshot(null);
              generalSessionBridge.requestSessions();
            }
          }}
        />
      </div>

      {activeModule === "databaseQuery" ? (
        <DatabaseQueryPage
          structure={structureWorkspace}
          onEditStructure={openStructureWorkbench}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
        />
      ) : null}

      {activeModule === "databaseFilter" ? <DatabaseFilterPage /> : null}

      {shouldKeepStructureWorkbenchMounted ? (
        <div
          className={activeModule === "structureWorkbench" ? "contents" : "hidden"}
          aria-hidden={activeModule !== "structureWorkbench"}
        >
          <StructureWorkbenchPage
            ref={structureCanvasOwnerRef}
            structure={structureWorkspace}
            onOpenModule={openModuleById}
          />
        </div>
      ) : null}

      {activeModule === "homopolymerPrediction" ? (
        <HomopolymerPropertyPredictionPage
          ref={structureCanvasOwnerRef}
          structure={structureWorkspace}
        />
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

      {activeModule === "labData" ? (
        <LabDataPage
          view={labDataView}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
          onChangeView={(view) => openLabData(view)}
        />
      ) : null}

      {activeModule === "experimentWorkflowDemo" ? (
        <ExperimentWorkflowDemoPage onBackHome={() => navigate({ module: "home", datasetKey: null })} />
      ) : null}

      {activeModule === "highThroughputWorkflowDemo" ? (
        <HighThroughputWorkflowDemoPage onBackHome={() => navigate({ module: "home", datasetKey: null })} />
      ) : null}

      {activeModule === "mdSimulationDemo" ? (
        <MdSimulationDemoPage onBackHome={() => navigate({ module: "home", datasetKey: null })} />
      ) : null}

      {activeModule === "monomerMdSimulation" ? (
        <MonomerMdSimulationPage onBackHome={() => navigate({ module: "home", datasetKey: null })} />
      ) : null}

      {activeModule === "monomerDft" ? (
        <MonomerDftPage
          structure={structureWorkspace}
          initialJobId={monomerDftJobId}
          onJobIdChange={openMonomerDft}
          onEditStructure={openStructureWorkbench}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
        />
      ) : null}

      {activeModule === "conditionalGeneration" ? (
        <ConditionalGenerationPage structure={structureWorkspace} />
      ) : null}

      {activeModule === "polytaoGeneration" ? (
        <PolytaoGenerationPage
          structure={structureWorkspace}
          onEditStructure={openStructureWorkbench}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
        />
      ) : null}

      {activeModule === "monomerPolymerization" ? (
        <MonomerPolymerizationPage
          structure={structureWorkspace}
          onEditStructure={openStructureWorkbench}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
        />
      ) : null}

      {activeModule === "reverseDesign" || preserveReverseDesignForKnowledge ? (
        <div
          className={activeModule === "reverseDesign" ? "contents" : "hidden"}
          aria-hidden={activeModule !== "reverseDesign"}
        >
          <ReverseDesignPage
            structure={structureWorkspace}
            onOpenKnowledge={openKnowledge}
            assistant={tgAssistant}
          />
        </div>
      ) : null}

      {activeModule === "explorer" ? (
        <PolymerExplorerDesktopPage
          smiles={smiles}
          setSmiles={setSmiles}
          iframeRef={iframeRef}
          setIsReady={setIsReady}
          getCurrentSmiles={getCurrentSmiles}
          request={request}
          setRequest={setRequest}
          isQueryLoading={isLoading}
          queryError={error}
          queryData={data}
          submitQuery={submit}
        />
      ) : null}
    </AppShell>
  );
}
