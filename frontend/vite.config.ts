import { loadEnv, mergeConfig } from "vite";
import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { ketcherCompatibility } from "./build/ketcher-compat.ts";
import { structureEngine } from "./build/structure-engine.ts";
import { developmentCompression } from "./build/development-compression.ts";
import { retryableImports } from "./build/retryable-imports.ts";
import { developmentEntryPreload } from "./build/development-entry-preload.ts";
import { ketcherRuntimePlugin } from "./build/ketcher-runtime-plugin.ts";

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

  return mergeConfig(ketcherCompatibility(), {
    plugins: [react(), ketcherRuntimePlugin(editor.engine), editor.metadata, developmentCompression(), developmentEntryPreload(editor.engine), retryableImports()],
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
