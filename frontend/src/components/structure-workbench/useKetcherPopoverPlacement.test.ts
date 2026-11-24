// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { placeKetcherPopovers } from "./useKetcherPopoverPlacement";

function rect(left: number, top: number, width = 0, height = 0) {
  return () => ({ left, top, x: left, y: top, width, height, right: left + width, bottom: top + height, toJSON: () => ({}) });
}

/** A stage at (300, 100) sized 1000 × 800, holding one Ketcher menu. */
function setup({ origin, left, top, width = 200, height = 130 }: {
  origin: [number, number];
  left: number;
  top: number;
  width?: number;
  height?: number;
}) {
  const host = document.createElement("div");
  host.getBoundingClientRect = rect(300, 100, 1000, 800);
  const root = document.createElement("div");
  root.className = "MuiPopover-root";
  root.getBoundingClientRect = rect(origin[0], origin[1]);
  const paper = document.createElement("div");
  paper.className = "MuiPaper-root";
  paper.style.left = `${left}px`;
  paper.style.top = `${top}px`;
  Object.defineProperty(paper, "offsetWidth", { value: width });
  Object.defineProperty(paper, "offsetHeight", { value: height });
  root.append(paper);
  host.append(root);
  return { host, paper };
}

describe("placeKetcherPopovers", () => {
  it("moves a menu back by its containing block's offset from the viewport", () => {
    const { host, paper } = setup({ origin: [321, 121], left: 700, top: 153 });
    placeKetcherPopovers(host);
    expect(paper.style.translate).toBe("-321px -121px");
  });

  it("leaves a menu alone when its containing block is the viewport and it fits", () => {
    const { host, paper } = setup({ origin: [0, 0], left: 700, top: 153 });
    placeKetcherPopovers(host);
    expect(paper.style.translate).toBe("");
  });

  it("keeps a menu that opens at the stage's right end inside the stage", () => {
    // Right edge would be 1191 + 200 = 1391; the stage allows 1300 − 8 = 1292, so 99px more.
    const { host, paper } = setup({ origin: [321, 121], left: 1191, top: 153 });
    placeKetcherPopovers(host);
    expect(paper.style.translate).toBe("-420px -121px");
  });

  it("keeps a menu that would run past the stage's bottom inside the stage", () => {
    // Bottom would be 820 + 130 = 950; the stage allows 900 − 8 = 892, so 58px up.
    const { host, paper } = setup({ origin: [0, 0], left: 700, top: 820 });
    placeKetcherPopovers(host);
    expect(paper.style.translate).toBe("0px -58px");
  });
});
