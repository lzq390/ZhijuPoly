// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ fetchStructure2D: vi.fn() }));

vi.mock("../../services/api", async () => {
  const actual = await vi.importActual<typeof import("../../services/api")>("../../services/api");
  return { ...actual, fetchStructure2D: api.fetchStructure2D };
});

import { MonomerMdStructureInput } from "./MonomerMdStructureInput";

function Harness({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <MonomerMdStructureInput
      value={value}
      error={null}
      onChange={setValue}
      getSharedSmiles={vi.fn().mockResolvedValue("CCN")}
      onEditStructure={vi.fn()}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MonomerMdStructureInput", () => {
  it("does not preview attachment-point input", async () => {
    render(<Harness initial="*CC*" />);
    await new Promise((resolve) => window.setTimeout(resolve, 430));
    expect(api.fetchStructure2D).not.toHaveBeenCalled();
  });

  it("cancels an older preview when the SMILES changes", async () => {
    api.fetchStructure2D
      .mockImplementationOnce(() => new Promise(() => undefined))
      .mockResolvedValueOnce({ structure_svg: "<svg viewBox='0 0 10 10'></svg>" });
    render(<Harness initial="CCO" />);
    await waitFor(() => expect(api.fetchStructure2D).toHaveBeenCalledTimes(1), { timeout: 1000 });
    const firstSignal = api.fetchStructure2D.mock.calls[0][1] as AbortSignal;
    fireEvent.change(screen.getByLabelText("单体 SMILES"), { target: { value: "CCC" } });
    await waitFor(() => expect(api.fetchStructure2D).toHaveBeenCalledTimes(2), { timeout: 1000 });
    expect(firstSignal.aborted).toBe(true);
    expect(api.fetchStructure2D).toHaveBeenLastCalledWith("CCC", expect.any(AbortSignal));
  });

  it("imports the current shared structure explicitly", async () => {
    api.fetchStructure2D.mockResolvedValue({ structure_svg: "<svg viewBox='0 0 10 10'></svg>" });
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "导入共享结构" }));
    await waitFor(() =>
      expect((screen.getByLabelText("单体 SMILES") as HTMLTextAreaElement).value).toBe("CCN"),
    );
  });
});
