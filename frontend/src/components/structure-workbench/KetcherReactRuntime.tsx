import { useEffect, useRef } from 'react';
import { preloadNativeRuntime } from './runtimeClient';
import type { RuntimeProps } from './ReactStructureEditor';

/** DOM boundary: SDK elements and hooks belong to its production renderer. */
export default function KetcherReactRuntime({ session, onInit }: RuntimeProps) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let dispose: (() => void) | undefined;
    let retired = false;
    void preloadNativeRuntime().then(runtime => {
      if (retired || session.signal.aborted) return;
      dispose = runtime.mount(container.current!, { session, onInit,
        staticResourcesUrl: `${import.meta.env.BASE_URL}ketcher-assets/3.8.0` }).dispose;
    }).catch(error => {
      if (!retired && !session.signal.aborted) session.fail(`结构编辑器资源加载失败：${String(error)}`);
    });
    return () => { retired = true; dispose?.(); };
  }, [session, onInit]);
  return <div ref={container} style={{ height: '100%', width: '100%' }} />;
}
