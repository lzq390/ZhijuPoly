import { defineConfig } from "vitest/config";

// Status authorization tests do not build or load the chemistry editor.
// The normal production build still uses vite.config.ts unchanged.
export default defineConfig({
  test: { environment: "jsdom", pool: "forks", maxWorkers: 1 }
});
