/** Static counterparts of the offline functional GIFs. Only the selected card
 * loads an animation; the directory itself never starts a calculation. */
const workflowModules = new Set([
  "knowledge", "polytaoGeneration", "monomerRetrosynthesis", "explorer",
  "databaseQuery", "databaseFilter", "database", "homopolymerPrediction",
  "monomerPolymerization", "mdSimulationDemo", "monomerMdSimulation",
  "monomerDft", "conditionalGeneration", "reverseDesign", "highThroughputWorkflowDemo"
]);

export function ResearchToolThumbnail({ moduleId }: { moduleId: string }) {
  return <svg className="np-research-tool__science" viewBox="0 0 360 180" aria-hidden="true" focusable="false">
    {workflowModules.has(moduleId)
      ? <image href={`/previews/research-tools/${moduleId}.svg`} width="360" height="180" />
      : <g fill="none" stroke="var(--np-theme-border)"><rect x="122" y="48" width="116" height="84" rx="12" /><path d="M144 76h72M144 90h53M144 104h64" strokeLinecap="round" strokeWidth="3" /></g>}
  </svg>;
}
