import compression from "compression";
import type { Plugin } from "vite";

/** Wrap Vite's own handlers, including their ETags and outdated-dependency guard.
 * Source/HMR/API responses retain their existing delivery path.
 */
export function developmentCompression(): Plugin {
  return {
    name: "development-dependency-compression",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(compression({
        threshold: 32 * 1024,
        level: 4,
        filter(request, response) {
          if (request.method !== "GET" && request.method !== "HEAD") return false;
          const pathname = (request.url || "").split("?", 1)[0];
          const dependency = /^\/node_modules\/\.vite\/deps\/[^/]+\.js$/.test(pathname) ||
            pathname === "/node_modules/ketcher-react/dist/index.css";
          if (!dependency || ![200, 304].includes(response.statusCode)) return false;
          // A 304 updates stored response headers too: retain the same variant
          // key even when Vite returns it without a Content-Type/body.
          if (response.statusCode === 304 || request.headers.range) {
            const vary = String(response.getHeader("Vary") || "").toLowerCase().split(/\s*,\s*/);
            if (!vary.includes("accept-encoding") && !vary.includes("*")) response.appendHeader("Vary", "Accept-Encoding");
            return false;
          }
          return compression.filter(request, response);
        }
      }));
    }
  };
}
