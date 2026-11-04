import { canvasModules, routeFromPath } from "./routing";
import { isSimulationPaymentPath } from "../shared/billing-routes";

function showLoadError() {
  const root = document.getElementById("root")!;
  root.setAttribute("role", "alert");
  root.textContent = "应用加载失败，请刷新页面重试。";
}

if (isSimulationPaymentPath(window.location.pathname)) {
  const referrer = document.createElement("meta");
  referrer.name = "referrer"; referrer.content = "no-referrer";
  document.head.append(referrer);
  document.title = "Nexpoly · 开发模拟付款";
  // This public entry has no account session, workspace storage or external fonts.
  void import("./mountSimulationPayment").then(({ mountSimulationPayment }) => mountSimulationPayment()).catch(showLoadError);
} else {
  // Start transport before evaluating the application's business dependency graph.
  // The visible page remains the sole owner of initialization and error recovery.
  const initialModule = routeFromPath(window.location.pathname).module;
  if (canvasModules.has(initialModule)) {
    void import("@structure-editor-preload").then(({ preloadStructureEditor }) => preloadStructureEditor()).catch(() => {});
  }
  // Discover the current page's dependency graph while the shell is still loading.
  void import("./pages").then(({ preloadPage }) => preloadPage(initialModule)).catch(() => {});

  // Keep font/stylesheet transport failures from preventing account access.
  void import("./index.css").catch(() => {});
  void import("./mountApp").then(({ mountApp }) => mountApp()).catch(showLoadError);
}
