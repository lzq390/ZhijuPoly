import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "DEV_PROXY_");
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

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy
    },
    preview: {
      proxy
    }
  };
});
