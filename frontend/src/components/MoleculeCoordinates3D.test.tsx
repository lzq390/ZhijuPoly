/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MonomerDftAtom } from "../types";
import { MoleculeCoordinates3D, type MoleculeCoordinateFrame } from "./MoleculeCoordinates3D";

let notifyResize: (() => void) | null = null;

const atoms: MonomerDftAtom[] = [{
  index: 1,
  atomic_number: 6,
  element: "C",
  position_angstrom: [0, 0, 0],
  charge_e: -0.1,
  force_ev_per_angstrom: [0.1, 0, 0]
}];

const frames: MoleculeCoordinateFrame[] = [
  { id: "initial", label: "初始结构", kind: "initial", atoms },
  { id: "step-1", label: "优化第 1 步", kind: "trajectory", atoms, step: 1, energyEv: -1 },
  { id: "final", label: "最终结构", kind: "final", atoms, energyEv: -2 }
];

describe("MoleculeCoordinates3D responsive viewer", () => {
  beforeEach(() => {
    notifyResize = null;
    class ResizeObserverMock {
      constructor(callback: ResizeObserverCallback) {
        notifyResize = () => callback([], this as unknown as ResizeObserver);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverMock);
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    delete window.$3Dmol;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("resizes and re-renders after its container changes, while exposing selected frame state", async () => {
    const viewer = {
      addModel: vi.fn(),
      setStyle: vi.fn(),
      addStyle: vi.fn(),
      addArrow: vi.fn(),
      removeAllShapes: vi.fn(),
      zoomTo: vi.fn(),
      render: vi.fn(),
      clear: vi.fn(),
      resize: vi.fn(),
      setBackgroundColor: vi.fn()
    };
    window.$3Dmol = { createViewer: vi.fn(() => viewer) };

    const { rerender } = render(<MoleculeCoordinates3D frames={frames} calculationType="optimization" />);
    await waitFor(() => expect(window.$3Dmol?.createViewer).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "最终" }).getAttribute("aria-pressed")).toBe("true");

    notifyResize?.();
    expect(viewer.resize).toHaveBeenCalledTimes(1);
    expect(viewer.render).toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "初始" }));
    expect(screen.getByRole("button", { name: "初始" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("slider", { name: "优化轨迹帧" })).toBeTruthy();

    const replacementFrames = frames.map((item) => ({ ...item, id: `replacement-${item.id}` }));
    rerender(<MoleculeCoordinates3D frames={replacementFrames} calculationType="optimization" />);
    expect(screen.getByRole("button", { name: "最终" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("explains that single-point calculations do not produce an optimization trajectory", () => {
    const viewer = {
      addModel: vi.fn(),
      setStyle: vi.fn(),
      addStyle: vi.fn(),
      addArrow: vi.fn(),
      removeAllShapes: vi.fn(),
      zoomTo: vi.fn(),
      render: vi.fn(),
      clear: vi.fn(),
      resize: vi.fn(),
      setBackgroundColor: vi.fn()
    };
    window.$3Dmol = { createViewer: vi.fn(() => viewer) };

    render(<MoleculeCoordinates3D
      frames={[frames[0], frames[2]]}
      calculationType="single_point"
    />);

    expect(screen.getByText("单点计算只评估固定结构，不更新坐标，因此不生成优化轨迹。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "轨迹" })).toBeNull();
    expect(screen.queryByRole("slider", { name: "优化轨迹帧" })).toBeNull();
  });

  it("enables charge and force controls only when the selected frame provides those values", async () => {
    const viewer = {
      addModel: vi.fn(),
      setStyle: vi.fn(),
      addStyle: vi.fn(),
      addArrow: vi.fn(),
      removeAllShapes: vi.fn(),
      zoomTo: vi.fn(),
      render: vi.fn(),
      clear: vi.fn(),
      resize: vi.fn(),
      setBackgroundColor: vi.fn()
    };
    window.$3Dmol = { createViewer: vi.fn(() => viewer) };
    const bareAtoms = atoms.map((atom) => ({
      ...atom,
      charge_e: null,
      force_ev_per_angstrom: null
    }));
    const chargeAtoms = atoms.map((atom) => ({
      ...atom,
      force_ev_per_angstrom: null
    }));
    const frameSpecificData: MoleculeCoordinateFrame[] = [
      { id: "initial-bare", label: "初始结构", kind: "initial", atoms: bareAtoms },
      { id: "trajectory-charge", label: "优化第 1 步", kind: "trajectory", atoms: chargeAtoms },
      { id: "final-full", label: "最终结构", kind: "final", atoms }
    ];

    render(<MoleculeCoordinates3D frames={frameSpecificData} calculationType="optimization" />);
    await waitFor(() => expect(window.$3Dmol?.createViewer).toHaveBeenCalledTimes(1));
    const chargeToggle = screen.getByRole("checkbox", { name: "电荷着色" }) as HTMLInputElement;
    const forceToggle = screen.getByRole("checkbox", { name: "力箭头" }) as HTMLInputElement;
    expect(chargeToggle.disabled).toBe(false);
    expect(forceToggle.disabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "轨迹" }));
    expect(chargeToggle.disabled).toBe(false);
    expect(forceToggle.disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "初始" }));
    expect(chargeToggle.disabled).toBe(true);
    expect(forceToggle.disabled).toBe(true);
  });
});
