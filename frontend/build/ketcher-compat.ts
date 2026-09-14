import inject from '@rollup/plugin-inject';
import { fileURLToPath } from 'node:url';
import type { UserConfig } from 'vite';

const processModule = fileURLToPath(new URL('./browser-process.mjs', import.meta.url));
export function ketcherCompatibility(): UserConfig {
  return {
    define: { global: 'globalThis' },
    optimizeDeps: { esbuildOptions: {
      inject: [processModule],
      // Vite's dependency scanner otherwise externalizes absolute injected paths.
      plugins: [{ name: 'ketcher-process-injection', setup(build) {
        build.onResolve({ filter: /browser-process\.mjs$/ }, () => ({ path: processModule }));
      } }],
    } },
    plugins: [{ ...inject({
      include: [/node_modules\/(?:ketcher-[^/]+|assert|util)\//],
      process: [processModule, 'process'],
    }), apply: 'build', enforce: 'post' }],
    build: { commonjsOptions: { transformMixedEsModules: true } },
  };
}
