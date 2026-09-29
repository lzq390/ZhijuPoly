import React from "react";
import ReactDOM from "react-dom/client";
import { AuthProvider } from "./auth/AuthProvider";
const App = React.lazy(() => import("./App"));
import { runStorageMigrations } from "./storageMigrations";

export function mountApp() {
  runStorageMigrations();
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode><AuthProvider><React.Suspense fallback={<p role="status">正在加载工作空间…</p>}><App /></React.Suspense></AuthProvider></React.StrictMode>
  );
}
