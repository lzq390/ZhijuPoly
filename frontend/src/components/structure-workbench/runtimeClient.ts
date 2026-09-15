import { bootstrapUrl } from 'virtual:ketcher-runtime';
import type { RuntimeProps } from './ReactStructureEditor';

type Runtime = { mount(container: HTMLElement, options: RuntimeProps & { staticResourcesUrl: string }): { dispose(): void } };
type Bootstrap = { preload(): Promise<Runtime>; cancelPrepared(): void; cancelPreload(): void };
type Attempt = { bootstrap?: Bootstrap; expired: boolean; promise: Promise<Runtime> };
let pending: Attempt | undefined;
let attempt = 0;
export function preloadNativeRuntime(): Promise<Runtime> {
  if (pending) return pending.promise;
  const url = attempt ? bootstrapUrl.replace(/([^/]+)$/, `retry/${attempt}/$1`) : bootstrapUrl;
  // Vite adds ?import to variable imports. Use the HTML's native importer to
  // preserve the identity of the module that owns the prepared Worker.
  const importer = (globalThis as unknown as Record<symbol, (url: string) => Promise<Bootstrap>>)[Symbol.for('nexpoly.ketcher.import')];
  const entry = { expired: false } as Attempt;
  pending = entry;
  let timer: ReturnType<typeof setTimeout>;
  const loading = (importer ? importer(url) : import(/* @vite-ignore */ url)).then(bootstrap => {
    entry.bootstrap = bootstrap;
    if (entry.expired) {
      bootstrap.cancelPreload();
      throw new DOMException('SDK loading attempt expired', 'AbortError');
    }
    return bootstrap.preload();
  });
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error('SDK resources timed out')), 12000);
  });
  entry.promise = Promise.race([loading, deadline]).catch(error => {
    entry.expired = true;
    entry.bootstrap?.cancelPreload();
    if (pending === entry) {
      pending = undefined;
      ++attempt;
      // Also invalidate an HTML bootstrap whose own import has not completed.
      window.dispatchEvent(new Event('nexpoly:ketcher-preload-cancel'));
    }
    throw error;
  }).finally(() => clearTimeout(timer));
  return entry.promise;
}
if (import.meta.hot) import.meta.hot.dispose(() => {
  pending?.bootstrap?.cancelPrepared();
  window.dispatchEvent(new Event('nexpoly:ketcher-preload-cancel'));
  pending = undefined;
});
