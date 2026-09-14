import { resolve } from "node:path";
import { normalizePath, type Plugin } from "vite";

/** Discover the bootstrap roots from HTML instead of another import round trip.
 * These modules do not eagerly import business pages or initialize the SDK.
 */
export function developmentEntryPreload(): Plugin {
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
        return ["routing.ts", "pages.ts", "mountApp.tsx"].map(file => {
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
