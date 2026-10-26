import type { StructureWorkspaceContext } from "../types";
import { ModulePageHeader } from "./ModulePageHeader";
import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { PolymerMdFormalPage } from "./polymer-md/PolymerMdFormalPage";
import "../styles/structure-workbench.css";
import "../styles/md-simulation.css";
import "../styles/polymer-md.css";
import "../styles/polymer-md-workbench.css";
import "../styles/polymer-md-typography.css";
import "../styles/polymer-md-controls.css";
import "../styles/polymer-md-palette.css";
export function PolymerMdSimulationPage({
  structure,
  onEditStructure
}: {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
}) {
  return <div className="np-polymer-md-page np-md-simulation np-structure-workbench np-module-page" data-module="polymer-md-simulation">
    <ModulePageHeader actions={<BrowsingRecordingControls module="mdSimulationDemo" />}>MD 模拟</ModulePageHeader>
    <PolymerMdFormalPage structure={structure} onEditStructure={onEditStructure} />
  </div>;
}
