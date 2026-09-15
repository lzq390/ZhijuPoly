import { Component, memo, useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { NativeEditorSession, type NativeKetcher } from "../../structure/nativeSession";
import type { StructureEditorHandle } from "../../structure/editor";
import type { StructureWorkspace } from "../../structure/workspace";
import { ownNativePopups } from "../../structure/nativePopups";
import { loadKetcherRuntime } from "./loadKetcherRuntime";

export const engine = "react";
export type RuntimeProps = { session: NativeEditorSession; onInit: (ketcher: NativeKetcher) => void };

class EditorBoundary extends Component<{ session: NativeEditorSession; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch() { this.props.session.fail("画板运行异常，草稿与上次成功快照已保留，请重试。"); }
  render() { return this.state.failed ? null : this.props.children; }
}

export default function ReactStructureEditor({ workspace, title }: { workspace: StructureWorkspace; title: string }) {
  const container = useRef<HTMLDivElement>(null);
  const [runtime, setRuntime] = useState<{ View: ComponentType<RuntimeProps>; session: NativeEditorSession; onInit: RuntimeProps["onInit"] } | null>(null);
  useEffect(() => {
    const element = container.current!;
    const lease = workspace.mountEditor();
    const session = new NativeEditorSession(message => lease.fail(message));
    lease.onRetire(session.dispose);
    const root = element.closest<HTMLElement>("[data-structure-editor]")!;
    root.dataset.ketcherSession = session.id;
    session.addCleanup(ownNativePopups(root, session.id));
    let starting = false;
    let restoring = false;
    let adapter: StructureEditorHandle | null = null;
    let sdk: NativeKetcher | null = null;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let fitTimer: ReturnType<typeof setTimeout> | undefined;
    const hasSize = () => {
      const bounds = element.getBoundingClientRect();
      return element.isConnected && bounds.width > 0 && bounds.height > 0;
    };
    const canInitialize = () => {
      if (!element.isConnected) return false;
      const module = element.closest<HTMLElement>("[data-module-content]");
      if (module?.dataset.modulePhase === "exiting") return false;
      for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
        if (ancestor.hasAttribute("hidden")) return false;
        if (ancestor.getAttribute("aria-hidden") !== "true") continue;
        // The incoming page already has its final layout during the fade.
        // Its accessibility lock blocks interaction, not SDK preparation.
        if (ancestor !== module || module.dataset.moduleTransitioning !== "true" ||
            !["blank", "entering"].includes(module.dataset.modulePhase || "")) return false;
      }
      return hasSize();
    };
    const canInteract = () => !element.closest('[hidden], [aria-hidden="true"], [inert]') && hasSize();
    const canFit = () => !session.signal.aborted && workspace.getSnapshot().status === "ready" && canInteract();
    const cancelFit = () => {
      clearTimeout(fitTimer);
      fitTimer = undefined;
    };
    const fit = () => {
      cancelFit();
      if (!canFit()) return;
      fitTimer = setTimeout(() => {
        fitTimer = undefined;
        if (canFit()) sdk?.editor?.centerViewportAccordingToStruct?.();
      }, 250);
    };
    session.addCleanup(() => {
      clearTimeout(deadline);
      cancelFit();
    });
    const restore = () => {
      if (!adapter || restoring || session.signal.aborted || !canInitialize()) return;
      restoring = true;
      void lease.initialize(adapter, { adoptInitialDocument: Boolean(sdk?.nexpolyInitialMol) }).then(fit);
    };
    const onInit = (ketcher: NativeKetcher) => {
      if (session.signal.aborted) { ketcher.retire(); return; }
      clearTimeout(deadline);
      sdk = ketcher;
      adapter = session.attach(ketcher);
      restore();
    };
    const update = () => {
      if (session.signal.aborted) return;
      if (!starting && canInitialize()) {
        starting = true;
        deadline = setTimeout(() => session.fail("结构编辑器加载超时，请重试。"), 15000);
        // Reuse any transport started at a canvas deep link. A fresh mount can
        // retry failed transport; only this visible session creates the SDK.
        void loadKetcherRuntime().then(module => {
          // Keep host phase/document updates outside the SDK's render tree.
          // SDK state updates still render normally within the same session.
          if (!session.signal.aborted) setRuntime({ View: memo(module.default), session, onInit });
        }, () => session.fail("结构编辑器资源加载失败，请重试。"));
      }
      restore();
      fit();
    };
    const resize = new ResizeObserver(update);
    resize.observe(element);
    // Visibility can change through retained page/3D wrappers without a resize.
    const visibility = new MutationObserver(update);
    for (let ancestor: HTMLElement | null = root; ancestor; ancestor = ancestor.parentElement) {
      visibility.observe(ancestor, { attributes: true, attributeFilter: [
        "hidden", "aria-hidden", "inert", "class", "style", "data-module-transitioning", "data-module-phase"
      ] });
    }
    update();
    return () => {
      clearTimeout(deadline);
      clearTimeout(fitTimer);
      resize.disconnect();
      visibility.disconnect();
      session.dispose();
      lease.dispose();
      if (root.dataset.ketcherSession === session.id) delete root.dataset.ketcherSession;
    };
  }, [workspace]);
  // Establish size before the lazy CSS arrives; visibility gates must not wait
  // for styles that are themselves loaded only once the container is visible.
  return <div ref={container} className="np-structure-native" role="region" aria-label={title}
    style={{ width: "100%", height: "100%", minHeight: 360 }}>
    {runtime && !runtime.session.signal.aborted && <EditorBoundary session={runtime.session}>
      <runtime.View session={runtime.session} onInit={runtime.onInit} />
    </EditorBoundary>}
  </div>;
}
