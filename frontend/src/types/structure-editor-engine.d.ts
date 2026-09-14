declare module "@structure-editor-engine" {
  import type { ComponentType } from "react";
  import type { StructureWorkspace } from "../structure/workspace";
  export const engine: "react" | "iframe";
  const Editor: ComponentType<{ workspace: StructureWorkspace; title: string }>;
  export default Editor;
}

declare module "@structure-editor-preload" {
  export function preloadStructureEditor(): Promise<void>;
}
