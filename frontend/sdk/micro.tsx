import React, { useMemo } from 'react';
import { createRoot } from 'react-dom/client';
import { Editor } from 'ketcher-react';
import { StandaloneStructServiceProvider, StandaloneStructService } from 'ketcher-standalone';
import { takeTransport } from './bootstrap.mjs';
import type { RuntimeProps } from '../src/components/structure-workbench/ReactStructureEditor';
import type { NativeKetcher } from '../src/structure/nativeSession';
import 'ketcher-react/dist/index.css';
import '../src/styles/ketcher-native.css';

type Options = RuntimeProps & { staticResourcesUrl: string };
export function mount(container: HTMLElement, options: Options): { dispose(): void } {
  options.session.check();
  let disposed = false;
  const root = createRoot(container, { onUncaughtError: error => {
    if (!disposed) options.session.fail(`结构编辑器运行失败：${String(error)}`);
  } });
  function Runtime() {
    const provider = useMemo(() => {
      const instance = new StandaloneStructServiceProvider();
      instance.createStructService = serviceOptions => {
        options.session.check();
        const transport = takeTransport(options.session);
        try {
          // r4 accepts an owned, already-running protocol transport.
          const service = new StandaloneStructService(serviceOptions, transport);
          return options.session.ownService(service);
        } catch (error) { transport.destroy(error); throw error; }
      };
      return instance;
    }, []);
    return <Editor deferMacromoleculesEditor structServiceProvider={provider}
      staticResourcesUrl={options.staticResourcesUrl}
      onInit={value => {
        const sdk = value as unknown as NativeKetcher;
        if (disposed || options.session.signal.aborted) { sdk.retire(); return; }
        options.onInit(sdk);
      }} errorHandler={error => {
        if (!disposed && !options.session.initialized) options.session.fail(`结构编辑器初始化失败：${String(error)}`);
      }} />;
  }
  root.render(<Runtime />);
  return { dispose() {
    if (disposed) return;
    disposed = true;
    options.session.dispose();
    queueMicrotask(() => root.unmount());
  } };
}
