// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SmilesQueryRequest } from "../types";
import { PolymerExplorerDesktopPage } from "./PolymerExplorerDesktopPage";

afterEach(() => cleanup());

describe("PolymerExplorerDesktopPage", () => {
  it("迁出性质预测后只保留结构相似和性能相似入口", () => {
    const request: SmilesQueryRequest = {
      smiles: "CCO",
      match_mode: "structure",
      similarity_threshold: 0.7,
      top_k: 10,
      property_name: null
    };
    render(
      <PolymerExplorerDesktopPage
        smiles="CCO"
        setSmiles={vi.fn()}
        iframeRef={{ current: null }}
        setIsReady={vi.fn()}
        getCurrentSmiles={vi.fn().mockResolvedValue("CCO")}
        request={request}
        setRequest={vi.fn()}
        isQueryLoading={false}
        queryError={null}
        queryData={null}
        submitQuery={vi.fn().mockResolvedValue(undefined)}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "选择探索模式" }));
    expect(screen.getAllByText("结构相似探索").length).toBeGreaterThan(0);
    expect(screen.getByText(/^性能相似探索/)).toBeTruthy();
    expect(screen.queryByText("性能预测探索")).toBeNull();
  });
});
