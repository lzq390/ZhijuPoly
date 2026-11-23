// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GUEST_SESSION, installSession } from "../../auth/session";
import * as api from "../../services/api";
import type { StructureDescriptorResponse } from "../../types";
import { StructureFactsStrip } from "./StructureFactsStrip";
import { attachmentPointWarning, formatStructureFact, formulaParts } from "./structureFacts";
import { clearStructureDescriptorCache, useStructureDescriptors } from "./useStructureDescriptors";

const POLYSTYRENE: StructureDescriptorResponse = {
  canonical_smiles: "*CC(*)c1ccccc1",
  formula: "C8H8",
  molecular_weight: 104.152,
  heavy_atom_count: 8,
  attachment_point_count: 2,
  ring_count: 1,
  aromatic_ring_count: 1,
  rotatable_bond_count: 2,
  hbond_donor_count: 0,
  hbond_acceptor_count: 0,
  heteroatom_count: 0,
  tpsa: 0,
  fraction_csp3: 0.25
};

const MEMBER = {
  authenticated: true,
  user: { id: "member", username: "member", must_change_password: false },
  session_id: "session-member",
  csrf_token: "fixture",
  capabilities: {},
  account_type: "member" as const
};

describe("structure fact formatting", () => {
  it("keeps one decimal for weights and areas and subscripts formula digits", () => {
    expect(formatStructureFact("molecular_weight", POLYSTYRENE)).toBe("104.2");
    expect(formatStructureFact("tpsa", { ...POLYSTYRENE, tpsa: 26.3 })).toBe("26.3");
    expect(formatStructureFact("aromatic_ring_count", POLYSTYRENE)).toBe("1");
    expect(formulaParts("C22H10N2O5")).toEqual([
      { text: "C", sub: false }, { text: "22", sub: true }, { text: "H", sub: false }, { text: "10", sub: true },
      { text: "N", sub: false }, { text: "2", sub: true }, { text: "O", sub: false }, { text: "5", sub: true }
    ]);
  });

  it("flags repeat units with one or more than two connection points", () => {
    expect(attachmentPointWarning(2)).toBeNull();
    expect(attachmentPointWarning(0)).toBeNull();
    expect(attachmentPointWarning(1)).toContain("有 1 个连接点");
    expect(attachmentPointWarning(3)).toContain("有 3 个连接点");
  });
});

describe("StructureFactsStrip", () => {
  afterEach(cleanup);

  it("renders nothing without a structure or without service access", () => {
    const { container, rerender } = render(<StructureFactsStrip state={{ status: "idle" }} onOpenHelp={vi.fn()} />);
    expect(container.innerHTML).toBe("");
    rerender(<StructureFactsStrip state={{ status: "no-access" }} onOpenHelp={vi.fn()} />);
    expect(container.innerHTML).toBe("");
  });

  it("labels every fact and links it to its explanation", () => {
    const onOpenHelp = vi.fn();
    render(<StructureFactsStrip state={{ status: "ready", descriptors: POLYSTYRENE }} onOpenHelp={onOpenHelp} />);
    const weight = screen.getByRole("button", { name: "分子量 104.2 g/mol" });
    const tip = document.getElementById(weight.getAttribute("aria-describedby") ?? "");
    expect(tip?.textContent).toContain("一个重复单元的质量");
    expect(screen.getByRole("button", { name: "芳香环 1" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "连接点 2" })).toBeTruthy();

    fireEvent.click(weight);
    expect(weight.closest("li")?.classList.contains("is-open")).toBe(true);
    fireEvent.keyDown(weight, { key: "Escape" });
    expect(weight.closest("li")?.classList.contains("is-open")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "结构信息说明" }));
    expect(onOpenHelp).toHaveBeenCalledOnce();
  });

  it("flags a repeat unit without two connection points", () => {
    render(
      <StructureFactsStrip
        state={{ status: "ready", descriptors: { ...POLYSTYRENE, attachment_point_count: 3 } }}
        onOpenHelp={vi.fn()}
      />
    );
    const points = screen.getByRole("button", { name: "连接点 3，需要检查" });
    expect(points.closest("li")?.classList.contains("is-warning")).toBe(true);
    expect(document.getElementById(points.getAttribute("aria-describedby") ?? "")?.textContent).toContain("有 3 个连接点");
  });

  it("keeps the previous values while recalculating and explains failures", () => {
    const { rerender } = render(
      <StructureFactsStrip state={{ status: "loading", previous: POLYSTYRENE }} onOpenHelp={vi.fn()} />
    );
    expect(screen.getByRole("region", { name: "结构信息" }).getAttribute("aria-busy")).toBe("true");
    expect(screen.getByRole("button", { name: "重原子 8" })).toBeTruthy();
    rerender(<StructureFactsStrip state={{ status: "loading", previous: null }} onOpenHelp={vi.fn()} />);
    expect(screen.getByRole("status")?.textContent).toContain("正在计算");
    rerender(<StructureFactsStrip state={{ status: "invalid" }} onOpenHelp={vi.fn()} />);
    expect(screen.getByRole("status")?.textContent).toContain("结构无法解析");
    rerender(<StructureFactsStrip state={{ status: "unavailable" }} onOpenHelp={vi.fn()} />);
    expect(screen.getByRole("status")?.textContent).toContain("暂时无法计算");
  });
});

describe("useStructureDescriptors", () => {
  beforeEach(() => {
    clearStructureDescriptorCache();
    installSession(MEMBER);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    installSession(GUEST_SESSION);
  });

  it("fetches once per settled structure and reuses cached results", async () => {
    const fetch = vi.spyOn(api, "fetchStructureDescriptors").mockResolvedValue(POLYSTYRENE);
    const { result, rerender } = renderHook(({ smiles }) => useStructureDescriptors(smiles), {
      initialProps: { smiles: "*CC(*)c1" }
    });
    rerender({ smiles: "*CC(*)c1ccccc1" });
    await waitFor(() => expect(result.current).toEqual({ status: "ready", descriptors: POLYSTYRENE }));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe("*CC(*)c1ccccc1");

    rerender({ smiles: "" });
    expect(result.current).toEqual({ status: "idle" });
    rerender({ smiles: "*CC(*)c1ccccc1" });
    expect(result.current).toEqual({ status: "ready", descriptors: POLYSTYRENE });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("separates unparsable structures from service failures", async () => {
    vi.spyOn(api, "fetchStructureDescriptors")
      .mockRejectedValueOnce(new api.ApiRequestError(422, "invalid smiles"))
      .mockRejectedValueOnce(new api.ApiRequestError(503, "down"));
    const { result, rerender } = renderHook(({ smiles }) => useStructureDescriptors(smiles), {
      initialProps: { smiles: "C1CC" }
    });
    await waitFor(() => expect(result.current).toEqual({ status: "invalid" }));
    rerender({ smiles: "CCO" });
    await waitFor(() => expect(result.current).toEqual({ status: "unavailable" }));
  });

  it("stays silent for guests instead of prompting to sign in", async () => {
    installSession(GUEST_SESSION);
    const fetch = vi.spyOn(api, "fetchStructureDescriptors");
    const loginPrompt = vi.fn();
    window.addEventListener("nexpoly:login-required", loginPrompt);
    const { result } = renderHook(() => useStructureDescriptors("*CC*"));
    await act(async () => undefined);
    expect(result.current).toEqual({ status: "no-access" });
    expect(fetch).not.toHaveBeenCalled();
    expect(loginPrompt).not.toHaveBeenCalled();
    window.removeEventListener("nexpoly:login-required", loginPrompt);
  });
});
