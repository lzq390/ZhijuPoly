// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StructureSvg } from "./StructureSvg";

const RDKIT_SVG = `<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="320px" height="220px" viewBox="0 0 320 220">
  <rect style="opacity:1.0;fill:#FFFFFF;stroke:none" width="320.0" height="220.0" x="0.0" y="0.0"></rect>
  <path d="M10 10 L20 20" stroke="#000000" />
</svg>`;

afterEach(() => cleanup());

function decodedImageSource() {
  return decodeURIComponent((screen.getByRole("img") as HTMLImageElement).src);
}

describe("StructureSvg", () => {
  it("removes a full-canvas RDKit white background when requested", () => {
    render(<StructureSvg svg={RDKIT_SVG} transparentBackground />);

    const source = decodedImageSource();
    expect(source).not.toContain("<rect");
    expect(source).toContain("<path");
  });

  it("keeps the source background by default", () => {
    render(<StructureSvg svg={RDKIT_SVG} />);

    expect(decodedImageSource()).toContain("<rect");
  });

  it("does not remove a white rectangle that does not cover the canvas", () => {
    const svgWithPartialRectangle = RDKIT_SVG.replace(
      'width="320.0" height="220.0"',
      'width="120" height="80"'
    );
    render(<StructureSvg svg={svgWithPartialRectangle} transparentBackground />);

    expect(decodedImageSource()).toContain("<rect");
  });
});
