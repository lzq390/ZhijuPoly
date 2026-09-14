import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { KnowledgeRecordingProvider } from "./hooks/useKnowledgeRecording";
import { KnowledgeRecordingControls } from "./components/knowledge-search/KnowledgeRecordingControls";
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
import type { DatasetKey } from "./components/database-analysis/types";
import type { LabDataView } from "./components/LabDataPage";
import type { StructureCanvasOwnerHandle } from "./components/StructureWorkbenchPage";
import {
  ConditionalGenerationPage, DatabaseAnalysis, DatabaseFilterPage, DatabaseQueryPage,
  ExperimentWorkflowDemoPage, HighThroughputWorkflowDemoPage, HomopolymerPropertyPredictionPage,
  KnowledgeSearch, LabDataPage, MdSimulationDemoPage, MonomerMdSimulationPage, MonomerDftPage,
  MonomerPolymerizationPage, PolytaoGenerationPage, ReverseDesignPage,
  PolymerSimilarityExplorerPage, StructureWorkbenchPage, preloadPage
} from "./pages";
import {
  canvasModules, getInitialRoute, normalizePath, routeFromPath, pathFromRoute,
  POLYTAO_ROUTE, HOMOPOLYMER_PREDICTION_ROUTE, LEGACY_POLYTAO_ROUTE,
  DATABASE_FILTER_ROUTE, LEGACY_DATABASE_FILTER_ROUTE, type ActiveModule, type AppRoute
} from "./routing";
import { useStructureWorkspace } from "./hooks/useStructureWorkspace";
import { StructureWorkspaceNotice } from "./components/structure-workbench/StructureWorkspaceNotice";
import { syncStructureForNavigation } from "./structure/navigation";
import { useQuery } from "./hooks/useQuery";
import { useTgAssistant } from "./hooks/useTgAssistant";
import { useModuleTransition, type ModuleNavigationRequest } from "./hooks/useModuleTransition";
import { getMonomerDftJobIdFromSearch, getMonomerDftPath } from "./lib/monomerDftRouting";
import { getMonomerMdJobIdFromSearch, getMonomerMdPath } from "./lib/monomerMdRouting";
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
import type { KnowledgeNavigationRequest } from "./types";

type AppNavigationRequest = ModuleNavigationRequest & {
  route: AppRoute;
  history: "push" | "none";
  knowledge?: { query: string; terms: string[] };
  jobId?: string | null;
  onCommit?: () => void;
};

type KnowledgeNavigationInput = string | KnowledgeNavigationRequest;
type AgentWorkspaceView = "general" | "projects" | "project";
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
  return <KnowledgeRecordingProvider><AppContent /></KnowledgeRecordingProvider>;
}

function AppContent() {
  const [knowledgeLocalMode, setKnowledgeLocalMode] = useState(true);
  const [activeModule, setActiveModule] = useState<ActiveModule>(() => getInitialRoute().module);
  const [selectedDatasetKey, setSelectedDatasetKey] = useState<DatasetKey | null>(() => getInitialRoute().datasetKey);
  const [labDataView, setLabDataView] = useState<LabDataView>(() => getInitialRoute().labDataView ?? "collect");
  const [monomerDftJobId, setMonomerDftJobId] = useState<string | null>(() => {
    if (typeof window === "undefined" || getInitialRoute().module !== "monomerDft") {
      return null;
    }
    return getMonomerDftJobIdFromSearch(window.location.search);
  });
  const [monomerMdJobId, setMonomerMdJobId] = useState<string | null>(() => {
    if (typeof window === "undefined" || getInitialRoute().module !== "monomerMdSimulation") {
      return null;
    }
    return getMonomerMdJobIdFromSearch(window.location.search);
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
  const moduleContentRef = useRef<HTMLDivElement | null>(null);
  const moduleMainRef = useRef<HTMLElement | null>(null);
  const structureWorkspace = useStructureWorkspace();
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

  const syncStructureBeforeNavigation = useCallback((signal: AbortSignal) => {
    const workspace = structureWorkspace.workspace;
    return syncStructureForNavigation(workspace,
      () => structureCanvasOwnerRef.current?.syncBeforeLeave(signal) ?? workspace.saveForNavigation(), signal);
  }, [structureWorkspace.workspace]);

  const moduleTransition = useModuleTransition<AppNavigationRequest>({
    activeModule, contentRef: moduleContentRef, mainRef: moduleMainRef,
    guard: () => canvasModules.has(activeModuleRef.current) ? syncStructureBeforeNavigation : undefined,
    commit: (navigation, replaceUnseen) => {
      const { route, href } = navigation;
      const from = activeModuleRef.current;
      let ownsEntry = false;
      if (navigation.history === "push") {
        if (`${normalizePath(window.location.pathname)}${window.location.search}` !== href) {
          if (replaceUnseen) window.history.replaceState(route, "", href);
          else window.history.pushState(route, "", href);
          ownsEntry = true;
        }
      } else if ([LEGACY_POLYTAO_ROUTE, LEGACY_DATABASE_FILTER_ROUTE].includes(normalizePath(window.location.pathname))) {
        window.history.replaceState(route, "", href);
      }
      if (navigation.knowledge) {
        setKnowledgeInitialQuery(navigation.knowledge.query);
        setKnowledgeInitialTerms(navigation.knowledge.terms);
        // Preserve the original keep-alive policy: the explicit knowledge
        // entry from reverse design retains it; popstate only updates query
        // props and must not introduce an extra retained workspace.
        if (navigation.source !== "history") setPreserveReverseDesignForKnowledge(from === "reverseDesign");
      }
      if (route.module === "monomerMdSimulation") setMonomerMdJobId(navigation.jobId ?? null);
      if (route.module === "monomerDft") setMonomerDftJobId(navigation.jobId ?? null);
      applyRoute(route);
      activeModuleRef.current = route.module;
      if (navigation.source !== "state") window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      navigation.onCommit?.();
      return ownsEntry;
    }
  });

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

  function navigate(route: AppRoute, extra: Partial<Omit<AppNavigationRequest, "route" | "target">> = {}) {
    // Fetch the target while the existing navigation transaction saves/exits.
    // A failed prefetch is presented by that page's own retry boundary.
    void preloadPage(route.module).catch(() => {});
    const item = [...standaloneModules, ...moduleGroups.flatMap((group) => group.items)].find((candidate) => candidate.id === route.module);
    moduleTransition.request({
      route, href: pathFromRoute(route), history: "push", source: "navigation", kind: "module",
      ...extra, target: { id: route.module, label: item?.label ?? (route.module === "home" ? "通用会话" : "实验数据") }
    });
  }

  useEffect(() => {
    if (activeModule === "structureWorkbench") {
      setHasMountedStructureWorkbench(true);
    }
  }, [activeModule]);

  useEffect(() => {
    function handlePopState() {
      const route = routeFromPath(window.location.pathname);
      const pathname = window.location.pathname;
      const search = window.location.search;
      const canonical = normalizePath(pathname) === LEGACY_POLYTAO_ROUTE ? POLYTAO_ROUTE
        : normalizePath(pathname) === LEGACY_DATABASE_FILTER_ROUTE ? DATABASE_FILTER_ROUTE : pathname;
      navigate(route, {
        href: `${canonical}${search}`, history: "none", source: "history",
        knowledge: route.module === "knowledge" ? { query: getKnowledgeQueryFromSearch(search), terms: getKnowledgeTermsFromSearch(search) } : undefined,
        jobId: route.module === "monomerDft" ? getMonomerDftJobIdFromSearch(search) : getMonomerMdJobIdFromSearch(search)
      });
    }

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, [moduleTransition.request]);

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

  function openMonomerMdSimulation(jobId: string | null = null, source: "navigation" | "state" = "navigation") {
    const route = { module: "monomerMdSimulation", datasetKey: null } satisfies AppRoute;
    navigate(route, { href: getMonomerMdPath(jobId), jobId, source });
  }

  function openMonomerDft(jobId: string | null = null, source: "navigation" | "state" = "navigation") {
    const route = { module: "monomerDft", datasetKey: null } satisfies AppRoute;
    navigate(route, { href: getMonomerDftPath(jobId), jobId, source });
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

    navigate(route, { href: path, knowledge: { query: trimmedQuery, terms } });
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
        navigate({ module: "monomerPolymerization", datasetKey: null }, { href: "/monomer-polymerization?mode=single" });
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
    navigate({ module: "home", datasetKey: null }, { kind: "command", onCommit: () => {
      setAgentWorkspaceView("project");
      projectBridge.openProject(directory);
    } });
  }

  function browseAgentProjects() {
    navigate({ module: "home", datasetKey: null }, { kind: "command", onCommit: () => {
      setAgentWorkspaceView("projects");
      projectBridge.browseProjects();
    } });
  }

  function createAgentProject() {
    navigate({ module: "home", datasetKey: null }, { kind: "command", onCommit: () => projectBridge.newProject() });
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
    navigate({ module: "home", datasetKey: null }, { kind: "command", onCommit: () => {
      setAgentWorkspaceView("general");
      setGeneralSessionSnapshot(null);
      setAgentWorkspaceFrameUrl(agentWorkspaceRouteUrl("/"));
      setAgentWorkspaceReloadKey((current) => current + 1);
    } });
  }

  function createGeneralSession() {
    navigate({ module: "home", datasetKey: null }, { kind: "command", onCommit: () => {
      setAgentWorkspaceView("general");
      generalSessionBridge.newSession();
    } });
  }

  function openGeneralSession(sessionID: string) {
    navigate({ module: "home", datasetKey: null }, { kind: "command", onCommit: () => {
      setAgentWorkspaceView("general");
      generalSessionBridge.openSession(sessionID);
    } });
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
          label: "聚合物相似性探索",
          description: "在共享结构画板中运行结构相似或性能相似检索。",
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
          description: "上传单体表批量生成聚合物候选，也可逐对聚合。",
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
          description: "配置单体或多组分正式协议，跟踪真实 MD Worker 任务与结果。",
          route: "/monomer-md-simulation",
          icon: <Microscope className="h-4 w-4" />,
          isActive: activeModule === "monomerMdSimulation",
          onClick: openMonomerMdSimulation
        },
        {
          id: "monomerDft",
          label: "单体 DFT",
          description: "计算单点性质、二阶力常数、振动频率并优化分子构型。",
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
    activeModule !== "monomerDft" &&
    !isTgKetcherOwner;

  return (
    <AppShell
      recordingControls={<KnowledgeRecordingControls global
        localMode={activeModule === "databaseFilter" || (activeModule === "knowledge" && knowledgeLocalMode)} />}
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
      moduleTransition={moduleTransition}
    >
      <StructureWorkspaceNotice workspace={structureWorkspace.workspace} />
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
          ref={structureCanvasOwnerRef}
          structure={structureWorkspace}
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
          onLocalModeChange={setKnowledgeLocalMode}
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
        <MdSimulationDemoPage
          structure={structureWorkspace}
          onEditStructure={openStructureWorkbench}
        />
      ) : null}

      {activeModule === "monomerMdSimulation" ? (
        <MonomerMdSimulationPage
          structure={structureWorkspace}
          initialJobId={monomerMdJobId}
          onJobIdChange={(jobId) => openMonomerMdSimulation(jobId, "state")}
          onEditStructure={openStructureWorkbench}
        />
      ) : null}

      {activeModule === "monomerDft" ? (
        <MonomerDftPage
          structure={structureWorkspace}
          initialJobId={monomerDftJobId}
          onJobIdChange={(jobId) => openMonomerDft(jobId, "state")}
          onEditStructure={openStructureWorkbench}
        />
      ) : null}

      {activeModule === "conditionalGeneration" ? (
        <ConditionalGenerationPage
          ref={structureCanvasOwnerRef}
          structure={structureWorkspace}
        />
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
        />
      ) : null}

      {activeModule === "reverseDesign" || preserveReverseDesignForKnowledge ? (
        <div
          className={activeModule === "reverseDesign" ? "contents" : "hidden"}
          aria-hidden={activeModule !== "reverseDesign"}
        >
          <ReverseDesignPage
            ref={structureCanvasOwnerRef}
            structure={structureWorkspace}
            onOpenKnowledge={openKnowledge}
            assistant={tgAssistant}
          />
        </div>
      ) : null}

      {activeModule === "explorer" ? (
        <PolymerSimilarityExplorerPage
          ref={structureCanvasOwnerRef}
          structure={structureWorkspace}
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
