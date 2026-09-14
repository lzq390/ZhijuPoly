import type { DatasetKey } from "./components/database-analysis/types";
import type { LabDataView } from "./components/LabDataPage";

export type ActiveModule =
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

export type AppRoute = {
  module: ActiveModule;
  datasetKey: DatasetKey | null;
  labDataView?: LabDataView;
};

export const canvasModules = new Set<ActiveModule>([
  "structureWorkbench", "homopolymerPrediction", "explorer", "databaseQuery", "conditionalGeneration", "reverseDesign"
]);

export const POLYTAO_ROUTE = "/polytao-generation";
export const HOMOPOLYMER_PREDICTION_ROUTE = "/homopolymer-property-prediction";
export const LEGACY_POLYTAO_ROUTE = "/conditional-generation/polytao";
export const DATABASE_FILTER_ROUTE = "/database-filter";
export const LEGACY_DATABASE_FILTER_ROUTE = "/database/property-filter";

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

export function normalizePath(pathname: string) {
  const normalized = pathname.replace(/\/+$/, "");
  return normalized.length > 0 ? normalized : "/";
}

export function routeFromPath(pathname: string): AppRoute {
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

export function pathFromRoute(route: AppRoute) {
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

export function getInitialRoute() {
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
