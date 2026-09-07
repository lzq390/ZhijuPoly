// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MdDemoAtomDistanceResponse,
  MdDemoAtomSelection,
  MdDemoTrajectoryPoint,
} from "../../types";
import {
  MdTrajectoryExplorer,
  nextMdAtomSelection,
  type MdAtomSelectionSlots
} from "./MdTrajectoryExplorer";

const points: MdDemoTrajectoryPoint[] = [
  { atom_id: 1, chain_id: 1, atom_type: "1", x: 0, y: 0, z: 0 },
  { atom_id: 2, chain_id: 1, atom_type: "2", x: 1, y: 0, z: 0 },
  { atom_id: 3, chain_id: 2, atom_type: "3", x: 0, y: 1, z: 0 }
];

const selections = points.map(({ atom_id, chain_id, atom_type }) => ({ atom_id, chain_id, atom_type }));

class ResizeObserverMock {
  observe() {}
  disconnect() {}
}

function Harness({ onCalculate = vi.fn() }: { onCalculate?: () => void }) {
  const [selected, setSelected] = useState<MdAtomSelectionSlots>([null, null]);
  return (
    <MdTrajectoryExplorer
      points={points}
      timePs={5}
      selections={selected}
      distance={null}
      distanceLoading={false}
      distanceError={null}
      onSelectionChange={setSelected}
      onCalculate={onCalculate}
      onClear={() => setSelected([null, null])}
    />
  );
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    globalAlpha: 1,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1
  } as unknown as CanvasRenderingContext2D);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("MD trajectory selection", () => {
  it("keeps two unique atoms and replaces the oldest selection with a third atom", () => {
    const [first, second, third] = selections as MdDemoAtomSelection[];
    expect(nextMdAtomSelection([null, null], first)).toEqual([first, null]);
    expect(nextMdAtomSelection([first, null], second)).toEqual([first, second]);
    expect(nextMdAtomSelection([first, second], second)).toEqual([first, second]);
    expect(nextMdAtomSelection([first, second], third)).toEqual([second, third]);
  });

  it("supports keyboard atom number entry and rejects duplicate atoms", () => {
    const onCalculate = vi.fn();
    render(<Harness onCalculate={onCalculate} />);
    const inputs = screen.getAllByRole("spinbutton");

    fireEvent.change(inputs[0], { target: { value: "1" } });
    fireEvent.keyDown(inputs[0], { key: "Enter" });
    expect(screen.getByText("链 1 · 类型 1")).toBeTruthy();

    fireEvent.change(inputs[1], { target: { value: "1" } });
    fireEvent.keyDown(inputs[1], { key: "Enter" });
    expect(screen.getByRole("alert").textContent).toContain("两个不同的原子");

    fireEvent.change(inputs[1], { target: { value: "2" } });
    fireEvent.keyDown(inputs[1], { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "计算距离" }));
    expect(onCalculate).toHaveBeenCalledOnce();
  });

  it("reuses the reserved output slot when the distance result arrives", () => {
    const baseProps = {
      points,
      timePs: 5,
      selections: [selections[0], selections[1]] as MdAtomSelectionSlots,
      distanceLoading: false,
      distanceError: null,
      onSelectionChange: vi.fn(),
      onCalculate: vi.fn(),
      onClear: vi.fn(),
    };
    const distance: MdDemoAtomDistanceResponse = {
      atom_1: selections[0],
      atom_2: selections[1],
      frames: [0, 1],
      time_ps: [0, 1],
      distance: [2, 2.1],
      series: {
        key: "atom_distance",
        label: "Atom distance",
        unit: "A",
        points: [
          { time_ps: 0, value: 2 },
          { time_ps: 1, value: 2.1 },
        ],
      },
      stats: {
        n_atoms: 3,
        n_frames: 2,
        source_n_frames: 2,
        n_chains: 2,
        use_pbc: true,
        min_distance: 2,
        max_distance: 2.1,
      },
    };
    const view = render(<MdTrajectoryExplorer {...baseProps} distance={null} />);
    const reservedSlot = view.container.querySelector(".np-md-distance-output");
    expect(reservedSlot?.querySelector(".np-md-distance-state")).not.toBeNull();

    view.rerender(<MdTrajectoryExplorer {...baseProps} distance={distance} />);
    expect(view.container.querySelector(".np-md-distance-output")).toBe(
      reservedSlot,
    );
    expect(reservedSlot?.querySelector(".np-md-distance-chart")).not.toBeNull();
  });
});
