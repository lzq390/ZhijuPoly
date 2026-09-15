import { readFile } from 'node:fs/promises';
import path from 'node:path';
import type { Plugin } from 'vite';
import { ensureKetcherRuntime } from './ketcher-runtime.mjs';
import { canvasRoutePaths } from '../shared/canvas-routes';
import { runtimeMiddleware } from './ketcher-runtime-http.mjs';

const virtual = 'virtual:ketcher-runtime';
const paths = Object.values(canvasRoutePaths);
export function ketcherRuntimePlugin(engine: string): Plugin {
  let runtime: Awaited<ReturnType<typeof ensureKetcherRuntime>>;
  let base = '/';
  let building: Promise<void> | undefined;
  let dirty = false;
  let closed = false;
  let debounce: ReturnType<typeof setTimeout> | undefined;
  const prefix = () => `${base}assets/ketcher/${runtime.manifest.version}/`;
  const update = async () => { runtime = await ensureKetcherRuntime(); };
  return {
    name: 'nexpoly-ketcher-runtime', enforce: 'pre',
    async configResolved(config) { base = config.base; if (engine === 'react') await update(); },
    resolveId(id) { if (id === virtual) return '\0' + virtual; },
    load(id) {
      if (id !== '\0' + virtual) return;
      return `export const bootstrapUrl=${JSON.stringify(engine === 'react' ? prefix() + runtime.manifest.bootstrap : '')};`;
    },
    transformIndexHtml: { order: 'post', handler() {
      if (engine !== 'react') return;
      const assets = new Set<string>();
      const visit = (file: string) => {
        if (assets.has(file)) return;
        assets.add(file);
        for (const child of runtime.manifest.graph.find(item => item.file === file)?.imports || []) visit(child);
      };
      visit(runtime.manifest.micro);
      const dependencies = [...assets].map(file => prefix() + file);
      return [{ tag: 'script', attrs: {}, injectTo: 'head-prepend', children:
        `{const load=url=>import(url);globalThis[Symbol.for('nexpoly.ketcher.import')]=load;if(${JSON.stringify(paths)}.includes(location.pathname.replace(/\\/+$/,'')||'/')){for(const href of ${JSON.stringify(dependencies)}){const link=document.createElement('link');link.rel='modulepreload';link.href=href;document.head.append(link);}const initialPath=location.pathname;let cancelled=false;const cancel=()=>{cancelled=true};const events=['nexpoly:structure-navigation','nexpoly:ketcher-preload-cancel','pagehide'];for(const event of events)window.addEventListener(event,cancel);load(${JSON.stringify(prefix() + runtime.manifest.bootstrap)}).then(m=>{if(!cancelled&&location.pathname===initialPath)m.prepareInitial()}).catch(()=>{}).finally(()=>{for(const event of events)window.removeEventListener(event,cancel)});}}` }];
    } },
    configureServer(server) {
      if (engine !== 'react') return;
      const serve = runtimeMiddleware(() => runtime, base);
      server.middlewares.use((request, response, next) => { void serve(request, response, next).catch(next); });
      server.watcher.add(['sdk', 'patches', 'build/ketcher-runtime.mjs'].map(file => path.resolve(server.config.root, file)));
      const rebuild = () => {
        if (building || closed) return;
        building = (async () => {
          while (dirty && !closed) {
            dirty = false;
            await update();
          }
          if (closed) return;
          const module = server.moduleGraph.getModuleById('\0' + virtual);
          if (module) server.moduleGraph.invalidateModule(module);
          server.ws.send({ type: 'full-reload' });
        })().catch(error => server.config.logger.error(String(error))).finally(() => {
          building = undefined;
          if (dirty && !closed) rebuild();
        });
      };
      const changed = (file: string) => {
        if (file.endsWith('.md') || file.endsWith('.test.mjs')) return;
        if (!/\/(sdk|patches)\/|\/build\/(ketcher-runtime|scope-ketcher-css)|\/styles\/ketcher-native\.css$/.test(file)) return;
        dirty = true;
        clearTimeout(debounce);
        debounce = setTimeout(rebuild, 100);
      };
      server.watcher.on('change', changed).on('add', changed).on('unlink', changed);
      server.httpServer?.once('close', () => {
        closed = true;
        clearTimeout(debounce);
        server.watcher.off('change', changed).off('add', changed).off('unlink', changed);
      });
    },
    configurePreviewServer(server) {
      if (engine !== 'react') return;
      const serve = runtimeMiddleware(() => runtime, base);
      server.middlewares.use((request, response, next) => { void serve(request, response, next).catch(next); });
    },
    async generateBundle() {
      if (engine !== 'react') return;
      for (const asset of runtime.manifest.assets) this.emitFile({ type: 'asset',
        fileName: `assets/ketcher/${runtime.manifest.version}/${asset.file}`, source: await readFile(path.join(runtime.directory, asset.file)) });
      this.emitFile({ type: 'asset', fileName: 'ketcher-runtime.json', source: JSON.stringify(runtime.manifest, null, 2) });
    }
  };
}
