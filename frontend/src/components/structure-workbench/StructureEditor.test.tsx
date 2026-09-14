// @vitest-environment jsdom
import { StrictMode } from "react";
import { act, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StructureWorkspace } from "../../structure/workspace";
import StructureEditor from "./IframeStructureEditor";

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it("defers hidden hydration and handles a late iframe load once under StrictMode", async () => {
  vi.useFakeTimers();
  let visible = false;
  let smiles = "";
  vi.spyOn(HTMLIFrameElement.prototype, "getBoundingClientRect")
    .mockImplementation(() => new DOMRect(0, 0, visible ? 900 : 0, visible ? 600 : 0));
  const workspace = new StructureWorkspace("CCN");
  const ketcher = {
    getSmiles: vi.fn(async () => smiles),
    getKet: vi.fn(async () => JSON.stringify({ root: { nodes: smiles ? [{ $ref: "mol0" }] : [] }, mol0: { atoms: smiles ? [{}] : [] } })),
    setMolecule: vi.fn(async (source: string) => { smiles = source; }),
    clear: vi.fn(() => { smiles = ""; }),
    changeEvent: { add: vi.fn(), remove: vi.fn() }
  };
  const { container, unmount } = render(<StrictMode><StructureEditor workspace={workspace} title="test canvas" /></StrictMode>);
  const frame = container.querySelector("iframe")!;
  Object.assign(frame.contentWindow!, { ketcher, scrollTo: vi.fn() });
  await act(async () => { frame.dispatchEvent(new Event("load")); await vi.advanceTimersByTimeAsync(20000); });
  expect(workspace.getSnapshot().status).toBe("loading");
  expect(ketcher.setMolecule).not.toHaveBeenCalled();

  visible = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(workspace.getSnapshot().status).toBe("ready");
  expect(ketcher.setMolecule).toHaveBeenCalledExactlyOnceWith("CCN");
  expect(ketcher.changeEvent.add).toHaveBeenCalledOnce();
  await act(async () => { frame.dispatchEvent(new Event("load")); await vi.advanceTimersByTimeAsync(1000); });
  expect(ketcher.setMolecule).toHaveBeenCalledTimes(1);
  unmount();
  expect(ketcher.changeEvent.remove).toHaveBeenCalledWith(ketcher.changeEvent.add.mock.calls[0][0]);
  expect(workspace.getSnapshot().status).toBe("unmounted");
  expect(vi.getTimerCount()).toBe(0);
});
