// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModuleTransitionContext } from "./ModuleTransitionContext";
import { useModalFocus } from "./useModalFocus";
import { MonomerMdTaskCenter } from "../components/monomer-md-simulation/MonomerMdTaskCenter";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe("module transition focus and portals", () => {
  it("closes a body portal during exit and does not reopen it on entry", () => {
    const renderPage = (blocked: boolean) => <ModuleTransitionContext.Provider value={blocked}>
      <MonomerMdTaskCenter selectedJob={null} activeJobs={[]} isActiveJobsLoading={false} activeJobsError={null}
        history={null} historyQuery={{ page: 1 }} isHistoryLoading={false} historyError={null}
        cancellingJobIds={[]} deletingJobIds={[]} deleteJobErrors={{}} onRefresh={vi.fn()} onSelect={vi.fn()}
        onCancel={vi.fn()} onDelete={vi.fn()} onChangeQuery={vi.fn()} />
    </ModuleTransitionContext.Provider>;
    const view = render(renderPage(false));
    fireEvent.click(screen.getAllByRole("combobox")[0]);
    expect(screen.getByRole("listbox").parentElement).toBe(document.body);
    view.rerender(renderPage(true));
    expect(screen.queryByRole("listbox")).toBeNull();
    view.rerender(renderPage(false));
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("a module modal cannot steal focus back after the transition lock is set", async () => {
    const onClose = vi.fn();
    function Page() {
      const scopeRef = useRef<HTMLDivElement | null>(null);
      const panelRef = useRef<HTMLDivElement | null>(null);
      useModalFocus({ active: true, open: true, scopeRef, panelRef, onClose, global: true });
      return <><button data-testid="anchor">主区焦点</button><section data-testid="module"><div ref={scopeRef}>
        <div ref={panelRef} role="dialog"><button>详情关闭</button></div>
      </div></section></>;
    }
    render(<Page />);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "详情关闭" })));
    const module = screen.getByTestId("module");
    act(() => {
      module.dataset.moduleTransitioning = "true";
      screen.getByTestId("anchor").focus();
      module.setAttribute("inert", "");
      module.setAttribute("aria-hidden", "true");
    });
    expect(document.activeElement).toBe(screen.getByTestId("anchor"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
