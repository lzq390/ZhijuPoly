import { useEffect, useRef } from "react";
import { createKetcherAdapter, type KetcherInstance } from "../../structure/editor";
import type { StructureWorkspace } from "../../structure/workspace";

export default function IframeStructureEditor({ workspace, title }: { workspace: StructureWorkspace; title: string }) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    let lease: ReturnType<StructureWorkspace["mountEditor"]>;
    let timer: ReturnType<typeof setInterval> | undefined;
    let cancelled = false;
    let initialized = false;
    let registeredKetcher: KetcherInstance | undefined;
    let resizeTimer: ReturnType<typeof setTimeout> | undefined;
    let currentEditor = () => false;
    let attempts = 0;
    function fitViewport() {
      if (cancelled) return;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (cancelled || !currentEditor() || workspace.getSnapshot().status !== "ready") return;
        const bounds = frame!.getBoundingClientRect();
        if (bounds.width && bounds.height) registeredKetcher?.editor?.centerViewportAccordingToStruct?.();
      }, 250);
    }
    function check() {
      if (cancelled || initialized) return;
      // A retained workbench can receive a DFT write while display:none.
      // Ketcher cannot lay out a SMILES document in a zero-size viewport.
      // Defer hydration (and its deadline) until the owner is visible again.
      const bounds = frame!.getBoundingClientRect();
      if (bounds.width === 0 || bounds.height === 0) return;
      try {
        const frameWindow = frame!.contentWindow;
        const ketcher = (frameWindow as (Window & { ketcher?: KetcherInstance }) | null)?.ketcher;
        if (ketcher) {
          registeredKetcher = ketcher;
          const current = workspace.guard();
          currentEditor = current;
          initialized = true;
          clearInterval(timer);
          const adapter = createKetcherAdapter(ketcher, () => {
            if (cancelled || !current()) return;
            const Event = (frameWindow as Window & typeof globalThis).Event;
            frameWindow!.dispatchEvent(new Event("resize"));
            frameWindow!.scrollTo(0, 0);
          }, () => !cancelled && current());
          void lease.initialize(adapter).then(() => { if (current()) fitViewport(); });
        } else if (++attempts >= 150) {
          clearInterval(timer);
          lease.fail("结构编辑器加载超时，请重试。");
        }
      } catch {
        clearInterval(timer);
        lease.fail("结构编辑器无法访问，请重试。");
      }
    }
    function start() {
      // A fast SDK may initialize just before the iframe load event. Do not
      // start another restore against the same live instance in that case.
      const loaded = (frame!.contentWindow as (Window & { ketcher?: KetcherInstance }) | null)?.ketcher;
      if (initialized && loaded === registeredKetcher) return;
      clearInterval(timer);
      lease?.dispose();
      lease = workspace.mountEditor();
      initialized = false;
      attempts = 0;
      timer = setInterval(check, 100);
      check();
    }
    start();
    frame.addEventListener("load", start);
    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(fitViewport);
    resizeObserver?.observe(frame);
    window.addEventListener("resize", fitViewport);
    return () => {
      cancelled = true;
      clearInterval(timer);
      clearTimeout(resizeTimer);
      resizeObserver?.disconnect();
      window.removeEventListener("resize", fitViewport);
      frame.removeEventListener("load", start);
      lease.dispose();
    };
  }, [workspace]);
  return <iframe ref={frameRef} title={title} src={`${import.meta.env.BASE_URL}ketcher/index.html`} />;
}


export const engine = "iframe";
