import { loadEnv, mergeConfig } from "vite";
import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { ketcherCompatibility } from "./build/ketcher-compat.ts";
import { structureEngine } from "./build/structure-engine.ts";
import { developmentCompression } from "./build/development-compression.ts";
import { retryableImports } from "./build/retryable-imports.ts";
import { developmentEntryPreload } from "./build/development-entry-preload.ts";
import { ketcherRuntimePlugin } from "./build/ketcher-runtime-plugin.ts";
import { developmentMcpProxy } from "./build/development-mcp-proxy.ts";
import { developmentMonitoringProxy } from "./build/development-monitoring-proxy.ts";

export default defineConfig(({ mode, command, isPreview }) => {
  const env = loadEnv(mode, process.cwd(), "DEV_PROXY_");
  const mcpEnv = loadEnv(mode, process.cwd(), "DEV_MCP_PROXY_");
  const monitoringEnv = loadEnv(mode, process.cwd(), ["DEV_MONITORING_PROXY_", "VITE_SERVER_MONITORING_ENABLED"]);
  const editorEnv = loadEnv(mode, process.cwd(), "VITE_STRUCTURE_EDITOR_ENGINE");
  const editor = structureEngine(process.env.VITE_STRUCTURE_EDITOR_ENGINE ?? editorEnv.VITE_STRUCTURE_EDITOR_ENGINE);
  const proxyTarget =
    process.env.DEV_PROXY_TARGET?.trim() ||
    env.DEV_PROXY_TARGET?.trim() ||
    "http://localhost:8000";
  const proxy = {
    ...developmentMonitoringProxy(
      process.env.VITE_SERVER_MONITORING_ENABLED ?? monitoringEnv.VITE_SERVER_MONITORING_ENABLED,
      process.env.DEV_MONITORING_PROXY_TARGET ?? monitoringEnv.DEV_MONITORING_PROXY_TARGET
    ),
    "/api": {
      target: proxyTarget,
      // Preserve the browser-facing Host for the API's Origin/Referer check.
      // Plain HTTP browsers can omit Sec-Fetch-Site; rewriting Host to the
      // internal Backend address would reject legitimate guest requests.
      changeOrigin: false
    },
    "/health": {
      target: proxyTarget,
      changeOrigin: false
    }
  };
  const mcpProxy = command === "serve" && !isPreview && mode === "development"
    ? developmentMcpProxy(
      process.env.DEV_MCP_PROXY_TARGET ?? mcpEnv.DEV_MCP_PROXY_TARGET,
      process.env.DEV_MCP_PROXY_ALLOWED_HOSTS ?? mcpEnv.DEV_MCP_PROXY_ALLOWED_HOSTS
    )
    : undefined;

  return mergeConfig(ketcherCompatibility(), {
    plugins: [mcpProxy, react(), ketcherRuntimePlugin(editor.engine), editor.metadata, developmentCompression(), developmentEntryPreload(editor.engine), retryableImports()],
    resolve: { alias: {
      "@structure-editor-engine": editor.implementation,
      "@structure-editor-preload": editor.preload
    } },
    build: { manifest: true },
    test: {
      setupFiles: ["./src/auth/testSession.ts"],
      // These suites use node:test and run via test:multiuser-tools in CI.
      exclude: [...configDefaults.exclude, "sdk/*.test.mjs", "scripts/multiuser*.test.mjs"]
    },
    server: {
      port: 5173,
      proxy
    },
    preview: {
      proxy
    }
  });
});
