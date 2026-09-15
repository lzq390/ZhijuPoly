import { resolve } from "node:path";
import { readFileSync } from "node:fs";
import { normalizePath, type Plugin } from "vite";
import { canvasRoutePaths } from "../shared/canvas-routes";

/** Discover the bootstrap roots from HTML instead of another import round trip.
 * These modules do not eagerly import business pages or initialize the SDK.
 */
export function developmentEntryPreload(engine: string): Plugin {
  return {
    name: "development-entry-preload",
    apply: "serve",
    transformIndexHtml: {
      // Let Vite process base paths. Its HTML link processing does not add the
      // HMR timestamp, so match the URL used by the import-analysis plugin here.
      order: "pre",
      handler(_html, context) {
        const server = context.server;
        if (context.path !== "/index.html" || !server) return;
        const roots = ["../shared/canvas-routes.ts", "routing.ts", "pages.ts", "mountApp.tsx", "App.tsx", "components/AppShell.tsx"];
        const pathname = new URL(context.originalUrl || context.path, "http://localhost").pathname.replace(/\/+$/, "") || "/";
        const module = Object.entries(canvasRoutePaths).find(([, route]) => route === pathname)?.[0];
        if (module) {
          // Keep the business loader the source of truth. Preload just this page
          // root, not its entire graph or any unrelated routes.
          const pages = readFileSync(resolve(server.config.root, "src/pages.ts"), "utf8");
          const entry = pages.match(new RegExp(`\\b${module}: page\\(\\(\\) => import\\("([^"]+)"\\)`))?.[1];
          if (!entry) throw new Error(`Missing canvas page preload root: ${module}`);
          roots.push(entry.replace(/^\.\//, "") + ".tsx");
          if (engine === "react") roots.push("components/structure-workbench/ReactStructureEditor.tsx");
        }
        return roots.map(file => {
          const id = normalizePath(resolve(server.config.root, "src", file));
          const timestamp = server.environments.client.moduleGraph.getModuleById(id)?.lastHMRTimestamp;
          return {
            tag: "link",
            attrs: { rel: "modulepreload", href: `/src/${file}${timestamp ? `?t=${timestamp}` : ""}` },
            injectTo: "head" as const
          };
        });
      }
    }
  };
}
