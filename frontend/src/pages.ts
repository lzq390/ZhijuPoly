import type { ComponentType } from "react";
import { createLazyPage } from "./components/createLazyPage";
import { retryModuleImport } from "./retryModuleImport";
import type { ActiveModule } from "./routing";

function page<T, C extends ComponentType<any>>(load: () => Promise<T>, select: (module: T) => C) {
  const retryLoad = retryModuleImport(load);
  return createLazyPage(() => retryLoad().then(module => ({ default: select(module) })));
}

const pages = {
  conditionalGeneration: page(() => import("./components/ConditionalGenerationPage"), m => m.ConditionalGenerationPage),
  database: page(() => import("./components/DatabaseAnalysis"), m => m.DatabaseAnalysis),
  databaseFilter: page(() => import("./components/DatabaseFilterPage"), m => m.DatabaseFilterPage),
  databaseQuery: page(() => import("./components/DatabaseQueryPage"), m => m.DatabaseQueryPage),
  experimentWorkflowDemo: page(() => import("./components/ExperimentWorkflowDemoPage"), m => m.ExperimentWorkflowDemoPage),
  highThroughputWorkflowDemo: page(() => import("./components/HighThroughputWorkflowDemoPage"), m => m.HighThroughputWorkflowDemoPage),
  homopolymerPrediction: page(() => import("./components/HomopolymerPropertyPredictionPage"), m => m.HomopolymerPropertyPredictionPage),
  knowledge: page(() => import("./components/KnowledgeSearch"), m => m.KnowledgeSearch),
  labData: page(() => import("./components/LabDataPage"), m => m.LabDataPage),
  mdSimulationDemo: page(() => import("./components/MdSimulationDemoPage"), m => m.MdSimulationDemoPage),
  monomerMdSimulation: page(() => import("./components/MonomerMdSimulationPage"), m => m.MonomerMdSimulationPage),
  monomerDft: page(() => import("./components/MonomerDftPage"), m => m.MonomerDftPage),
  monomerPolymerization: page(() => import("./components/MonomerPolymerizationPage"), m => m.MonomerPolymerizationPage),
  polytaoGeneration: page(() => import("./components/PolytaoGenerationPage"), m => m.PolytaoGenerationPage),
  reverseDesign: page(() => import("./components/ReverseDesignPage"), m => m.ReverseDesignPage),
  explorer: page(() => import("./components/PolymerSimilarityExplorerPage"), m => m.PolymerSimilarityExplorerPage),
  structureWorkbench: page(() => import("./components/StructureWorkbenchPage"), m => m.StructureWorkbenchPage)
} satisfies Record<Exclude<ActiveModule, "home">, ReturnType<typeof createLazyPage>>;

export const ConditionalGenerationPage = pages.conditionalGeneration.Page;
export const DatabaseAnalysis = pages.database.Page;
export const DatabaseFilterPage = pages.databaseFilter.Page;
export const DatabaseQueryPage = pages.databaseQuery.Page;
export const ExperimentWorkflowDemoPage = pages.experimentWorkflowDemo.Page;
export const HighThroughputWorkflowDemoPage = pages.highThroughputWorkflowDemo.Page;
export const HomopolymerPropertyPredictionPage = pages.homopolymerPrediction.Page;
export const KnowledgeSearch = pages.knowledge.Page;
export const LabDataPage = pages.labData.Page;
export const MdSimulationDemoPage = pages.mdSimulationDemo.Page;
export const MonomerMdSimulationPage = pages.monomerMdSimulation.Page;
export const MonomerDftPage = pages.monomerDft.Page;
export const MonomerPolymerizationPage = pages.monomerPolymerization.Page;
export const PolytaoGenerationPage = pages.polytaoGeneration.Page;
export const ReverseDesignPage = pages.reverseDesign.Page;
export const PolymerSimilarityExplorerPage = pages.explorer.Page;
export const StructureWorkbenchPage = pages.structureWorkbench.Page;

export function preloadPage(module: ActiveModule): Promise<void> {
  return module === "home" ? Promise.resolve() : pages[module].preload();
}
