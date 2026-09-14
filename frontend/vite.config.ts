import { defineConfig, loadEnv, mergeConfig } from "vite";
import react from "@vitejs/plugin-react";
import { ketcherCompatibility } from "./build/ketcher-compat.ts";
import { structureEngine } from "./build/structure-engine.ts";
import { developmentCompression } from "./build/development-compression.ts";
import { retryableImports } from "./build/retryable-imports.ts";
import { developmentEntryPreload } from "./build/development-entry-preload.ts";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "DEV_PROXY_");
  const editorEnv = loadEnv(mode, process.cwd(), "VITE_STRUCTURE_EDITOR_ENGINE");
  const editor = structureEngine(process.env.VITE_STRUCTURE_EDITOR_ENGINE ?? editorEnv.VITE_STRUCTURE_EDITOR_ENGINE);
  const proxyTarget =
    process.env.DEV_PROXY_TARGET?.trim() ||
    env.DEV_PROXY_TARGET?.trim() ||
    "http://localhost:8000";
  const proxy = {
    "/api": {
      target: proxyTarget,
      changeOrigin: true
    },
    "/health": {
      target: proxyTarget,
      changeOrigin: true
    }
  };

  return mergeConfig(ketcherCompatibility(), {
    plugins: [react(), editor.metadata, developmentCompression(), developmentEntryPreload(), retryableImports()],
    resolve: { alias: {
      "@structure-editor-engine": editor.implementation,
      "@structure-editor-preload": editor.preload
    } },
    build: { manifest: true },
    server: {
      port: 5173,
      proxy
    },
    preview: {
      proxy
    }
  });
});
