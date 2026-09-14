import { canvasModules, routeFromPath } from "./routing";
import "./index.css";

// Start transport before evaluating the application's business dependency graph.
// The visible page remains the sole owner of initialization and error recovery.
const initialModule = routeFromPath(window.location.pathname).module;
if (canvasModules.has(initialModule)) {
  void import("@structure-editor-preload").then(({ preloadStructureEditor }) => preloadStructureEditor()).catch(() => {});
}
// Discover the current page's dependency graph while the shell is still loading.
void import("./pages").then(({ preloadPage }) => preloadPage(initialModule)).catch(() => {});

void import("./mountApp").then(({ mountApp }) => mountApp()).catch(() => {
  const root = document.getElementById("root")!;
  root.setAttribute("role", "alert");
  root.textContent = "应用加载失败，请刷新页面重试。";
});
