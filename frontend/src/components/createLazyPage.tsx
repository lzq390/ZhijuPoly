import { Component, createElement, useEffect, useState, useSyncExternalStore, type ComponentProps, type ComponentType, type ReactNode } from "react";

class PageBoundary extends Component<{ children: ReactNode; onRetry: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-6" role="alert">
        <span>页面加载失败，请重试。</span>
        <button type="button" className="rounded border px-3 py-2" onClick={this.props.onRetry}>重试加载页面</button>
      </div>
    ) : this.props.children;
  }
}

/** Keep the resolved component (and its ref) stable across navigation and prefetch.
 * Unlike a permanently rejected React.lazy payload, a failed fetch can be retried.
 */
export function createLazyPage<T extends ComponentType<any>>(load: () => Promise<{ default: T }>) {
  let loaded: T | undefined;
  let pending: Promise<void> | undefined;
  let failure: { error: unknown } | undefined;
  let revision = 0;
  const listeners = new Set<() => void>();
  const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
  const getSnapshot = () => revision;
  const publish = () => { revision++; listeners.forEach(listener => listener()); };
  function preload(): Promise<void> {
    if (loaded) return Promise.resolve();
    if (pending) return pending;
    failure = undefined;
    pending = Promise.resolve().then(load).then(module => {
      loaded = module.default;
      pending = undefined;
      publish();
    }, error => {
      pending = undefined;
      failure = { error };
      publish();
      throw error;
    });
    publish();
    return pending;
  }
  function ResolvedPage(props: ComponentProps<T>) {
    if (failure) throw failure.error;
    if (!loaded) return <div className="flex h-full items-center justify-center p-6 text-sm text-slate-500" role="status">页面加载中…</div>;
    return createElement(loaded, props);
  }
  function Page(props: ComponentProps<T>) {
    const [attempt, setAttempt] = useState(0);
    useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
    useEffect(() => { if (!loaded && !failure) void preload().catch(() => {}); }, []);
    // A local loading state reveals the resolved page immediately. React's
    // Suspense fallback throttle otherwise adds ~300 ms before canvas mounting.
    return (
      <PageBoundary key={attempt} onRetry={() => {
        failure = undefined;
        setAttempt(value => value + 1);
        void preload().catch(() => {});
      }}>
        <ResolvedPage {...props} />
      </PageBoundary>
    );
  }
  return { Page, preload };
}
