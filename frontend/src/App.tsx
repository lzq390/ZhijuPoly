import { useEffect, useState } from "react";
import {
  Activity,
  Atom,
  BarChart3,
  BookOpen,
  ClipboardList,
  Database,
  Grid2X2,
  Microscope,
  Search,
  Sparkles
} from "lucide-react";
import { AppShell, type AppShellModuleGroup } from "./components/AppShell";
import { AssistantHomePage } from "./components/AssistantHomePage";
import { ConditionalGenerationPage } from "./components/ConditionalGenerationPage";
import { DatabaseAnalysis, type DatasetKey } from "./components/DatabaseAnalysis";
import { DatabaseQueryPage } from "./components/DatabaseQueryPage";
import { ExperimentWorkflowDemoPage } from "./components/ExperimentWorkflowDemoPage";
import { HighThroughputWorkflowDemoPage } from "./components/HighThroughputWorkflowDemoPage";
import { KnowledgeSearch } from "./components/KnowledgeSearch";
import { LabDataPage, type LabDataView } from "./components/LabDataPage";
import { MdSimulationDemoPage } from "./components/MdSimulationDemoPage";
import { ReverseDesignPage } from "./components/ReverseDesignPage";
import { PolymerExplorerDesktopPage } from "./components/PolymerExplorerDesktopPage";
import { StructureWorkbenchPage } from "./components/StructureWorkbenchPage";
import { useKetcher } from "./hooks/useKetcher";
import { usePredict } from "./hooks/usePredict";
import { useQuery } from "./hooks/useQuery";
import { standardizeSmiles } from "./services/api";
import {
  type AssistantModuleContext,
  type KnowledgeNavigationRequest,
  type PredictableProperty,
  type StructureWorkspaceContext
} from "./types";

type ActiveModule =
  | "home"
  | "structureWorkbench"
  | "explorer"
  | "mdSimulationDemo"
  | "reverseDesign"
  | "conditionalGeneration"
  | "databaseQuery"
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

  if (path === "/explorer") {
    return { module: "explorer", datasetKey: null };
  }

  if (path === "/md-simulation") {
    return { module: "mdSimulationDemo", datasetKey: null };
  }

  if (path === "/reverse-design") {
    return { module: "reverseDesign", datasetKey: null };
  }

  if (path === "/conditional-generation") {
    return { module: "conditionalGeneration", datasetKey: null };
  }

  if (path === "/database-query") {
    return { module: "databaseQuery", datasetKey: null };
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

  if (route.module === "explorer") {
    return "/explorer";
  }

  if (route.module === "mdSimulationDemo") {
    return "/md-simulation";
  }

  if (route.module === "reverseDesign") {
    return "/reverse-design";
  }

  if (route.module === "conditionalGeneration") {
    return "/conditional-generation";
  }

  if (route.module === "databaseQuery") {
    return "/database-query";
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

export default function App() {
  const [activeModule, setActiveModule] = useState<ActiveModule>(() => getInitialRoute().module);
  const [selectedDatasetKey, setSelectedDatasetKey] = useState<DatasetKey | null>(() => getInitialRoute().datasetKey);
  const [labDataView, setLabDataView] = useState<LabDataView>(() => getInitialRoute().labDataView ?? "collect");
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
  const [preserveReverseDesignForKnowledge, setPreserveReverseDesignForKnowledge] = useState(false);
  const [hasMountedStructureWorkbench, setHasMountedStructureWorkbench] = useState(
    () => getInitialRoute().module === "structureWorkbench"
  );
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher();
  const { request, setRequest, isLoading, error, data, submit } = useQuery();
  const predict = usePredict();
  const [selectedProperties, setSelectedProperties] = useState<PredictableProperty[]>([]);

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

  function openStructureWorkbench() {
    navigate({ module: "structureWorkbench", datasetKey: null });
  }

  function openMdSimulationDemo() {
    navigate({ module: "mdSimulationDemo", datasetKey: null });
  }

  function openReverseDesign() {
    navigate({ module: "reverseDesign", datasetKey: null });
  }

  function openConditionalGeneration() {
    navigate({ module: "conditionalGeneration", datasetKey: null });
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

  function openLabData(view: LabDataView = "collect") {
    navigate({ module: "labData", datasetKey: null, labDataView: view });
  }

  function openModuleById(moduleId: string) {
    switch (moduleId) {
      case "structureWorkbench":
        openStructureWorkbench();
        break;
      case "labData":
        openLabData("collect");
        break;
      case "databaseQuery":
        openDatabaseQuery();
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
      case "reverseDesign":
        openReverseDesign();
        break;
      case "conditionalGeneration":
        openConditionalGeneration();
        break;
      case "experimentWorkflowDemo":
        openExperimentWorkflowDemo();
        break;
      case "highThroughputWorkflowDemo":
        openHighThroughputWorkflowDemo();
        break;
    }
  }

  const moduleGroups: AppShellModuleGroup[] = [
    {
      title: "结构",
      items: [
        {
          id: "structureWorkbench",
          label: "结构工作台",
          description: "统一绘制、输入和预览当前共享结构。",
          route: "/structure-workbench",
          icon: <Grid2X2 className="h-4 w-4" />,
          isActive: activeModule === "structureWorkbench",
          onClick: openStructureWorkbench
        }
      ]
    },
    {
      title: "数据与知识",
      items: [
        {
          id: "labData",
          label: "实验数据采集",
          description: "录入实验样品、测试项目和测量结果。",
          route: "/lab-data/collect",
          icon: <ClipboardList className="h-4 w-4" />,
          isActive: activeModule === "labData",
          onClick: () => openLabData("collect")
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
          id: "database",
          label: "数据库分析",
          description: "浏览过程、性能、DFT 与结构数据集。",
          route: "/database",
          icon: <Database className="h-4 w-4" />,
          isActive: activeModule === "database",
          onClick: openDatabase
        },
        {
          id: "knowledge",
          label: "知识检索",
          description: "检索聚合物文献、摘要和合成知识。",
          route: "/knowledge",
          icon: <BookOpen className="h-4 w-4" />,
          isActive: activeModule === "knowledge",
          onClick: () => openKnowledge()
        }
      ]
    },
    {
      title: "性能探索",
      items: [
        {
          id: "explorer",
          label: "聚合物性能探索",
          description: "编辑结构、相似匹配、3D 预览和性能预测。",
          route: "/explorer",
          icon: <Atom className="h-4 w-4" />,
          isActive: activeModule === "explorer",
          onClick: openExplorer
        },
        {
          id: "mdSimulationDemo",
          label: "MD 模拟",
          description: "输入 SMILES 和默认参数，演示分子动力学流程与轨迹结果。",
          route: "/md-simulation",
          icon: <Activity className="h-4 w-4" />,
          isActive: activeModule === "mdSimulationDemo",
          onClick: openMdSimulationDemo
        }
      ]
    },
    {
      title: "聚合物设计",
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
          id: "conditionalGeneration",
          label: "条件聚合物生成",
          description: "基于目标条件生成候选聚合物。",
          route: "/conditional-generation",
          icon: <Microscope className="h-4 w-4" />,
          isActive: activeModule === "conditionalGeneration",
          onClick: openConditionalGeneration
        },
        {
          id: "experimentWorkflowDemo",
          label: "实验工作流演示",
          description: "查看从设计到实验记录的流程样例。",
          route: "/experiment-workflow-demo",
          icon: <BarChart3 className="h-4 w-4" />,
          isActive: activeModule === "experimentWorkflowDemo",
          onClick: openExperimentWorkflowDemo
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
    }
  ];
  const assistantModules: AssistantModuleContext[] = moduleGroups.flatMap((group) =>
    group.items.map((item) => ({
      id: item.id,
      title: item.label,
      route: item.route,
      group: group.title,
      description: item.description
    }))
  );
  const isFullBleedModule =
    activeModule === "explorer" ||
    activeModule === "structureWorkbench" ||
    activeModule === "reverseDesign" ||
    activeModule === "experimentWorkflowDemo" ||
    activeModule === "highThroughputWorkflowDemo" ||
    activeModule === "mdSimulationDemo";
  const shouldKeepStructureWorkbenchMounted = hasMountedStructureWorkbench && activeModule !== "explorer";

  return (
    <AppShell
      activeModule={activeModule}
      fullBleed={isFullBleedModule}
      moduleGroups={moduleGroups}
      onOpenHome={() => navigate({ module: "home", datasetKey: null })}
    >
      <div className={activeModule === "home" ? "h-full" : "hidden"}>
        <AssistantHomePage
          activeModule={activeModule}
          modules={assistantModules}
          moduleGroups={moduleGroups}
          onOpenModule={openModuleById}
        />
      </div>

      {activeModule === "databaseQuery" ? (
        <DatabaseQueryPage
          structure={structureWorkspace}
          onEditStructure={openStructureWorkbench}
          onBackHome={() => navigate({ module: "home", datasetKey: null })}
        />
      ) : null}

      {shouldKeepStructureWorkbenchMounted ? (
        <div
          className={activeModule === "structureWorkbench" ? "contents" : "hidden"}
          aria-hidden={activeModule !== "structureWorkbench"}
        >
          <StructureWorkbenchPage
            structure={structureWorkspace}
            onBackHome={() => navigate({ module: "home", datasetKey: null })}
            onOpenModule={openModuleById}
          />
        </div>
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

      {activeModule === "conditionalGeneration" ? (
        <ConditionalGenerationPage
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
            onEditStructure={openStructureWorkbench}
            onOpenKnowledge={openKnowledge}
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
          predict={predict}
          selectedProperties={selectedProperties}
          setSelectedProperties={setSelectedProperties}
        />
      ) : null}
    </AppShell>
  );
}
