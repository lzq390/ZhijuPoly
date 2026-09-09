import { createContext, useContext } from "react";

/** Module-owned portals must disappear with the workspace, not with the sidebar. */
export const ModuleTransitionContext = createContext(false);
export const useModuleTransitioning = () => useContext(ModuleTransitionContext);
