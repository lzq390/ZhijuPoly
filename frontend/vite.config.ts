import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/online-retrieval": {
        target: "http://localhost:5002",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/online-retrieval/, "") || "/"
      }
    }
  }
});
