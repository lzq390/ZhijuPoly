import { useEffect, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
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
import { KetcherEditor } from "./components/KetcherEditor";
import { KnowledgeSearch } from "./components/KnowledgeSearch";
import { LabDataPage, type LabDataView } from "./components/LabDataPage";
import { MdSimulationDemoPage } from "./components/MdSimulationDemoPage";
import { QueryPanel } from "./components/QueryPanel";
import { ResultsDisplay } from "./components/ResultsDisplay";
import { ReverseDesignPage } from "./components/ReverseDesignPage";
import { StructurePreview3D } from "./components/StructurePreview3D";
import { StructureWorkbenchPage } from "./components/StructureWorkbenchPage";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { useKetcher } from "./hooks/useKetcher";
import { usePredict } from "./hooks/usePredict";
import { useQuery } from "./hooks/useQuery";
import { standardizeSmiles } from "./services/api";
import {
  type AssistantModuleContext,
  type KnowledgeNavigationRequest,
  type PredictableProperty,
  type ResultsTab,
  type StructureWorkspaceContext,
  type WorkspaceMode
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
      ? "性能预测"
      : request.match_mode === "property"
        ? "性能相似"
        : "结构相似";
  const activeModeLabel =
    panelMode === "predict"
      ? "性能预测"
      : request.match_mode === "property"
        ? "性能相似"
        : "结构相似";

  const resultCount =
    activeResultsTab === "predict" ? Object.keys(predict.data?.predictions ?? {}).length : data?.total ?? 0;
  const resultTiming =
    activeResultsTab === "predict" ? null : data?.query_time_ms;
  const latestResultDescription =
    activeResultsTab === "predict"
      ? predict.data
        ? "已返回预测值。"
        : "执行后显示预测数量。"
      : resultTiming
        ? `${resultTiming.toFixed(1)} ms 完成`
        : "执行后显示结果数量和耗时。";

  async function handleQuerySubmit() {
    setActiveResultsTab("query");
    const currentSmiles = await getCurrentSmiles();
    await submit({ ...request, smiles: currentSmiles });
  }

  async function handlePredictSubmit() {
    setActiveResultsTab("predict");
    try {
      const currentSmiles = await getCurrentSmiles();
      await predict.submit({
        smiles: currentSmiles,
        properties: selectedProperties
      });
    } catch {
      // Error state is already captured by the hook and shown in the results panel.
    }
  }

  const resultPanelTitle = activeResultsTab === "predict" ? "预测结果" : "相似匹配结果";
  const resultPanelDescription =
    activeResultsTab === "predict"
      ? "预测完成后，这里会展示所选性能的预测值。"
      : "相似匹配完成后，这里会展示摘要、2D 结构、SMILES 和相似度分数。";
  const resultPrimaryBadge =
    activeResultsTab === "predict"
      ? predict.data
        ? `${Object.keys(predict.data.predictions).length} 项预测`
        : "暂无预测"
      : data
        ? `${data.total} 条记录`
        : "暂无结果";
  const resultSecondaryBadge =
    activeResultsTab === "predict"
      ? predict.isLoading
        ? "预测中"
        : "预测模式"
      : request.match_mode === "property"
        ? "性能相似"
        : "结构相似";

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
        <div className={activeModule === "explorer" ? "contents" : "hidden"} aria-hidden={activeModule !== "explorer"}>
          <nav className="flex flex-col gap-3 rounded-[26px] border border-white/70 bg-white/80 px-4 py-4 shadow-sm backdrop-blur md:flex-row md:items-center md:justify-between md:px-5">
            <div className="flex items-center gap-3">
              <Button type="button" variant="outline" onClick={() => navigate({ module: "home", datasetKey: null })}>
                <ArrowLeft className="mr-2 h-4 w-4" />
                首页
              </Button>
              <div>
                <div className="text-[11px] font-medium uppercase tracking-[0.2em] text-teal-700/70">当前模块</div>
                <div className="font-heading text-lg font-semibold tracking-tight text-slate-950">
                  聚合物性能探索
                </div>
              </div>
            </div>
            <Badge className="bg-teal-50 text-teal-800">探索页</Badge>
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
                <Badge>聚合物相似匹配与性能预测</Badge>
              </div>

              <div className="mt-6 overflow-x-auto">
                <h1 className="font-heading whitespace-nowrap text-[2.5rem] font-semibold tracking-[-0.04em] text-slate-950 md:text-[4rem] md:leading-[0.95]">
                  聚合物性能探索
                </h1>
                <p className="mt-4 whitespace-nowrap text-base leading-7 text-slate-600 md:text-lg">
                  将结构编辑、相似匹配、3D 预览和性能预测整合到一个研究工作台。
                </p>
              </div>

              <div className="mt-8 grid gap-3 md:grid-cols-3">
                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                    {panelMode === "predict" ? <Sparkles className="h-4 w-4 text-teal-600" /> : <Atom className="h-4 w-4 text-teal-600" />}
                    当前模式
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                    {activeModeLabel}
                  </div>
                  <div className="mt-2 text-sm leading-6 text-mutedForeground">
                    {panelMode === "predict"
                      ? "在控制卡片中选择目标性能，并对当前结构运行预测。"
                      : "在控制卡片中切换结构相似或性能相似匹配。"}
                  </div>
                </div>

                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-white/80 p-5 text-center shadow-sm backdrop-blur">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-mutedForeground">
                    <Microscope className="h-4 w-4 text-sky-600" />
                    结构输入
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight text-slate-950">
                    {smiles.trim().length > 0 ? "已就绪" : "等待输入"}
                  </div>
                  <div className="mt-2 text-sm leading-6 text-mutedForeground">
                    结构编辑器会同步更新 SMILES，用于匹配或预测。
                  </div>
                </div>

                <div className="flex min-h-[188px] flex-col justify-center rounded-[26px] border border-white/80 bg-slate-950 p-5 text-center text-slate-50 shadow-[0_22px_50px_rgba(8,17,31,0.2)]">
                  <div className="flex items-center justify-center gap-2 text-[11px] font-medium uppercase tracking-[0.2em] text-slate-400">
                    <Database className="h-4 w-4 text-teal-300" />
                    最新结果
                  </div>
                  <div className="font-heading mt-3 text-[1.45rem] font-semibold tracking-tight">{resultCount}</div>
                  <div className="mt-2 text-sm leading-6 text-slate-300">
                    {latestResultDescription}
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
                      <div className="text-xs font-medium uppercase tracking-[0.18em] text-teal-700/70">结果</div>
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
    </AppShell>
  );
}
